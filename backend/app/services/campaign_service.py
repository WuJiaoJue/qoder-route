"""Daily probe for Qoder credit campaigns (check-in), reachable with a PAT.

Why this exists
---------------
Qoder exposes claimable credit campaigns at an OpenAPI endpoint that
authenticates with the same `pt-* -> jt-*` job token this proxy already obtains
for quota refresh. No CLI, no desktop app, no extra credential.

The point is not to claim automatically. It is to **notice**: campaigns appear
without warning, and an account that never asks has no way to learn one is open.
When `claimable` flips true, this raises a log-bus event so it is visible in the
panel instead of being silently missed.

The identity-header trap
------------------------
Four `Cosy-Machine*` headers must all be present. Per cli2api (MIT, which
documents this the hard way), a partial identity makes the endpoint
**silently drop the claimable credit campaign** — the response still returns
HTTP 200 with an empty campaign list, which is indistinguishable from "nothing
is offered today". So the headers are always derived and always complete; a
missing one would produce a false negative that looks like success.

The derived values are keyed on the PAT, so they are stable per account and no
two accounts on this host share an identity.

Measured state (2026-11-11)
---------------------------
All enabled paid accounts currently return
`showCampaign=false, claimable=false, campaigns=[]`. That is the expected quiet
case, not a failure: the same PATs fetch plan/quota fine, and the CN endpoint
answers 401 for these Global accounts.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import time
from typing import Any, Optional

import httpx

from app.services import logbus, quota_service, settings_service, tls

logger = logging.getLogger("qoderroute.campaign")

# The campaigns endpoint is served by the desktop OpenAPI surface and expects
# the *desktop* Cosy version, which is a different product line from the CLI
# version `qoder_version.py` tracks from npm. It is therefore a runtime setting
# rather than a constant: AGENTS.md forbids hardcoding Cosy-Version, and if
# Qoder bumps the desktop client the only failure mode would be an empty
# campaign list that looks identical to "nothing offered today".
CAMPAIGNS_PATH = "/sash/api/v1/me/campaigns"
DESKTOP_ORIGIN = "https://qoder.sh"

PROBE_INTERVAL_SECONDS = 24 * 3600
PROBE_TIMEOUT = 20.0

# account_id -> {"ts": epoch, "result": {...}}
_last: dict[int, dict[str, Any]] = {}


def machine_identity(pat: str) -> dict[str, str]:
    """Derive the four machine-identity headers from a PAT.

    Mirrors cli2api's derivation, which is the shape verified to work here.

    Note this is deliberately *not* `Account.machine_id`: that column holds a
    dashed UUID used by the signer's inference context, while this endpoint
    expects an md5-hex id, a 43-char base64url token and an 18-char type. Reusing
    the UUID would send three malformed values, and malformed identity headers
    produce the silent-empty-list failure described above. Per-account uniqueness
    — the property that actually matters — is already guaranteed because the
    derivation is keyed on the PAT, and PATs are unique in the pool.
    """
    machine_id = hashlib.md5(("machine:" + pat).encode()).hexdigest()
    token_digest = hashlib.sha512(("machinetoken:" + pat).encode()).digest()
    machine_token = base64.urlsafe_b64encode(token_digest).decode().rstrip("=")[:43]
    machine_type = hashlib.md5(("machinetype:" + pat).encode()).hexdigest().replace("-", "")[:18]
    machine_code = hashlib.md5(("machinecode:" + pat).encode()).hexdigest()
    return {
        "Cosy-MachineId": machine_id,
        "Cosy-MachineToken": machine_token,
        "Cosy-MachineType": machine_type,
        "Cosy-MachineCode": machine_code,
    }


def campaign_headers(pat: str, bearer: str) -> dict[str, str]:
    """Headers for the campaigns endpoint: desktop identity + complete machine set."""
    headers = {
        "Authorization": "Bearer " + bearer,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Qoder",
        # Desktop client, not the CLI: this endpoint keys off the client type.
        "Cosy-ClientType": "10",
        "Cosy-Version": str(settings_service.get("campaign_cosy_version")),
        "Origin": DESKTOP_ORIGIN,
        "Referer": f"{quota_service.QODER_BASE}/growth-page/activity-iframe",
    }
    headers.update(machine_identity(pat))
    return headers


def _parse_campaigns(payload: dict) -> list[dict]:
    """Normalise the campaign list, tolerating both envelope shapes."""
    items = payload.get("campaigns")
    if not isinstance(items, list):
        data = payload.get("data")
        if isinstance(data, dict):
            items = data.get("campaigns") or []
        elif isinstance(data, list):
            items = data
        else:
            items = []
    out = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        benefit = item.get("benefit") or {}
        out.append({
            "campaign_id": str(item.get("campaignId") or ""),
            "action_type": str(item.get("actionType") or ""),
            "claim_status": str(item.get("claimStatus") or ""),
            "kind": str((benefit or {}).get("kind") or "") if isinstance(benefit, dict) else "",
            "amount": float((benefit or {}).get("amount") or 0.0) if isinstance(benefit, dict) else 0.0,
        })
    return out


async def probe_campaigns(pat: str) -> dict[str, Any]:
    """List this account's campaigns. Never raises; failures are reported."""
    bearer = await quota_service.get_job_token(pat)
    if not bearer:
        return {"ok": False, "error": "job token unavailable"}

    url = quota_service.QODER_BASE + CAMPAIGNS_PATH
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT, verify=tls.qoder_verify()) as client:
            resp = await client.get(url, headers=campaign_headers(pat, bearer))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}

    if resp.status_code != 200:
        return {"ok": False, "error": f"HTTP {resp.status_code}",
                "body": resp.text[:200]}
    try:
        payload = resp.json()
    except (json.JSONDecodeError, ValueError):
        return {"ok": False, "error": "non-JSON response", "body": resp.text[:200]}

    campaigns = _parse_campaigns(payload)
    claimable = [
        c for c in campaigns
        if c["action_type"] == "CLAIM_BENEFIT" and c["claim_status"] == "CLAIMABLE"
    ]
    return {
        "ok": True,
        "uid": payload.get("uid"),
        "show_campaign": bool(payload.get("showCampaign")),
        "claimable_flag": bool(payload.get("claimable")),
        "campaign_count": len(campaigns),
        "claimable": claimable,
        "campaigns": campaigns,
    }


async def maybe_probe(account_id: int, name: str, pat: str) -> Optional[dict]:
    """Probe at most once per day per account; alert only when something is claimable.

    Called from the quota loop. Kept cheap and non-fatal: a probe failure must
    never disturb quota refresh, which is the loop's actual job.
    """
    entry = _last.get(account_id)
    if entry and time.time() - entry["ts"] < PROBE_INTERVAL_SECONDS:
        return None

    result = await probe_campaigns(pat)
    _last[account_id] = {"ts": time.time(), "result": result}

    if not result.get("ok"):
        # Expected in the quiet case (no campaign configured for the account).
        # Logged at debug level so a permanently-unavailable endpoint does not
        # produce a warning every day.
        logger.debug("campaign probe id=%s: %s", account_id, result.get("error"))
        return result

    if result.get("claimable"):
        total = sum(c["amount"] for c in result["claimable"])
        logbus.push(
            "warning", "campaign",
            f"可签到领取: {name} 有 {len(result['claimable'])} 个待领积分活动，共 {total:g} credits",
            account_id=account_id,
            action="campaign_available",
        )
        logger.warning(
            "campaign available on account id=%s (%s): %d claimable, %.2f credits total",
            account_id, name, len(result["claimable"]), total,
        )
    return result


def snapshot() -> dict[str, Any]:
    """Current probe state for the panel."""
    rows = []
    for account_id, entry in sorted(_last.items()):
        result = entry["result"]
        rows.append({
            "account_id": account_id,
            "probed_at": entry["ts"],
            "age_hours": round((time.time() - entry["ts"]) / 3600, 1),
            "ok": bool(result.get("ok")),
            "error": result.get("error"),
            "show_campaign": result.get("show_campaign"),
            "claimable_flag": result.get("claimable_flag"),
            "campaign_count": result.get("campaign_count"),
            "claimable": result.get("claimable") or [],
        })
    return {
        "interval_hours": PROBE_INTERVAL_SECONDS / 3600,
        "accounts_probed": len(rows),
        "anything_claimable": any(r["claimable"] for r in rows),
        "accounts": rows,
    }


def force_expire() -> None:
    """Make the next maybe_probe() actually hit the endpoint for every account."""
    _last.clear()


async def probe_all() -> dict[str, Any]:
    """Probe every active account, at most once per interval each.

    Deliberately includes quota-exhausted accounts, unlike `_quota_refresher`,
    which skips them. An exhausted account is precisely the one where a claimed
    campaign matters most, and polling it here costs one GET per account per day.
    """
    from app.core.database import async_session
    from app.models.account import Account
    from sqlalchemy import select

    if not settings_service.get("campaign_probe_enabled"):
        return {"skipped": "disabled", "accounts": 0}

    async with async_session() as session:
        stmt = select(Account.id, Account.name, Account.pat_token).where(
            Account.is_active == True  # noqa: E712 - SQLAlchemy expression
        )
        rows = (await session.execute(stmt)).all()

    probed = 0
    alerts = 0
    for account_id, name, pat in rows:
        if not pat:
            continue
        entry = _last.get(account_id)
        if entry and time.time() - entry["ts"] < PROBE_INTERVAL_SECONDS:
            continue
        result = await maybe_probe(account_id, name, pat)
        probed += 1
        if result and result.get("claimable"):
            alerts += 1
        await asyncio.sleep(0.4)  # politeness, matching the quota loop's pacing
    return {"accounts": len(rows), "probed": probed, "alerts": alerts}
