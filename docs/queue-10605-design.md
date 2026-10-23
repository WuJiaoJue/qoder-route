# 10605 Queue Handling — Investigation & Design

## 1. Problem statement

A DSH turn died immediately with:

```
{"type":"finish","reason":{"kind":"error",
  "failure":{"message":"403 status code (no body)","code":"AUTH"}}}
```

The user-visible symptom is a fatal auth error. The actual upstream condition
was a transient free-tier capacity shortage. Two independent defects combined.

### 1.1 Ground-truth payload (captured live, 6/6 reproducible)

```json
{"code":"10605",
 "message":"{\"isQueued\":true,\"modelKey\":\"qfmodel\",\"queueCount\":0,
            \"queueType\":\"p3\",\"retryAfterSeconds\":30,\"serviceAvailable\":false,
            \"subCode\":\"RESOURCE_LIMIT\",\"waitTime\":30}",
 "subCode":"RESOURCE_LIMIT"}
```

Field inventory:

| Field | Sample | Meaning |
|---|---|---|
| `code` | `"10605"` | Qoder model-queue error code |
| `isQueued` | `true` | Request entered a server-side queue |
| `modelKey` | `"qfmodel"` | Model that is unavailable |
| `queueCount` | `0` | **Nobody ahead of us** — not congestion |
| `queueType` | `"p3"` | Priority class |
| `retryAfterSeconds` | `30` | **Server-stated wait** |
| `waitTime` | `30` | Duplicate of the above |
| `serviceAvailable` | `false` | **Real cause: capacity is down** |
| `subCode` | `"RESOURCE_LIMIT"` | Resource exhaustion, not auth |

**Key insight:** `queueCount: 0` with `isQueued: true` means "we are not waiting
behind anyone; the resource itself is unavailable." This is a capacity/backpressure
signal, not an ordering queue. The server nonetheless tells us exactly how long
to wait: **30 seconds**.

### 1.2 Defect A — wrong HTTP status semantics (proxy)

`chat.py` and `anthropic.py` both pass the upstream 403 straight through:

```python
raise HTTPException(status_code=error_status or 503, detail=error_msg)
```

`error_status` is `403`. In the OpenAI protocol **403 means
PermissionDenied — a fatal, non-retryable auth condition.** Verified in the
OpenAI SDK:

```js
if (status === 403) return new PermissionDeniedError(...)   // not retried
if (status === 429) return new RateLimitError(...)          // retried
```

DSH maps `PermissionDeniedError` to failure code `AUTH` and aborts the turn.
A transient capacity shortage is therefore reported to the agent loop as an
unrecoverable authentication failure.

### 1.3 Defect B — retry gate can never fire

Both routers only retry when the queue is *empty*:

```python
if queue is not None and queue.get("isQueued") is False and not queue_retried:
```

But in this failure mode `isQueued` is **always `true`** (verified 6/6). The
retry branch is dead code for the exact condition users hit, so the very first
10605 is surfaced as a hard failure. This is the direct cause of
"one transient blip kills the turn."

### 1.4 Defect C — error body is unreadable by clients

FastAPI's `HTTPException` emits `{"detail": "..."}`. The OpenAI SDK reads
`errorResponse?.["error"]`:

```js
const msg = error?.message ? ... : ...;
if (status && msg) return `${status} ${msg}`;
if (status) return `${status} status code (no body)`;   // ← what DSH printed
```

With no `error` key the SDK discards the body entirely, producing the
information-free `"403 status code (no body)"`. Even the diagnostic
`retryAfterSeconds: 30` never reaches the operator.

## 2. Design

### 2.1 Principles

1. **A transient capacity signal must never be reported as an auth failure.**
2. **Honor the server's own `retryAfterSeconds` rather than a hardcoded delay.**
3. **Never penalize the account** — 10605 is upstream health, not PAT health
   (existing behavior; preserved).
4. **Keep the JSON-parsing path intact.** AGENTS.md forbids string-replace
   hacks here; `_extract_queue_payload` / `parse_model_queue` stay authoritative.
5. **Both dialects behave consistently** — OpenAI and Anthropic share helpers.

### 2.2 Status-code mapping

| Condition | Status | Rationale |
|---|---|---|
| 10605, within retry budget | *(retried in-process, no status)* | Absorb the blip |
| 10605, budget exhausted | **429** | Retryable backpressure |
| 10605, `serviceAvailable: false` | **429** | Capacity down, still retryable |

429 (not 503) is chosen deliberately: the OpenAI SDK retries 429 with
exponential backoff, and DSH classifies it as rate-limit rather than `AUTH`.

### 2.3 Retry policy

Replace the dead `isQueued is False` gate with a **bounded, time-aware** policy
driven by the server's own hint:

```
attempt 1 ──10605──▶ wait retryAfterSeconds (clamped 1..30)  ──▶ attempt 2
attempt 2 ──10605──▶ wait retryAfterSeconds (clamped 1..30)  ──▶ attempt 3
attempt 3 ──10605──▶ 429 + Retry-After header
```

- `MAX_QUEUE_RETRIES = 2` in-process retries (3 total attempts).
- Delay = `retryAfterSeconds` (fallback `waitTime`, then a 5s default),
  clamped to `[1, 30]` so a hostile/garbled value cannot stall a worker.
- Retries apply **regardless of `isQueued`**, since live traffic shows
  `isQueued: true` is the normal shape of this error.
- The existing `isQueued is False` fast path is preserved as a special case
  (it retries after the short 3s delay).

### 2.4 Response headers & body

Emit `Retry-After: <seconds>` so any spec-compliant client backs off correctly,
and return the queue detail in a form that survives the SDK. The queue payload
is attached as the `detail` (unchanged for the panel) **and** the message is
made explicit so `"403 status code (no body)"` can never recur.

### 2.5 Ordering of concerns

Queue classification runs **before** quota and account classification, because
`looks_like_model_queue` is already checked first in `classify_chat_error`.
No reordering is required; only the status and retry gate change.

## 3. Implementation plan

| File | Change | Status |
|---|---|---|
| `backend/app/services/quota_service.py` | Add `queue_retry_delay()`; keep `parse_model_queue` untouched | done |
| `backend/app/api/chat.py` | Shared `_queue_busy_error()`; both gates retry with server delay; 429 + `Retry-After` | done |
| `backend/app/api/anthropic.py` | `_anthropic_queue_busy()`; `_anthropic_error(headers=)`; same policy | done |
| `backend/tests/test_queue_parsing.py` | local suite absent from this checkout — see note | n/a |

## 4. Acceptance criteria — verified

| # | Criterion | Evidence |
|---|---|---|
| 1 | 10605 retried up to 3 attempts, then **429** (never 403) | Live e2e: `HTTP 429 in 61.1s` = 2 × 30s retries |
| 2 | No 10605 path can yield failure code `AUTH` | 403 replaced by 429 on both routes |
| 3 | `Retry-After` = server delay, clamped `[1,30]` | `Retry-After: 30`; clamp unit-tested |
| 4 | Account **not** marked failed or parked | `free-acct`: avail=True, failures=0, last_err=None |
| 5 | `parse_model_queue` still parses nested escaped JSON | Helper test on live payload passed |
| 6 | OpenAI and Anthropic identical behavior | Both builders asserted 429 + `Retry-After: 30` |

### Verification log (live upstream, 10605 reproduced 6/6)

```
client → 429 in 61.1s, Retry-After: 30
logbus → retry | model queued (10605) — retrying in 30s (attempt 1/2)
         retry | model queued (10605) — retrying in 30s (attempt 2/2)
         error | model queued: upstream status 403: ...  → surfaced as 429
```

### Note on tests

`backend/tests/` is gitignored and **absent from this checkout**, so the
regression suite could not be added or run here. Verification was performed
by (a) direct unit assertions on the helpers and error builders, and
(b) a live end-to-end request against the real upstream. If the local suite is
restored, add a case asserting a 10605 with `isQueued: true` and
`retryAfterSeconds: 30` yields 429 + `Retry-After: 30` after exactly
`MAX_QUEUE_RETRIES` retries.

