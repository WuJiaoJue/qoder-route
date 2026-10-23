"""Sync the runtime model catalog from Qoder's signed model-list endpoint.

The shipped catalog in ``model_catalog.MODEL_CATALOG`` is a static baseline.
Qoder's real catalog is dynamic — credit factors follow promotions and models
appear or retire without a client release — so this service fetches the live
list, merges it over the baseline, and installs the result as the *effective*
catalog (see ``model_catalog.apply_dynamic_catalog``).

The snapshot is persisted to ``data/model_catalog_sync.json`` so the last good
sync survives a backend restart.

Failure policy: every path is non-fatal. If the fetch fails, times out, or
returns something unusable, the baseline catalog stays active and the next
cycle retries. Model routing must never depend on this succeeding.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

import httpx
from sqlalchemy import select

from app.core.database import async_session
from app.models.account import Account
from app.services import (
    logbus,
    model_catalog,
    quota_service,
    settings_service,
    signer_service,
)

logger = logging.getLogger("qoderroute.model_sync")

SYNC_INTERVAL_SECONDS = 6 * 3600

# The upstream model-list call needs a PAT; any enabled account works. Free-tier
# rows are excluded only because a paid account is the safer authenticator when
# both exist — the endpoint is read-only and cheap.
_TIER_KEYS = frozenset({"auto", "ultimate", "performance", "efficient", "lite"})


def _store_path() -> Path:
    # ``config.data_dir`` is documented as unused; paths in this project are
    # relative to the backend root, matching ``data/qoderroute.db``.
    return Path(__file__).resolve().parents[2] / "data" / "model_catalog_sync.json"


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _convert_entry(m: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Map an upstream model-list row onto our catalog row shape."""
    key = str(m.get("key") or "").strip()
    if not key or m.get("enable") is False:
        return None

    thinking_config = m.get("thinking_config") or {}
    enabled = thinking_config.get("enabled") or {}
    efforts_map = enabled.get("efforts") or {}
    efforts = list(efforts_map.keys()) if isinstance(efforts_map, dict) else []
    default_effort = next(
        (
            k
            for k, v in (efforts_map.items() if isinstance(efforts_map, dict) else [])
            if isinstance(v, dict) and v.get("is_default")
        ),
        None,
    )

    windows = m.get("available_context_windows")
    if not windows:
        cfg = m.get("context_config") or {}
        windows = sorted(
            _as_int(v.get("token_count"), 0)
            for v in (cfg.values() if isinstance(cfg, dict) else [])
            if isinstance(v, dict) and v.get("token_count") is not None
        )
    windows = [w for w in (windows or []) if w > 0]
    max_input = _as_int(m.get("max_input_tokens"), 180_000)

    return {
        "key": key,
        "name": str(m.get("display_name") or key),
        "credit_factor": _as_float(m.get("price_factor")),
        "is_reasoning": bool(m.get("is_reasoning")),
        "is_vision": bool(m.get("is_vl")),
        "max_input_tokens": max_input,
        "context_windows": windows,
        "kind": "tier" if key in _TIER_KEYS else "model",
        # Rides through apply_dynamic_catalog() into the thinking overlay.
        "thinking_efforts": efforts,
        "default_thinking_effort": default_effort,
    }


def _merge_with_baseline(converted: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Upstream wins for shared keys; baseline-only keys are preserved.

    Preserving baseline-only entries matters: a key that upstream omits (a
    retired or temporarily hidden model) must not vanish from the panel while
    clients may still be sending it.
    """
    upstream_by_key = {e["key"]: e for e in converted}
    merged: list[dict[str, Any]] = []
    for base in model_catalog.MODEL_CATALOG:
        row = dict(base)
        up = upstream_by_key.get(str(base["key"]))
        if up is not None:
            row.update({k: v for k, v in up.items() if k != "probe_default"})
        merged.append(row)
    baseline_keys = {str(e["key"]) for e in model_catalog.MODEL_CATALOG}
    merged.extend(up for up in converted if up["key"] not in baseline_keys)
    return merged


def load_synced() -> int:
    """Restore a persisted snapshot into memory. Returns the entry count."""
    path = _store_path()
    if not path.exists():
        return 0
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("Could not read synced model catalog: %s", exc)
        return 0
    entries = payload.get("models") or []
    if not isinstance(entries, list) or not entries:
        return 0
    try:
        model_catalog.apply_dynamic_catalog(entries)
    except (TypeError, ValueError) as exc:
        logger.warning("Ignoring malformed synced model catalog: %s", exc)
        return 0
    logger.info("Restored synced model catalog (%d entries)", len(entries))
    return len(entries)


def _persist(entries: list[dict[str, Any]], synced_at: float) -> None:
    path = _store_path()
    tmp = path.with_suffix(".json.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-rename so a crash mid-write cannot leave a half file that
        # would poison the next startup.
        tmp.write_text(
            json.dumps({"synced_at": synced_at, "models": entries}, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(path)
    except OSError as exc:
        logger.warning("Could not persist synced model catalog: %s", exc)


def sync_status() -> dict[str, Any]:
    """Last sync snapshot info for the panel (never raises)."""
    count = len(model_catalog.effective_catalog())
    path = _store_path()
    if not path.exists():
        return {"ok": True, "synced_at": None, "model_count": count}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return {
            "ok": True,
            "synced_at": payload.get("synced_at"),
            "model_count": len(payload.get("models") or []),
        }
    except (OSError, ValueError):
        return {"ok": True, "synced_at": None, "model_count": count}


async def sync_model_catalog() -> dict[str, Any]:
    """Fetch the live catalog, merge it over the baseline, activate and persist."""
    async with async_session() as session:
        acct = (
            await session.execute(
                select(Account)
                .where(Account.enabled == True)  # noqa: E712
                .where(Account.machine_id.is_not(None))
                .order_by(Account.is_paid.desc(), Account.id.asc())
                .limit(1)
            )
        ).scalars().first()

    if acct is None:
        return {"ok": False, "error": "No enabled account to authenticate the sync"}

    pat = acct.pat_token
    jt = await quota_service.get_job_token(pat)
    if not jt:
        return {"ok": False, "error": "Job token exchange failed"}
    uid = await quota_service.get_uid(pat) or ""
    host = f"https://{settings_service.get_qoder_infer_base()}.qoder.sh"

    async with httpx.AsyncClient(timeout=25) as client:
        signed = await signer_service.post_to_signer(
            "/sign_get",
            json={
                "jt": jt,
                "uid": uid,
                "machine_id": acct.machine_id,
                "base_url": host,
                "url_path": "/api/v2/model/list?Encode=1",
            },
            client=client,
        )
        if signed.status_code != 200:
            return {"ok": False, "error": f"Signer HTTP {signed.status_code}"}
        payload = signed.json()
        request_url = payload.get("url")
        if not request_url:
            return {"ok": False, "error": "Signer did not return a signed URL"}
        gr = await client.get(
            request_url,
            headers={**quota_service._base_headers(), **(payload.get("headers") or {})},
        )

    if gr.status_code != 200:
        return {"ok": False, "error": f"Upstream HTTP {gr.status_code}"}
    try:
        rows = (gr.json() or {}).get("chat") or []
    except ValueError:
        return {"ok": False, "error": "Upstream returned a non-JSON payload"}
    if not isinstance(rows, list):
        return {"ok": False, "error": "Upstream model list was not a list"}

    converted = [e for e in (_convert_entry(m) for m in rows if isinstance(m, dict)) if e]
    if not converted:
        return {"ok": False, "error": "Upstream returned an empty model list"}

    before_keys = model_catalog.model_keys()
    merged = _merge_with_baseline(converted)
    model_catalog.apply_dynamic_catalog(merged)
    synced_at = time.time()
    _persist(merged, synced_at)

    added = len({e["key"] for e in merged} - before_keys)
    logbus.push(
        "info",
        "routing",
        f"Model catalog synced: {len(merged)} models (+{added} new)",
    )
    return {
        "ok": True,
        "synced_at": synced_at,
        "model_count": len(merged),
        "added": added,
        "updated": len(merged) - added,
    }


async def sync_loop() -> None:
    """Periodic background sync; failures wait for the next cycle."""
    while True:
        await asyncio.sleep(SYNC_INTERVAL_SECONDS)
        try:
            await sync_model_catalog()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a sync failure must never kill the loop
            logger.warning("Model catalog sync failed", exc_info=True)
