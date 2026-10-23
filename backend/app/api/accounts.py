import logging
import time

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import Integer, and_, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import async_session, get_db
from app.models.request_summary import RequestSummary
from app.models.schemas import (
    AccountCreate, AccountOut, AccountPoolStatus, AccountUpdate, DashboardStats
)
from app.models.account import Account
from app.services.account_pool import pool
from app.services.qoder_client import validate_pat, qoder_model_display
from app.services import logbus
from app.services import quota_service

logger = logging.getLogger("qoderroute.api.accounts")
router = APIRouter(prefix="/api/accounts", tags=["accounts"])


# ── Static routes FIRST (before /{account_id} shadow-catches them) ──

# Legacy fallback: completions used to be identified only by message text.
# New events carry phase=done + outcome=ok. Keep the spellings so a buffer
# still holding pre-schema lines counts, and so a new dialect cannot silently
# vanish from Usage if someone forgets the structured fields.
_COMPLETION_MESSAGES = frozenset({
    "stream done",              # OpenAI streaming
    "completion ok",            # OpenAI non-streaming
    "anthropic stream done",    # Anthropic streaming
    "anthropic completion ok",  # Anthropic non-streaming
})


def _is_completion(e: dict) -> bool:
    if e.get("source") != "chat":
        return False
    if e.get("phase") == "done" and e.get("outcome") == "ok":
        return True
    return e.get("message") in _COMPLETION_MESSAGES


# Traffic window presets: label -> (seconds, bucket size). Buckets grow with the
# window so the series stays ~60-96 points at every scale.
_ACTIVITY_WINDOWS: dict[str, tuple[int, int]] = {
    "1h": (3600, 60),
    "6h": (6 * 3600, 5 * 60),
    "24h": (24 * 3600, 15 * 60),
}


@router.get("/stats/activity")
async def dashboard_activity(window: str = "1h"):
    """Chat traffic over a window, aggregated from the persisted request table.

    Reads `request_summaries` (24h retention) rather than the in-memory log bus:
    the ring buffer holds only 5 000 lines and the hydrated request index only
    2 000 rows, so a 6h or 24h window would silently under-report. Aggregating in
    SQL is both exact and restart-proof — the chart no longer blanks on reload.

    Counts completions (`phase=done` + `outcome=ok`), matching the Requests view.
    Tokens are completion tokens, matching the lifetime counters on accounts.
    """
    spec = _ACTIVITY_WINDOWS.get(window)
    if spec is None:
        raise HTTPException(
            status_code=422,
            detail=f"unknown window '{window}'; expected one of {sorted(_ACTIVITY_WINDOWS)}",
        )
    window_sec, bucket_sec = spec

    now = time.time()
    n_buckets = window_sec // bucket_sec
    # Align buckets to whole wall-clock units. A floating `now`-anchored window
    # moves bucket edges on every poll, so the chart morphs even with no traffic.
    end = int(now // bucket_sec) * bucket_sec
    start = end - window_sec

    series = [
        {"t": start + i * bucket_sec, "requests": 0, "tokens": 0, "credits": 0.0}
        for i in range(n_buckets + 1)
    ]
    by_model: dict[str, dict] = {}
    totals = {"requests": 0, "tokens": 0, "credits": 0.0}

    def _add(bucket_ts: float, requests: int, tokens: int, credits: float, model: str | None) -> None:
        idx = min(max(int((bucket_ts - start) // bucket_sec), 0), n_buckets)
        series[idx]["requests"] += requests
        series[idx]["tokens"] += tokens
        series[idx]["credits"] += credits
        totals["requests"] += requests
        totals["tokens"] += tokens
        totals["credits"] += credits
        if model:
            slot = by_model.setdefault(model, {"model": model, "requests": 0, "tokens": 0, "credits": 0.0})
            slot["requests"] += requests
            slot["tokens"] += tokens
            slot["credits"] += credits

    # Bucket index computed in SQL: integer division of the offset from `start`.
    bucket_idx = cast((RequestSummary.ts - start) / bucket_sec, Integer)
    completed = and_(
        RequestSummary.ts >= start,
        # Upper bound is one bucket past the aligned window end so the bucket
        # currently in progress is included (clamped into the final slot by
        # _add). Cutting exactly at `end` would hide up to a full bucket of
        # fresh traffic and make the chart lag the Requests log.
        RequestSummary.ts < end + bucket_sec,
        RequestSummary.phase == "done",
        RequestSummary.outcome == "ok",
    )

    async with async_session() as session:
        rows = (await session.execute(
            select(
                bucket_idx.label("b"),
                RequestSummary.model,
                func.count().label("n"),
                func.coalesce(func.sum(RequestSummary.completion_tokens), 0),
                func.coalesce(func.sum(RequestSummary.credits), 0.0),
            )
            .where(completed)
            .group_by("b", RequestSummary.model)
        )).all()

        for b, model, n, tokens, credits in rows:
            _add(start + int(b) * bucket_sec, int(n), int(tokens), float(credits), model)

        # A completion is persisted fire-and-forget at terminal event time, so the
        # newest rows can lag by a beat. Overlay only those live completions that
        # are not in the table yet, so the chart stays current without double count.
        live = [
            e for e in logbus.recent(limit=5000)
            if _is_completion(e) and float(e.get("ts") or 0) >= end - 2 * bucket_sec
        ]
        live_ids = {str(e.get("request_id") or "") for e in live} - {""}
        already: set[str] = set()
        if live_ids:
            already = {
                r[0] for r in (await session.execute(
                    select(RequestSummary.request_id).where(
                        RequestSummary.request_id.in_(live_ids)
                    )
                )).all()
            }

    for e in live:
        rid = str(e.get("request_id") or "")
        if not rid or rid in already:
            continue
        ts = float(e.get("ts") or 0)
        if not start <= ts < end + bucket_sec:
            continue
        already.add(rid)
        _add(
            ts, 1,
            int(e.get("completion_tokens") or 0),
            float(e.get("credits") or 0.0),
            str(e.get("model") or "") or None,
        )

    # Live accessor: the effective catalog may have been synced.
    display = {key: name for name, key in qoder_model_display()}
    models = sorted(by_model.values(), key=lambda m: m["requests"], reverse=True)
    for m in models:
        m["display"] = display.get(m["model"], m["model"])
        m["credits"] = round(m["credits"], 2)
    for point in series:
        point["credits"] = round(point["credits"], 2)

    return {
        "window_key": window,
        "window_sec": window_sec,
        "bucket_sec": bucket_sec,
        "series": series,
        "by_model": models,
        "window": {
            "requests": totals["requests"],
            "tokens": totals["tokens"],
            "credits": round(totals["credits"], 2),
        },
    }


@router.get("/stats/dashboard", response_model=DashboardStats)
async def dashboard_stats(db: AsyncSession = Depends(get_db)):
    stats = await pool.get_stats(db)
    accounts = await pool.list_accounts(db)
    exhausted = sum(1 for a in accounts if a.is_quota_exceeded)

    return DashboardStats(
        total_accounts=stats["total_accounts"],
        active_accounts=stats["active_accounts"],
        available_now=stats["available_now"],
        accounts_in_cooldown=stats["accounts_in_cooldown"],
        accounts_exhausted=exhausted,
        accounts_disabled=stats["accounts_disabled"],
        total_requests=stats["total_requests"],
        total_tokens=stats["total_tokens"],
        credits_spent=stats["credits_spent"],
        recent_errors=[
            {
                "account_id": a.id,
                "account_name": a.name,
                "message": a.last_error_message,
                "at": a.last_error_at.isoformat() if a.last_error_at else None,
            }
            for a in accounts
            if a.last_error_message
        ][:20],
    )


# ── Collection routes ──

@router.get("", response_model=AccountPoolStatus)
async def list_accounts(db: AsyncSession = Depends(get_db)):
    accounts = await pool.list_accounts(db)
    stats = await pool.get_stats(db)
    return AccountPoolStatus(
        total_accounts=stats["total_accounts"],
        active_accounts=stats["active_accounts"],
        available_accounts=stats["available_now"],
        accounts_in_cooldown=stats["accounts_in_cooldown"],
        total_requests=stats["total_requests"],
        accounts=[AccountOut.model_validate(a) for a in accounts],
    )


# ── Filtered account views ──

@router.get("/available")
async def list_available_accounts(db: AsyncSession = Depends(get_db)):
    """Only available accounts — mirrors the routing filters in get_next_account."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    stmt = select(Account).where(
        and_(
            Account.is_active == True,
            # Mirrors the routing gate: a disabled account is never "available".
            Account.enabled == True,
            Account.is_available == True,
            Account.is_quota_exceeded == False,
            (Account.quota_remaining.is_(None))
            | (Account.quota_remaining > 0)
            # Free-tier rows carry a 0/0 quota that hides personal
            # resource-pack credits; show them as available.
            | (Account.is_paid == False),
            (Account.cooldown_until.is_(None)) | (Account.cooldown_until < now),
            Account.consecutive_failures < settings.max_consecutive_failures,
        )
    )
    result = await db.execute(stmt)
    accounts = result.scalars().all()

    return {
        "filter": "available",
        "count": len(accounts),
        "accounts": [AccountOut.model_validate(a) for a in accounts],
    }


@router.get("/exhausted")
async def list_exhausted_accounts(db: AsyncSession = Depends(get_db)):
    """Only exhausted accounts (is_quota_exceeded == True)."""
    from sqlalchemy import and_
    
    stmt = select(Account).where(
        and_(
            Account.is_active == True,
            Account.is_quota_exceeded == True,
        )
    )
    result = await db.execute(stmt)
    accounts = result.scalars().all()
    
    return {
        "filter": "exhausted",
        "count": len(accounts),
        "accounts": [AccountOut.model_validate(a) for a in accounts],
    }


@router.post("", response_model=AccountOut)
async def create_account(body: AccountCreate, db: AsyncSession = Depends(get_db)):
    valid, msg = await validate_pat(body.pat_token)
    if not valid:
        raise HTTPException(status_code=400, detail=f"Token validation failed: {msg}")

    # Reject plan-less (free tier) accounts up front. An account WITH a plan
    # but no quota is still added — the quota refresh parks it as exhausted.
    # Credits on Qoder are one-shot, so a failed plan fetch must not skip
    # this check and let a free PAT into the pool.
    pq = await quota_service.fetch_plan_quota(body.pat_token)
    # fetch_plan_quota returns a partial dict when only userinfo/quota answered;
    # without the plan payload the tier is unknown and the free-tier check below
    # would silently pass.
    if pq is None or not pq.get("plan_fetched", True):
        raise HTTPException(
            status_code=400,
            detail="Could not fetch plan/quota from Qoder — not added. Retry when the API is reachable.",
        )
    tier = str(pq.get("plan_tier") or "").strip().lower()
    # Free-tier accounts are allowed — they can still use free models (e.g. qfmodel)
    # that don't consume credits. Quota tracking doesn't apply to them.

    try:
        account = await pool.add_account(
            db,
            name=quota_service.resolve_account_name(body.name, pq),
            pat_token=body.pat_token,
            priority=body.priority,
            model_level=body.model_level,
            default_model=body.default_model,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    # Capture the PK before refresh_quota/refresh: a failed refresh expires
    # the instance, and reading account.id then lazy-loads (MissingGreenlet).
    account_id = account.id
    await pool.refresh_quota(account_id)
    try:
        await db.refresh(account)
        return AccountOut.model_validate(account)
    except Exception:  # noqa: BLE001 — auto-delete expires the instance
        pass
    try:
        db.expunge(account)
    except Exception:  # noqa: BLE001
        pass
    fresh = await db.get(Account, account_id)
    if fresh is None:
        raise HTTPException(
            status_code=409,
            detail="Account is exhausted and was removed (auto-delete exhausted is on).",
        )
    return AccountOut.model_validate(fresh)


# ── Item routes LAST ──

@router.delete("/{account_id}")
async def delete_account(account_id: int, db: AsyncSession = Depends(get_db)):
    success = await pool.remove_account(db, account_id)
    if not success:
        raise HTTPException(status_code=404, detail="Account not found")
    return {"ok": True}


@router.patch("/{account_id}", response_model=AccountOut)
async def update_account(
    account_id: int,
    body: AccountUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Partial update of operator-controlled fields (priority, enabled, name).

    Only fields present in the request body are changed. Out-of-range
    `priority` is rejected with 422 by schema validation rather than silently
    clamped, so a typo is visible instead of quietly reordering routing.
    """
    # `exclude_unset` is what makes this a true PATCH: an explicit
    # `{"enabled": false}` must be distinguished from an omitted field.
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=422, detail="No updatable fields provided")

    acc = await pool.update_account(db, account_id, **fields)
    if acc is None:
        raise HTTPException(status_code=404, detail="Account not found")
    return AccountOut.model_validate(acc)


@router.post("/{account_id}/quota/refresh")
async def refresh_account_quota(account_id: int):
    data = await pool.refresh_quota(account_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Account not found or quota fetch failed")
    return {"ok": True, "quota": data}


@router.get("/{account_id}/pat")
async def reveal_account_pat(account_id: int, db: AsyncSession = Depends(get_db)):
    """Return the full PAT for clipboard copy. Localhost panel — no redaction."""
    acc = await pool.get_account_by_id(db, account_id)
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")
    return {"pat": acc.pat_token}
