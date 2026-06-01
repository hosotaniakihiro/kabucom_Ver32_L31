# ============================================================
# File   : core/startup/ranking_entry_filter_rescue_patch.py
# Version: V1.1-RECURSION-SAFE-RANKING-TECH-RESCUE
# ------------------------------------------------------------
# 目的:
#   ranking_entry_fast_runtime_patch v5 で prefilter は高速化されたが、
#   _passes_ranking_only_filters() が全候補を filter_reject し、
#   candidates=0 / pending=0 になるケースを救済する。
#
# V1.1:
#   - ranking_entry_flat_price_guard_patch と相互ラップして
#     RecursionError になる問題を防ぐ。
#   - original filter が再帰した場合は、強ランキング条件だけで救済可否を判定する。
#   - SCORE_NG/流動性NGは通常は救済しないが、再帰時は reason=FILTER_RECURSION として
#     強条件を満たすものだけ救済する。
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


def _unwrap_no_cycle(fn: Any) -> Any:
    """Best-effort unwrap.  循環していたら最後の安全な関数で止める。"""
    seen: set[int] = set()
    cur = fn
    last = fn
    try:
        while callable(cur) and hasattr(cur, "_original"):
            if id(cur) in seen:
                return last
            seen.add(id(cur))
            last = cur
            nxt = getattr(cur, "_original", None)
            if not callable(nxt):
                return cur
            cur = nxt
        return cur
    except Exception:
        return fn


def _reason_rescuable(reason_s: str) -> bool:
    if reason_s.startswith("RANKING_TECH_"):
        return True
    if reason_s in {"FILTER_RECURSION", "ORIGINAL_FILTER_RECURSION", "ORIGINAL_FILTER_UNAVAILABLE"}:
        return True
    if _env_bool("RANKING_ENTRY_RESCUE_FLAT_PRICE_REASON", True):
        if reason_s.startswith("BUY_PRICE_NOT_UP") or reason_s.startswith("SELL_PRICE_NOT_DOWN"):
            return True
    return False


def _rescue_allowed(row: dict[str, Any], side: str, score: float, reason: Any) -> tuple[bool, dict[str, Any]]:
    reason_s = str(reason or "")
    rank = _safe_int(row.get("rank_position") or row.get("rank"), 999999)
    price = _safe_float(row.get("price") or row.get("current_price") or row.get("close"), 0.0)
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

    if not _reason_rescuable(reason_s):
        return False, {**diag, "ng": "not_rescuable_reason"}
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
            logger.warning("[RANKING FILTER RESCUE] allow symbol=%s side=%s diag=%s", row.get("symbol"), side, diag)
            return True, "RANKING_FILTER_RESCUE"
        logger.debug("[RANKING FILTER RESCUE] no rescue symbol=%s side=%s diag=%s", row.get("symbol"), side, diag)
        return False, str(reason or "RESCUE_NG")
    except Exception:
        logger.exception("[RANKING FILTER RESCUE] rescue check failed symbol=%s", row.get("symbol") if isinstance(row, dict) else None)
        return False, str(reason or "RESCUE_EXCEPTION")


def _patched_passes_ranking_only_filters(row, side, prev_h, score, parts):
    global _IN_FILTER
    if _IN_FILTER:
        # 循環呼び出しを検知。originalへ入らず、強条件だけ判定する。
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

        # 既に古い rescue が入っていても、さらに外側から再帰安全版で包む。
        if getattr(cur, "_ranking_filter_rescue_v11", False):
            _INSTALLED = True
            return True

        _ORIG = cur
        _patched_passes_ranking_only_filters._ranking_filter_rescue_v11 = True  # type: ignore[attr-defined]
        _patched_passes_ranking_only_filters._original = cur  # type: ignore[attr-defined]
        efr._passes_ranking_only_filters = _patched_passes_ranking_only_filters

        # flat_price_guard 側が古い rescue を original として掴んでいる場合に備え、
        # 再帰安全版を最終フィルタとして必ず上書きする。
        _INSTALLED = True
        logger.warning(
            "[RANKING FILTER RESCUE] installed v1.1 recursion_safe=True enabled=%s min_score=%.1f max_rank=%s min_turnover=%.0f min_abs_day=%.2f original=%s",
            _env_bool("RANKING_ENTRY_STRONG_TECH_RESCUE_ENABLED", True),
            _env_float("RANKING_ENTRY_RESCUE_MIN_SCORE", 60.0),
            _env_int("RANKING_ENTRY_RESCUE_MAX_RANK", 10),
            _env_float("RANKING_ENTRY_RESCUE_MIN_TURNOVER", 100000000.0),
            _env_float("RANKING_ENTRY_RESCUE_MIN_ABS_DAY_PCT", 3.0),
            getattr(cur, "__name__", repr(cur)),
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
