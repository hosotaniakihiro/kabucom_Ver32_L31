# ============================================================
# File   : core/startup/low_movement_tonosama_no_highlow_patch.py
# Version: V1-TONOSAMA-NO-HIGHLOW-FALLBACK
# ------------------------------------------------------------
# 目的:
#   TONOSAMA pending が entry_controller に渡る時、top-level / _raw の両方に
#   high/low/_intrabar_range_pct が無いケースがある。
#
#   その場合、LOW MOVE GUARD が
#     reason=no_high_low
#   で止めてしまい、TONOSAMA AI BRIDGE OK 後も発注前で止まる。
#
# 方針:
#   - 既存 low_movement_entry_guard_patch の判定本体は維持する。
#   - _range_pct_from_row() だけを補助し、TONOSAMA かつ score/volume 条件を満たす時だけ
#     疑似range_pctを返す。
#   - min price はユーザー方針に合わせ、TONOSAMAだけ既定300円へ下げる。
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
        return float(str(v).replace(",", ""))
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


def _score(row: dict) -> float:
    vals = [
        row.get("_tonosama_score"),
        row.get("pending_score"),
        row.get("score"),
        row.get("final_score"),
        row.get("score_buy"),
        row.get("score_sell"),
    ]
    return max(abs(_sf(v, 0.0)) for v in vals)


def _volume_signal(row: dict) -> float:
    vals = [
        row.get("_max_volume_surge_ratio"),
        row.get("volume_surge_ratio"),
        row.get("volume_speed"),
        row.get("dominant_ratio"),
    ]
    return max(_sf(v, 0.0) for v in vals)


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

        score = _score(row if isinstance(row, dict) else {})
        volume_sig = _volume_signal(row if isinstance(row, dict) else {})
        min_score = _env_float("LOW_MOVE_TONOSAMA_NO_HIGHLOW_MIN_SCORE", 0.01)
        min_vol = _env_float("LOW_MOVE_TONOSAMA_NO_HIGHLOW_MIN_VOLUME_SIGNAL", 1.0)
        if score < min_score or volume_sig < min_vol:
            logger.warning(
                "[LOW MOVE TONOSAMA FALLBACK] no high/low denied score=%.4f min_score=%.4f volume_signal=%.4f min_volume_signal=%.4f",
                score,
                min_score,
                volume_sig,
                min_vol,
            )
            return 0.0

        fallback = _env_float("LOW_MOVE_TONOSAMA_NO_HIGHLOW_FALLBACK_RANGE_PCT", _env_float("LOW_MOVE_TONOSAMA_STRONG_RANGE_PCT", 0.012))
        logger.warning(
            "[LOW MOVE TONOSAMA FALLBACK] use fallback range_pct=%.4f score=%.4f volume_signal=%.4f symbol=%s",
            fallback,
            score,
            volume_sig,
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
        # TONOSAMAだけは低位株除外を300円基準にする。
        # 通常SUMMARY/RANKINGは既存LOW_MOVE_MIN_ENTRY_PRICEを維持。
        os.environ.setdefault("LOW_MOVE_TONOSAMA_MIN_ENTRY_PRICE", "300")
        os.environ.setdefault("LOW_MOVE_TONOSAMA_ALLOW_NO_HIGHLOW_FALLBACK", "1")
        os.environ.setdefault("LOW_MOVE_TONOSAMA_NO_HIGHLOW_FALLBACK_RANGE_PCT", "0.012")

        import core.startup.low_movement_entry_guard_patch as lm
        cur = getattr(lm, "_range_pct_from_row", None)
        if not callable(cur):
            logger.warning("[LOW MOVE TONOSAMA FALLBACK] target missing")
            return False
        if getattr(cur, "_tonosama_no_highlow_fallback_patch", False):
            _INSTALLED = True
            return True
        _ORIG_RANGE_FN = cur
        _patched_range_pct_from_row._tonosama_no_highlow_fallback_patch = True  # type: ignore[attr-defined]
        _patched_range_pct_from_row._original = cur  # type: ignore[attr-defined]
        lm._range_pct_from_row = _patched_range_pct_from_row
        _INSTALLED = True
        logger.warning(
            "[LOW MOVE TONOSAMA FALLBACK] installed v1 min_price=%s fallback_range=%s",
            os.environ.get("LOW_MOVE_TONOSAMA_MIN_ENTRY_PRICE"),
            os.environ.get("LOW_MOVE_TONOSAMA_NO_HIGHLOW_FALLBACK_RANGE_PCT"),
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
