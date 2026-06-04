# ============================================================
# File   : core/startup/entry_ranking_scalp_order_rescue_patch.py
# Version: V1.1-RANKING-SCALP-ORDER-RESCUE-WATCHER
# ------------------------------------------------------------
# Purpose:
#   ランキング候補が ENTRY許可 まで進むが、実注文前に
#   RANGE_5M_FILTER_NG / ranking AI model missing で落ちる問題を救済する。
#
# V1.1:
#   - usercustomize後に main runtime の LOW_MOVE_GUARD / ENTRY_FINAL_FILTER が
#     entry_controller.range_5m_filter を再パッチして救済が上書きされるため、
#     watcherで一定時間 re-enforce する。
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
import threading
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)
_INSTALLED = False
_WATCHER_STARTED = False
_ORIGINAL_RANGE: Callable | None = None
_ORIGINAL_AI: Callable | None = None
_PATCHED_RANGE: Callable | None = None
_PATCHED_AI: Callable | None = None


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


def _build_patches(old_range: Callable | None, old_ai_check: Callable | None):
    def _patched_range_5m_filter(entry_row: dict) -> bool:
        # 既存ガードがOKならそのまま通す。
        if callable(old_range) and not getattr(old_range, "_entry_ranking_scalp_rescue_v11", False):
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
            return {"allow": True, "confidence": 1.0, "reason": reason, "lot_multiplier": 1.0}
        except Exception:
            logger.exception("[ENTRY RANKING SCALP RESCUE] ai fallback build failed")
            return None

    def _patched_ai_final_entry_check(entry_row: dict):
        ret = None
        if callable(old_ai_check) and not getattr(old_ai_check, "_entry_ranking_scalp_rescue_v11", False):
            try:
                ret = old_ai_check(entry_row)
                if isinstance(ret, dict) and bool(ret.get("allow", False)):
                    return ret
                reason = str((ret or {}).get("reason") if isinstance(ret, dict) else ret)
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

    _patched_range_5m_filter._entry_ranking_scalp_rescue_v11 = True  # type: ignore[attr-defined]
    _patched_ai_final_entry_check._entry_ranking_scalp_rescue_v11 = True  # type: ignore[attr-defined]
    return _patched_range_5m_filter, _patched_ai_final_entry_check


def _apply_once(*, force_rebuild: bool = False) -> bool:
    global _ORIGINAL_RANGE, _ORIGINAL_AI, _PATCHED_RANGE, _PATCHED_AI
    try:
        import trading.handlers.entry_controller as ec
        import trading.filters.volatility_filter as vf
        import AI.entry_gate as eg

        current_range = getattr(ec, "range_5m_filter", None)
        current_ai = getattr(ec, "ai_final_entry_check", None)

        # 他patchに上書きされたら、その関数を新しい original として包み直す。
        if force_rebuild or _PATCHED_RANGE is None or not getattr(current_range, "_entry_ranking_scalp_rescue_v11", False):
            _ORIGINAL_RANGE = current_range if not getattr(current_range, "_entry_ranking_scalp_rescue_v11", False) else _ORIGINAL_RANGE
            _ORIGINAL_AI = current_ai if not getattr(current_ai, "_entry_ranking_scalp_rescue_v11", False) else _ORIGINAL_AI
            _PATCHED_RANGE, _PATCHED_AI = _build_patches(_ORIGINAL_RANGE, _ORIGINAL_AI)

        ec.range_5m_filter = _PATCHED_RANGE
        ec.ai_final_entry_check = _PATCHED_AI
        vf.range_5m_filter = _PATCHED_RANGE
        eg.ai_final_entry_check = _PATCHED_AI
        return True
    except Exception:
        logger.exception("[ENTRY RANKING SCALP RESCUE] apply failed")
        return False


def _watcher_loop() -> None:
    try:
        duration = _env_float("ENTRY_RANKING_SCALP_RESCUE_WATCH_SEC", 180.0)
        interval = max(0.5, _env_float("ENTRY_RANKING_SCALP_RESCUE_WATCH_INTERVAL_SEC", 1.0))
        end = time.time() + duration
        last_log = 0.0
        while time.time() < end:
            ok = _apply_once(force_rebuild=False)
            now = time.time()
            if now - last_log >= 15.0:
                logger.warning("[ENTRY RANKING SCALP RESCUE] watcher enforce ok=%s remaining=%.1fs", ok, max(0.0, end - now))
                last_log = now
            time.sleep(interval)
    except Exception:
        logger.exception("[ENTRY RANKING SCALP RESCUE] watcher failed")


def install() -> bool:
    global _INSTALLED, _WATCHER_STARTED
    if not _env_bool("ENTRY_RANKING_SCALP_ORDER_RESCUE_ENABLED", True):
        logger.warning("[ENTRY RANKING SCALP RESCUE] disabled by env")
        return False
    ok = _apply_once(force_rebuild=True)
    if ok:
        os.environ.setdefault("ENTRY_RANKING_SCALP_ORDER_RESCUE_ENABLED", "1")
        os.environ.setdefault("ENTRY_RANKING_SCALP_RANGE_MIN_PCT", "0.0045")
        os.environ.setdefault("ENTRY_RANKING_SCALP_MIN_SCORE", "50")
        os.environ.setdefault("ENTRY_RANKING_SCALP_MAX_PRICE", "7000")
        os.environ.setdefault("ENTRY_RANKING_SCALP_RESCUE_WATCH_SEC", "180")
        os.environ.setdefault("ENTRY_RANKING_SCALP_RESCUE_WATCH_INTERVAL_SEC", "1")
        _INSTALLED = True
        logger.warning(
            "[ENTRY RANKING SCALP RESCUE] installed v1.1 range_min=%s score_min=%s max_price=%s ai_any_ng=%s watcher_sec=%s",
            os.environ.get("ENTRY_RANKING_SCALP_RANGE_MIN_PCT"),
            os.environ.get("ENTRY_RANKING_SCALP_MIN_SCORE"),
            os.environ.get("ENTRY_RANKING_SCALP_MAX_PRICE"),
            os.environ.get("ENTRY_RANKING_SCALP_AI_FALLBACK_ANY_NG", "0"),
            os.environ.get("ENTRY_RANKING_SCALP_RESCUE_WATCH_SEC"),
        )
        if not _WATCHER_STARTED:
            _WATCHER_STARTED = True
            threading.Thread(target=_watcher_loop, name="entry-ranking-scalp-rescue-watch", daemon=True).start()
        return True
    return False


try:
    install()
except Exception:
    logger.exception("[ENTRY RANKING SCALP RESCUE] auto install failed")


__all__ = ["install"]
