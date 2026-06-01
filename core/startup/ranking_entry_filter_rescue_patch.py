# ============================================================
# File   : core/startup/ranking_entry_filter_rescue_patch.py
# Version: V1.0-RANKING-STRONG-SCORE-TECH-FILTER-RESCUE
# ------------------------------------------------------------
# 目的:
#   ranking_entry_fast_runtime_patch v5 で prefilter は高速化されたが、
#   _passes_ranking_only_filters() が全候補を filter_reject し、
#   candidates=0 / pending=0 になるケースを救済する。
#
# 方針:
#   - SCORE_NG や流動性NGは救済しない
#   - RANKING_TECH_* 系の技術理由だけで落ちた場合、
#     rank上位・売買代金/出来高十分・day変化あり・score十分なら候補に残す
#   - 最終の final_entry_safety_guard / board / position / credit guard は維持
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)
_INSTALLED = False
_ORIG = None


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


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).replace(",", "").replace("%", ""))
    except Exception:
        return float(default)


def _safe_int(v: Any, default: int = 999999) -> int:
    try:
        return int(_safe_float(v, float(default)))
    except Exception:
        return int(default)


def _rescue_allowed(row: dict[str, Any], side: str, score: float, reason: Any) -> tuple[bool, dict[str, Any]]:
    reason_s = str(reason or "")
    rank = _safe_int(row.get("rank_position") or row.get("rank"), 999999)
    price = _safe_float(row.get("price") or row.get("current_price"), 0.0)
    volume = _safe_float(row.get("volume") or row.get("trading_volume"), 0.0)
    turnover = _safe_float(row.get("turnover") or row.get("trading_value"), 0.0)
    day = _safe_float(row.get("day_change_pct") or row.get("change_percentage") or row.get("change_rate"), 0.0)
    rt = str(row.get("rank_type") or row.get("ranking_type") or "")

    min_score = _env_float("RANKING_ENTRY_RESCUE_MIN_SCORE", 60.0)
    max_rank = _env_int("RANKING_ENTRY_RESCUE_MAX_RANK", 10)
    min_turnover = _env_float("RANKING_ENTRY_RESCUE_MIN_TURNOVER", 100000000.0)
    min_volume = _env_float("RANKING_ENTRY_RESCUE_MIN_VOLUME", 30000.0)
    min_abs_day = _env_float("RANKING_ENTRY_RESCUE_MIN_ABS_DAY_PCT", 3.0)
    min_price = _env_float("RANKING_ENTRY_RESCUE_MIN_PRICE", 300.0)
    max_price = _env_float("RANKING_ENTRY_RESCUE_MAX_PRICE", 7000.0)

    diag = {
        "reason": reason_s,
        "rank": rank,
        "rank_type": rt,
        "side": side,
        "score": score,
        "price": price,
        "volume": volume,
        "turnover": turnover,
        "day": day,
        "min_score": min_score,
    }

    if not reason_s.startswith("RANKING_TECH_"):
        return False, {**diag, "ng": "not_tech_reason"}
    if score < min_score:
        return False, {**diag, "ng": "score_low"}
    if rank > max_rank:
        return False, {**diag, "ng": "rank_low"}
    if price < min_price or price > max_price:
        return False, {**diag, "ng": "price_range"}
    if volume < min_volume:
        return False, {**diag, "ng": "volume_low"}
    if turnover < min_turnover:
        return False, {**diag, "ng": "turnover_low"}
    if abs(day) < min_abs_day:
        return False, {**diag, "ng": "day_move_low"}
    if side == "BUY" and day <= 0:
        return False, {**diag, "ng": "buy_day_not_positive"}
    if side == "SELL" and day >= 0:
        return False, {**diag, "ng": "sell_day_not_negative"}
    return True, {**diag, "rescue": True}


def _patched_passes_ranking_only_filters(row, side, prev_h, score, parts):
    ok, reason = _ORIG(row, side, prev_h, score, parts)  # type: ignore[misc]
    if ok:
        return ok, reason
    try:
        if not _env_bool("RANKING_ENTRY_STRONG_TECH_RESCUE_ENABLED", True):
            return ok, reason
        if not isinstance(row, dict):
            return ok, reason
        allowed, diag = _rescue_allowed(row, str(side or ""), _safe_float(score, 0.0), reason)
        if allowed:
            row["ranking_filter_rescue"] = True
            row["ranking_filter_rescue_reason"] = str(reason or "")
            row["ranking_filter_rescue_diag"] = str(diag)
            logger.warning("[RANKING FILTER RESCUE] allow symbol=%s side=%s diag=%s", row.get("symbol"), side, diag)
            return True, "RANKING_TECH_RESCUE"
        return ok, reason
    except Exception:
        logger.exception("[RANKING FILTER RESCUE] wrapper failed symbol=%s", row.get("symbol") if isinstance(row, dict) else None)
        return ok, reason


def install() -> bool:
    global _INSTALLED, _ORIG
    if _INSTALLED:
        return True
    try:
        import trading.ranking.entry_from_ranking as efr
        cur = getattr(efr, "_passes_ranking_only_filters", None)
        if not callable(cur):
            logger.warning("[RANKING FILTER RESCUE] target unavailable")
            return False
        if getattr(cur, "_ranking_filter_rescue_v1", False):
            _INSTALLED = True
            return True
        _ORIG = cur
        _patched_passes_ranking_only_filters._ranking_filter_rescue_v1 = True  # type: ignore[attr-defined]
        _patched_passes_ranking_only_filters._original = cur  # type: ignore[attr-defined]
        efr._passes_ranking_only_filters = _patched_passes_ranking_only_filters
        _INSTALLED = True
        logger.warning(
            "[RANKING FILTER RESCUE] installed enabled=%s min_score=%.1f max_rank=%s min_turnover=%.0f min_abs_day=%.2f",
            _env_bool("RANKING_ENTRY_STRONG_TECH_RESCUE_ENABLED", True),
            _env_float("RANKING_ENTRY_RESCUE_MIN_SCORE", 60.0),
            _env_int("RANKING_ENTRY_RESCUE_MAX_RANK", 10),
            _env_float("RANKING_ENTRY_RESCUE_MIN_TURNOVER", 100000000.0),
            _env_float("RANKING_ENTRY_RESCUE_MIN_ABS_DAY_PCT", 3.0),
        )
        return True
    except Exception:
        logger.exception("[RANKING FILTER RESCUE] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[RANKING FILTER RESCUE] auto install failed")

__all__ = ["install"]
