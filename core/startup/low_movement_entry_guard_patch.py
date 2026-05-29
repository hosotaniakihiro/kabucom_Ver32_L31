# ============================================================
# File   : core/startup/low_movement_entry_guard_patch.py
# Version: Ver15-RANKING-NO-HIGHLOW-MOMENTUM-FALLBACK
# ------------------------------------------------------------
# あまり動かない銘柄へのエントリーを発注直前で止める。
# さらに、ランキング方向に逆らうエントリーも禁止する。
#
# Ver15:
#   - RANKING pending はランキング情報だけで作るため high/low が
#     0 または未設定のケースがある。
#   - high/low が無いだけで ATR_1M_FILTER_NG にせず、RANKING では
#     ATR / slope / macd-signal / score が十分なら通す。
#   - 価格帯制限も RANKING 用に LOW_MOVE_RANKING_MIN/MAX_ENTRY_PRICE を追加。
#
# Ver14:
#   - pending entry の top-level には high/low/_intrabar_range_pct が無く、
#     _raw 内にだけ TONOSAMA 特徴量が残るケースに対応
#   - _row_to_dict() で _raw dict を展開し、top-level 欠損項目だけ補完
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)
_INSTALLED = False
_ORIG_ATR_FILTER = None
_ORIG_RANGE_FILTER = None


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return default
        s = str(v).strip()
        if not s or s.lower() in {"nan", "none", "nat", "<na>"}:
            return default
        return float(s.replace(",", ""))
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
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


def _is_blank_value(v: Any) -> bool:
    try:
        if v is None:
            return True
        s = str(v).strip()
        return s == "" or s.lower() in {"nan", "none", "nat", "<na>"}
    except Exception:
        return True


def _row_to_dict(row: Any) -> dict:
    try:
        if row is None:
            return {}
        if isinstance(row, dict):
            d = dict(row)
        elif hasattr(row, "to_dict"):
            v = row.to_dict()
            d = dict(v) if isinstance(v, dict) else {}
        else:
            d = {}

        raw = d.get("_raw")
        if hasattr(raw, "to_dict"):
            try:
                raw = raw.to_dict()
            except Exception:
                raw = None
        if isinstance(raw, dict) and raw:
            for k, v in raw.items():
                if k not in d or _is_blank_value(d.get(k)):
                    d[k] = v
            d["_raw_merged_for_low_move_guard"] = True
        return d
    except Exception:
        return {}


def _first(row: dict, keys: tuple[str, ...], default=None):
    for k in keys:
        try:
            v = row.get(k)
            if v is not None and str(v).strip() != "":
                return v
        except Exception:
            pass
    return default


def _norm_symbol(v: Any) -> str:
    try:
        s = str(v or "").strip()
        if s.endswith(".0") and s[:-2].isdigit():
            return s[:-2]
        return s
    except Exception:
        return ""


def _norm_text(v: Any) -> str:
    try:
        return str(v or "").strip().upper()
    except Exception:
        return ""


def _source_text(row: dict) -> str:
    return _norm_text(_first(row, ("source", "entry_source", "pipeline_source"), ""))


def _is_tonosama_entry(row_or_entry: Any) -> bool:
    row = _row_to_dict(row_or_entry)
    src = _source_text(row)
    et = _norm_text(_first(row, ("entry_type", "type", "entry_kind"), ""))
    return src == "TONOSAMA" or et == "TONOSAMA"


def _is_ranking_entry(row_or_entry: Any) -> bool:
    row = _row_to_dict(row_or_entry)
    src = _source_text(row)
    et = _norm_text(_first(row, ("entry_type", "type", "entry_kind"), ""))
    rank_type = _first(row, ("rank_type", "ranking_type", "rank_kind"), None)
    return src == "RANKING" or et == "RANKING" or rank_type is not None


def _range_pct_from_row(row: dict) -> float:
    raw = _first(
        row,
        (
            "_intrabar_range_pct",
            "intrabar_range_pct",
            "range_pct",
            "price_range_pct",
            "range_1m_pct",
            "range_3m_pct",
            "range_5m_pct",
            "disp_range_pct",
        ),
        None,
    )
    v = _safe_float(raw, 0.0)
    if v <= 0:
        return 0.0
    if v > 1.0:
        return v / 100.0
    return v


def _ranking_momentum_ok(row: dict, *, close: float, symbol: str, high: float, low: float) -> bool:
    if not _env_bool("LOW_MOVE_RANKING_ALLOW_NO_HIGHLOW_MOMENTUM", True):
        return False

    atr = _safe_float(_first(row, ("atr", "atr_1m", "atr_5m"), 0.0), 0.0)
    atr_ratio = atr / close if close > 0 else 0.0
    min_atr_ratio = _env_float("LOW_MOVE_RANKING_MIN_ATR_RATIO", 0.0035)

    slope_values = []
    for k in ("slope_atr_scaled", "slope", "score_slope", "disp_slope", "_slope"):
        if k in row:
            slope_values.append(_safe_float(row.get(k), 0.0))
    max_abs_slope = max([abs(x) for x in slope_values], default=0.0)
    min_abs_slope = _env_float("LOW_MOVE_RANKING_MIN_ABS_SLOPE", 0.001)

    macd = _safe_float(row.get("macd"), 0.0)
    signal = _safe_float(row.get("signal"), 0.0)
    macd_gap = abs(macd - signal)
    min_macd_gap = _env_float("LOW_MOVE_RANKING_MIN_MACD_GAP", 0.0)

    score = _safe_float(_first(row, ("pending_score", "score", "final_score", "display_score", "score_total"), 0.0), 0.0)
    min_score = _env_float("LOW_MOVE_RANKING_MIN_SCORE_FOR_NO_HIGHLOW", 70.0)

    ok = (
        atr_ratio >= min_atr_ratio
        and max_abs_slope >= min_abs_slope
        and score >= min_score
        and macd_gap >= min_macd_gap
    )
    if ok:
        logger.warning(
            "[LOW MOVE GUARD] RANKING high/low missing but allowed by momentum symbol=%s close=%.1f high=%.1f low=%.1f atr=%.4f atr_ratio=%.5f min_atr=%.5f max_abs_slope=%.6f min_slope=%.6f macd=%.4f signal=%.4f score=%.2f min_score=%.2f",
            symbol,
            close,
            high,
            low,
            atr,
            atr_ratio,
            min_atr_ratio,
            max_abs_slope,
            min_abs_slope,
            macd,
            signal,
            score,
            min_score,
        )
        return True

    logger.warning(
        "[LOW MOVE GUARD] RANKING no_high_low momentum NG symbol=%s close=%.1f high=%.1f low=%.1f atr_ratio=%.5f min_atr=%.5f max_abs_slope=%.6f min_slope=%.6f macd_gap=%.4f min_macd_gap=%.4f score=%.2f min_score=%.2f",
        symbol,
        close,
        high,
        low,
        atr_ratio,
        min_atr_ratio,
        max_abs_slope,
        min_abs_slope,
        macd_gap,
        min_macd_gap,
        score,
        min_score,
    )
    return False


def _install_ranking_direction_guard() -> bool:
    try:
        from core.startup import ranking_direction_entry_guard_patch as p
        ok = p.install()
        logger.warning("[LOW MOVE GUARD] ranking_direction_entry_guard_patch installed=%s", ok)
        return bool(ok)
    except Exception as e:
        logger.warning("[LOW MOVE GUARD] ranking_direction_entry_guard_patch install failed: %s", e, exc_info=False)
        return False


def _install_scoring_flag_pattern_bridge() -> bool:
    try:
        from core.startup import scoring_flag_pattern_bridge_patch as p
        ok = p.install()
        logger.warning("[LOW MOVE GUARD] scoring_flag_pattern_bridge_patch installed=%s", ok)
        return bool(ok)
    except Exception as e:
        logger.warning("[LOW MOVE GUARD] scoring_flag_pattern_bridge_patch install failed: %s", e, exc_info=False)
        return False


def _install_entry_direction_confirm_guard() -> bool:
    try:
        from core.startup import entry_direction_confirm_guard_patch as p
        ok = p.install()
        logger.warning("[LOW MOVE GUARD] entry_direction_confirm_guard_patch pure_guard_installed=%s", ok)
        return bool(ok)
    except Exception as e:
        logger.warning("[LOW MOVE GUARD] entry_direction_confirm_guard_patch install failed: %s", e, exc_info=False)
        return False


def _install_final_entry_safety_guard() -> bool:
    try:
        from core.startup import final_entry_safety_guard_patch as p
        ok = p.install()
        logger.warning("[LOW MOVE GUARD] final_entry_safety_guard_patch installed=%s", ok)
        return bool(ok)
    except Exception as e:
        logger.warning("[LOW MOVE GUARD] final_entry_safety_guard_patch install failed: %s", e, exc_info=False)
        return False


def _install_entry_price_improvement_patch() -> bool:
    try:
        from core.startup import entry_price_improvement_patch as p
        ok = p.install()
        logger.warning("[LOW MOVE GUARD] entry_price_improvement_patch installed=%s", ok)
        return bool(ok)
    except Exception as e:
        logger.warning("[LOW MOVE GUARD] entry_price_improvement_patch install failed: %s", e, exc_info=False)
        return False


def _install_ma_cross_state_runtime_patch() -> bool:
    try:
        from core.startup import ma_cross_state_runtime_patch as p
        ok = p.install()
        logger.warning("[LOW MOVE GUARD] ma_cross_state_runtime_patch installed=%s", ok)
        return bool(ok)
    except Exception as e:
        logger.warning("[LOW MOVE GUARD] ma_cross_state_runtime_patch install failed: %s", e, exc_info=False)
        return False


def _install_vwap_state_runtime_patch() -> bool:
    try:
        from core.startup import vwap_state_runtime_patch as p
        ok = p.install()
        logger.warning("[LOW MOVE GUARD] vwap_state_runtime_patch installed=%s", ok)
        return bool(ok)
    except Exception as e:
        logger.warning("[LOW MOVE GUARD] vwap_state_runtime_patch install failed: %s", e, exc_info=False)
        return False


def _call_entry_direction_confirm(entry_row: Any) -> bool:
    try:
        from core.startup.entry_direction_confirm_guard_patch import check_entry_direction_confirm
        return bool(check_entry_direction_confirm(entry_row))
    except RecursionError:
        logger.error("[LOW MOVE GUARD] entry_direction_confirm recursion detected. fail-safe NG.", exc_info=False)
        return False
    except Exception as e:
        logger.warning("[LOW MOVE GUARD] entry_direction_confirm skipped due to error: %s", e, exc_info=False)
        return True


def _low_movement_guard(entry_row: Any) -> bool:
    row = _row_to_dict(entry_row)
    symbol = _norm_symbol(_first(row, ("symbol", "code", "stock_code"), ""))
    source_tonosama = _is_tonosama_entry(row)
    source_ranking = _is_ranking_entry(row)
    close = _safe_float(_first(row, ("close_price", "close", "price", "current_price"), 0.0), 0.0)
    high = _safe_float(_first(row, ("high_price", "high"), 0.0), 0.0)
    low = _safe_float(_first(row, ("low_price", "low"), 0.0), 0.0)

    if close <= 0:
        logger.warning("[LOW MOVE GUARD] NG symbol=%s reason=no_close close=%s", symbol, close)
        return False

    if source_tonosama:
        min_price = _env_float("LOW_MOVE_TONOSAMA_MIN_ENTRY_PRICE", _env_float("LOW_MOVE_MIN_ENTRY_PRICE", 1500.0))
        max_price = _env_float("LOW_MOVE_TONOSAMA_MAX_ENTRY_PRICE", 12000.0)
    elif source_ranking:
        min_price = _env_float("LOW_MOVE_RANKING_MIN_ENTRY_PRICE", _env_float("LOW_MOVE_MIN_ENTRY_PRICE", 1500.0))
        max_price = _env_float("LOW_MOVE_RANKING_MAX_ENTRY_PRICE", _env_float("LOW_MOVE_MAX_ENTRY_PRICE", 7000.0))
    else:
        min_price = _env_float("LOW_MOVE_MIN_ENTRY_PRICE", 1500.0)
        max_price = _env_float("LOW_MOVE_MAX_ENTRY_PRICE", 7000.0)

    if close < min_price or close > max_price:
        logger.warning(
            "[LOW MOVE GUARD] NG symbol=%s reason=price_out_of_range close=%.1f min_price=%.1f max_price=%.1f tonosama=%s ranking=%s",
            symbol, close, min_price, max_price, source_tonosama, source_ranking,
        )
        return False

    range_pct = 0.0
    range_source = "high_low"
    if high > 0 and low > 0 and high >= low:
        range_pct = (high - low) / close if close > 0 else 0.0
    else:
        range_pct = _range_pct_from_row(row)
        range_source = "row_range_pct"
        if range_pct <= 0:
            if source_ranking and _ranking_momentum_ok(row, close=close, symbol=symbol, high=high, low=low):
                return True
            logger.warning(
                "[LOW MOVE GUARD] NG symbol=%s reason=no_high_low close=%.1f high=%.1f low=%.1f row_range_pct=%.4f raw_merged=%s keys=%s",
                symbol, close, high, low, range_pct, row.get("_raw_merged_for_low_move_guard"), sorted(list(row.keys()))[:80],
            )
            return False
        high = close * (1.0 + range_pct / 2.0)
        low = close * max(0.0001, (1.0 - range_pct / 2.0))
        logger.warning(
            "[LOW MOVE GUARD] high/low missing but use row range fallback symbol=%s close=%.1f range_pct=%.4f source=%s raw_merged=%s pseudo_high=%.1f pseudo_low=%.1f",
            symbol, close, range_pct, range_source, row.get("_raw_merged_for_low_move_guard"), high, low,
        )

    split = _env_float("LOW_MOVE_TIER_SPLIT_PRICE", 3000.0)
    if source_tonosama:
        min_range_pct = _env_float("LOW_MOVE_TONOSAMA_MIN_RANGE_PCT", 0.006)
        strong_range_pct = _env_float("LOW_MOVE_TONOSAMA_STRONG_RANGE_PCT", 0.012)
    elif source_ranking:
        min_range_pct = _env_float("LOW_MOVE_RANKING_MIN_RANGE_PCT_LOW_PRICE", 0.012) if close < split else _env_float("LOW_MOVE_RANKING_MIN_RANGE_PCT_HIGH_PRICE", 0.006)
        strong_range_pct = _env_float("LOW_MOVE_RANKING_STRONG_RANGE_PCT", 0.018)
    else:
        min_range_pct = _env_float("LOW_MOVE_MIN_RANGE_PCT_LOW_PRICE", 0.015) if close < split else _env_float("LOW_MOVE_MIN_RANGE_PCT_HIGH_PRICE", 0.008)
        strong_range_pct = _env_float("LOW_MOVE_STRONG_RANGE_PCT", 0.020)

    if range_pct < min_range_pct:
        logger.warning(
            "[LOW MOVE GUARD] NG symbol=%s reason=range_too_small close=%.1f high=%.1f low=%.1f range_pct=%.4f min=%.4f source=%s tonosama=%s ranking=%s",
            symbol, close, high, low, range_pct, min_range_pct, range_source, source_tonosama, source_ranking,
        )
        return False

    slope_values = []
    for k in ("slope_atr_scaled", "slope", "score_slope", "disp_slope", "_slope"):
        if k in row:
            slope_values.append(_safe_float(row.get(k), 0.0))

    macd = _safe_float(row.get("macd"), 0.0)
    signal = _safe_float(row.get("signal"), 0.0)
    max_abs_slope = max([abs(x) for x in slope_values], default=0.0)

    if slope_values:
        abs_slope = max_abs_slope
        if source_tonosama:
            min_abs_slope = _env_float("LOW_MOVE_TONOSAMA_MIN_ABS_SLOPE", 0.0001)
        elif source_ranking:
            min_abs_slope = _env_float("LOW_MOVE_RANKING_MIN_ABS_SLOPE", 0.001)
        else:
            min_abs_slope = _env_float("LOW_MOVE_MIN_ABS_SLOPE_LOW_PRICE", 0.0003) if close < split else _env_float("LOW_MOVE_MIN_ABS_SLOPE_HIGH_PRICE", 0.0002)
        if abs_slope < min_abs_slope and range_pct < strong_range_pct:
            logger.warning(
                "[LOW MOVE GUARD] NG symbol=%s reason=slope_too_small close=%.1f abs_slope=%.6f min=%.6f range_pct=%.4f strong_range=%.4f source=%s tonosama=%s ranking=%s",
                symbol, close, abs_slope, min_abs_slope, range_pct, strong_range_pct, range_source, source_tonosama, source_ranking,
            )
            return False
        if abs_slope < min_abs_slope and range_pct >= strong_range_pct:
            logger.warning(
                "[LOW MOVE GUARD] slope small but allowed by strong range symbol=%s close=%.1f abs_slope=%.6f min=%.6f range_pct=%.4f strong_range=%.4f source=%s tonosama=%s ranking=%s",
                symbol, close, abs_slope, min_abs_slope, range_pct, strong_range_pct, range_source, source_tonosama, source_ranking,
            )

    if abs(macd) < 0.0001 and abs(signal) < 0.0001 and max_abs_slope < 0.0001 and range_pct < strong_range_pct:
        logger.warning(
            "[LOW MOVE GUARD] NG symbol=%s reason=no_momentum macd=%.6f signal=%.6f slope=%.6f range_pct=%.4f strong_range=%.4f source=%s tonosama=%s ranking=%s",
            symbol, macd, signal, max_abs_slope, range_pct, strong_range_pct, range_source, source_tonosama, source_ranking,
        )
        return False

    logger.info(
        "[LOW MOVE GUARD] OK symbol=%s close=%.1f range_pct=%.4f min_range=%.4f strong_range=%.4f macd=%.4f signal=%.4f max_abs_slope=%.6f source=%s tonosama=%s ranking=%s raw_merged=%s",
        symbol, close, range_pct, min_range_pct, strong_range_pct, macd, signal, max_abs_slope, range_source, source_tonosama, source_ranking, row.get("_raw_merged_for_low_move_guard"),
    )
    return True


def _apply_all_entry_guards(entry_row: Any) -> bool:
    if entry_row is None:
        return True
    if not _low_movement_guard(entry_row):
        return False
    if not _call_entry_direction_confirm(entry_row):
        return False
    return True


def _patched_range_5m_filter(entry_row: Any = None, *args, **kwargs):
    try:
        allow = True
        if callable(_ORIG_RANGE_FILTER):
            allow = _ORIG_RANGE_FILTER(entry_row, *args, **kwargs)
        if isinstance(allow, tuple):
            return allow
        if not bool(allow):
            if _is_tonosama_entry(entry_row) and _env_bool("LOW_MOVE_TONOSAMA_IGNORE_ORIG_RANGE_NG", True):
                logger.warning(
                    "[LOW MOVE GUARD] original range_5m_filter NG ignored for TONOSAMA; recheck low movement guard. symbol=%s",
                    _norm_symbol(_first(_row_to_dict(entry_row), ("symbol", "code", "stock_code"), "")),
                )
            else:
                return False
        return _apply_all_entry_guards(entry_row)
    except RecursionError:
        logger.error("[LOW MOVE GUARD] recursion detected in patched range filter. fail-safe NG. Check duplicate wrappers.", exc_info=False)
        return False
    except Exception as e:
        logger.warning("[LOW MOVE GUARD] patched range filter failed: %s", e, exc_info=False)
        return False


def _patched_atr_1m_filter(entry_row: Any = None, *args, **kwargs):
    try:
        allow = True
        if callable(_ORIG_ATR_FILTER):
            allow = _ORIG_ATR_FILTER(entry_row, *args, **kwargs)
        if isinstance(allow, tuple):
            return allow
        if not bool(allow):
            return False
        return _apply_all_entry_guards(entry_row)
    except RecursionError:
        logger.error("[LOW MOVE GUARD] recursion detected in patched atr filter. fail-safe NG. Check duplicate wrappers.", exc_info=False)
        return False
    except Exception as e:
        logger.warning("[LOW MOVE GUARD] patched atr filter failed: %s", e, exc_info=False)
        return False


def _is_low_move_wrapped(func: Any) -> bool:
    try:
        return bool(
            getattr(func, "_low_move_guard_v2", False)
            or getattr(func, "_low_move_guard_v1", False)
            or getattr(func, "_low_move_guard_v12", False)
            or getattr(func, "_low_move_guard_v13", False)
            or getattr(func, "_low_move_guard_v14", False)
            or getattr(func, "_low_move_guard_v15", False)
        )
    except Exception:
        return False


def install() -> bool:
    global _INSTALLED, _ORIG_ATR_FILTER, _ORIG_RANGE_FILTER
    ok_direction = _install_ranking_direction_guard()
    ok_scoring_bridge = _install_scoring_flag_pattern_bridge()
    ok_entry_direction = _install_entry_direction_confirm_guard()
    ok_final_safety = _install_final_entry_safety_guard()
    ok_price_improve = _install_entry_price_improvement_patch()
    ok_ma_cross = _install_ma_cross_state_runtime_patch()
    ok_vwap_state = _install_vwap_state_runtime_patch()

    if _INSTALLED:
        return True

    try:
        import trading.handlers.entry_controller as ec
        old_atr = getattr(ec, "atr_1m_filter", None)
        old_range = getattr(ec, "range_5m_filter", None)

        if callable(old_atr) and not _is_low_move_wrapped(old_atr):
            _ORIG_ATR_FILTER = old_atr
            _patched_atr_1m_filter._low_move_guard_v15 = True  # type: ignore[attr-defined]
            _patched_atr_1m_filter._original = old_atr  # type: ignore[attr-defined]
            ec.atr_1m_filter = _patched_atr_1m_filter
            logger.warning("[LOW MOVE GUARD] patched entry_controller.atr_1m_filter")
        else:
            logger.warning("[LOW MOVE GUARD] atr_1m_filter already wrapped or missing")

        if callable(old_range) and not _is_low_move_wrapped(old_range):
            _ORIG_RANGE_FILTER = old_range
            _patched_range_5m_filter._low_move_guard_v15 = True  # type: ignore[attr-defined]
            _patched_range_5m_filter._original = old_range  # type: ignore[attr-defined]
            ec.range_5m_filter = _patched_range_5m_filter
            logger.warning("[LOW MOVE GUARD] patched entry_controller.range_5m_filter")
        else:
            logger.warning("[LOW MOVE GUARD] range_5m_filter already wrapped or missing")

        _INSTALLED = True
        logger.warning(
            "[LOW MOVE GUARD] installed v15 raw_merge=True ranking_no_highlow_momentum=%s ranking_min_price=%s ranking_max_price=%s ranking_min_score=%s tonosama_max_price=%s tonosama_min_range=%s tonosama_ignore_orig_range_ng=%s direction=%s scoring_bridge=%s entry_direction=%s final_safety=%s price_improve=%s ma_cross=%s vwap=%s",
            _env_bool("LOW_MOVE_RANKING_ALLOW_NO_HIGHLOW_MOMENTUM", True),
            os.getenv("LOW_MOVE_RANKING_MIN_ENTRY_PRICE", os.getenv("LOW_MOVE_MIN_ENTRY_PRICE", "1500")),
            os.getenv("LOW_MOVE_RANKING_MAX_ENTRY_PRICE", os.getenv("LOW_MOVE_MAX_ENTRY_PRICE", "7000")),
            os.getenv("LOW_MOVE_RANKING_MIN_SCORE_FOR_NO_HIGHLOW", "70.0"),
            os.getenv("LOW_MOVE_TONOSAMA_MAX_ENTRY_PRICE", "12000"),
            os.getenv("LOW_MOVE_TONOSAMA_MIN_RANGE_PCT", "0.006"),
            _env_bool("LOW_MOVE_TONOSAMA_IGNORE_ORIG_RANGE_NG", True),
            ok_direction, ok_scoring_bridge, ok_entry_direction, ok_final_safety, ok_price_improve, ok_ma_cross, ok_vwap_state,
        )
        return True
    except Exception as e:
        logger.warning("[LOW MOVE GUARD] install failed: %s", e, exc_info=False)
        return False


try:
    install()
except Exception as e:
    logger.warning("[LOW MOVE GUARD] auto install failed: %s", e, exc_info=False)

__all__ = ["install"]
