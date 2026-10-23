"""Canonical Qoder model catalog used by routing, UI and public APIs.

Credit factors and capabilities mirror the current Qoder catalog-v6. They are
multipliers, not fixed per-request prices; upstream billing and request shape
can change the final credit charge reported by upstream.
"""
from __future__ import annotations

from typing import Any


MODEL_CATALOG: tuple[dict[str, Any], ...] = (
    {"key": "auto", "name": "Auto", "credit_factor": 0.5, "is_reasoning": False, "is_vision": True, "max_input_tokens": 180_000, "context_windows": [], "kind": "tier"},
    {"key": "ultimate", "name": "Ultimate", "credit_factor": 2.0, "is_reasoning": True, "is_vision": True, "max_input_tokens": 1_000_000, "context_windows": [200_000, 400_000, 1_000_000], "kind": "tier"},
    {"key": "fast", "name": "Fast", "credit_factor": 0.8, "is_reasoning": False, "is_vision": True, "max_input_tokens": 200_000, "context_windows": [], "kind": "tier"},
    {"key": "efficient", "name": "Efficient", "credit_factor": 0.3, "is_reasoning": False, "is_vision": True, "max_input_tokens": 180_000, "context_windows": [], "kind": "tier"},
    {"key": "lite", "name": "Lite", "credit_factor": 0.0, "is_reasoning": False, "is_vision": False, "max_input_tokens": 180_000, "context_windows": [], "kind": "tier"},
    {"key": "sonus", "name": "Sonus", "credit_factor": 8.0, "is_reasoning": True, "is_vision": True, "max_input_tokens": 200_000, "context_windows": [200_000], "kind": "model"},
    {"key": "cmodel", "name": "Cantus", "credit_factor": 4.0, "is_reasoning": True, "is_vision": True, "max_input_tokens": 200_000, "context_windows": [200_000], "kind": "model"},
    {"key": "qmodel_38max", "name": "Qwen3.8-Max", "credit_factor": 0.5, "is_reasoning": True, "is_vision": True, "max_input_tokens": 200_000, "context_windows": [200_000], "kind": "model"},
    {"key": "qfmodel", "name": "Qwen3.8-Flash", "credit_factor": 0.1, "is_reasoning": True, "is_vision": True, "max_input_tokens": 200_000, "context_windows": [200_000], "kind": "model"},
    {"key": "qmodel_latest", "name": "Qwen3.7-Max", "credit_factor": 0.5, "is_reasoning": False, "is_vision": True, "max_input_tokens": 200_000, "context_windows": [200_000], "kind": "model"},
    {"key": "qmodel", "name": "Qwen3.7-Plus", "credit_factor": 0.1, "is_reasoning": False, "is_vision": True, "max_input_tokens": 200_000, "context_windows": [200_000], "kind": "model"},
    {"key": "kmodel_latest", "name": "Kimi-K3", "credit_factor": 1.4, "is_reasoning": False, "is_vision": True, "max_input_tokens": 200_000, "context_windows": [200_000], "kind": "model"},
    {"key": "kmodel", "name": "Kimi-K2.8-Preview", "credit_factor": 0.8, "is_reasoning": False, "is_vision": True, "max_input_tokens": 200_000, "context_windows": [200_000], "kind": "model"},
    {"key": "gmodel", "name": "GLM-5.3", "credit_factor": 0.8, "is_reasoning": True, "is_vision": True, "max_input_tokens": 200_000, "context_windows": [200_000], "kind": "model"},
    {"key": "gfmodel", "name": "GLM-5.3-Flash", "credit_factor": 0.1, "is_reasoning": True, "is_vision": True, "max_input_tokens": 200_000, "context_windows": [200_000], "kind": "model"},
    {"key": "dmodel", "name": "DeepSeek-V4-Pro", "credit_factor": 0.8, "is_reasoning": True, "is_vision": True, "max_input_tokens": 200_000, "context_windows": [200_000], "kind": "model"},
    {"key": "dfmodel", "name": "DeepSeek-Flash", "credit_factor": 0.3, "is_reasoning": True, "is_vision": True, "max_input_tokens": 200_000, "context_windows": [200_000], "kind": "model"},
    {"key": "mmodel", "name": "MiniMax-M3", "credit_factor": 0.2, "is_reasoning": False, "is_vision": True, "max_input_tokens": 200_000, "context_windows": [200_000], "kind": "model"},
)

MODEL_BY_KEY = {entry["key"]: entry for entry in MODEL_CATALOG}
MODEL_KEYS = frozenset(MODEL_BY_KEY)

# ── Runtime overlay ───────────────────────────────────────────────────────────
# The hardcoded catalog above is the *baseline*: it is what the proxy ships with
# and what it falls back to when the live list cannot be fetched. Qoder's real
# catalog is dynamic (promotions change credit factors, models appear/retire
# without a client release), so `model_sync` fetches the live list and installs
# it here.
#
# Every lookup below reads through these accessors rather than the module-level
# constants. That matters: several modules build their maps at import time
# (`qoder_client.QODER_MODEL_LEVELS`, `direct_client.MODEL_KEY_MAP`,
# `direct_client._CONTEXT_WINDOW_BY_MODEL`, ...). Those snapshots would keep
# serving the baseline forever, silently ignoring a sync. They now call the
# accessors instead.
_dynamic_catalog: tuple[dict[str, Any], ...] | None = None
_dynamic_thinking: dict[str, dict[str, Any]] | None = None


def effective_catalog() -> tuple[dict[str, Any], ...]:
    """The active catalog: the synced snapshot when present, else the baseline."""
    return _dynamic_catalog if _dynamic_catalog is not None else MODEL_CATALOG


def effective_thinking_config() -> dict[str, dict[str, Any]]:
    """Active thinking metadata, synced when available."""
    return _dynamic_thinking if _dynamic_thinking is not None else THINKING_CONFIG_BY_MODEL


def catalog_by_key() -> dict[str, dict[str, Any]]:
    """Lookup map for the active catalog."""
    return {str(e["key"]): e for e in effective_catalog()}


def model_keys() -> set[str]:
    """Keys present in the active catalog."""
    return {str(e["key"]) for e in effective_catalog()}


def apply_dynamic_catalog(entries: list[dict[str, Any]]) -> None:
    """Install a synced catalog. Empty input is ignored (never blank the UI)."""
    global _dynamic_catalog, _dynamic_thinking
    rows = tuple(
        {str(k): v for k, v in e.items()}
        for e in entries
        if e.get("key")
    )
    if not rows:
        return
    _dynamic_catalog = rows
    # Thinking metadata rides on the same rows so a model that gains/loses a
    # thinking config upstream is reflected without a code change. A row without
    # the fields (baseline shape) falls back to THINKING_CONFIG_BY_MODEL.
    overlay: dict[str, dict[str, Any]] = {}
    for e in rows:
        key = str(e["key"])
        if "thinking_efforts" in e:
            overlay[key] = {
                "efforts": list(e.get("thinking_efforts") or []),
                "default_effort": e.get("default_thinking_effort"),
            }
    _dynamic_thinking = {**THINKING_CONFIG_BY_MODEL, **overlay}


def reset_dynamic_catalog() -> None:
    """Drop any synced snapshot (tests / manual revert to baseline)."""
    global _dynamic_catalog, _dynamic_thinking
    _dynamic_catalog = None
    _dynamic_thinking = None


# Qoder exposes this independently from ``is_reasoning``.  A model such as
# Kimi-K3 is catalogued as non-reasoning while still offering a controllable
# thinking mode.  Keeping the two flags separate prevents the UI from implying
# that Kimi cannot emit thinking blocks.
THINKING_CONFIG_BY_MODEL: dict[str, dict[str, Any]] = {
    "ultimate": {"efforts": ["low", "medium", "high", "xhigh", "max"], "default_effort": "high"},
    "sonus": {"efforts": ["low", "medium", "high", "xhigh", "max"], "default_effort": "high"},
    "cmodel": {"efforts": ["low", "medium", "high", "xhigh", "max"], "default_effort": "high"},
    "qmodel_38max": {"efforts": ["low", "medium", "xhigh"], "default_effort": "xhigh"},
    "qfmodel": {"efforts": ["low", "medium", "xhigh"], "default_effort": "xhigh"},
    "qmodel_latest": {"efforts": [], "default_effort": None},
    "qmodel": {"efforts": [], "default_effort": None},
    "kmodel_latest": {"efforts": ["low", "high", "max"], "default_effort": "max"},
    "gmodel": {"efforts": ["low", "high", "max"], "default_effort": "max"},
    "gfmodel": {"efforts": ["high", "max"], "default_effort": "max"},
    "dmodel": {"efforts": ["high", "max"], "default_effort": "max"},
    "dfmodel": {"efforts": ["low", "high", "max"], "default_effort": "max"},
}


# Free-for-paid-plan models: routed without requiring account credits.
# ``lite`` is catalogued with credit_factor 0.0; ``efficient`` is charged 0
# credits upstream for paid plans (qoder promo: "Free and unlimited for paid
# plans") and verified billable=false on the live upstream, so a 0-credit paid
# account may still call it.
_FREE_MODEL_KEYS = frozenset({"efficient", "lite"})


def is_free_model(model_level: str) -> bool:
    """A model that paid plans may call with zero remaining credits.

    Conservative by design: only models known to be free inside a paid plan
    (or catalogued with a 0 credit factor) get the quota gate bypass.
    """
    entry = catalog_by_key().get(str(model_level or ""))
    if entry is not None and float(entry.get("credit_factor") or 0.0) <= 0.0:
        return True
    return str(model_level or "") in _FREE_MODEL_KEYS


def context_length_of(entry: dict[str, Any]) -> int:
    """Largest advertised context window, else the catalog max_input_tokens."""
    windows = entry.get("context_windows") or []
    if windows:
        return int(max(windows))
    return int(entry["max_input_tokens"])


def public_model_catalog() -> list[dict[str, Any]]:
    """Return detached JSON-safe rows so callers cannot mutate constants."""
    rows: list[dict[str, Any]] = []
    thinking_map = effective_thinking_config()
    for entry in effective_catalog():
        thinking = thinking_map.get(str(entry["key"]))
        row = {
            key: list(value) if isinstance(value, list) else value
            for key, value in entry.items()
        }
        row["context_length"] = context_length_of(entry)
        row["supports_thinking"] = thinking is not None
        row["thinking_efforts"] = list(thinking["efforts"]) if thinking else []
        row["default_thinking_effort"] = (
            thinking["default_effort"] if thinking else None
        )
        rows.append(row)
    return rows
