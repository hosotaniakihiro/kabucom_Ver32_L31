# ============================================================
# File   : core/startup/entry_immediate_movement_guard_patch.py
# Version: V1.2-SUMMARY-RANGE-RESCUE
# ------------------------------------------------------------
# 目的:
#   エントリー後に株価が動かない銘柄を減らす。
#
# V1.2:
#   - SUMMARY AI は open==close / slope≈0 になりやすく、出来高・値幅が十分でも
#     immediate_movement_weak で全落ちしていた。
#   - SUMMARY/SUMMARY_AI の score>=4 かつ range_pct が大きい候補を救済する。
#   - close_pos_dir をログに必ず出し、なぜ落ちたかを見える化する。
# ============================================================
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)
_INSTALLED = False
_ORIG_BUILD = None

_TRUE = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
_FALSE = {"0", "false", "no", "n", "off", "ng", "disable", "disabled"}


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        s = str(v).strip().lower()
        if s in _TRUE:
            return True
        if s in _FALSE:
            return False
    except Exception:
        pass
    return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return float(default)
        s = str(v).strip()
        if s == "" or s.lower() in {"none", "nan", "nat", "-"}:
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _safe_str(v: Any, default: str = "") -> str:
    try:
        if v is None:
            return default
        return str(v).strip()
    except Exception:
        return default


def _first(row: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for k in keys:
        if k in row and row.get(k) is not None:
            return row.get(k)
    return default


def _row_from_item(item: dict[str, Any]) -> dict[str, Any]:
    try:
        row = item.get("entry_row")
        if isinstance(row, dict):
            return row
        ent = item.get("entry")
        if isinstance(ent, dict):
            return ent
    except Exception:
        pass
    return {}


def _close_position_pct(side: str, high_p: float, low_p: float, close_p: float) -> tuple[float, float]:
    """return raw close position 0-100 and side-direction score 0-100."""
    try:
        if high_p > low_p and close_p > 0:
            raw = (close_p - low_p) / max(1e-9, high_p - low_p) * 100.0
            raw = max(0.0, min(100.0, raw))
            side_score = (100.0 - raw) if str(side).upper() == "SELL" else raw
            return raw, side_score
    except Exception:
        pass
    return 50.0, 50.0


def _calc_diag(item: dict[str, Any]) -> dict[str, Any]:
    row = _row_from_item(item)
    side = _safe_str(item.get("side") or row.get("side") or row.get("entry_decision")).upper()
    symbol = _safe_str(item.get("symbol") or row.get("symbol"))
    source = _safe_str(row.get("source") or item.get("source") or item.get("entry_type")).upper()
    entry_type = _safe_str(item.get("entry_type") or row.get("entry_type")).upper()

    open_p = _safe_float(_first(row, "open", "open_price"), 0.0)
    high_p = _safe_float(_first(row, "high", "high_price"), 0.0)
    low_p = _safe_float(_first(row, "low", "low_price"), 0.0)
    close_p = _safe_float(_first(row, "close", "close_price", "price", "current_price"), 0.0)

    base = close_p if close_p > 0 else max(open_p, high_p, low_p, 1.0)
    range_pct = ((high_p - low_p) / base * 100.0) if high_p > 0 and low_p > 0 and high_p >= low_p and base > 0 else 0.0
    body_pct = ((close_p - open_p) / open_p * 100.0) if open_p > 0 and close_p > 0 else 0.0
    close_pos, close_pos_dir = _close_position_pct(side, high_p, low_p, close_p)

    slope = _safe_float(_first(row, "slope", "disp_slope"), 0.0)
    slope_atr = _safe_float(_first(row, "slope_atr_scaled", "disp_slope_atr_scaled"), 0.0)
    score_slope = _safe_float(_first(row, "score_slope"), 0.0)
    macd = _safe_float(_first(row, "macd"), 0.0)
    signal = _safe_float(_first(row, "signal"), 0.0)
    rsi = _safe_float(_first(row, "rsi"), 50.0)
    volume = _safe_float(_first(row, "volume", "latest_volume", "trading_volume"), 0.0)
    tick_count = _safe_float(_first(row, "tick_count", "ticks"), 0.0)
    mtf = _safe_float(_first(row, "mtf", "score_mtf", "mtf_score"), 0.0)
    score = abs(_safe_float(_first(row, "score", "score_total", "final_score", "display_score"), 0.0))
    priority = _safe_float(item.get("priority_score"), 0.0)

    if side == "SELL":
        body_dir = -body_pct
        slope_dir = -slope
        slope_atr_dir = -slope_atr
        score_slope_dir = -score_slope
        macd_dir = signal - macd
        rsi_dir_ok = rsi <= _env_float("ENTRY_MOVE_SELL_MAX_RSI", 62.0)
    else:
        body_dir = body_pct
        slope_dir = slope
        slope_atr_dir = slope_atr
        score_slope_dir = score_slope
        macd_dir = macd - signal
        rsi_dir_ok = rsi >= _env_float("ENTRY_MOVE_BUY_MIN_RSI", 38.0)

    return {
        "symbol": symbol,
        "side": side,
        "source": source,
        "entry_type": entry_type,
        "open": open_p,
        "high": high_p,
        "low": low_p,
        "close": close_p,
        "range_pct": range_pct,
        "body_pct": body_pct,
        "body_dir": body_dir,
        "close_pos": close_pos,
        "close_pos_dir": close_pos_dir,
        "slope": slope,
        "slope_dir": slope_dir,
        "slope_atr_scaled": slope_atr,
        "slope_atr_dir": slope_atr_dir,
        "score_slope": score_slope,
        "score_slope_dir": score_slope_dir,
        "macd": macd,
        "signal": signal,
        "macd_dir": macd_dir,
        "rsi": rsi,
        "rsi_dir_ok": rsi_dir_ok,
        "volume": volume,
        "tick_count": tick_count,
        "mtf": mtf,
        "score": score,
        "priority": priority,
    }


def _is_summary_source(source: str, entry_type: str) -> bool:
    return source in {"SUMMARY", "SUMMARY_AI", "PUSH"} or entry_type == "SUMMARY_AI"


def _movement_ok(item: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    d = _calc_diag(item)
    side = d.get("side")
    source = _safe_str(d.get("source")).upper()
    entry_type = _safe_str(d.get("entry_type")).upper()

    if side not in {"BUY", "SELL"}:
        return False, {**d, "ng": "side_invalid"}

    is_fast_source = source in {"RANKING", "RANKING_5S", "TONOSAMA", "EARLY_SCALP"}
    is_summary = _is_summary_source(source, entry_type)

    min_range = _env_float("ENTRY_MOVE_MIN_RANGE_PCT_FAST", 0.20) if is_fast_source else _env_float("ENTRY_MOVE_MIN_RANGE_PCT_SUMMARY", 0.28)
    min_body = _env_float("ENTRY_MOVE_MIN_BODY_DIR_PCT_FAST", 0.03) if is_fast_source else _env_float("ENTRY_MOVE_MIN_BODY_DIR_PCT_SUMMARY", 0.05)
    min_slope_atr = _env_float("ENTRY_MOVE_MIN_SLOPE_ATR_FAST", 0.0004) if is_fast_source else _env_float("ENTRY_MOVE_MIN_SLOPE_ATR_SUMMARY", 0.0006)
    min_score_slope = _env_float("ENTRY_MOVE_MIN_SCORE_SLOPE", 0.04)
    min_macd_dir = _env_float("ENTRY_MOVE_MIN_MACD_DIR", -0.05)
    min_close_pos_dir = _env_float("ENTRY_MOVE_MIN_CLOSE_POS_DIR_FAST", 60.0) if is_fast_source else _env_float("ENTRY_MOVE_MIN_CLOSE_POS_DIR_SUMMARY", 58.0)
    strong_mtf = _env_float("ENTRY_MOVE_STRONG_MTF_RELAX", 6.0)
    strong_score = _env_float("ENTRY_MOVE_STRONG_SCORE_RELAX", 4.0 if is_summary else 5.5)

    range_ok = float(d["range_pct"]) >= min_range
    body_ok = float(d["body_dir"]) >= min_body
    close_pos_ok = float(d["close_pos_dir"]) >= min_close_pos_dir
    slope_ok = float(d["slope_atr_dir"]) >= min_slope_atr or float(d["score_slope_dir"]) >= min_score_slope
    macd_ok = float(d["macd_dir"]) >= min_macd_dir
    rsi_ok = bool(d["rsi_dir_ok"])

    strong_context = float(d["mtf"]) >= strong_mtf or float(d["score"]) >= strong_score or float(d["priority"]) >= strong_score

    # 通常: 値幅があり、方向が body / close_pos / slope のどれかで確認できる。
    if range_ok and rsi_ok and macd_ok and (body_ok or close_pos_ok or slope_ok):
        return True, {**d, "ok": "range_body_closepos_or_slope"}

    # SUMMARY AI: open==close/slope≈0 でも、スコア4以上かつ値幅・出来高が十分なら救済する。
    if is_summary and _env_bool("ENTRY_MOVE_SUMMARY_RANGE_RESCUE_ENABLED", True):
        summary_score_min = _env_float("ENTRY_MOVE_SUMMARY_RANGE_RESCUE_MIN_SCORE", 4.0)
        summary_range_min = _env_float("ENTRY_MOVE_SUMMARY_RANGE_RESCUE_MIN_RANGE_PCT", 1.5)
        summary_volume_min = _env_float("ENTRY_MOVE_SUMMARY_RANGE_RESCUE_MIN_VOLUME", 300000.0)
        summary_close_pos_min = _env_float("ENTRY_MOVE_SUMMARY_RANGE_RESCUE_MIN_CLOSE_POS_DIR", 35.0)
        summary_macd_min = _env_float("ENTRY_MOVE_SUMMARY_RANGE_RESCUE_MIN_MACD_DIR", -0.20)
        if (
            float(d["score"]) >= summary_score_min
            and float(d["range_pct"]) >= summary_range_min
            and float(d["volume"]) >= summary_volume_min
            and float(d["close_pos_dir"]) >= summary_close_pos_min
            and float(d["macd_dir"]) >= summary_macd_min
            and rsi_ok
        ):
            return True, {
                **d,
                "ok": "summary_range_rescue",
                "summary_score_min": summary_score_min,
                "summary_range_min": summary_range_min,
                "summary_volume_min": summary_volume_min,
                "summary_close_pos_min": summary_close_pos_min,
                "summary_macd_min": summary_macd_min,
            }

    # 強い文脈: close_posまたはslopeがあれば通す。
    if strong_context and range_ok and macd_ok and (close_pos_ok or slope_ok):
        return True, {**d, "ok": "strong_context_range_closepos_or_slope"}

    return False, {
        **d,
        "ng": "immediate_movement_weak",
        "range_ok": range_ok,
        "body_ok": body_ok,
        "close_pos_ok": close_pos_ok,
        "slope_ok": slope_ok,
        "macd_ok": macd_ok,
        "rsi_ok": rsi_ok,
        "strong_context": strong_context,
        "min_range": min_range,
        "min_body": min_body,
        "min_close_pos_dir": min_close_pos_dir,
        "min_slope_atr": min_slope_atr,
        "min_score_slope": min_score_slope,
        "min_macd_dir": min_macd_dir,
    }


def _patched_build_scored_candidates(*args, **kwargs):
    candidates = _ORIG_BUILD(*args, **kwargs)  # type: ignore[misc]
    if not _env_bool("ENTRY_IMMEDIATE_MOVEMENT_GUARD_ENABLED", True):
        return candidates
    try:
        src = _safe_str(kwargs.get("pipeline_source") or "").upper()
        if src and src not in {x.strip().upper() for x in os.getenv("ENTRY_MOVE_GUARD_SOURCES", "SUMMARY,RANKING,TONOSAMA,EARLY_SCALP").split(",") if x.strip()}:
            return candidates

        kept: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        passed_diag: list[dict[str, Any]] = []
        for item in list(candidates or []):
            if not isinstance(item, dict):
                kept.append(item)
                continue
            ok, diag = _movement_ok(item)
            view = {
                "symbol": diag.get("symbol"),
                "side": diag.get("side"),
                "source": diag.get("source"),
                "entry_type": diag.get("entry_type"),
                "range_pct": round(float(diag.get("range_pct") or 0.0), 4),
                "body_dir": round(float(diag.get("body_dir") or 0.0), 4),
                "close_pos_dir": round(float(diag.get("close_pos_dir") or 0.0), 2),
                "slope_atr_dir": round(float(diag.get("slope_atr_dir") or 0.0), 6),
                "score_slope_dir": round(float(diag.get("score_slope_dir") or 0.0), 4),
                "macd_dir": round(float(diag.get("macd_dir") or 0.0), 4),
                "rsi": round(float(diag.get("rsi") or 0.0), 2),
                "volume": round(float(diag.get("volume") or 0.0), 0),
                "mtf": round(float(diag.get("mtf") or 0.0), 3),
                "score": round(float(diag.get("score") or 0.0), 3),
            }
            if ok:
                kept.append(item)
                passed_diag.append({**view, "ok": diag.get("ok")})
            else:
                skipped.append({**view, "ng": diag.get("ng")})
        if skipped or passed_diag:
            logger.warning(
                "[ENTRY IMMEDIATE MOVE GUARD] filtered before=%s after=%s passed=%s skipped=%s",
                len(list(candidates or [])),
                len(kept),
                passed_diag[:20],
                skipped[:50],
            )
        return kept
    except Exception:
        logger.exception("[ENTRY IMMEDIATE MOVE GUARD] failed; fail-open")
        return candidates


def install() -> bool:
    global _INSTALLED, _ORIG_BUILD
    try:
        import trading.handlers.entry_controller as ec
        cur = getattr(ec, "_build_scored_candidates", None)
        if getattr(cur, "_entry_immediate_movement_guard_v12", False):
            _INSTALLED = True
            return True
        original = getattr(cur, "_original", None) if callable(cur) else None
        if callable(original):
            _ORIG_BUILD = original
        elif callable(cur):
            _ORIG_BUILD = cur
        else:
            logger.warning("[ENTRY IMMEDIATE MOVE GUARD] target missing")
            return False
        _patched_build_scored_candidates._entry_immediate_movement_guard_v1 = True  # type: ignore[attr-defined]
        _patched_build_scored_candidates._entry_immediate_movement_guard_v11 = True  # type: ignore[attr-defined]
        _patched_build_scored_candidates._entry_immediate_movement_guard_v12 = True  # type: ignore[attr-defined]
        _patched_build_scored_candidates._original = _ORIG_BUILD  # type: ignore[attr-defined]
        ec._build_scored_candidates = _patched_build_scored_candidates
        _INSTALLED = True
        logger.warning(
            "[ENTRY IMMEDIATE MOVE GUARD] installed v1.2 enabled=%s sources=%s summary_min_range=%.3f summary_min_body=%.3f summary_min_close_pos_dir=%.1f summary_rescue=%s rescue_score=%.3f rescue_range=%.3f rescue_volume=%.0f",
            _env_bool("ENTRY_IMMEDIATE_MOVEMENT_GUARD_ENABLED", True),
            os.getenv("ENTRY_MOVE_GUARD_SOURCES", "SUMMARY,RANKING,TONOSAMA,EARLY_SCALP"),
            _env_float("ENTRY_MOVE_MIN_RANGE_PCT_SUMMARY", 0.28),
            _env_float("ENTRY_MOVE_MIN_BODY_DIR_PCT_SUMMARY", 0.05),
            _env_float("ENTRY_MOVE_MIN_CLOSE_POS_DIR_SUMMARY", 58.0),
            _env_bool("ENTRY_MOVE_SUMMARY_RANGE_RESCUE_ENABLED", True),
            _env_float("ENTRY_MOVE_SUMMARY_RANGE_RESCUE_MIN_SCORE", 4.0),
            _env_float("ENTRY_MOVE_SUMMARY_RANGE_RESCUE_MIN_RANGE_PCT", 1.5),
            _env_float("ENTRY_MOVE_SUMMARY_RANGE_RESCUE_MIN_VOLUME", 300000.0),
        )
        return True
    except Exception:
        logger.exception("[ENTRY IMMEDIATE MOVE GUARD] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[ENTRY IMMEDIATE MOVE GUARD] auto install failed")

__all__ = ["install"]
