"""Persist terminal request summaries so Usage/Requests survive a restart."""
from __future__ import annotations

import asyncio
import logging
import time

from sqlalchemy import delete, select

from app.core.database import async_session
from app.models.request_summary import RequestSummary
from app.services import logbus

logger = logging.getLogger("qoderroute.request_store")

# Fallback only; the live value is the `request_retention_days` setting.
RETENTION_SECONDS = 86400
_writes = 0


def retention_seconds() -> float:
    """Configured retention, or ``inf`` when retention is unlimited (0 days).

    Reads the setting lazily: `request_store` is imported by the app factory and
    by the quota loop, so a module-level import of settings_service risks an
    initialisation order dependency this does not need.
    """
    from app.services import settings_service

    try:
        days = int(settings_service.get("request_retention_days"))
    except (TypeError, ValueError):
        days = 30
    if days <= 0:
        return float("inf")
    return days * 86400.0


def _row_from_snapshot(snap: dict) -> dict:
    return {
        "request_id": str(snap["request_id"])[:32],
        "ts": float(snap.get("ts") or time.time()),
        "last_ts": float(snap.get("last_ts") or snap.get("ts") or time.time()),
        "dialect": snap.get("dialect"),
        "model": snap.get("model"),
        "account_id": snap.get("account_id"),
        "account_name": (str(snap["account_name"])[:128] if snap.get("account_name") else None),
        "phase": snap.get("phase"),
        "outcome": snap.get("outcome"),
        "completion_tokens": int(snap.get("completion_tokens") or 0),
        "credits": float(snap.get("credits") or 0.0),
        "latency_ms": snap.get("latency_ms"),
        "first_token_ms": snap.get("first_token_ms"),
        "message": (snap.get("message") or "")[:256],
        "level": snap.get("level"),
    }


async def persist_snapshot(snap: dict) -> None:
    global _writes
    row = _row_from_snapshot(snap)
    try:
        async with async_session() as session:
            existing = await session.get(RequestSummary, row["request_id"])
            if existing is None:
                session.add(RequestSummary(**row))
            else:
                for key, value in row.items():
                    if key != "request_id":
                        setattr(existing, key, value)
            _writes += 1
            if _writes % 25 == 0:
                horizon = retention_seconds()
                if horizon != float("inf"):
                    # Skipping the DELETE entirely under unlimited retention is
                    # deliberate: `ts < now - inf` is false for every row, but
                    # issuing a pointless ranged delete still costs a write and
                    # makes the intent unreadable.
                    cutoff = time.time() - horizon
                    await session.execute(
                        delete(RequestSummary).where(RequestSummary.ts < cutoff)
                    )
            await session.commit()
    except Exception as e:
        logger.warning("request summary persist failed: %s", e)


def schedule(evt: dict) -> None:
    """Fire-and-forget persist of the current in-memory snapshot for this request."""
    rid = evt.get("request_id")
    if not rid:
        return
    snap = logbus.get_request(rid) or {
        "request_id": rid,
        "ts": evt.get("ts"),
        "last_ts": evt.get("ts"),
        **evt,
    }
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(persist_snapshot(snap), name="request-summary-persist")


async def load_recent() -> int:
    """Hydrate the in-memory request index from the newest retained rows.

    Bounded by the index's own capacity (`logbus._REQUESTS_MAX`) and fetched
    newest-first: the in-memory index evicts anything past that cap anyway, so
    reading every retained row would load thousands of records, immediately
    discard the oldest, and make startup time grow with retention. Under
    unlimited retention that difference is the gap between a fixed cost and a
    scan of the whole table.
    """
    limit = logbus.REQUESTS_INDEX_MAX
    horizon = retention_seconds()
    async with async_session() as session:
        stmt = select(RequestSummary).order_by(RequestSummary.ts.desc()).limit(limit)
        if horizon != float("inf"):
            stmt = stmt.where(RequestSummary.ts >= time.time() - horizon)
        result = await session.execute(stmt)
        # Descending for the LIMIT; restore ascending order for the hydrate loop.
        rows = list(result.scalars().all())[::-1]
    for row in rows:
        logbus.hydrate_request({
            "request_id": row.request_id,
            "ts": row.ts,
            "last_ts": row.last_ts,
            "dialect": row.dialect,
            "model": row.model,
            "account_id": row.account_id,
            "account_name": row.account_name,
            "phase": row.phase,
            "outcome": row.outcome,
            "completion_tokens": row.completion_tokens,
            "credits": row.credits,
            "latency_ms": row.latency_ms,
            "first_token_ms": row.first_token_ms,
            "message": row.message,
            "level": row.level,
            "live": False,
        })
    logger.info("Hydrated %d request summaries", len(rows))
    return len(rows)


async def clear_all() -> int:
    """Wipe persisted request summaries (used by DELETE /api/logs)."""
    async with async_session() as session:
        result = await session.execute(delete(RequestSummary))
        await session.commit()
        count = result.rowcount or 0
    logger.info("Cleared %d request summaries", count)
    return count
