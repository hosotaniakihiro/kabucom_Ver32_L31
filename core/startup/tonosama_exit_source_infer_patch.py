# -*- coding: utf-8 -*-
"""
TONOSAMA exit source inference patch.

Why
---
trading.handlers.exit_handler already has a dedicated TONOSAMA exit path, but
run_exit_pipeline only uses it when a position exposes:

    entry_source == "ranking" and entry_mode == "TONOSAMA"

The current Position ORM model does not have entry_source / entry_mode columns,
so DB positions can silently fall through to the slower NORMAL exit even when
the entry reason was TONOSAMA / 殿様イナゴ.

This patch keeps the existing exit pipeline intact and only teaches its _get()
helper to infer TONOSAMA metadata from reason/source/history text.
"""
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V1-TONOSAMA-EXIT-SOURCE-INFER"
_INSTALLED = False
_ORIGINAL_GET = None

_TONOSAMA_HINTS = (
    "tonosama",
    "殿様",
    "イナゴ",
    "出来高急増",
    "volume_surge",
    "volume surge",
    "tonosama_entry",
    "tonosama entry",
)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _text(v: Any) -> str:
    try:
        if v is None:
            return ""
        return str(v)
    except Exception:
        return ""


def _raw_get(obj: Any, name: str, default=None):
    try:
        if isinstance(obj, dict):
            return obj.get(name, default)
        return getattr(obj, name, default)
    except Exception:
        return default


def _iter_history_text(pos: Any) -> list[str]:
    texts: list[str] = []
    try:
        histories = _raw_get(pos, "histories", None)
        if histories:
            for h in list(histories)[-5:]:
                for name in ("reason", "action", "side", "symbolname"):
                    texts.append(_text(_raw_get(h, name, "")))
    except Exception:
        pass
    return texts


def _looks_tonosama_position(pos: Any) -> bool:
    texts: list[str] = []
    for name in (
        "entry_source",
        "source",
        "entry_mode",
        "entry_type",
        "trigger_type",
        "reason",
        "entry_reason",
        "entry_comment",
        "source_reason",
        "mode",
    ):
        texts.append(_text(_raw_get(pos, name, "")))
    texts.extend(_iter_history_text(pos))

    joined = " ".join(t for t in texts if t).lower()
    if not joined:
        return False
    return any(h.lower() in joined for h in _TONOSAMA_HINTS)


def _patched_get(obj: Any, name: str, default=None):
    # First keep the original behavior 100% intact.
    try:
        if callable(_ORIGINAL_GET):
            v = _ORIGINAL_GET(obj, name, default)
        else:
            v = _raw_get(obj, name, default)
    except Exception:
        v = default

    if v not in (None, ""):
        return v

    # Only synthesize TONOSAMA metadata for fields used by run_exit_pipeline.
    try:
        lname = str(name or "").lower()
        if lname in {"entry_source", "source"} and _looks_tonosama_position(obj):
            return "ranking"
        if lname in {"entry_mode", "entry_type", "trigger_type"} and _looks_tonosama_position(obj):
            return "TONOSAMA"
        if lname == "hold_limit_sec" and _looks_tonosama_position(obj):
            return _env_float("TONOSAMA_EXIT_MAX_HOLD_SEC", 60.0)
    except Exception:
        pass

    return default


def install() -> bool:
    global _INSTALLED, _ORIGINAL_GET
    if _INSTALLED:
        return True
    if os.environ.get("DISABLE_TONOSAMA_EXIT_SOURCE_INFER_PATCH", "").strip() == "1":
        logger.warning("[TONOSAMA EXIT INFER] disabled by env")
        return False

    try:
        import trading.handlers.exit_handler as eh

        current = getattr(eh, "_get", None)
        if current is _patched_get:
            _INSTALLED = True
            return True
        _ORIGINAL_GET = current
        eh._get = _patched_get

        # Scalp-oriented TONOSAMA defaults. Existing env values can override.
        eh.TONO_STOP_LOSS = -abs(_env_float("TONOSAMA_EXIT_STOP_LOSS_PCT", 0.0030))       # -0.30%
        eh.TONO_TAKE_PROFIT = abs(_env_float("TONOSAMA_EXIT_TAKE_PROFIT_PCT", 0.0020))   # +0.20%
        eh.TONO_TRAIL_GAP = abs(_env_float("TONOSAMA_EXIT_TRAIL_GAP_PCT", 0.0020))       # high/low from best -0.20%
        eh.TONO_MAX_HOLD_SEC = int(_env_float("TONOSAMA_EXIT_MAX_HOLD_SEC", 60.0))

        _INSTALLED = True
        logger.warning(
            "[TONOSAMA EXIT INFER] installed version=%s stop=%.4f take=%.4f trail=%.4f max_hold=%s",
            VERSION,
            eh.TONO_STOP_LOSS,
            eh.TONO_TAKE_PROFIT,
            eh.TONO_TRAIL_GAP,
            eh.TONO_MAX_HOLD_SEC,
        )
        return True
    except Exception:
        logger.exception("[TONOSAMA EXIT INFER] install failed")
        return False


__all__ = ["VERSION", "install"]
