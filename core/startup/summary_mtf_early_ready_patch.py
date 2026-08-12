# ============================================================
# File   : core/startup/summary_mtf_early_ready_patch.py
# Version: V1.4-SUMMARY-MTF-EARLY-READY-CHAIN-ORDER-RANGE-REPAIR
# ------------------------------------------------------------
# 【目的】
#   SUMMARY 3m/5m の AI entry が
#     summary_mtf_not_ready:hist_short:<14
#   で全落ちし、5MAを超えた初動を拾えない問題を緩和する。
#
# 【V1.4追加】
#   - SUMMARY_AI direct snapshot 経路で entry_order_builder に渡る row が
#     high == low == close のままになり、LOW_MOVE_RANGE_TOO_SMALL で
#     実発注直前に落ちる問題を防ぐため、
#     summary_ai_order_builder_range_repair_patch を連鎖installする。
#   - 低変動ガードは緩和しない。履歴summaryの day_high/day_low 等で
#     レンジを補完してから従来の strict guard を再実行する。
#
# 【V1.3追加】
#   - main.py memory_only 運用で 3m/5m PUSH summary が空になる場合に備え、
#     summary_mtf_push_raw_fallback_patch を連鎖installする。
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_PATCHED = False
_ORIGINAL_SUMMARY_MTF_STATUS = None
_CONTROLLER_CACHE_PATCHED = False
_NEUTRAL_SCORE_GUARD_INSTALLED = False
_PUSH_RAW_FALLBACK_INSTALLED = False
_ORDER_RANGE_REPAIR_INSTALLED = False


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


def _install_neutral_score_guard() -> bool:
    global _NEUTRAL_SCORE_GUARD_INSTALLED
    if _NEUTRAL_SCORE_GUARD_INSTALLED:
        return True
    try:
        from core.startup import summary_controller_neutral_score_guard_patch as neutral_guard
        ok = bool(neutral_guard.install())
        _NEUTRAL_SCORE_GUARD_INSTALLED = ok
        logger.warning("[SUMMARY MTF EARLY READY PATCH] neutral score guard installed=%s", ok)
        return ok
    except Exception:
        logger.exception("[SUMMARY MTF EARLY READY PATCH] neutral score guard install failed")
        return False


def _install_push_raw_fallback() -> bool:
    global _PUSH_RAW_FALLBACK_INSTALLED
    if _PUSH_RAW_FALLBACK_INSTALLED:
        return True
    try:
        from core.startup import summary_mtf_push_raw_fallback_patch as raw_fallback
        ok = bool(raw_fallback.install())
        _PUSH_RAW_FALLBACK_INSTALLED = ok
        logger.warning("[SUMMARY MTF EARLY READY PATCH] push raw mtf fallback installed=%s", ok)
        return ok
    except Exception:
        logger.exception("[SUMMARY MTF EARLY READY PATCH] push raw mtf fallback install failed")
        return False


def _install_order_builder_range_repair() -> bool:
    global _ORDER_RANGE_REPAIR_INSTALLED
    if _ORDER_RANGE_REPAIR_INSTALLED:
        return True
    try:
        from core.startup import summary_ai_order_builder_range_repair_patch as order_range_repair
        ok = bool(order_range_repair.install())
        _ORDER_RANGE_REPAIR_INSTALLED = ok
        logger.warning("[SUMMARY MTF EARLY READY PATCH] summary_ai order builder range repair installed=%s", ok)
        return ok
    except Exception:
        logger.exception("[SUMMARY MTF EARLY READY PATCH] summary_ai order builder range repair install failed")
        return False


def _install_controller_cache_history_payload_patch() -> bool:
    """3m/5m merged cache が latest-only になるのを防ぐ。"""
    global _CONTROLLER_CACHE_PATCHED
    if _CONTROLLER_CACHE_PATCHED:
        return True

    try:
        import pandas as pd
        import trading.summary.controller_cache as cc

        old_fn = getattr(cc, "choose_merged_cache_payload", None)
        if not callable(old_fn):
            logger.warning("[SUMMARY MTF EARLY READY PATCH] controller_cache.choose_merged_cache_payload not callable")
            return False
        if getattr(old_fn, "_history_payload_patch_v1", False):
            _CONTROLLER_CACHE_PATCHED = True
            return True

        def _choose_merged_cache_payload_history_first(interval, df_hist, df_latest, normalize_fn):
            try:
                payload = cc.limit_history_rows_per_symbol(df_hist, int(interval), normalize_fn=normalize_fn)
                payload = cc.dedupe_symbol_datetime(payload, normalize_fn=normalize_fn)
                payload = cc.attach_display_ready(payload)

                try:
                    from core.startup.summary_controller_neutral_score_guard_patch import neutralize_summary_scores
                    payload = neutralize_summary_scores(payload, context=f"choose_merged_cache_payload:{interval}")
                except Exception:
                    pass

                if isinstance(payload, pd.DataFrame) and not payload.empty:
                    logger.warning(
                        "[SUMMARY CACHE HISTORY PAYLOAD PATCH] interval=%s use history payload rows=%s symbols=%s latest_dt=%s display_ready=%s",
                        interval,
                        len(payload),
                        cc._safe_symbol_count(payload),
                        cc._safe_latest_dt(payload),
                        cc._safe_display_ready_count(payload),
                    )
                    return payload

                logger.warning("[SUMMARY CACHE HISTORY PAYLOAD PATCH] interval=%s history empty -> fallback old latest payload", interval)
                return old_fn(interval, df_hist, df_latest, normalize_fn)
            except Exception:
                logger.exception("[SUMMARY CACHE HISTORY PAYLOAD PATCH] failed interval=%s -> fallback old", interval)
                return old_fn(interval, df_hist, df_latest, normalize_fn)

        _choose_merged_cache_payload_history_first._history_payload_patch_v1 = True  # type: ignore[attr-defined]
        _choose_merged_cache_payload_history_first._original = old_fn  # type: ignore[attr-defined]
        cc.choose_merged_cache_payload = _choose_merged_cache_payload_history_first

        _CONTROLLER_CACHE_PATCHED = True
        logger.warning("[SUMMARY MTF EARLY READY PATCH] controller_cache history payload patch installed: merged cache keeps history for 1m/3m/5m")
        return True
    except Exception:
        logger.exception("[SUMMARY MTF EARLY READY PATCH] controller_cache history payload patch install failed")
        return False


def install() -> bool:
    global _PATCHED, _ORIGINAL_SUMMARY_MTF_STATUS

    neutral_ok = _install_neutral_score_guard()
    controller_ok = _install_controller_cache_history_payload_patch()
    raw_fallback_ok = _install_push_raw_fallback()
    order_range_ok = _install_order_builder_range_repair()

    if _PATCHED:
        return bool(controller_ok or neutral_ok or raw_fallback_ok or order_range_ok)

    try:
        import AI.entry_gate as target

        cur = getattr(target, "_summary_mtf_status", None)
        if not callable(cur):
            logger.warning("[SUMMARY MTF EARLY READY PATCH] target _summary_mtf_status not callable")
            return bool(controller_ok or neutral_ok or raw_fallback_ok or order_range_ok)
        if getattr(cur, "_summary_mtf_early_ready_patch", False):
            _PATCHED = True
            return bool(controller_ok or neutral_ok or raw_fallback_ok or order_range_ok)

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
        logger.warning(
            "[SUMMARY MTF EARLY READY PATCH] installed controller_cache_history_payload=%s neutral_score_guard=%s push_raw_fallback=%s order_range_repair=%s",
            controller_ok,
            neutral_ok,
            raw_fallback_ok,
            order_range_ok,
        )
        return True
    except Exception:
        logger.exception("[SUMMARY MTF EARLY READY PATCH] install failed")
        return bool(controller_ok or neutral_ok or raw_fallback_ok or order_range_ok)


__all__ = ["install"]
