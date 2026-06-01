# ============================================================
# File   : core/startup/low_movement_tonosama_no_highlow_patch.py
# Version: V3-TONOSAMA-NO-HIGHLOW-STRONG-SCORE-FALLBACK
# ------------------------------------------------------------
# 目的:
#   TONOSAMA pending が entry_controller に渡る時、top-level / _raw の両方に
#   high/low/_intrabar_range_pct が無いケースがある。
#
#   その場合、LOW MOVE GUARD が
#     reason=no_high_low
#   で止めてしまい、TONOSAMA AI BRIDGE OK 後も発注前で止まる。
#
# Ver2:
#   - entry row の top-level だけでなく、_raw / raw / entry_conditions から
#     volume_speed / volume_surge_ratio / dominant_ratio / turnover を拾う。
#
# Ver3:
#   - 最新ログでは score=2.4〜2.6 と十分なのに、entry_controller に渡る
#     dict から volume系特徴量が落ち、volume_signal=0.0 で no_high_low 拒否。
#   - TONOSAMA限定で、score が十分強い場合は volume_signal が欠けても
#     fallback range を与えて LOW MOVE GUARD を通す。
#   - 可能なら _intrabar_range_pct / range_pct も nested から拾う。
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
        s = str(v).replace(",", "").strip()
        if s.lower() in {"nan", "none", "nat", "<na>"}:
            return default
        x = float(s)
        if x != x:
            return default
        return x
    except Exception:
        return default


def _is_tonosama(row: Any, mod: Any) -> bool:
    try:
        fn = getattr(mod, "_is_tonosama_entry", None)
        if callable(fn):
            return bool(fn(row))
        if isinstance(row, dict):
            src = str(row.get("source") or "").upper()
            typ = str(row.get("entry_type") or "").upper()
            return src == "TONOSAMA" or typ == "TONOSAMA"
    except Exception:
        pass
    return False


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


def _score(row: dict) -> float:
    vals = [
        row.get("_tonosama_score"),
        row.get("pending_score"),
        row.get("score"),
        row.get("final_score"),
        row.get("display_score"),
        row.get("score_buy"),
        row.get("score_sell"),
    ]
    nested_score = _max_from_keys(row, ("_tonosama_score", "pending_score", "score", "final_score", "display_score", "score_buy", "score_sell", "score_raw", "score_buy_raw", "score_sell_raw"))
    vals.append(nested_score)
    return max(abs(_sf(v, 0.0)) for v in vals)


def _direct_range_signal(row: dict) -> float:
    rng = _max_from_keys(row, ("_intrabar_range_pct", "intrabar_range_pct", "range_pct", "row_range_pct", "range_1m_pct", "range_3m_pct", "range_5m_pct"))
    if rng > 0:
        return rng
    hi = _max_from_keys(row, ("high", "high_price", "current_high", "high_1m", "high_3m", "high_5m"))
    lo = _max_from_keys(row, ("low", "low_price", "current_low", "low_1m", "low_3m", "low_5m"))
    close = _max_from_keys(row, ("close", "close_price", "current_price", "price"))
    if hi > 0 and lo > 0 and close > 0 and hi > lo:
        return (hi - lo) / close
    return 0.0


def _volume_signal(row: dict) -> float:
    signal = _max_from_keys(
        row,
        (
            "_max_volume_surge_ratio",
            "max_volume_surge_ratio",
            "volume_surge_ratio",
            "volume_surge_ratio_1m",
            "volume_surge_ratio_3m",
            "volume_surge_ratio_5m",
            "volume_speed",
            "dominant_ratio",
            "volume_surge_ratio_5s",
        ),
    )
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

        d_range = _direct_range_signal(row if isinstance(row, dict) else {})
        if d_range > 0:
            logger.warning(
                "[LOW MOVE TONOSAMA FALLBACK] use nested range_pct=%.4f symbol=%s",
                d_range,
                row.get("symbol") if isinstance(row, dict) else "",
            )
            return float(d_range)

        use_raw_volume = _env_on("LOW_MOVE_TONOSAMA_NO_HIGHLOW_USE_RAW_VOLUME_SIGNAL", True)
        score = _score(row if isinstance(row, dict) else {})
        volume_sig = _volume_signal(row if isinstance(row, dict) else {}) if use_raw_volume else 0.0
        min_score = _env_float("LOW_MOVE_TONOSAMA_NO_HIGHLOW_MIN_SCORE", 0.01)
        min_vol = _env_float("LOW_MOVE_TONOSAMA_NO_HIGHLOW_MIN_VOLUME_SIGNAL", 1.0)
        strong_score_min = _env_float("LOW_MOVE_TONOSAMA_NO_HIGHLOW_STRONG_SCORE_MIN", 2.0)
        allow_strong_score = _env_on("LOW_MOVE_TONOSAMA_NO_HIGHLOW_ALLOW_STRONG_SCORE", True)

        ok_by_volume = score >= min_score and volume_sig >= min_vol
        ok_by_strong_score = allow_strong_score and score >= strong_score_min
        if not (ok_by_volume or ok_by_strong_score):
            logger.warning(
                "[LOW MOVE TONOSAMA FALLBACK] no high/low denied score=%.4f min_score=%.4f strong_score_min=%.4f volume_signal=%.4f min_volume_signal=%.4f use_raw_volume=%s nested_keys=%s",
                score,
                min_score,
                strong_score_min,
                volume_sig,
                min_vol,
                use_raw_volume,
                [sorted(list(d.keys()))[:12] for d in _nested_dicts(row if isinstance(row, dict) else {})[:3]],
            )
            return 0.0

        fallback = _env_float("LOW_MOVE_TONOSAMA_NO_HIGHLOW_FALLBACK_RANGE_PCT", _env_float("LOW_MOVE_TONOSAMA_STRONG_RANGE_PCT", 0.012))
        logger.warning(
            "[LOW MOVE TONOSAMA FALLBACK] use fallback range_pct=%.4f score=%.4f volume_signal=%.4f reason=%s symbol=%s",
            fallback,
            score,
            volume_sig,
            "strong_score" if ok_by_strong_score and not ok_by_volume else "volume_signal",
            row.get("symbol") if isinstance(row, dict) else "",
        )
        return float(fallback)
    except Exception:
        logger.exception("[LOW MOVE TONOSAMA FALLBACK] patched range failed")
        return _ORIG_RANGE_FN(row) if callable(_ORIG_RANGE_FN) else 0.0


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
        import core.startup.low_movement_entry_guard_patch as lm
        cur = getattr(lm, "_range_pct_from_row", None)
        if not callable(cur):
            logger.warning("[LOW MOVE TONOSAMA FALLBACK] target missing")
            return False
        if getattr(cur, "_tonosama_no_highlow_fallback_patch_v3", False):
            _INSTALLED = True
            return True
        _ORIG_RANGE_FN = getattr(cur, "_original", cur)
        _patched_range_pct_from_row._tonosama_no_highlow_fallback_patch_v3 = True  # type: ignore[attr-defined]
        _patched_range_pct_from_row._original = _ORIG_RANGE_FN  # type: ignore[attr-defined]
        lm._range_pct_from_row = _patched_range_pct_from_row
        _INSTALLED = True
        logger.warning(
            "[LOW MOVE TONOSAMA FALLBACK] installed v3 min_price=%s fallback_range=%s use_raw_volume=%s strong_score=%s",
            os.environ.get("LOW_MOVE_TONOSAMA_MIN_ENTRY_PRICE"),
            os.environ.get("LOW_MOVE_TONOSAMA_NO_HIGHLOW_FALLBACK_RANGE_PCT"),
            os.environ.get("LOW_MOVE_TONOSAMA_NO_HIGHLOW_USE_RAW_VOLUME_SIGNAL"),
            os.environ.get("LOW_MOVE_TONOSAMA_NO_HIGHLOW_STRONG_SCORE_MIN"),
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
