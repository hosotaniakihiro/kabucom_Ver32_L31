# ============================================================
# File   : core/startup/summary_mtf_early_ready_patch.py
# Version: V1.0-SUMMARY-MTF-EARLY-MA5-READY-FAILOPEN
# ------------------------------------------------------------
# 【目的】
#   SUMMARY 3m/5m の AI entry が
#     summary_mtf_not_ready:hist_short:<14
#   で全落ちし、5MAを超えた初動を拾えない問題を緩和する。
#
# 【背景】
#   AI/entry_gate.py Ver26.33 は SUMMARY 3m/5m で symbol_hist_len < 14 を
#   fail-close する。これは安全だが、ユーザー要望の
#   「3分足/5分足の5MA超えの早い段階で入りたい」と矛盾する。
#
# 【方針】
#   - AI.entry_gate._summary_mtf_status を runtime patch
#   - 元判定が hist_short で block の場合だけ救済判定
#   - technical_ready/display_ready があり、hist が一定以上、MA5/close/slope の方向が合う場合は skip に変換
#   - それ以外は元の block を維持
#
# 【ENV】
#   SUMMARY_MTF_EARLY_READY_ENABLED=1
#   SUMMARY_MTF_EARLY_HIST_MIN=5
#   SUMMARY_MTF_EARLY_MIN_SCORE=1.0
#   SUMMARY_MTF_EARLY_MIN_SLOPE=0.0001
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_PATCHED = False
_ORIGINAL_SUMMARY_MTF_STATUS = None


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
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


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        s = str(v).strip()
        if not s or s.lower() in {"nan", "none", "nat", "<na>"}:
            return default
        return float(s.replace(",", ""))
    except Exception:
        return default


def _bool_like(v: Any, default: bool = False) -> bool:
    try:
        if isinstance(v, bool):
            return v
        if v is None:
            return default
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on", "ok"}:
            return True
        if s in {"0", "false", "no", "n", "off", "ng", ""}:
            return False
        return default
    except Exception:
        return default


def _side_of(row: dict[str, Any]) -> str:
    s = str(row.get("entry_decision") or row.get("side") or "").strip().upper()
    return s if s in {"BUY", "SELL"} else ""


def _score_of(row: dict[str, Any], side: str) -> float:
    if side == "BUY":
        return max(
            _safe_float(row.get("buy_score"), 0.0),
            _safe_float(row.get("score_buy"), 0.0),
            _safe_float(row.get("score"), 0.0),
            _safe_float(row.get("final_score"), 0.0),
        )
    if side == "SELL":
        return max(
            _safe_float(row.get("sell_score"), 0.0),
            _safe_float(row.get("score_sell"), 0.0),
            abs(_safe_float(row.get("score"), 0.0)),
            abs(_safe_float(row.get("final_score"), 0.0)),
        )
    return abs(_safe_float(row.get("score"), 0.0))


def _early_ma5_ready(row: dict[str, Any], *, interval: int, reason: str) -> tuple[bool, str]:
    if not _env_bool("SUMMARY_MTF_EARLY_READY_ENABLED", True):
        return False, "disabled"

    if int(interval or 1) not in {3, 5}:
        return False, "not_3m_5m"

    if "hist_short" not in str(reason or ""):
        return False, "not_hist_short"

    technical_ready = _bool_like(row.get("technical_ready"), False)
    display_ready = _bool_like(row.get("display_ready"), technical_ready)
    if not technical_ready or not display_ready:
        return False, "not_ready_flags"

    hist = _safe_float(row.get("symbol_hist_len"), 0.0)
    min_hist = _env_float("SUMMARY_MTF_EARLY_HIST_MIN", 5.0)
    if hist < min_hist:
        return False, f"hist_too_short:{hist:.0f}<{min_hist:.0f}"

    side = _side_of(row)
    if not side:
        return False, "side_missing"

    close = _safe_float(row.get("close_price") or row.get("close") or row.get("price"), 0.0)
    ma5 = _safe_float(row.get("ma5") or row.get("ma_5"), 0.0)
    slope = _safe_float(row.get("slope_atr_scaled"), _safe_float(row.get("slope"), 0.0))
    score = _score_of(row, side)

    min_score = _env_float("SUMMARY_MTF_EARLY_MIN_SCORE", 1.0)
    min_slope = abs(_env_float("SUMMARY_MTF_EARLY_MIN_SLOPE", 0.0001))

    if score < min_score:
        return False, f"score_low:{score:.3f}<{min_score:.3f}"

    if close <= 0 or ma5 <= 0:
        # ma5がない場合でも、slope方向が強ければ救済する。
        if side == "BUY" and slope >= min_slope:
            return True, f"early_mtf_buy_slope_only:hist={hist:.0f}:slope={slope:.5f}:score={score:.2f}"
        if side == "SELL" and slope <= -min_slope:
            return True, f"early_mtf_sell_slope_only:hist={hist:.0f}:slope={slope:.5f}:score={score:.2f}"
        return False, "ma5_or_close_missing"

    if side == "BUY":
        if close >= ma5 and slope >= -min_slope:
            return True, f"early_mtf_buy_ma5_ready:hist={hist:.0f}:close={close:.2f}:ma5={ma5:.2f}:slope={slope:.5f}:score={score:.2f}"
        return False, f"buy_ma5_direction_ng:close={close:.2f}:ma5={ma5:.2f}:slope={slope:.5f}"

    if side == "SELL":
        if close <= ma5 and slope <= min_slope:
            return True, f"early_mtf_sell_ma5_ready:hist={hist:.0f}:close={close:.2f}:ma5={ma5:.2f}:slope={slope:.5f}:score={score:.2f}"
        return False, f"sell_ma5_direction_ng:close={close:.2f}:ma5={ma5:.2f}:slope={slope:.5f}"

    return False, "side_ng"


def install() -> bool:
    global _PATCHED, _ORIGINAL_SUMMARY_MTF_STATUS
    if _PATCHED:
        return True

    try:
        import AI.entry_gate as target

        cur = getattr(target, "_summary_mtf_status", None)
        if not callable(cur):
            logger.warning("[SUMMARY MTF EARLY READY PATCH] target _summary_mtf_status not callable")
            return False
        if getattr(cur, "_summary_mtf_early_ready_patch", False):
            _PATCHED = True
            return True

        _ORIGINAL_SUMMARY_MTF_STATUS = cur

        def _patched_summary_mtf_status(row: dict, *, source: str, interval: int):
            status, reason = _ORIGINAL_SUMMARY_MTF_STATUS(row, source=source, interval=interval)
            try:
                if source == "SUMMARY" and status == "block":
                    ok, detail = _early_ma5_ready(row, interval=int(interval), reason=str(reason))
                    if ok:
                        logger.warning(
                            "[SUMMARY MTF EARLY READY PATCH] fail-open SUMMARY MTF symbol=%s interval=%s side=%s original=%s detail=%s",
                            row.get("symbol"), interval, _side_of(row), reason, detail,
                        )
                        return "skip", detail
                    logger.info(
                        "[SUMMARY MTF EARLY READY PATCH] keep block symbol=%s interval=%s side=%s original=%s detail=%s",
                        row.get("symbol"), interval, _side_of(row), reason, detail,
                    )
            except Exception:
                logger.exception("[SUMMARY MTF EARLY READY PATCH] decision failed symbol=%s", row.get("symbol") if isinstance(row, dict) else None)
            return status, reason

        _patched_summary_mtf_status._summary_mtf_early_ready_patch = True  # type: ignore[attr-defined]
        target._summary_mtf_status = _patched_summary_mtf_status
        _PATCHED = True
        logger.warning("[SUMMARY MTF EARLY READY PATCH] installed")
        return True
    except Exception:
        logger.exception("[SUMMARY MTF EARLY READY PATCH] install failed")
        return False


__all__ = ["install"]
