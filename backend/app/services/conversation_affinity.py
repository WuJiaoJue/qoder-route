"""Conversation -> account stickiness.

Why this exists
---------------
Upstream prefix caching is keyed on prompt content, *not* on the ``session_id``
we forward (measured: a stable and a rotating session id both report the same
``cached_tokens``). But the cache lives in the account's upstream shard, so a
multi-turn conversation that bounces between pooled accounts pays a cold cache
on every hop. Pinning successive turns to one account is what actually keeps
the cache warm.

Design notes borrowed from zhangdailin/API-Console's ``request_affinity.go``:

* Affinity is a **preference, never a lock**. If the pinned account is no longer
  routable (exhausted, disabled, cooled down, or explicitly excluded because a
  request just failed on it) routing falls through to the normal strategy and
  re-pins.
* Entries **expire**. A conversation that goes quiet should not hold an account
  forever, and a long-lived key must not outlive a pool reshuffle.
* The derived key is bounded and **never stored raw**: it is a hash, so this
  module cannot be used to read back anyone's prompt.

Known limitation, stated plainly
--------------------------------
When the client sends no session header (DSH does not), the key is a hash of the
system prompt plus the first user message. Two different conversations that open
with identical text therefore collide and share an account. That is harmless —
it costs nothing but a slightly warmer or colder cache — but it means the
prefix-derived key is an *account preference*, not an identity. Prefer sending
``x-session-id`` when the client can.
"""
from __future__ import annotations

import hashlib
import time
from collections import OrderedDict
from typing import Iterable, Optional

# How long a conversation stays pinned to one account.
AFFINITY_TTL_SECONDS = 3600.0
# Bound the map so a busy host cannot grow it without limit.
MAX_ENTRIES = 4096


class ConversationAffinityStore:
    """TTL + LRU map of conversation key -> preferred account id.

    Plain dict guarded by the caller's existing in-flight lock: every read and
    write happens inside ``AccountPool._in_flight_lock``, so no separate lock is
    needed and lookup cannot race the lease it influences.
    """

    def __init__(self, ttl: float = AFFINITY_TTL_SECONDS, max_entries: int = MAX_ENTRIES) -> None:
        self._ttl = ttl
        self._max_entries = max_entries
        self._entries: "OrderedDict[str, tuple[int, float]]" = OrderedDict()

    def get(self, key: Optional[str]) -> Optional[int]:
        """Return the pinned account id, or ``None`` when absent or expired."""
        if not key:
            return None
        item = self._entries.get(key)
        if item is None:
            return None
        account_id, expires_at = item
        if expires_at <= time.time():
            # Expired: drop it rather than let a stale pin linger.
            self._entries.pop(key, None)
            return None
        # Refresh recency for the LRU bound only; the TTL is intentionally NOT
        # extended here, so a pin cannot live forever by being touched.
        self._entries.move_to_end(key)
        return account_id

    def pin(self, key: Optional[str], account_id: int) -> None:
        if not key:
            return
        self._entries[key] = (account_id, time.time() + self._ttl)
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)

    def unpin_account(self, account_id: int) -> int:
        """Drop every pin pointing at ``account_id``. Returns how many were removed.

        Used when an account leaves the pool (deleted, disabled, exhausted) so a
        conversation is not repeatedly looked up to a dead preference before
        falling through.
        """
        doomed = [k for k, (aid, _exp) in self._entries.items() if aid == account_id]
        for k in doomed:
            self._entries.pop(k, None)
        return len(doomed)

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)


def derive_conversation_key(
    explicit_session_id: Optional[str],
    messages: Iterable[dict],
) -> Optional[str]:
    """Build a stable key for a conversation.

    Order of preference mirrors API-Console's chain: an explicit client-supplied
    session id always wins; the content prefix is the last resort.
    """
    if explicit_session_id:
        return f"explicit:{explicit_session_id}"

    system_parts: list[str] = []
    first_user: Optional[str] = None
    for message in messages:
        role = str(message.get("role") or "")
        content = message.get("content")
        if isinstance(content, list):
            # OpenAI content-part arrays: concatenate the text pieces.
            content = "".join(
                str(part.get("text") or "")
                for part in content
                if isinstance(part, dict)
            )
        text = str(content or "")
        if role in ("system", "developer"):
            system_parts.append(text)
        elif role == "user" and first_user is None:
            first_user = text
            break  # only the first user turn is needed for the prefix

    if first_user is None:
        return None
    seed = "prefix:" + "\0".join(system_parts) + "\0" + first_user
    return "prefix:" + hashlib.sha256(seed.encode("utf-8", "replace")).hexdigest()[:24]


def shard_offset(conversation_key: Optional[str], bucket_count: int) -> int:
    """Deterministic shard index for a conversation within one priority tier.

    Uses the same hash for the same key every time, so a conversation keeps
    landing on the same account while the pool is unchanged. Plain modulo over
    the tier size (rather than a consistent-hash ring) is deliberate: it means
    adding or removing an account reshuffles *some* conversations, which is
    acceptable here because the pool changes rarely and a reshuffle only costs a
    cold cache, never correctness.
    """
    if bucket_count <= 1 or not conversation_key:
        return 0
    digest = hashlib.sha256(conversation_key.encode("utf-8", "replace")).digest()
    # 4 bytes is far more than enough entropy for a handful of buckets.
    return int.from_bytes(digest[:4], "big") % bucket_count


# One shared store per process, like the account pool itself.
store = ConversationAffinityStore()
