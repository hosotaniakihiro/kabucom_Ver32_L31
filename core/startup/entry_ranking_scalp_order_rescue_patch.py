# ============================================================
# File   : core/startup/entry_ranking_scalp_order_rescue_patch.py
# Version: V1.2-RANKING-SCALP-FINAL-RESCUE
# ------------------------------------------------------------
# Purpose:
#   ランキング候補が ENTRY許可 まで進むが、実注文前に
#   RANGE_5M_FILTER_NG / ATR_1M_FILTER_NG / ranking AI model missing / mtf_low
#   で落ちる問題を救済する。
#
# V1.2:
#   - range_5m_filter が df_5m=, symbol=, min_pct= で呼ばれても落ちないようにする。
#   - AI gate の mtf_low も RANKING高スコア候補なら救済対象にする。
#   - ATRが 0.0005 以上なら RANKING高スコア候補をfail-openする。
#   - watcher時間を延長し、後続patchに上書きされても再適用する。
#
# Safety:
#   - SELL_CREDIT_GUARD_NG は回避しない。空売り不可銘柄は引き続き落とす。
#   - RANKING由来だけ対象。
#   - 高スコア・価格上限内のランキング候補だけ救済。
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
_ORIGINAL_ATR: Callable | None = None
_PATCHED_RANGE: Callable | None = None
_PATCHED_AI: Callable | None = None
_PATCHED_ATR: Callable | None = None


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
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _as_row(entry_row: Any = None, *args, **kwargs) -> dict:
    try:
        if isinstance(entry_row, dict):
            return entry_row
        if hasattr(entry_row, "to_dict"):
            d = entry_row.to_dict()
            if isinstance(d, dict):
                return d
    except Exception:
        pass
    # df_5m形式で呼ばれた時も、symbolだけはkwargsから拾う。
    out = {}
    try:
        if "symbol" in kwargs:
            out["symbol"] = kwargs.get("symbol")
        if "side" in kwargs:
            out["side"] = kwargs.get("side")
        if "source" in kwargs:
            out["source"] = kwargs.get("source")
    except Exception:
        pass
    return out


def _source_is_ranking(row: dict) -> bool:
    try:
        src = str(row.get("source") or row.get("pipeline_source") or row.get("entry_source") or row.get("entry_type") or "").strip().upper()
        et = str(row.get("entry_type") or row.get("type") or row.get("strategy") or "").strip().upper()
        return src == "RANKING" or et == "RANKING" or et == "RANKING_5S"
    except Exception:
        return False


def _side(row: dict) -> str:
    return str(row.get("entry_decision") or row.get("side") or "").strip().upper()


def _price(row: dict) -> float:
    return _safe_float(row.get("close") or row.get("close_price") or row.get("price") or row.get("current_price"), 0.0)


def _atr(row: dict) -> float:
    return _safe_float(row.get("atr_1m") or row.get("atr") or row.get("ATR") or row.get("atr14") or row.get("atr_14"), 0.0)


def _range_ratio(row: dict) -> float:
    high = _safe_float(row.get("high") or row.get("high_price"), 0.0)
    low = _safe_float(row.get("low") or row.get("low_price"), 0.0)
    px = _price(row)
    if high > 0 and low > 0 and px > 0 and high >= low:
        return (high - low) / px
    return 0.0


def _score_for_side(row: dict, side: str) -> float:
    score = _safe_float(row.get("score"), 0.0)
    if side == "SELL":
        return max(_safe_float(row.get("score_sell"), 0.0), abs(score), _safe_float(row.get("ranking_score"), 0.0))
    return max(_safe_float(row.get("score_buy"), 0.0), score, _safe_float(row.get("ranking_score"), 0.0))


def _ranking_candidate_ok(row: dict) -> bool:
    if not _source_is_ranking(row):
        return False
    side = _side(row)
    score = _score_for_side(row, side)
    px = _price(row)
    min_score = _env_float("ENTRY_RANKING_SCALP_MIN_SCORE", 50.0)
    max_price = _env_float("ENTRY_RANKING_SCALP_MAX_PRICE", 7000.0)
    if side not in {"BUY", "SELL"}:
        return False
    if score < min_score:
        return False
    if px > 0 and px > max_price:
        return False
    return True


def _build_patches(old_range: Callable | None, old_ai_check: Callable | None, old_atr: Callable | None):
    def _patched_range_5m_filter(entry_row: Any = None, *args, **kwargs) -> bool:
        row = _as_row(entry_row, *args, **kwargs)
        # 既存ガードがOKならそのまま通す。df_5mキーワードにも対応。
        if callable(old_range) and not getattr(old_range, "_entry_ranking_scalp_rescue_v12", False):
            try:
                ok = bool(old_range(entry_row, *args, **kwargs))
                if ok:
                    return True
            except TypeError as e:
                if "unexpected keyword argument" in str(e):
                    try:
                        ok = bool(old_range(entry_row))
                        if ok:
                            return True
                    except Exception:
                        pass
                    logger.warning("[ENTRY RANKING SCALP RESCUE] original range_5m_filter keyword mismatch tolerated err=%s", e)
                else:
                    logger.exception("[ENTRY RANKING SCALP RESCUE] original range_5m_filter TypeError")
            except Exception:
                logger.exception("[ENTRY RANKING SCALP RESCUE] original range_5m_filter failed")

        try:
            if not _ranking_candidate_ok(row):
                return bool(_env_bool("ENTRY_RANKING_SCALP_RANGE_ERROR_FAILOPEN", True) and kwargs.get("df_5m") is not None)
            px = _price(row)
            ratio = _range_ratio(row)
            min_ratio = _env_float("ENTRY_RANKING_SCALP_RANGE_MIN_PCT", 0.0045)
            side = _side(row)
            score = _score_for_side(row, side)
            # high/lowが無いRANKING候補は、レンジだけで全落ちさせない。
            if ratio <= 0 and _env_bool("ENTRY_RANKING_SCALP_RANGE_NO_HIGHLOW_FAILOPEN", True):
                logger.warning(
                    "[ENTRY RANKING SCALP RESCUE] RANGE no high/low fail-open symbol=%s side=%s price=%.2f score=%.2f kwargs=%s",
                    row.get("symbol"), side, px, score, sorted(kwargs.keys()),
                )
                return True
            if px > 0 and ratio >= min_ratio:
                logger.warning(
                    "[ENTRY RANKING SCALP RESCUE] RANGE_5M rescued symbol=%s side=%s price=%.2f range_ratio=%.5f min=%.5f score=%.2f",
                    row.get("symbol"), side, px, ratio, min_ratio, score,
                )
                return True
        except Exception:
            logger.exception("[ENTRY RANKING SCALP RESCUE] range rescue failed")
        return False

    def _patched_atr_1m_filter(entry_row: Any = None, *args, **kwargs) -> bool:
        row = _as_row(entry_row, *args, **kwargs)
        if callable(old_atr) and not getattr(old_atr, "_entry_ranking_scalp_rescue_v12", False):
            try:
                ok = bool(old_atr(entry_row, *args, **kwargs))
                if ok:
                    return True
            except Exception:
                logger.exception("[ENTRY RANKING SCALP RESCUE] original atr_1m_filter failed")
        try:
            if not _ranking_candidate_ok(row):
                return False
            px = _price(row)
            atr = _atr(row)
            ratio = (atr / px) if px > 0 and atr > 0 else 0.0
            min_ratio = _env_float("ENTRY_RANKING_SCALP_ATR_MIN_RATIO", 0.0005)
            if ratio >= min_ratio:
                logger.warning(
                    "[ENTRY RANKING SCALP RESCUE] ATR rescued symbol=%s side=%s price=%.2f atr=%.6f ratio=%.6f min=%.6f score=%.2f",
                    row.get("symbol"), _side(row), px, atr, ratio, min_ratio, _score_for_side(row, _side(row)),
                )
                return True
        except Exception:
            logger.exception("[ENTRY RANKING SCALP RESCUE] atr rescue failed")
        return False

    def _ranking_ai_fallback(entry_row: Any) -> dict | None:
        try:
            row = _as_row(entry_row)
            if not _ranking_candidate_ok(row):
                return None
            side = _side(row)
            score = _score_for_side(row, side)
            reason = f"RANKING_SCALP_RULE_PASS|score={score:.2f}|model_missing_or_mtf_low_fallback=1"
            return {"allow": True, "confidence": 1.0, "reason": reason, "lot_multiplier": 1.0}
        except Exception:
            logger.exception("[ENTRY RANKING SCALP RESCUE] ai fallback build failed")
            return None

    def _patched_ai_final_entry_check(entry_row: Any = None, *args, **kwargs):
        ret = None
        if callable(old_ai_check) and not getattr(old_ai_check, "_entry_ranking_scalp_rescue_v12", False):
            try:
                ret = old_ai_check(entry_row, *args, **kwargs)
                if isinstance(ret, dict) and bool(ret.get("allow", False)):
                    return ret
                reason = str((ret or {}).get("reason") if isinstance(ret, dict) else ret)
                reason_l = reason.lower()
                rescue_any_ng = _env_bool("ENTRY_RANKING_SCALP_AI_FALLBACK_ANY_NG", True)
                rescue_reasons = ("model not found", "not found", "mtf_low", "ai_allow_false")
                if rescue_any_ng or any(x in reason_l for x in rescue_reasons):
                    fb = _ranking_ai_fallback(entry_row)
                    if fb is not None:
                        logger.warning("[ENTRY RANKING SCALP RESCUE] AI fallback allow symbol=%s original=%s", _as_row(entry_row).get("symbol"), reason)
                        return fb
                return ret
            except Exception as e:
                reason = str(e)
                if "model" not in reason.lower() and "not found" not in reason.lower():
                    logger.exception("[ENTRY RANKING SCALP RESCUE] original ai_final_entry_check failed")
                    return {"allow": False, "confidence": 0.0, "reason": f"AI_EXCEPTION:{e}"}

        fb = _ranking_ai_fallback(entry_row)
        if fb is not None:
            row = _as_row(entry_row)
            logger.warning("[ENTRY RANKING SCALP RESCUE] ranking AI fallback allow symbol=%s side=%s score=%.2f", row.get("symbol"), _side(row), _score_for_side(row, _side(row)))
            return fb
        return ret if isinstance(ret, dict) else {"allow": False, "confidence": 0.0, "reason": "AI_FALLBACK_NOT_APPLICABLE"}

    _patched_range_5m_filter._entry_ranking_scalp_rescue_v11 = True  # type: ignore[attr-defined]
    _patched_range_5m_filter._entry_ranking_scalp_rescue_v12 = True  # type: ignore[attr-defined]
    _patched_atr_1m_filter._entry_ranking_scalp_rescue_v12 = True  # type: ignore[attr-defined]
    _patched_ai_final_entry_check._entry_ranking_scalp_rescue_v11 = True  # type: ignore[attr-defined]
    _patched_ai_final_entry_check._entry_ranking_scalp_rescue_v12 = True  # type: ignore[attr-defined]
    return _patched_range_5m_filter, _patched_ai_final_entry_check, _patched_atr_1m_filter


def _apply_once(*, force_rebuild: bool = False) -> bool:
    global _ORIGINAL_RANGE, _ORIGINAL_AI, _ORIGINAL_ATR, _PATCHED_RANGE, _PATCHED_AI, _PATCHED_ATR
    try:
        import trading.handlers.entry_controller as ec
        import trading.filters.volatility_filter as vf
        import AI.entry_gate as eg

        current_range = getattr(ec, "range_5m_filter", None)
        current_ai = getattr(ec, "ai_final_entry_check", None)
        current_atr = getattr(ec, "atr_1m_filter", None)

        # 他patchに上書きされたら、その関数を新しい original として包み直す。
        if force_rebuild or _PATCHED_RANGE is None or not getattr(current_range, "_entry_ranking_scalp_rescue_v12", False):
            _ORIGINAL_RANGE = current_range if not getattr(current_range, "_entry_ranking_scalp_rescue_v12", False) else _ORIGINAL_RANGE
            _ORIGINAL_AI = current_ai if not getattr(current_ai, "_entry_ranking_scalp_rescue_v12", False) else _ORIGINAL_AI
            _ORIGINAL_ATR = current_atr if not getattr(current_atr, "_entry_ranking_scalp_rescue_v12", False) else _ORIGINAL_ATR
            _PATCHED_RANGE, _PATCHED_AI, _PATCHED_ATR = _build_patches(_ORIGINAL_RANGE, _ORIGINAL_AI, _ORIGINAL_ATR)

        ec.range_5m_filter = _PATCHED_RANGE
        ec.ai_final_entry_check = _PATCHED_AI
        ec.atr_1m_filter = _PATCHED_ATR
        vf.range_5m_filter = _PATCHED_RANGE
        eg.ai_final_entry_check = _PATCHED_AI
        return True
    except Exception:
        logger.exception("[ENTRY RANKING SCALP RESCUE] apply failed")
        return False


def _watcher_loop() -> None:
    try:
        duration = _env_float("ENTRY_RANKING_SCALP_RESCUE_WATCH_SEC", 600.0)
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
    os.environ.setdefault("ENTRY_RANKING_SCALP_ORDER_RESCUE_ENABLED", "1")
    os.environ.setdefault("ENTRY_RANKING_SCALP_RANGE_MIN_PCT", "0.0045")
    os.environ.setdefault("ENTRY_RANKING_SCALP_RANGE_NO_HIGHLOW_FAILOPEN", "1")
    os.environ.setdefault("ENTRY_RANKING_SCALP_RANGE_ERROR_FAILOPEN", "1")
    os.environ.setdefault("ENTRY_RANKING_SCALP_ATR_MIN_RATIO", "0.0005")
    os.environ.setdefault("ENTRY_RANKING_SCALP_MIN_SCORE", "50")
    os.environ.setdefault("ENTRY_RANKING_SCALP_MAX_PRICE", "7000")
    os.environ.setdefault("ENTRY_RANKING_SCALP_AI_FALLBACK_ANY_NG", "1")
    os.environ.setdefault("ENTRY_RANKING_SCALP_RESCUE_WATCH_SEC", "600")
    os.environ.setdefault("ENTRY_RANKING_SCALP_RESCUE_WATCH_INTERVAL_SEC", "1")
    ok = _apply_once(force_rebuild=True)
    if ok:
        _INSTALLED = True
        logger.warning(
            "[ENTRY RANKING SCALP RESCUE] installed v1.2 range_min=%s score_min=%s max_price=%s atr_min=%s ai_any_ng=%s watcher_sec=%s",
            os.environ.get("ENTRY_RANKING_SCALP_RANGE_MIN_PCT"),
            os.environ.get("ENTRY_RANKING_SCALP_MIN_SCORE"),
            os.environ.get("ENTRY_RANKING_SCALP_MAX_PRICE"),
            os.environ.get("ENTRY_RANKING_SCALP_ATR_MIN_RATIO"),
            os.environ.get("ENTRY_RANKING_SCALP_AI_FALLBACK_ANY_NG"),
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