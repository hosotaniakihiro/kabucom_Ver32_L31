# ============================================================
# File   : core/startup/entry_ranking_scalp_order_rescue_patch.py
# Version: V1.0-RANKING-SCALP-ORDER-RESCUE
# ------------------------------------------------------------
# Purpose:
#   ランキング候補が ENTRY許可 まで進むが、実注文前に
#   RANGE_5M_FILTER_NG / ranking AI model missing で落ちる問題を救済する。
#
# Safety:
#   - SELL_CREDIT_GUARD_NG は回避しない。空売り不可銘柄は引き続き落とす。
#   - ATR_1M_FILTER は維持。
#   - RANKING由来だけ対象。
#   - 5分レンジ救済は 0.45%以上の値動きがあるものだけ。
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)
_INSTALLED = False


def _env_bool(name: str, default: bool) -> bool:
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
        if v is None or v == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _source_is_ranking(row: dict) -> bool:
    try:
        src = str(row.get("source") or row.get("entry_type") or "").strip().upper()
        et = str(row.get("entry_type") or "").strip().upper()
        return src == "RANKING" or et == "RANKING" or et == "RANKING_5S"
    except Exception:
        return False


def _side(row: dict) -> str:
    return str(row.get("entry_decision") or row.get("side") or "").strip().upper()


def _price(row: dict) -> float:
    return _safe_float(row.get("close") or row.get("close_price") or row.get("price") or row.get("current_price"), 0.0)


def _range_ratio(row: dict) -> float:
    high = _safe_float(row.get("high") or row.get("high_price"), 0.0)
    low = _safe_float(row.get("low") or row.get("low_price"), 0.0)
    px = _price(row)
    if high > 0 and low > 0 and px > 0 and high >= low:
        return (high - low) / px
    return 0.0


def _score_for_side(row: dict, side: str) -> float:
    if side == "SELL":
        return max(_safe_float(row.get("score_sell"), 0.0), abs(_safe_float(row.get("score"), 0.0)))
    return max(_safe_float(row.get("score_buy"), 0.0), _safe_float(row.get("score"), 0.0))


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    if not _env_bool("ENTRY_RANKING_SCALP_ORDER_RESCUE_ENABLED", True):
        logger.warning("[ENTRY RANKING SCALP RESCUE] disabled by env")
        return False
    try:
        import trading.handlers.entry_controller as ec
        import trading.filters.volatility_filter as vf
        import AI.entry_gate as eg

        old_range = getattr(ec, "range_5m_filter", None)
        old_ai_check = getattr(ec, "ai_final_entry_check", None)
        old_vf_range = getattr(vf, "range_5m_filter", None)
        old_eg_ai_check = getattr(eg, "ai_final_entry_check", None)

        if getattr(old_range, "_entry_ranking_scalp_rescue_v1", False):
            _INSTALLED = True
            return True

        def _patched_range_5m_filter(entry_row: dict) -> bool:
            if callable(old_range):
                try:
                    ok = bool(old_range(entry_row))
                    if ok:
                        return True
                except Exception:
                    logger.exception("[ENTRY RANKING SCALP RESCUE] original range_5m_filter failed")
                    return False

            try:
                if not _source_is_ranking(entry_row):
                    return False
                px = _price(entry_row)
                ratio = _range_ratio(entry_row)
                min_ratio = _env_float("ENTRY_RANKING_SCALP_RANGE_MIN_PCT", 0.0045)
                max_price = _env_float("ENTRY_RANKING_SCALP_MAX_PRICE", 7000.0)
                score_min = _env_float("ENTRY_RANKING_SCALP_MIN_SCORE", 50.0)
                side = _side(entry_row)
                score = _score_for_side(entry_row, side)
                if px > 0 and px <= max_price and ratio >= min_ratio and score >= score_min:
                    logger.warning(
                        "[ENTRY RANKING SCALP RESCUE] RANGE_5M rescued symbol=%s side=%s price=%.2f range_ratio=%.5f min=%.5f score=%.2f",
                        entry_row.get("symbol"), side, px, ratio, min_ratio, score,
                    )
                    return True
            except Exception:
                logger.exception("[ENTRY RANKING SCALP RESCUE] range rescue failed")
            return False

        def _ranking_ai_fallback(entry_row: dict) -> dict | None:
            try:
                if not _source_is_ranking(entry_row):
                    return None
                side = _side(entry_row)
                if side not in {"BUY", "SELL"}:
                    return None
                score = _score_for_side(entry_row, side)
                min_score = _env_float("ENTRY_RANKING_SCALP_MIN_SCORE", 50.0)
                if score < min_score:
                    return None
                reason = f"RANKING_SCALP_RULE_PASS|score={score:.2f}|model_missing_fallback=1"
                if side == "SELL":
                    return {"allow": True, "confidence": 1.0, "reason": reason, "lot_multiplier": 1.0}
                return {"allow": True, "confidence": 1.0, "reason": reason, "lot_multiplier": 1.0}
            except Exception:
                logger.exception("[ENTRY RANKING SCALP RESCUE] ai fallback build failed")
                return None

        def _patched_ai_final_entry_check(entry_row: dict):
            ret = None
            if callable(old_ai_check):
                try:
                    ret = old_ai_check(entry_row)
                    if isinstance(ret, dict) and bool(ret.get("allow", False)):
                        return ret
                    reason = str((ret or {}).get("reason") if isinstance(ret, dict) else ret)
                    # model not found / invalid だけ救済。AIが明確にNGを返した時は救済しすぎない。
                    if "model not found" not in reason and "MODEL" not in reason.upper() and "not found" not in reason.lower():
                        fb = _ranking_ai_fallback(entry_row)
                        if fb is not None and _env_bool("ENTRY_RANKING_SCALP_AI_FALLBACK_ANY_NG", False):
                            logger.warning("[ENTRY RANKING SCALP RESCUE] AI any-ng fallback symbol=%s original=%s", entry_row.get("symbol"), reason)
                            return fb
                        return ret
                except Exception as e:
                    reason = str(e)
                    if "model" not in reason.lower() and "not found" not in reason.lower():
                        logger.exception("[ENTRY RANKING SCALP RESCUE] original ai_final_entry_check failed")
                        return {"allow": False, "confidence": 0.0, "reason": f"AI_EXCEPTION:{e}"}

            fb = _ranking_ai_fallback(entry_row)
            if fb is not None:
                logger.warning("[ENTRY RANKING SCALP RESCUE] ranking AI fallback allow symbol=%s side=%s score=%.2f", entry_row.get("symbol"), _side(entry_row), _score_for_side(entry_row, _side(entry_row)))
                return fb
            return ret if isinstance(ret, dict) else {"allow": False, "confidence": 0.0, "reason": "AI_FALLBACK_NOT_APPLICABLE"}

        _patched_range_5m_filter._entry_ranking_scalp_rescue_v1 = True  # type: ignore[attr-defined]
        _patched_ai_final_entry_check._entry_ranking_scalp_rescue_v1 = True  # type: ignore[attr-defined]

        # entry_controller imports functions directly, so patch both module and imported aliases.
        ec.range_5m_filter = _patched_range_5m_filter
        ec.ai_final_entry_check = _patched_ai_final_entry_check
        if callable(old_vf_range):
            vf.range_5m_filter = _patched_range_5m_filter
        if callable(old_eg_ai_check):
            eg.ai_final_entry_check = _patched_ai_final_entry_check

        os.environ.setdefault("ENTRY_RANKING_SCALP_ORDER_RESCUE_ENABLED", "1")
        os.environ.setdefault("ENTRY_RANKING_SCALP_RANGE_MIN_PCT", "0.0045")
        os.environ.setdefault("ENTRY_RANKING_SCALP_MIN_SCORE", "50")
        os.environ.setdefault("ENTRY_RANKING_SCALP_MAX_PRICE", "7000")

        _INSTALLED = True
        logger.warning(
            "[ENTRY RANKING SCALP RESCUE] installed range_min=%s score_min=%s max_price=%s ai_any_ng=%s",
            os.environ.get("ENTRY_RANKING_SCALP_RANGE_MIN_PCT"),
            os.environ.get("ENTRY_RANKING_SCALP_MIN_SCORE"),
            os.environ.get("ENTRY_RANKING_SCALP_MAX_PRICE"),
            os.environ.get("ENTRY_RANKING_SCALP_AI_FALLBACK_ANY_NG", "0"),
        )
        return True
    except Exception:
        logger.exception("[ENTRY RANKING SCALP RESCUE] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[ENTRY RANKING SCALP RESCUE] auto install failed")


__all__ = ["install"]
