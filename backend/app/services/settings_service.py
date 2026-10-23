"""DB-backed runtime settings with an in-memory cache.

Reads are synchronous dict lookups, so hot paths (e.g. the worker log
reader checking a flag per line) never touch the database.
"""
import logging
from typing import Optional

from sqlalchemy import select

from app.core.database import async_session
from app.models.app_setting import AppSetting

logger = logging.getLogger("qoderroute.settings")

SettingValue = bool | str | int | list[str]

_QODER_INFER_BASES = frozenset({"api1", "api2", "api3"})

# How the pool picks the next PAT. Ordering is applied in Python (not SQL)
# because `spread` depends on live in-flight counts, which only exist in memory.
#   fill_first — highest priority, then lowest id. Default: sticks to one PAT so
#                a near-empty account's remaining credit is not scattered.
#   spread     — fewest in-flight requests first, then most remaining credit.
#                Useful for bursts / parallel agents: no single PAT gets stacked.
#   least_used — fewest lifetime requests first, then lowest id. Evens out wear.
#   by_conversation — deterministic shard: one conversation always starts on the
#                same account, different conversations land on different
#                accounts. Distributes concurrency across the pool without
#                breaking conversation locality; degrades to fill_first when the
#                client sends no session identifier.
ROUTING_STRATEGIES = frozenset({"fill_first", "spread", "least_used", "by_conversation"})

_DEFAULTS: dict[str, SettingValue] = {
    "worker_logs_enabled": True,
    "worker_retry_allow": False,
    "worker_proxy_use": False,
    "accounts_show_email": True,
    "accounts_show_tokens": True,
    "accounts_show_requests": True,
    "accounts_auto_delete_exhausted": False,
    "qoder_infer_base": "api3",
    "routing_strategy": "fill_first",
    # Keep successive turns of one conversation on the same pooled account so
    # upstream prefix caching stays warm. A preference only: an expired pin or
    # an unroutable account falls through to the normal strategy.
    "conversation_affinity": True,
}

_cache: dict[str, SettingValue] = {
    key: list(value) if isinstance(value, list) else value
    for key, value in _DEFAULTS.items()
}


def _normalize_value(key: str, value: object) -> Optional[SettingValue]:
    """Validate a setting supplied by the API or read from storage."""
    default = _DEFAULTS.get(key)
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        if value == "true":
            return True
        if value == "false":
            return False
        return None
    if isinstance(default, int) and not isinstance(default, bool):
        # Values round-trip through the key/value table as strings, so accept
        # both an int (from the API) and a numeric string (from storage).
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            parsed = value
        elif isinstance(value, str):
            try:
                parsed = int(value.strip())
            except ValueError:
                return None
        else:
            return None
        return parsed
    if key == "qoder_infer_base" and isinstance(value, str):
        candidate = value.strip().lower()
        if candidate in _QODER_INFER_BASES:
            return candidate
    if key == "routing_strategy" and isinstance(value, str):
        candidate = value.strip().lower()
        if candidate in ROUTING_STRATEGIES:
            return candidate
    return None


def _serialize_value(value: SettingValue) -> str:
    if isinstance(value, list):
        import json
        return json.dumps(value, separators=(",", ":"))
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


async def load() -> None:
    """Pull persisted values into the cache. Called once at startup."""
    async with async_session() as session:
        rows = (await session.execute(select(AppSetting))).scalars().all()
    loaded = {
        key: list(value) if isinstance(value, list) else value
        for key, value in _DEFAULTS.items()
    }
    for row in rows:
        normalized = _normalize_value(row.key, row.value)
        if normalized is not None:
            loaded[row.key] = normalized
    _cache.clear()
    _cache.update(loaded)
    logger.info(f"Settings loaded: {_cache}")


def get(key: str) -> SettingValue:
    return _cache.get(key, _DEFAULTS.get(key, False))


def get_qoder_infer_base() -> str:
    value = _cache.get("qoder_infer_base", _DEFAULTS["qoder_infer_base"])
    normalized = _normalize_value("qoder_infer_base", value)
    return str(normalized or _DEFAULTS["qoder_infer_base"])


def snapshot() -> dict[str, SettingValue]:
    return {
        key: list(value) if isinstance(value := _cache.get(key, default), list) else value
        for key, default in _DEFAULTS.items()
    }


async def update(values: dict[str, SettingValue]) -> dict[str, SettingValue]:
    """Persist known keys and refresh the cache. Unknown keys are ignored."""
    normalized_values: dict[str, SettingValue] = {}
    for key, value in values.items():
        normalized = _normalize_value(key, value)
        if normalized is not None:
            normalized_values[key] = normalized

    async with async_session() as session:
        for key, value in normalized_values.items():
            row = await session.get(AppSetting, key)
            if row is None:
                session.add(AppSetting(key=key, value=_serialize_value(value)))
            else:
                row.value = _serialize_value(value)
        await session.commit()
    _cache.update(normalized_values)
    return snapshot()
