# Conversation Affinity and `by_conversation` Sharding

## Problem

`fill_first` sends every request to the highest-priority routable account. With a
pooled multi-account setup that means one account absorbs all concurrency while
the rest idle. Before this change the only alternative, `spread`, rebalances per
request — so successive turns of the *same* conversation land on different
accounts and lose any per-conversation locality.

Neither gives both properties. `by_conversation` does.

## What was measured first (and what it ruled out)

The initial justification for conversation locality was upstream prefix caching:
if the cache lives in an account's shard, bouncing turns across accounts should
keep it cold. Two experiments disproved that:

1. **`session_id` is irrelevant to caching.** Identical prompts with a stable
   versus a rotating `x-session-id` both reported `cached_tokens = 1024` on every
   turn (13 samples per arm, zero variance).
2. **The cache is not account-sharded.** With affinity off, 6 concurrent turns
   scattered across 3 accounts and *every one* still reported
   `cached_tokens = 1024`.

So conversation locality buys **nothing measurable for cache warmth**, and the
proxy resends full history each turn, so it buys nothing for server-side context
either. This is why `zhangdailin/API-Console`'s `ConversationAffinity: true` did
not transfer as a justification: their Qoder path is stateful, ours is not.

What remains true is the concurrency argument, which is about **our** pool
behaviour rather than Qoder's: pinning a conversation is stable, and sharding
*different* conversations across the pool stops one account carrying everything.

Note the limit of the evidence: a probe of 8 concurrent requests on a single
account found **no observable upstream per-account concurrency cap** (8/8
succeeded, no `10605`/`RESOURCE_LIMIT`, and concurrent wall time was 3.3× better
than serialized). Testing higher concurrency was judged not worth the load on
real accounts. So `by_conversation` is justified as better pool utilisation and
as insurance, not as a fix for a demonstrated upstream ceiling.

## Design

### `by_conversation` routing strategy

A fourth value for `routing_strategy`: `fill_first` | `spread` | `least_used` |
`by_conversation`.

Ordering is a **rotation within each priority tier**, not a single pick:

```python
for prio in sorted({a.priority for a in accounts}, reverse=True):
    tier = [a for a in accounts if a.priority == prio]
    offset = shard_offset(conversation_key, len(tier))
    rotated.extend(tier[offset:] + tier[:offset])
```

Three properties follow from that shape:

- **Priority still dominates.** Sharding happens *within* a tier, so the
  operator override remains absolute. A tier of one behaves like `fill_first`.
- **The whole tier stays behind the shard target.** If the shard's account is
  parked, cooled down or excluded, the caller's existing fall-through walks to
  the next account in the ring. No extra availability code, and no request is
  ever lost to a bad shard.
- **Rotation is stable.** The same key always produces the same offset, so a
  conversation keeps its account while the pool is unchanged.

`shard_offset` is a pure function (`sha256(key)[:4] % bucket_count`), so it
survives restarts with no stored state. Plain modulo rather than a
consistent-hash ring is deliberate: the pool changes rarely, and a reshuffle
costs a cold cache at worst, never correctness.

When the client sends no session identifier there is nothing to shard on, so the
strategy degrades to `fill_first` rather than inventing a key.

### `conversation_affinity` (the pin)

Also added, as a separate toggle (default on): remember the account a
conversation last used and prefer it, with a 1-hour TTL and a 4096-entry LRU
bound.

This is **not** the same mechanism as sharding and is not redundant with it:
sharding predicts an account from the key, the pin records what actually served
the turn. The pin adapts when pool composition shifts mid-conversation; the
shard does not. Both are preferences, never locks.

Key derivation order mirrors API-Console's chain: an explicit client session id
wins; otherwise a hash of the system prompt plus the first user message. The
prefix-derived key is an *account preference, not an identity* — two unrelated
conversations that open with the same text collide, which costs only cache
warmth.

## Verification

Unit (in-process, synthetic tier):

| Property | Result |
|---|---|
| same conversation → same account | 5/5 identical |
| different conversations spread | 40 conversations over 4 accounts, all used |
| priority dominates the shard | 30 conversations, top tier only |
| no conversation key | degrades to `fill_first` |
| ring keeps all candidates | rotation preserves the full set |
| hash uniformity | max/min bucket ratio 1.015–1.08 over 9000 keys |

Live pool (7 accounts, top tier = 3):

```
A) 12 conversations            -> used 3 accounts
B) same conversation x5        -> 陈晓燕 x5                 stable
C) 8 concurrent, ONE convo     -> 陈晓燕 x8                 locality held
D) 8 concurrent, 8 convs       -> spread over 3 accounts    concurrency win
```

## Configuration

| Setting | Default | Effect |
|---|---|---|
| `routing_strategy` | `fill_first` | set to `by_conversation` to shard per conversation |
| `conversation_affinity` | `true` | pin a conversation to the account that last served it |

Both are runtime settings (Settings → Account pool), persisted, no restart.
