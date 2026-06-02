# ============================================================
# File   : core/startup/ranking_entry_filter_rescue_patch.py
# Version: V1.4-RELAX-RECURSION-MAX-RANK
# ------------------------------------------------------------
# 目的:
#   ranking_entry_fast_runtime_patch v5 で prefilter は高速化されたが、
#   _passes_ranking_only_filters() が FLAT_PRICE_FILTER_RECURSION を返し、
#   高流動性・上位rankの候補まで rank_low で落ちるケースを救済する。
#
# V1.4:
#   - 再帰系救済の既定 max_rank を 5 -> 10 に緩和。
#   - min_score=55 / min_turnover=1億 / min_volume=3万は維持。
#   - 価格範囲 300〜7000円、方向一致 BUY day>=0 / SELL day<=0 は維持。
#   - final_entry_safety_guard / board / credit / position guard は後段で維持。
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any, Callable

logger = logging.getLogger(__name__)
_INSTALLED = False
_ORIG: Callable[..., tuple[bool, str]] | None = None
_IN_FILTER = False


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


def _reason_rescuable(reason_s: str) -> bool:
    if reason_s.startswith("RANKING_TECH_"):
        return True
    if reason_s in {
        "FILTER_RECURSION",
        "ORIGINAL_FILTER_RECURSION",
        "ORIGINAL_FILTER_UNAVAILABLE",
        "FLAT_PRICE_FILTER_RECURSION",
    }:
        return True
    if _env_bool("RANKING_ENTRY_RESCUE_FLAT_PRICE_REASON", True):
        if reason_s.startswith("BUY_PRICE_NOT_UP") or reason_s.startswith("SELL_PRICE_NOT_DOWN"):
            return True
    return False


def _is_recursion_reason(reason_s: str) -> bool:
    return reason_s in {"FILTER_RECURSION", "ORIGINAL_FILTER_RECURSION", "FLAT_PRICE_FILTER_RECURSION"}


def _first(row: dict[str, Any], keys: tuple[str, ...], default: Any = None) -> Any:
    for k in keys:
        try:
            v = row.get(k)
            if v is not None and str(v).strip() != "":
                return v
        except Exception:
            pass
    return default


def _rescue_allowed(row: dict[str, Any], side: str, score: float, reason: Any) -> tuple[bool, dict[str, Any]]:
    reason_s = str(reason or "")
    recursion_reason = _is_recursion_reason(reason_s)

    rank = _safe_int(_first(row, ("rank_position", "rank", "No", "no"), 999999), 999999)
    price = _safe_float(_first(row, ("price", "current_price", "CurrentPrice", "close"), 0.0), 0.0)
    volume = _safe_float(_first(row, ("volume", "trading_volume", "TradingVolume"), 0.0), 0.0)
    turnover = _safe_float(_first(row, ("turnover", "trading_value", "Turnover"), 0.0), 0.0)
    day = _safe_float(_first(row, ("day_change_pct", "change_percentage", "change_rate", "ChangePercentage", "ChangeRatio"), 0.0), 0.0)
    rt = str(_first(row, ("rank_type", "ranking_type", "CategoryName"), ""))
    side_u = str(side or "").upper().strip()

    if turnover <= 0 and price > 0 and volume > 0:
        turnover = price * volume

    min_score = _env_float("RANKING_ENTRY_RESCUE_RECURSION_MIN_SCORE" if recursion_reason else "RANKING_ENTRY_RESCUE_MIN_SCORE", 55.0 if recursion_reason else 60.0)
    max_rank = _env_int("RANKING_ENTRY_RESCUE_RECURSION_MAX_RANK" if recursion_reason else "RANKING_ENTRY_RESCUE_MAX_RANK", 10 if recursion_reason else 10)
    min_turnover = _env_float("RANKING_ENTRY_RESCUE_RECURSION_MIN_TURNOVER" if recursion_reason else "RANKING_ENTRY_RESCUE_MIN_TURNOVER", 100000000.0)
    min_volume = _env_float("RANKING_ENTRY_RESCUE_RECURSION_MIN_VOLUME" if recursion_reason else "RANKING_ENTRY_RESCUE_MIN_VOLUME", 30000.0)
    min_abs_day = _env_float("RANKING_ENTRY_RESCUE_RECURSION_MIN_ABS_DAY_PCT" if recursion_reason else "RANKING_ENTRY_RESCUE_MIN_ABS_DAY_PCT", 0.0 if recursion_reason else 3.0)
    min_price = _env_float("RANKING_ENTRY_RESCUE_MIN_PRICE", 300.0)
    max_price = _env_float("RANKING_ENTRY_RESCUE_MAX_PRICE", 7000.0)

    diag = {
        "reason": reason_s,
        "recursion_reason": recursion_reason,
        "rank": rank,
        "rank_type": rt,
        "side": side_u,
        "score": score,
        "price": price,
        "volume": volume,
        "turnover": turnover,
        "day": day,
        "min_score": min_score,
        "max_rank": max_rank,
        "min_turnover": min_turnover,
        "min_volume": min_volume,
    }

    if not _reason_rescuable(reason_s):
        return False, {**diag, "ng": "not_rescuable_reason"}
    if score < min_score:
        return False, {**diag, "ng": "score_low"}
    if rank > max_rank:
        return False, {**diag, "ng": "rank_low"}
    if price > 0 and (price < min_price or price > max_price):
        return False, {**diag, "ng": "price_range"}
    if volume > 0 and volume < min_volume:
        return False, {**diag, "ng": "volume_low"}
    if turnover > 0 and turnover < min_turnover:
        return False, {**diag, "ng": "turnover_low"}
    if min_abs_day > 0 and abs(day) < min_abs_day:
        return False, {**diag, "ng": "day_move_low"}

    if not recursion_reason:
        if side_u == "BUY" and day <= 0:
            return False, {**diag, "ng": "buy_day_not_positive"}
        if side_u == "SELL" and day >= 0:
            return False, {**diag, "ng": "sell_day_not_negative"}
    else:
        if day != 0:
            if side_u == "BUY" and day < 0:
                return False, {**diag, "ng": "buy_day_negative"}
            if side_u == "SELL" and day > 0:
                return False, {**diag, "ng": "sell_day_positive"}

    return True, {**diag, "rescue": True}


def _try_rescue(row: Any, side: str, score: float, reason: Any) -> tuple[bool, str]:
    try:
        if not _env_bool("RANKING_ENTRY_STRONG_TECH_RESCUE_ENABLED", True):
            return False, str(reason or "RESCUE_DISABLED")
        if not isinstance(row, dict):
            return False, str(reason or "ROW_NOT_DICT")
        allowed, diag = _rescue_allowed(row, str(side or ""), _safe_float(score, 0.0), reason)
        if allowed:
            row["ranking_filter_rescue"] = True
            row["ranking_filter_rescue_reason"] = str(reason or "")
            row["ranking_filter_rescue_diag"] = str(diag)
            logger.warning("[RANKING FILTER RESCUE] allow symbol=%s side=%s diag=%s", row.get("symbol") or row.get("Symbol"), side, diag)
            return True, "RANKING_FILTER_RESCUE"
        logger.info("[RANKING FILTER RESCUE] reject symbol=%s side=%s diag=%s", row.get("symbol") or row.get("Symbol"), side, diag)
        return False, str(reason or "RESCUE_NG")
    except Exception:
        logger.exception("[RANKING FILTER RESCUE] rescue check failed symbol=%s", row.get("symbol") if isinstance(row, dict) else None)
        return False, str(reason or "RESCUE_EXCEPTION")


def _patched_passes_ranking_only_filters(row, side, prev_h, score, parts):
    global _IN_FILTER
    if _IN_FILTER:
        ok, reason = _try_rescue(row, side, score, "FILTER_RECURSION")
        return ok, reason

    _IN_FILTER = True
    try:
        ok = False
        reason: Any = "ORIGINAL_FILTER_UNAVAILABLE"
        try:
            if callable(_ORIG):
                ok, reason = _ORIG(row, side, prev_h, score, parts)
        except RecursionError:
            logger.warning("[RANKING FILTER RESCUE] original filter recursion detected symbol=%s side=%s", row.get("symbol") if isinstance(row, dict) else None, side)
            ok, reason = False, "ORIGINAL_FILTER_RECURSION"
        if ok:
            return ok, reason
        rescue_ok, rescue_reason = _try_rescue(row, side, score, reason)
        if rescue_ok:
            return True, rescue_reason
        return False, reason
    finally:
        _IN_FILTER = False


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
        if getattr(cur, "_ranking_filter_rescue_v14", False):
            _INSTALLED = True
            return True
        _ORIG = cur
        _patched_passes_ranking_only_filters._ranking_filter_rescue_v14 = True  # type: ignore[attr-defined]
        _patched_passes_ranking_only_filters._ranking_filter_rescue_v13 = True  # type: ignore[attr-defined]
        _patched_passes_ranking_only_filters._original = cur  # type: ignore[attr-defined]
        efr._passes_ranking_only_filters = _patched_passes_ranking_only_filters
        _INSTALLED = True
        logger.warning(
            "[RANKING FILTER RESCUE] installed v1.4 recursion_safe=True flat_recursion_rescue=True enabled=%s min_score=%.1f recursion_min_score=%.1f max_rank=%s recursion_max_rank=%s recursion_min_turnover=%.0f",
            _env_bool("RANKING_ENTRY_STRONG_TECH_RESCUE_ENABLED", True),
            _env_float("RANKING_ENTRY_RESCUE_MIN_SCORE", 60.0),
            _env_float("RANKING_ENTRY_RESCUE_RECURSION_MIN_SCORE", 55.0),
            _env_int("RANKING_ENTRY_RESCUE_MAX_RANK", 10),
            _env_int("RANKING_ENTRY_RESCUE_RECURSION_MAX_RANK", 10),
            _env_float("RANKING_ENTRY_RESCUE_RECURSION_MIN_TURNOVER", 100000000.0),
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
