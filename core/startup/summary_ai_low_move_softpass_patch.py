# -*- coding: utf-8 -*-
# ============================================================
# File   : core/startup/summary_ai_low_move_softpass_patch.py
# Version: V2-ENFORCED-SUMMARY-AI-LOW-ATR-SOFTPASS
# ------------------------------------------------------------
# Purpose:
#   - 5016/7412 のように SUMMARY AI BUY が score/流動性は強いのに
#     元 atr_1m_filter の ATR ratio だけで発注前に止まるケースを救済する。
#   - 低出来高・横ばい銘柄を通さないため、SUMMARY/SUMMARY_AI かつ
#     score/turnover/price 条件を満たす候補だけ soft-pass する。
#   - 後段 patch が entry_controller filters を再ラップしても、watcher で再適用する。
#   - 方向確認ガードは維持する。
# ============================================================
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

VERSION = "V2-ENFORCED-SUMMARY-AI-LOW-ATR-SOFTPASS"
_INSTALLED = False
_WATCHER_STARTED = False
_ORIG_ATR = None
_ORIG_RANGE = None

_TRUE = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return bool(default)
        return str(raw).strip().lower() in _TRUE
    except Exception:
        return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return float(default)
        return float(str(raw).replace(",", ""))
    except Exception:
        return float(default)


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        s = str(v).strip().replace(",", "")
        if s.lower() in {"nan", "none", "nat", "<na>"}:
            return float(default)
        return float(s)
    except Exception:
        return float(default)


def _row_to_dict(row: Any) -> dict[str, Any]:
    try:
        if isinstance(row, dict):
            d = dict(row)
        elif hasattr(row, "to_dict"):
            x = row.to_dict()
            d = dict(x) if isinstance(x, dict) else {}
        else:
            d = {}
        raw = d.get("_raw")
        if hasattr(raw, "to_dict"):
            try:
                raw = raw.to_dict()
            except Exception:
                raw = None
        if isinstance(raw, dict):
            for k, v in raw.items():
                if k not in d or d.get(k) in (None, ""):
                    d[k] = v
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


def _norm_text(v: Any) -> str:
    try:
        return str(v or "").strip().upper()
    except Exception:
        return ""


def _norm_symbol(v: Any) -> str:
    try:
        s = str(v or "").strip()
        if s.endswith(".0") and s[:-2].isdigit():
            return s[:-2]
        return s
    except Exception:
        return ""


def _turnover(row: dict[str, Any], close: float) -> float:
    val = _safe_float(_first(row, ("turnover", "trading_value", "TradingValue", "Turnover", "売買代金"), 0.0), 0.0)
    if val > 0:
        return val
    vol = _safe_float(_first(row, ("volume", "Volume", "trading_volume", "TradingVolume", "出来高"), 0.0), 0.0)
    if close > 0 and vol > 0:
        return close * vol
    return 0.0


def _side(row: dict[str, Any]) -> str:
    s = _norm_text(_first(row, ("side", "entry_side", "entry_decision", "ai_side"), ""))
    if s in {"BUY", "LONG", "2", "買", "買い"}:
        return "BUY"
    if s in {"SELL", "SHORT", "1", "売", "売り"}:
        return "SELL"
    return s


def _summary_ai_score(row: dict[str, Any], side: str) -> float:
    if side == "BUY":
        keys = ("score_buy", "buy_score", "ai_buy_score", "score", "score_total", "final_score", "display_score", "priority", "confidence")
    elif side == "SELL":
        keys = ("score_sell", "sell_score", "ai_sell_score", "score", "score_total", "final_score", "display_score", "priority", "confidence")
    else:
        keys = ("score", "score_total", "final_score", "display_score", "priority", "confidence")
    return abs(_safe_float(_first(row, keys, 0.0), 0.0))


def _is_summary_ai(row: dict[str, Any]) -> bool:
    src = _norm_text(_first(row, ("source", "entry_source", "pipeline_source", "src"), ""))
    et = _norm_text(_first(row, ("entry_type", "type", "entry_kind", "strategy"), ""))
    reason = _norm_text(_first(row, ("reason", "ai_reason"), ""))
    model = _norm_text(_first(row, ("model", "model_used"), ""))
    if "SUMMARY" in src or "SUMMARY" in et or "SUMMARY" in reason:
        return True
    if et in {"SUMMARY_AI", "AI_SUMMARY"}:
        return True
    if src in {"SUMMARY", "SUMMARY_AI", "AI", "PUSH"} and ("MTF" in model or "SUMMARY" in et):
        return True
    return False


def _summary_ai_low_move_softpass_ok(entry_row: Any, *, label: str) -> bool:
    if not _env_bool("SUMMARY_AI_LOW_MOVE_SOFTPASS", True):
        return False
    row = _row_to_dict(entry_row)
    if not row or not _is_summary_ai(row):
        return False

    symbol = _norm_symbol(_first(row, ("symbol", "code", "stock_code"), ""))
    side = _side(row)
    close = _safe_float(_first(row, ("close_price", "close", "price", "current_price"), 0.0), 0.0)
    score = _summary_ai_score(row, side)
    volume = _safe_float(_first(row, ("volume", "Volume", "trading_volume", "TradingVolume", "出来高"), 0.0), 0.0)
    turnover = _turnover(row, close)
    atr = _safe_float(_first(row, ("atr", "atr_1m", "atr_3m", "atr_5m"), 0.0), 0.0)
    atr_ratio = atr / close if close > 0 else 0.0
    slope_vals = [_safe_float(row.get(k), 0.0) for k in ("slope_atr_scaled", "slope", "score_slope", "disp_slope", "_slope") if k in row]
    slope = max([abs(x) for x in slope_vals], default=0.0)

    min_score = _env_float("SUMMARY_AI_LOW_MOVE_SOFTPASS_MIN_SCORE", 4.0)
    min_turnover = _env_float("SUMMARY_AI_LOW_MOVE_SOFTPASS_MIN_TURNOVER", 10000000.0)
    min_volume = _env_float("SUMMARY_AI_LOW_MOVE_SOFTPASS_MIN_VOLUME", 30000.0)
    min_price = _env_float("SUMMARY_AI_LOW_MOVE_SOFTPASS_MIN_PRICE", 200.0)
    max_price = _env_float("SUMMARY_AI_LOW_MOVE_SOFTPASS_MAX_PRICE", 7000.0)
    allow_volume_missing = _env_bool("SUMMARY_AI_LOW_MOVE_SOFTPASS_ALLOW_VOLUME_MISSING", True)

    volume_ok = volume >= min_volume or (allow_volume_missing and turnover >= min_turnover)
    ok = close >= min_price and close <= max_price and score >= min_score and turnover >= min_turnover and volume_ok
    if ok:
        logger.warning(
            "[LOW MOVE GUARD] SUMMARY_AI_LOW_MOVE_SOFTPASS_ALLOW label=%s symbol=%s side=%s close=%.1f score=%.3f volume=%.0f turnover=%.0f atr=%.4f atr_ratio=%.6f slope=%.6f version=%s",
            label,
            symbol,
            side,
            close,
            score,
            volume,
            turnover,
            atr,
            atr_ratio,
            slope,
            VERSION,
        )
        return True

    logger.warning(
        "[LOW MOVE GUARD] SUMMARY_AI_LOW_MOVE_SOFTPASS_NG label=%s symbol=%s side=%s close=%.1f score=%.3f/%s volume=%.0f/%s turnover=%.0f/%s atr_ratio=%.6f source=%s entry_type=%s version=%s",
        label,
        symbol,
        side,
        close,
        score,
        min_score,
        volume,
        min_volume,
        turnover,
        min_turnover,
        atr_ratio,
        _first(row, ("source", "entry_source", "pipeline_source"), ""),
        _first(row, ("entry_type", "type", "entry_kind"), ""),
        VERSION,
    )
    return False


def _direction_ok(entry_row: Any) -> bool:
    try:
        from core.startup import low_movement_entry_guard_patch as lmg
        fn = getattr(lmg, "_call_entry_direction_confirm", None)
        if callable(fn):
            return bool(fn(entry_row))
    except Exception:
        logger.debug("[LOW MOVE GUARD] SUMMARY_AI softpass direction check skipped", exc_info=True)
    return True


def _wrap_filter(old_func: Any, label: str):
    def _patched(entry_row: Any = None, *args: Any, **kwargs: Any):
        try:
            allow = True
            if callable(old_func):
                allow = old_func(entry_row, *args, **kwargs)
            if isinstance(allow, tuple):
                return allow
            if bool(allow):
                return allow
            if _summary_ai_low_move_softpass_ok(entry_row, label=label) and _direction_ok(entry_row):
                return True
            return False
        except RecursionError:
            logger.error("[LOW MOVE GUARD] SUMMARY_AI low move %s wrapper recursion; fail-safe NG", label, exc_info=False)
            return False
        except Exception as e:
            logger.warning("[LOW MOVE GUARD] SUMMARY_AI low move %s wrapper failed: %s", label, e, exc_info=False)
            return False

    _patched._summary_ai_low_move_softpass_v2 = True  # type: ignore[attr-defined]
    _patched._summary_ai_low_move_softpass_v1 = True  # type: ignore[attr-defined]
    _patched._original = getattr(old_func, "_original", old_func)  # type: ignore[attr-defined]
    _patched._wrapped_target = old_func  # type: ignore[attr-defined]
    return _patched


def _apply_wrappers(*, reason: str = "install") -> list[str]:
    global _ORIG_ATR, _ORIG_RANGE
    changed: list[str] = []
    try:
        import trading.handlers.entry_controller as ec
        old_atr = getattr(ec, "atr_1m_filter", None)
        old_range = getattr(ec, "range_5m_filter", None)
        if callable(old_atr) and not getattr(old_atr, "_summary_ai_low_move_softpass_v2", False):
            _ORIG_ATR = old_atr
            ec.atr_1m_filter = _wrap_filter(old_atr, "atr_1m")
            changed.append("atr_1m_filter")
        if callable(old_range) and not getattr(old_range, "_summary_ai_low_move_softpass_v2", False):
            _ORIG_RANGE = old_range
            ec.range_5m_filter = _wrap_filter(old_range, "range_5m")
            changed.append("range_5m_filter")
        if changed:
            logger.warning("[LOW MOVE GUARD] SUMMARY_AI low move softpass applied reason=%s changed=%s version=%s", reason, changed, VERSION)
    except Exception:
        logger.exception("[LOW MOVE GUARD] SUMMARY_AI low move softpass apply failed reason=%s", reason)
    return changed


def _watcher_loop() -> None:
    try:
        loops = int(_env_float("SUMMARY_AI_LOW_MOVE_SOFTPASS_WATCH_LOOPS", 90))
        sleep_sec = _env_float("SUMMARY_AI_LOW_MOVE_SOFTPASS_WATCH_SLEEP", 1.0)
        for i in range(max(1, loops)):
            time.sleep(max(0.2, sleep_sec))
            _apply_wrappers(reason=f"watcher:{i + 1}")
        logger.warning("[LOW MOVE GUARD] SUMMARY_AI low move softpass watcher done loops=%s version=%s", loops, VERSION)
    except Exception:
        logger.exception("[LOW MOVE GUARD] SUMMARY_AI low move softpass watcher failed")


def _start_watcher() -> None:
    global _WATCHER_STARTED
    if _WATCHER_STARTED:
        return
    if not _env_bool("SUMMARY_AI_LOW_MOVE_SOFTPASS_WATCHER", True):
        return
    _WATCHER_STARTED = True
    th = threading.Thread(target=_watcher_loop, name="summary-ai-low-move-softpass-watcher", daemon=True)
    th.start()
    logger.warning("[LOW MOVE GUARD] SUMMARY_AI low move softpass watcher started version=%s", VERSION)


def install() -> bool:
    global _INSTALLED
    os.environ.setdefault("SUMMARY_AI_LOW_MOVE_SOFTPASS", "1")
    os.environ.setdefault("SUMMARY_AI_LOW_MOVE_SOFTPASS_MIN_SCORE", "4.0")
    os.environ.setdefault("SUMMARY_AI_LOW_MOVE_SOFTPASS_MIN_TURNOVER", "10000000")
    os.environ.setdefault("SUMMARY_AI_LOW_MOVE_SOFTPASS_MIN_VOLUME", "30000")
    os.environ.setdefault("SUMMARY_AI_LOW_MOVE_SOFTPASS_ALLOW_VOLUME_MISSING", "1")
    os.environ.setdefault("SUMMARY_AI_LOW_MOVE_SOFTPASS_MIN_PRICE", "200")
    os.environ.setdefault("SUMMARY_AI_LOW_MOVE_SOFTPASS_MAX_PRICE", "7000")
    os.environ.setdefault("SUMMARY_AI_LOW_MOVE_SOFTPASS_WATCHER", "1")
    os.environ.setdefault("SUMMARY_AI_LOW_MOVE_SOFTPASS_WATCH_LOOPS", "90")
    os.environ.setdefault("SUMMARY_AI_LOW_MOVE_SOFTPASS_WATCH_SLEEP", "1.0")

    try:
        try:
            from core.startup import low_movement_entry_guard_patch as lmg
            lmg.install()
        except Exception:
            logger.debug("[LOW MOVE GUARD] base low movement install skipped/failed", exc_info=True)

        changed = _apply_wrappers(reason="install")
        _INSTALLED = True
        _start_watcher()
        logger.warning(
            "[LOW MOVE GUARD] SUMMARY_AI low move softpass installed=%s changed=%s min_score=%s min_turnover=%s min_volume=%s watcher=%s version=%s",
            True,
            changed,
            os.getenv("SUMMARY_AI_LOW_MOVE_SOFTPASS_MIN_SCORE"),
            os.getenv("SUMMARY_AI_LOW_MOVE_SOFTPASS_MIN_TURNOVER"),
            os.getenv("SUMMARY_AI_LOW_MOVE_SOFTPASS_MIN_VOLUME"),
            os.getenv("SUMMARY_AI_LOW_MOVE_SOFTPASS_WATCHER"),
            VERSION,
        )
        return True
    except Exception:
        logger.exception("[LOW MOVE GUARD] SUMMARY_AI low move softpass install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[LOW MOVE GUARD] SUMMARY_AI low move softpass auto install failed")


__all__ = ["VERSION", "install"]
