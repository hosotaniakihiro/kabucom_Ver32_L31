# ============================================================
# File   : core/startup/summary_ai_atr_failopen_patch.py
# Version: V1.0-SUMMARY-AI-ATR-FAILOPEN
# ------------------------------------------------------------
# SUMMARY_AI が AI_OK / approved / pending まで進んだのに、
# entry_controller の atr_1m_filter だけで ENTRY_SKIP になる問題を緩和する。
#
# 背景:
#   サマリー候補は出来高・売買代金・AI gate を通過しているが、
#   1分ATRが0/欠損/直近PUSH不足の場合に ATR_1M_FILTER_NG で全落ちする。
#   これにより OrderId まで到達しない。
#
# 方針:
#   - SUMMARY_AI のみ対象
#   - 元 atr_1m_filter が True ならそのまま通す
#   - False の場合でも、volume/turnover/price が十分なら fail-open
#   - RANGE_5M / direction / fresh quote / order側ガードは残す
#
# 環境変数:
#   SUMMARY_AI_ATR_FAILOPEN_ENABLED=1
#   SUMMARY_AI_ATR_FAILOPEN_MIN_VOLUME=30000
#   SUMMARY_AI_ATR_FAILOPEN_MIN_TURNOVER=10000000
#   SUMMARY_AI_ATR_FAILOPEN_MIN_PRICE=1500
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)
_INSTALLED = False
_ORIG_ATR_FILTER = None


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok"}
    except Exception:
        return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _f(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        x = float(v)
        return float(default) if x != x else x
    except Exception:
        return float(default)


def _s(v: Any) -> str:
    try:
        return str(v or "").strip().upper()
    except Exception:
        return ""


def _get(row: Any, key: str, default: Any = None) -> Any:
    try:
        if isinstance(row, dict):
            return row.get(key, default)
        if hasattr(row, "get"):
            return row.get(key, default)
    except Exception:
        pass
    return default


def _is_summary_ai(row: Any) -> bool:
    src = _s(_get(row, "source"))
    et = _s(_get(row, "entry_type"))
    return src == "SUMMARY" and et == "SUMMARY_AI"


def _price(row: Any) -> float:
    for k in ("current_price", "price", "close_price", "close"):
        v = _f(_get(row, k), 0.0)
        if v > 0:
            return v
    return 0.0


def _can_failopen(row: Any) -> tuple[bool, dict[str, Any]]:
    volume = _f(_get(row, "volume"), 0.0)
    turnover = _f(_get(row, "turnover") or _get(row, "trading_value"), 0.0)
    price = _price(row)
    if turnover <= 0 and volume > 0 and price > 0:
        turnover = volume * price

    min_volume = _env_float("SUMMARY_AI_ATR_FAILOPEN_MIN_VOLUME", 30000.0)
    min_turnover = _env_float("SUMMARY_AI_ATR_FAILOPEN_MIN_TURNOVER", 10000000.0)
    min_price = _env_float("SUMMARY_AI_ATR_FAILOPEN_MIN_PRICE", 1500.0)

    detail = {
        "symbol": _get(row, "symbol"),
        "side": _get(row, "side") or _get(row, "entry_decision"),
        "interval": _get(row, "interval"),
        "price": price,
        "volume": volume,
        "turnover": turnover,
        "atr": _get(row, "atr") or _get(row, "atr_1m"),
        "min_volume": min_volume,
        "min_turnover": min_turnover,
        "min_price": min_price,
    }

    if price < min_price:
        detail["reason"] = "PRICE_LOW"
        return False, detail
    if volume < min_volume:
        detail["reason"] = "VOLUME_LOW"
        return False, detail
    if turnover < min_turnover:
        detail["reason"] = "TURNOVER_LOW"
        return False, detail
    detail["reason"] = "SUMMARY_AI_LIQUID_ENOUGH"
    return True, detail


def install() -> bool:
    global _INSTALLED, _ORIG_ATR_FILTER
    if _INSTALLED:
        return True
    if not _env_bool("SUMMARY_AI_ATR_FAILOPEN_ENABLED", True):
        logger.warning("[SUMMARY AI ATR FAILOPEN] disabled by env")
        return False

    try:
        import trading.handlers.entry_controller as ec
    except Exception as e:
        logger.warning("[SUMMARY AI ATR FAILOPEN] import entry_controller failed err=%s", e, exc_info=False)
        return False

    orig = getattr(ec, "atr_1m_filter", None)
    if not callable(orig):
        logger.warning("[SUMMARY AI ATR FAILOPEN] atr_1m_filter unavailable")
        return False

    _ORIG_ATR_FILTER = orig

    def patched_atr_1m_filter(entry_row: Any = None, *args, **kwargs):
        allow = False
        try:
            allow = bool(_ORIG_ATR_FILTER(entry_row, *args, **kwargs))
        except Exception as e:
            logger.warning("[SUMMARY AI ATR FAILOPEN] original atr failed err=%s", e, exc_info=False)
            allow = False

        if allow:
            return True

        if not _is_summary_ai(entry_row):
            return False

        ok, detail = _can_failopen(entry_row)
        if ok:
            logger.warning("[SUMMARY AI ATR FAILOPEN] allow despite ATR_NG detail=%s", detail)
            return True

        logger.info("[SUMMARY AI ATR FAILOPEN] keep ATR_NG detail=%s", detail)
        return False

    patched_atr_1m_filter._summary_ai_atr_failopen_v1 = True  # type: ignore[attr-defined]
    ec.atr_1m_filter = patched_atr_1m_filter

    _INSTALLED = True
    logger.warning(
        "[SUMMARY AI ATR FAILOPEN] installed min_volume=%s min_turnover=%s min_price=%s",
        _env_float("SUMMARY_AI_ATR_FAILOPEN_MIN_VOLUME", 30000.0),
        _env_float("SUMMARY_AI_ATR_FAILOPEN_MIN_TURNOVER", 10000000.0),
        _env_float("SUMMARY_AI_ATR_FAILOPEN_MIN_PRICE", 1500.0),
    )
    return True

try:
    install()
except Exception as e:
    logger.warning("[SUMMARY AI ATR FAILOPEN] auto install failed err=%s", e, exc_info=False)

__all__ = ["install"]
