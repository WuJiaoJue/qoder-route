# Account Management: Priority Editing & Enable/Disable — Design

Status: **proposed** (analysis complete, not yet implemented)
Scope: `backend/app/{api,services,core,models}`, `frontend/src/*`

## 1. Problem statement

The account pool currently exposes only *create*, *delete*, and *quota refresh*.
Three operator capabilities are missing:

| Capability | Today | Consequence |
|---|---|---|
| Edit priority after creation | only at `POST /api/accounts` | must delete + re-add (loses counters); or raw SQLite edits |
| Temporarily disable an account | **removed by design** | must delete the account to take it out of rotation |
| Choose a routing strategy | single hardcoded policy | cannot prefer fill-first vs spread vs cheapest |

The immediate trigger: to make `qfmodel` route to the paid account `paid-A`
instead of the free-tier `free-acct`, the operator had to **write SQL
directly** (`UPDATE accounts SET priority=1 WHERE id=4`). That is not an
acceptable steady-state workflow.

## 2. Current behaviour (verified in code)

### 2.1 Routing

`AccountPool.get_next_account()` — fill-first, ordered by:

```python
.order_by(Account.priority.desc(), Account.id.asc())
```

All accounts ship with `priority=0`, so effective order is **ascending id**.
Two query branches exist (`free_model` True/False); both repeat the same
`ORDER BY`. `_row_is_routable()` re-checks a freshly loaded row before latching
the lease.

### 2.2 Account state fields

| Field | Written by | Meaning |
|---|---|---|
| `is_active` | startup backfill only | **frozen to 1** — cannot be used |
| `is_available` | `_refresh`, `mark_success`, `mark_failure`, `_park_exhausted` | derived: routable right now |
| `is_quota_exceeded` | quota service | parked, credit verdict |
| `cooldown_until` | `mark_failure` | transient failure backoff |
| `consecutive_failures` | `mark_success` / `mark_failure` | failure budget |

### 2.3 Why `is_active` cannot be reused for disable — **the key constraint**

```python
# core/database.py:95
async def _reactivate_all_accounts(conn):
    """Account disable was removed — wake any rows left inactive in the DB."""
    await conn.execute(text("UPDATE accounts SET is_active = 1 WHERE is_active = 0"))
```

Called from `init_db()` on **every boot**. Any `is_active=0` row is revived at
startup, so the flag is unusable as a persistent operator switch.

### 2.4 Why `is_available` cannot be reused either

`is_available` is a **derived** flag written in **six** places. Reusing it for
"disabled" would be silently reverted:

| Line | Writer | Assignment | Effect on a would-be disable |
|---|---|---|---|
| 132 | `_refresh()` (periodic) | `= not in_cooldown and not is_quota_exceeded` | **reverts** within one refresh cycle |
| 289 | `refresh_quota()` (manual) | `= True` **unconditionally** | **reverts** on any quota refresh |
| 374 | `_park_exhausted()` | `= False` | coincides with exhausted, not disable |
| 425 | `mark_success()` (per completion) | `= not is_quota_exceeded` | **reverts** on any in-flight success |
| 545 | `mark_failure()` | `= True` (default) | **reverts** when failures are below max |
| 551 | `mark_failure()` | `= False` (cooldown) | coincides with cooldown, not disable |

Line 289 is the sharpest edge: a *manual* quota refresh would resurrect a
disabled account with no warning. A disable flag must therefore be a
**separate, authoritative column** that all six writers respect — none of them
may clear it, and each must treat it as an additional gate.

## 3. Design decisions

### D1 — New column `enabled` (not a reuse of `is_active`)

```
accounts.enabled  BOOLEAN NOT NULL DEFAULT 1
```

- Additive migration via the existing idempotent `ADD COLUMN` pattern
  (`_migrate_account_quota_columns`).
- `DEFAULT 1` backfills every existing row as enabled — no behaviour change
  on upgrade.
- Deliberately **not** named `is_active`: that column is owned by the legacy
  startup backfill and must not be conflated.
- `_reactivate_all_accounts` is **left untouched** (it manages `is_active`).

### D2 — Routing respects `enabled` in all three places

`enabled` must gate at every layer, or a disabled account can leak back in:

1. `get_next_account()` — both `free_model` branches add `.where(Account.enabled == True)`
2. `_row_is_routable()` — early `if not acc.enabled: return False` (protects the
   fresh re-check and `begin_request`)
3. `_refresh()` — iterate enabled rows only, and never flip `is_available` for a
   disabled row (leave its other state intact for a clean re-enable)

### D3 — Disable is **authoritative and persistent**, independent of other state

Crucially, disabling must **not** be undone by quota/success bookkeeping.
Each of the six `is_available` writers needs an explicit decision:

| Line | Writer | Required handling |
|---|---|---|
| 132 | `_refresh` | fold in: `should_be = acc.enabled and not in_cooldown and not is_quota_exceeded` |
| 289 | `refresh_quota` | `acc.is_available = acc.enabled` (never a bare `True`) |
| 374 | `_park_exhausted` | unchanged — parking is orthogonal to enablement |
| 425 | `mark_success` | `acc.is_available = acc.enabled and not acc.is_quota_exceeded` |
| 545/551 | `mark_failure` | cooldown logic unchanged; final value ANDed with `enabled` |

This preserves orthogonal information: a disabled account **remembers** its
quota/cooldown/failure state, so re-enabling restores it exactly as it was
rather than pretending it is healthy.

Because `is_available` alone is insufficient (line 289's manual refresh path),
routing must **also** gate on `enabled` directly — defence in depth, matching
the pattern where `_row_is_routable` re-checks a freshly loaded row.

### D4 — Disabled accounts are visible but inert

- `GET /api/accounts` (pool status): **includes** disabled rows so the operator
  can re-enable them. `AccountOut` gains `enabled`.
- `GET /api/accounts/available`: **excludes** them (it mirrors routing filters).
- `GET /api/accounts/exhausted`: unchanged.
- `GET /api/accounts/stats/dashboard`: add `accounts_disabled` count.
- New `GET /api/accounts/disabled` for symmetry with available/exhausted.

The panel's account list therefore shows a third visual state (dimmed + a
"Disabled" badge) rather than hiding the row.

### D5 — `PATCH /api/accounts/{id}` for mutable fields

A single partial-update endpoint, not several verbs:

```
PATCH /api/accounts/{id}
  { "priority"?: int, "enabled"?: bool, "name"?: str, "default_model"?: str }
```

Rules:
- Only fields present in the body change (partial update / `exclude_unset`).
- `priority` clamped to a sane range (e.g. `[-100, 100]`) to avoid absurd values.
- `name` uniqueness is **not** enforced (delete+re-add already reuses names by
  design — see AGENTS.md on the `?account=` filter).
- `pat_token` and `model_level` are **not** patchable here: rotating a PAT
  should re-run validation, and `model_level` is always `'auto'` (AGENTS.md:
  it is not a routing dimension).
- Returns the updated `AccountOut`.
- Emits a `logbus` pool event (`updated` / `disabled` / `enabled`) with a
  `reason`, matching the existing `added`/`parked`/`cooldown` vocabulary.

### D6 — Re-enable must not bypass the quota gate

Setting `enabled=true` **does not** clear `is_quota_exceeded`, cooldown, or
failures. A re-enabled but exhausted account stays parked until quota returns
(existing `refresh_quota` un-park path). This keeps "operator intent" and
"upstream health" as separate axes and prevents accidentally routing work to a
dead PAT.

### D7 — Routing strategy: **implemented** (v2)

Originally deferred; now built as the minimal honest version that analysis
recommended — only the final candidate-ordering step is swapped, the query is
not forked.

Setting: `routing_strategy` ∈ {`fill_first` (default), `spread`, `least_used`}.

| Strategy | Ordering | When to use |
|---|---|---|
| `fill_first` (default) | priority ↓, id ↑ (SQL order) | Normal. Sticks to one PAT so a near-empty account's credit is not scattered. |
| `spread` | priority ↓, in-flight ↑, remaining credit ↓, id ↑ | Bursts / parallel agents — no single PAT gets stacked. |
| `least_used` | priority ↓, lifetime requests ↑, id ↑ | Even out wear across accounts. |

Two invariants made explicit:

1. **Priority always wins first**, under every strategy. It is an explicit
   operator override, so a higher-priority account must win regardless of load.
   Strategies only reorder *within* equal priority.
2. Ordering is applied **in Python** (`AccountPool._order_candidates`), not in
   SQL, because `spread` ranks on live in-flight counts that exist only in
   process memory. The SQL `ORDER BY` is retained as the deterministic
   tie-break and as the single source of truth for the *filter*.

Verified on identical pool state (two routable accounts, equal priority):

```
fill_first  -> picks id=3 free-acct  (lowest id)
spread      -> picks id=4 paid-A     (0 in-flight, far more credit)
least_used  -> picks id=3 free-acct  (7 lifetime requests vs 1102)
```

Live HTTP request under `spread` also selected `paid-A`, confirming the setting
reaches real routing, not just the ordering helper.


## 4. Implementation plan

| # | File | Change |
|---|---|---|
| 1 | `models/account.py` | add `enabled = Column(Boolean, nullable=False, default=True, server_default="1")` |
| 2 | `core/database.py` | add `"enabled": "BOOLEAN DEFAULT 1"` to `new_columns` |
| 3 | `models/schemas.py` | `AccountOut.enabled: bool`; new `AccountUpdate` (all fields optional) |
| 4 | `services/account_pool.py` | gate routing + `_row_is_routable` + `_refresh` + `mark_success`; add `set_enabled()` / `update_account()`; `get_stats` disabled count |
| 5 | `api/accounts.py` | `PATCH /{id}`; `GET /disabled`; exclude disabled from `/available`; add `accounts_disabled` to dashboard |
| 6 | `frontend/src/types/index.ts` | `Account.enabled`; dashboard field |
| 7 | `frontend/src/hooks/useApi.ts` | `useUpdateAccount()` invalidating the 4 standard keys |
| 8 | `frontend/src/components/accounts/AccountManager.tsx` | priority editor + enable/disable toggle in the hover row |
| 9 | `frontend/src/components/ui/GlassPanel.tsx` | extend `Status` with `'disabled'` |

## 5. Migration & rollback

- **Forward:** additive column with a default — existing rows become enabled, so
  routing behaviour is unchanged on upgrade. `init_db` runs the `ALTER TABLE`
  idempotently.
- **Rollback:** the column may be left in place (unused by old code) with no
  effect. Reverting the code requires no data surgery.
- `is_active` and `_reactivate_all_accounts` are intentionally left alone, so
  the legacy path keeps working.

## 6. Acceptance criteria

1. `PATCH /api/accounts/4 {"priority": 5}` changes routing order without SQL.
2. `PATCH /api/accounts/3 {"enabled": false}` removes `free-acct` from routing
   **immediately and persistently**; a subsequent `qfmodel` request is served by
   `paid-A` on the first attempt (no 30s queue retry).
3. The disabled account **survives** a backend restart and at least one
   `_refresh` cycle (this is what `is_active`/`is_available` could not do).
4. `mark_success` on a disabled account does **not** set `is_available=True`.
5. Re-enabling restores the prior quota/cooldown state; an exhausted account
   stays parked.
6. Disabled accounts still appear in `GET /api/accounts` with `enabled=false`
   so they can be re-enabled, but never in `/available`.
7. `total_accounts` accounting stays coherent (add `accounts_disabled`).

## 7. Worked example — does this fix the original problem?

The trigger case: `qfmodel` (a **paid** model) kept being routed to the
free-tier account `free-acct` because it sorts first (`priority=0, id=3` vs
`paid-A` at `id=4`), costing a 30-second 10605 queue retry on every cold start
(measured: **33 881 ms** for one request, vs ~1.5 s via `paid-A`).

Two independent fixes from this design, either of which suffices:

**Option A — priority (minimal):**
```
PATCH /api/accounts/4 {"priority": 1}
```
`paid-A` sorts first; `free-acct` stays available for free models.

**Option B — disable (explicit):**
```
PATCH /api/accounts/3 {"enabled": false}
```
`free-acct` leaves rotation entirely and, unlike `is_available`, **stays out**
across restarts and quota refreshes.

Current state of the pool (post-manual-SQL, `enabled` not yet implemented):

| id | name | priority | role |
|---|---|---|---|
| 4 | paid-A | **1** | paid model traffic |
| 1 | paid-B | 0 | parked (quota) |
| 2 | paid-C | 0 | parked (quota) |
| 3 | free-acct | 0 | free-tier; free models only |

Note the manual `priority=1` edit currently in the DB is exactly the workflow
`PATCH` is meant to replace.

## 8. Decisions (confirmed by operator)

| # | Question | Decision |
|---|---|---|
| 1 | Disabled row presentation | **Dim in place + `Disabled` badge** — stays in the main list, toggle to restore. No separate tab, so no `/disabled` endpoint is needed. |
| 2 | In-flight requests on disable | **Let them finish**; disable only blocks *new* leases. |
| 3 | `priority` range | **Clamp to `[-100, 100]`** (422 on out-of-range input). |
| 4 | Routing strategy | **Built in v2** (per D7): `fill_first` / `spread` / `least_used`. |

Consequences for the implementation plan:

- Drop `GET /api/accounts/disabled` from step 5 — the disabled rows ride along
  in the existing `GET /api/accounts` payload (D4) and the UI dims them.
- No `logbus` event vocabulary change is strictly required, but `updated` /
  `disabled` / `enabled` actions are still emitted for the Pool tab.
- Since disable never interrupts in-flight work, `get_next_account` is the only
  place that must block new leases — `begin_request` inherits it via
  `_row_is_routable`.
