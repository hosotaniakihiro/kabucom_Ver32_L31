# ============================================================
# File   : core/startup/low_movement_tonosama_no_highlow_patch.py
# Version: V5-RANGE-5M-FILTER-INLINED
# ------------------------------------------------------------
# 目的:
#   TONOSAMA pending が entry_controller に渡る時、top-level high/low が
#   欠けるため、LOW MOVE GUARD や volatility_filter.range_5m_filter で
#   RANGE_5M_FILTER_NG になる問題を防ぐ。
#
# V5:
#   - trading.filters.volatility_filter.range_5m_filter(entry_row) の
#     wrap (score/volume/range信号によるTONOSAMA救済) は
#     _range_5m_filter_from_entry_row 本体 (V6) へインライン化したため撤去。
#     low_movement_entry_guard_patch._range_pct_from_row への救済は維持。
#
# V4 (旧):
#   - 従来の low_movement_entry_guard_patch._range_pct_from_row 救済に加え、
#     trading.filters.volatility_filter.range_5m_filter(entry_row) もwrap。
#   - TONOSAMA限定で、nested/raw の _intrabar_range_pct / score / volume / surge
#     を使って 5分レンジ判定の欠損を救済する。
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_INSTALLED = False
_ORIG_RANGE_FN = None


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _env_on(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _sf(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return default
        s = str(v).replace(",", "").replace("%", "").strip()
        if s.lower() in {"nan", "none", "nat", "<na>"}:
            return default
        x = float(s)
        if x != x:
            return default
        return x
    except Exception:
        return default


def _as_dict(v: Any) -> dict[str, Any]:
    try:
        if isinstance(v, dict):
            return dict(v)
        if hasattr(v, "to_dict"):
            d = v.to_dict()
            return dict(d) if isinstance(d, dict) else {}
    except Exception:
        pass
    return {}


def _nested_dicts(row: Any) -> list[dict[str, Any]]:
    base = _as_dict(row)
    out: list[dict[str, Any]] = []
    if base:
        out.append(base)
    for k in ("_raw", "raw", "candidate_raw", "source_row", "entry_conditions", "conditions", "metrics", "features", "detail", "ai_detail"):
        d = _as_dict(base.get(k)) if base else {}
        if d:
            out.append(d)
            for kk in ("_raw", "raw", "entry_conditions", "conditions", "metrics", "features", "detail", "ai_detail"):
                dd = _as_dict(d.get(kk))
                if dd:
                    out.append(dd)
    return out


def _max_from_keys(row: Any, keys: tuple[str, ...]) -> float:
    m = 0.0
    for d in _nested_dicts(row):
        for k in keys:
            if k in d:
                m = max(m, _sf(d.get(k), 0.0))
    return float(m)


def _first_text(row: Any, keys: tuple[str, ...]) -> str:
    for d in _nested_dicts(row):
        for k in keys:
            v = d.get(k)
            if v is not None and str(v).strip() != "":
                return str(v).strip()
    return ""


def _is_tonosama(row: Any, mod: Any = None) -> bool:
    try:
        if mod is not None:
            fn = getattr(mod, "_is_tonosama_entry", None)
            if callable(fn):
                return bool(fn(row))
        src = _first_text(row, ("source", "pipeline_source")).upper()
        typ = _first_text(row, ("entry_type", "type")).upper()
        return src == "TONOSAMA" or typ == "TONOSAMA"
    except Exception:
        return False


def _symbol(row: Any) -> str:
    return _first_text(row, ("symbol", "Symbol", "銘柄コード", "code", "stock_code"))


def _score(row: Any) -> float:
    return _max_from_keys(row, ("_tonosama_score", "pending_score", "score", "score_total", "final_score", "display_score", "score_buy", "score_sell", "score_raw"))


def _direct_range_signal(row: Any) -> float:
    rng = _max_from_keys(row, ("_intrabar_range_pct", "intrabar_range_pct", "range_pct", "row_range_pct", "range_1m_pct", "range_3m_pct", "range_5m_pct"))
    if rng > 0:
        return rng / 100.0 if rng > 1.0 else rng
    hi = _max_from_keys(row, ("high", "high_price", "current_high", "high_1m", "high_3m", "high_5m"))
    lo = _max_from_keys(row, ("low", "low_price", "current_low", "low_1m", "low_3m", "low_5m"))
    close = _max_from_keys(row, ("close", "close_price", "current_price", "price"))
    if hi > 0 and lo > 0 and close > 0 and hi > lo:
        return (hi - lo) / close
    return 0.0


def _volume_signal(row: Any) -> float:
    signal = _max_from_keys(row, ("_max_volume_surge_ratio", "max_volume_surge_ratio", "volume_surge_ratio", "volume_speed", "dominant_ratio", "volume_surge_ratio_5s"))
    if signal > 0:
        return signal
    volume = _max_from_keys(row, ("volume", "latest_volume", "_latest_volume", "volume_1m", "volume_3m", "volume_5m", "latest_5sec_volume", "volume_raw"))
    turnover = _max_from_keys(row, ("turnover", "turnover_raw", "trading_value", "trading_value_raw", "売買代金"))
    min_vol = _env_float("LOW_MOVE_TONOSAMA_NO_HIGHLOW_MIN_ABS_VOLUME", _env_float("TONOSAMA_ALERT_MIN_LATEST_VOLUME", 30000.0))
    min_turnover = _env_float("LOW_MOVE_TONOSAMA_NO_HIGHLOW_MIN_TURNOVER", _env_float("TONOSAMA_ALERT_MIN_TURNOVER", 10000000.0))
    if volume >= min_vol or turnover >= min_turnover:
        return _env_float("LOW_MOVE_TONOSAMA_NO_HIGHLOW_INFERRED_VOLUME_SIGNAL", 1.0)
    return 0.0


def _patched_range_pct_from_row(row: dict) -> float:
    try:
        v = _ORIG_RANGE_FN(row) if callable(_ORIG_RANGE_FN) else 0.0
        if v and v > 0:
            return float(v)
        if not _env_on("LOW_MOVE_TONOSAMA_ALLOW_NO_HIGHLOW_FALLBACK", True):
            return 0.0
        import core.startup.low_movement_entry_guard_patch as lm
        if not _is_tonosama(row, lm):
            return 0.0
        d_range = _direct_range_signal(row)
        if d_range > 0:
            logger.warning("[LOW MOVE TONOSAMA FALLBACK] use nested range_pct=%.4f symbol=%s", d_range, _symbol(row))
            return float(d_range)
        score = _score(row)
        volume_sig = _volume_signal(row) if _env_on("LOW_MOVE_TONOSAMA_NO_HIGHLOW_USE_RAW_VOLUME_SIGNAL", True) else 0.0
        min_score = _env_float("LOW_MOVE_TONOSAMA_NO_HIGHLOW_MIN_SCORE", 0.01)
        min_vol = _env_float("LOW_MOVE_TONOSAMA_NO_HIGHLOW_MIN_VOLUME_SIGNAL", 1.0)
        strong_score_min = _env_float("LOW_MOVE_TONOSAMA_NO_HIGHLOW_STRONG_SCORE_MIN", 2.0)
        ok_by_volume = score >= min_score and volume_sig >= min_vol
        ok_by_strong_score = _env_on("LOW_MOVE_TONOSAMA_NO_HIGHLOW_ALLOW_STRONG_SCORE", True) and score >= strong_score_min
        if not (ok_by_volume or ok_by_strong_score):
            logger.warning("[LOW MOVE TONOSAMA FALLBACK] no high/low denied score=%.4f volume_signal=%.4f symbol=%s", score, volume_sig, _symbol(row))
            return 0.0
        fallback = _env_float("LOW_MOVE_TONOSAMA_NO_HIGHLOW_FALLBACK_RANGE_PCT", _env_float("LOW_MOVE_TONOSAMA_STRONG_RANGE_PCT", 0.012))
        logger.warning("[LOW MOVE TONOSAMA FALLBACK] use fallback range_pct=%.4f score=%.4f volume_signal=%.4f symbol=%s", fallback, score, volume_sig, _symbol(row))
        return float(fallback)
    except Exception:
        logger.exception("[LOW MOVE TONOSAMA FALLBACK] patched range failed")
        return _ORIG_RANGE_FN(row) if callable(_ORIG_RANGE_FN) else 0.0


# range_5m_filter (TONOSAMA score/volume/range信号救済) は
# trading/filters/volatility_filter.py の _range_5m_filter_from_entry_row (V6)
# へインライン化済み。


def install() -> bool:
    global _INSTALLED, _ORIG_RANGE_FN
    if _INSTALLED:
        return True
    try:
        os.environ.setdefault("LOW_MOVE_TONOSAMA_MIN_ENTRY_PRICE", "300")
        os.environ.setdefault("LOW_MOVE_TONOSAMA_ALLOW_NO_HIGHLOW_FALLBACK", "1")
        os.environ.setdefault("LOW_MOVE_TONOSAMA_NO_HIGHLOW_FALLBACK_RANGE_PCT", "0.012")
        os.environ.setdefault("LOW_MOVE_TONOSAMA_NO_HIGHLOW_USE_RAW_VOLUME_SIGNAL", "1")
        os.environ.setdefault("LOW_MOVE_TONOSAMA_NO_HIGHLOW_ALLOW_STRONG_SCORE", "1")
        os.environ.setdefault("LOW_MOVE_TONOSAMA_NO_HIGHLOW_STRONG_SCORE_MIN", "2.0")
        os.environ.setdefault("TONOSAMA_VOL_RANGE_RESCUE_ENABLED", "1")
        os.environ.setdefault("TONOSAMA_VOL_RANGE_RESCUE_MIN_SCORE", "2.0")
        os.environ.setdefault("TONOSAMA_VOL_RANGE_RESCUE_MIN_RANGE_PCT", "0.006")
        os.environ.setdefault("TONOSAMA_VOL_RANGE_RESCUE_MIN_VOLUME_SIGNAL", "1.0")

        import core.startup.low_movement_entry_guard_patch as lm
        cur = getattr(lm, "_range_pct_from_row", None)
        if callable(cur) and not getattr(cur, "_tonosama_no_highlow_fallback_patch_v4", False):
            _ORIG_RANGE_FN = getattr(cur, "_original", cur)
            _patched_range_pct_from_row._tonosama_no_highlow_fallback_patch_v4 = True  # type: ignore[attr-defined]
            _patched_range_pct_from_row._original = _ORIG_RANGE_FN  # type: ignore[attr-defined]
            lm._range_pct_from_row = _patched_range_pct_from_row

        _INSTALLED = True
        logger.warning(
            "[LOW MOVE TONOSAMA FALLBACK] installed v4 fallback_range=%s vol_range_rescue=%s vol_min_score=%s vol_min_range=%s",
            os.environ.get("LOW_MOVE_TONOSAMA_NO_HIGHLOW_FALLBACK_RANGE_PCT"),
            os.environ.get("TONOSAMA_VOL_RANGE_RESCUE_ENABLED"),
            os.environ.get("TONOSAMA_VOL_RANGE_RESCUE_MIN_SCORE"),
            os.environ.get("TONOSAMA_VOL_RANGE_RESCUE_MIN_RANGE_PCT"),
        )
        return True
    except Exception:
        logger.exception("[LOW MOVE TONOSAMA FALLBACK] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[LOW MOVE TONOSAMA FALLBACK] auto install failed")


__all__ = ["install"]
