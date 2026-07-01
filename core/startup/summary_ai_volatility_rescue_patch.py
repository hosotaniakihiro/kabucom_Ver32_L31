# ============================================================
# File   : core/startup/summary_ai_volatility_rescue_patch.py
# Version: V1-SUMMARY-AI-STRONG-VOL-RESCUE
# ------------------------------------------------------------
# SUMMARY_AIの強い候補が、entry_controller内の ATR_1M_FILTER_NG /
# RANGE_5M_FILTER_NG だけで全落ちする問題を救済する。
#
# 低ボラ銘柄の除外は維持するため、以下を満たすSUMMARY_AIだけ通す。
#   - score_buy/score_sell/score/final_score の絶対値 >= 3.0
#   - turnover >= 1,000万円
#   - volume >= 3,000 または turnover >= 1,000万円
#   - abs(slope/slope_atr_scaled/score_slope) >= 0.001
#   - 価格 200〜7000円
#
# low_movement_entry_guard_patch 等が後から entry_controller の filter を
# 再wrapしても、watcher が再適用する。
# ============================================================

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V1-SUMMARY-AI-STRONG-VOL-RESCUE"
_INSTALLED = False
_WATCHER_STARTED = False


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _row_to_dict(v: Any) -> dict[str, Any]:
    try:
        if isinstance(v, dict):
            d = dict(v)
        elif hasattr(v, "to_dict"):
            tmp = v.to_dict()
            d = dict(tmp) if isinstance(tmp, dict) else {}
        else:
            d = {}
        raw = d.get("_raw")
        if hasattr(raw, "to_dict"):
            try:
                raw = raw.to_dict()
            except Exception:
                raw = None
        if isinstance(raw, dict):
            for k, val in raw.items():
                if k not in d or d.get(k) in (None, ""):
                    d[k] = val
        return d
    except Exception:
        return {}


def _first(row: dict[str, Any], keys: tuple[str, ...], default: Any = None) -> Any:
    for k in keys:
        try:
            v = row.get(k)
            if v is not None and str(v).strip() != "":
                return v
        except Exception:
            pass
    return default


def _norm(v: Any) -> str:
    try:
        return str(v or "").strip().upper()
    except Exception:
        return ""


def _is_summary_ai(row: dict[str, Any]) -> bool:
    src = _norm(_first(row, ("source", "entry_source", "pipeline_source", "src"), ""))
    et = _norm(_first(row, ("entry_type", "type", "strategy"), ""))
    reason = _norm(_first(row, ("reason", "ai_reason", "entry_reason"), ""))
    model = _norm(_first(row, ("model", "model_used"), ""))
    if et in {"SUMMARY_AI", "AI_SUMMARY"}:
        return True
    if src in {"SUMMARY", "SUMMARY_AI", "PUSH"} and ("SUMMARY" in reason or "MTF" in model):
        return True
    return False


def _side(row: dict[str, Any]) -> str:
    s = _norm(_first(row, ("side", "entry_decision", "ai_side", "dominant_side"), ""))
    return s if s in {"BUY", "SELL"} else ""


def _score(row: dict[str, Any]) -> float:
    side = _side(row)
    if side == "BUY":
        keys = ("score_buy", "buy_score", "ai_buy_score", "score", "final_score", "display_score", "score_total")
    elif side == "SELL":
        keys = ("score_sell", "sell_score", "ai_sell_score", "score", "final_score", "display_score", "score_total")
    else:
        keys = ("score", "final_score", "display_score", "score_total", "score_buy", "score_sell")
    return abs(_safe_float(_first(row, keys, 0.0), 0.0))


def _strong_summary_ai_ok(entry_row: Any, label: str) -> bool:
    if not _env_bool("SUMMARY_AI_VOL_RESCUE_ENABLED", True):
        return False
    row = _row_to_dict(entry_row)
    if not _is_summary_ai(row):
        return False

    symbol = str(_first(row, ("symbol", "code", "stock_code", "銘柄コード"), "")).strip()
    price = _safe_float(_first(row, ("close_price", "close", "price", "current_price"), 0.0), 0.0)
    volume = _safe_float(_first(row, ("volume", "Volume", "trading_volume", "TradingVolume", "出来高"), 0.0), 0.0)
    turnover = _safe_float(_first(row, ("turnover", "trading_value", "TradingValue", "売買代金", "amount"), 0.0), 0.0)
    if turnover <= 0 and price > 0 and volume > 0:
        turnover = price * volume
    score = _score(row)
    slopes = [
        abs(_safe_float(row.get(k), 0.0))
        for k in ("slope_atr_scaled", "slope", "score_slope", "disp_slope")
        if k in row
    ]
    slope_abs = max(slopes, default=0.0)

    min_score = _env_float("SUMMARY_AI_VOL_RESCUE_MIN_SCORE", 3.0)
    min_turnover = _env_float("SUMMARY_AI_VOL_RESCUE_MIN_TURNOVER", 10000000.0)
    min_volume = _env_float("SUMMARY_AI_VOL_RESCUE_MIN_VOLUME", 3000.0)
    min_slope = _env_float("SUMMARY_AI_VOL_RESCUE_MIN_ABS_SLOPE", 0.001)
    min_price = _env_float("SUMMARY_AI_VOL_RESCUE_MIN_PRICE", 200.0)
    max_price = _env_float("SUMMARY_AI_VOL_RESCUE_MAX_PRICE", 7000.0)

    ok = (
        price >= min_price
        and price <= max_price
        and score >= min_score
        and turnover >= min_turnover
        and (volume >= min_volume or turnover >= min_turnover)
        and slope_abs >= min_slope
    )
    if ok:
        logger.warning(
            "[SUMMARY AI VOL RESCUE] allow original_%s_NG symbol=%s side=%s price=%.1f score=%.3f turnover=%.0f volume=%.0f slope_abs=%.6f min_score=%.2f min_turnover=%.0f min_slope=%.6f version=%s",
            label,
            symbol,
            _side(row),
            price,
            score,
            turnover,
            volume,
            slope_abs,
            min_score,
            min_turnover,
            min_slope,
            VERSION,
        )
        return True

    logger.info(
        "[SUMMARY AI VOL RESCUE] keep NG symbol=%s label=%s price=%.1f score=%.3f/%s turnover=%.0f/%s volume=%.0f/%s slope_abs=%.6f/%s",
        symbol,
        label,
        price,
        score,
        min_score,
        turnover,
        min_turnover,
        volume,
        min_volume,
        slope_abs,
        min_slope,
    )
    return False


def _wrap_filter(fn: Any, label: str):
    if not callable(fn):
        return fn
    if getattr(fn, f"_summary_ai_vol_rescue_{label}_v1", False):
        return fn

    def _wrapped(entry_row: Any = None, *args: Any, **kwargs: Any):
        ret = fn(entry_row, *args, **kwargs)
        if isinstance(ret, tuple):
            return ret
        if bool(ret):
            return ret
        if _strong_summary_ai_ok(entry_row, label):
            return True
        return ret

    setattr(_wrapped, f"_summary_ai_vol_rescue_{label}_v1", True)
    _wrapped._original = fn  # type: ignore[attr-defined]
    return _wrapped


def _apply_once(reason: str = "install") -> bool:
    try:
        import trading.handlers.entry_controller as ec
        changed = False
        old_atr = getattr(ec, "atr_1m_filter", None)
        new_atr = _wrap_filter(old_atr, "ATR")
        if new_atr is not old_atr:
            ec.atr_1m_filter = new_atr
            changed = True
        old_range = getattr(ec, "range_5m_filter", None)
        new_range = _wrap_filter(old_range, "RANGE")
        if new_range is not old_range:
            ec.range_5m_filter = new_range
            changed = True
        if changed:
            logger.warning("[SUMMARY AI VOL RESCUE] patched entry_controller filters reason=%s version=%s", reason, VERSION)
        return True
    except Exception:
        logger.exception("[SUMMARY AI VOL RESCUE] apply failed reason=%s", reason)
        return False


def _watcher() -> None:
    loops = int(max(1, _env_float("SUMMARY_AI_VOL_RESCUE_WATCH_LOOPS", 80)))
    interval = max(0.2, _env_float("SUMMARY_AI_VOL_RESCUE_WATCH_INTERVAL_SEC", 0.5))
    for i in range(loops):
        try:
            _apply_once(reason=f"watcher:{i}")
        except Exception:
            logger.debug("[SUMMARY AI VOL RESCUE] watcher apply failed", exc_info=True)
        time.sleep(interval)


def install() -> bool:
    global _INSTALLED, _WATCHER_STARTED
    os.environ.setdefault("SUMMARY_AI_VOL_RESCUE_ENABLED", "1")
    os.environ.setdefault("SUMMARY_AI_VOL_RESCUE_MIN_SCORE", "3.0")
    os.environ.setdefault("SUMMARY_AI_VOL_RESCUE_MIN_TURNOVER", "10000000")
    os.environ.setdefault("SUMMARY_AI_VOL_RESCUE_MIN_VOLUME", "3000")
    os.environ.setdefault("SUMMARY_AI_VOL_RESCUE_MIN_ABS_SLOPE", "0.001")
    ok = _apply_once("install")
    _INSTALLED = bool(ok)
    if ok and not _WATCHER_STARTED:
        _WATCHER_STARTED = True
        threading.Thread(target=_watcher, name="summary-ai-vol-rescue-watch", daemon=True).start()
    logger.warning("[SUMMARY AI VOL RESCUE] installed=%s watcher=%s version=%s", ok, _WATCHER_STARTED, VERSION)
    return bool(ok)


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI VOL RESCUE] auto install failed")


__all__ = ["install"]
