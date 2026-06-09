# ============================================================
# File   : core/startup/ranking_entry_final_rescue_patch.py
# Version: V1.2-RANKING-FINAL-RESCUE-FORCE-SAFE-AI
# ------------------------------------------------------------
# 目的:
#   RANKING pending が entry_controller まで到達しているのに、
#   最終段で全落ちして注文が出ない問題を、古い/弱いAI判定を
#   無理に通さない範囲で緩和する。
#
# V1.2:
#   - sitecustomize 側の既定値 RANKING_FINAL_RESCUE_AI_FAILOPEN=1 に
#     負けないよう、このモジュールではAI fail-openを明示的に0へ強制。
#   - ATR救済とrange_5m_filterのTypeError互換は維持。
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)
_DONE = False


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _row_dict(v: Any) -> dict:
    try:
        if isinstance(v, dict):
            return v
        if hasattr(v, "to_dict"):
            d = v.to_dict()
            return d if isinstance(d, dict) else {}
    except Exception:
        pass
    return {}


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _safe_str(v: Any) -> str:
    try:
        return str(v or "").strip()
    except Exception:
        return ""


def _is_ranking_row(row: Any) -> bool:
    d = _row_dict(row)
    try:
        src = _safe_str(d.get("source") or d.get("pipeline_source") or d.get("entry_source")).upper()
        et = _safe_str(d.get("entry_type") or d.get("type") or d.get("strategy")).upper()
        return src == "RANKING" or et == "RANKING"
    except Exception:
        return False


def _score(row: Any) -> float:
    d = _row_dict(row)
    return max(
        _safe_float(d.get("score"), 0.0),
        _safe_float(d.get("score_buy"), 0.0),
        _safe_float(d.get("ranking_score"), 0.0),
        _safe_float(d.get("final_score"), 0.0),
    )


def _price(row: Any) -> float:
    d = _row_dict(row)
    return _safe_float(d.get("close_price") or d.get("close") or d.get("price") or d.get("current_price"), 0.0)


def _atr(row: Any) -> float:
    d = _row_dict(row)
    return _safe_float(d.get("atr_1m") or d.get("atr") or d.get("ATR") or d.get("atr14") or d.get("atr_14"), 0.0)


def _volume(row: Any) -> float:
    d = _row_dict(row)
    return _safe_float(d.get("volume") or d.get("Volume") or d.get("trading_volume"), 0.0)


def _mtf(row: Any) -> float:
    d = _row_dict(row)
    return max(
        _safe_float(d.get("mtf"), 0.0),
        _safe_float(d.get("score_mtf"), 0.0),
        _safe_float(d.get("mtf_score"), 0.0),
    )


def _ranking_rescue_ok(row: Any) -> bool:
    if not _is_ranking_row(row):
        return False
    sc = _score(row)
    price = _price(row)
    vol = _volume(row)
    min_score = _env_float("RANKING_FINAL_RESCUE_MIN_SCORE", 55.0)
    min_volume = _env_float("RANKING_FINAL_RESCUE_MIN_VOLUME", 30000.0)
    if price <= 0:
        return False
    if sc < min_score:
        return False
    if min_volume > 0 and vol < min_volume:
        return False
    return True


def _patch_entry_controller() -> bool:
    try:
        import trading.handlers.entry_controller as ec
    except Exception:
        logger.exception("[RANKING FINAL RESCUE] entry_controller import failed")
        return False

    ok_any = False

    try:
        cur = getattr(ec, "atr_1m_filter", None)
        if callable(cur) and not getattr(cur, "_ranking_final_rescue_atr_v12", False):
            orig = getattr(cur, "_original_atr_1m_filter", cur)

            def patched_atr(entry_row: Any = None, *args, **kwargs):
                ret = orig(entry_row, *args, **kwargs)
                try:
                    if bool(ret):
                        return ret
                    if not _env_bool("RANKING_FINAL_RESCUE_ATR_FAILOPEN", True):
                        return ret
                    if not _ranking_rescue_ok(entry_row):
                        return ret
                    price = _price(entry_row)
                    atr = _atr(entry_row)
                    ratio = (atr / price) if price > 0 else 0.0
                    min_ratio = _env_float("RANKING_FINAL_RESCUE_ATR_MIN_RATIO", 0.0005)
                    if atr > 0 and ratio >= min_ratio:
                        logger.warning(
                            "[RANKING FINAL RESCUE] ATR fail-open symbol=%s score=%.3f atr=%.6f price=%.3f ratio=%.6f min_ratio=%.6f",
                            _row_dict(entry_row).get("symbol"), _score(entry_row), atr, price, ratio, min_ratio,
                        )
                        return True
                    return ret
                except Exception:
                    return ret

            patched_atr._ranking_final_rescue_atr_v1 = True  # type: ignore[attr-defined]
            patched_atr._ranking_final_rescue_atr_v11 = True  # type: ignore[attr-defined]
            patched_atr._ranking_final_rescue_atr_v12 = True  # type: ignore[attr-defined]
            patched_atr._original_atr_1m_filter = orig  # type: ignore[attr-defined]
            ec.atr_1m_filter = patched_atr
            ok_any = True
            logger.warning("[RANKING FINAL RESCUE] patched entry_controller.atr_1m_filter v1.2")
    except Exception:
        logger.exception("[RANKING FINAL RESCUE] atr patch failed")

    try:
        cur = getattr(ec, "range_5m_filter", None)
        if callable(cur) and not getattr(cur, "_ranking_final_rescue_range_v12", False):
            orig = getattr(cur, "_original_range_5m_filter", cur)

            def patched_range(entry_row: Any = None, *args, **kwargs):
                try:
                    return orig(entry_row, *args, **kwargs)
                except TypeError as e:
                    text = str(e)
                    if "unexpected keyword argument" in text:
                        try:
                            return orig(entry_row)
                        except Exception:
                            pass
                    allow = _env_bool("RANKING_FINAL_RESCUE_RANGE_ERROR_FAILOPEN", True)
                    logger.warning("[RANKING FINAL RESCUE] range_5m_filter TypeError fail_open=%s err=%s", allow, e)
                    return bool(allow)
                except Exception as e:
                    allow = _env_bool("RANKING_FINAL_RESCUE_RANGE_ERROR_FAILOPEN", True)
                    logger.warning("[RANKING FINAL RESCUE] range_5m_filter error fail_open=%s err=%s", allow, e)
                    return bool(allow)

            patched_range._ranking_final_rescue_range_v1 = True  # type: ignore[attr-defined]
            patched_range._ranking_final_rescue_range_v11 = True  # type: ignore[attr-defined]
            patched_range._ranking_final_rescue_range_v12 = True  # type: ignore[attr-defined]
            patched_range._original_range_5m_filter = orig  # type: ignore[attr-defined]
            ec.range_5m_filter = patched_range
            ok_any = True
            logger.warning("[RANKING FINAL RESCUE] patched entry_controller.range_5m_filter v1.2")
    except Exception:
        logger.exception("[RANKING FINAL RESCUE] range patch failed")

    try:
        cur = getattr(ec, "ai_final_entry_check", None)
        if callable(cur) and not getattr(cur, "_ranking_final_rescue_ai_v12", False):
            orig = getattr(cur, "_original_ai_final_entry_check", cur)

            def patched_ai(entry_row: Any = None, *args, **kwargs):
                ai = orig(entry_row, *args, **kwargs)
                try:
                    if isinstance(ai, dict) and ai.get("allow"):
                        return ai
                    if not _env_bool("RANKING_FINAL_RESCUE_AI_FAILOPEN", False):
                        return ai
                    if not _ranking_rescue_ok(entry_row):
                        return ai
                    reason = _safe_str(ai.get("reason") if isinstance(ai, dict) else ai).lower()
                    rescue_reasons = [x.strip().lower() for x in os.getenv("RANKING_FINAL_RESCUE_AI_REASONS", "model not found,ranking entry model not found").split(",")]
                    if reason and not any(x and x in reason for x in rescue_reasons):
                        return ai
                    sc = _score(entry_row)
                    mtf = _mtf(entry_row)
                    conf = max(_env_float("RANKING_FINAL_RESCUE_AI_CONFIDENCE", 0.72), _safe_float(ai.get("confidence") if isinstance(ai, dict) else 0.0, 0.0))
                    out = dict(ai) if isinstance(ai, dict) else {}
                    out.update({
                        "allow": True,
                        "confidence": conf,
                        "reason": f"ranking_final_rescue score={sc:.2f} mtf={mtf:.2f} original={reason or 'unknown'}",
                        "lot_multiplier": max(1.0, _safe_float(out.get("lot_multiplier"), 1.0)),
                    })
                    logger.warning("[RANKING FINAL RESCUE] AI fail-open symbol=%s score=%.3f mtf=%.3f conf=%.3f original_reason=%s", _row_dict(entry_row).get("symbol"), sc, mtf, conf, reason)
                    return out
                except Exception:
                    return ai

            patched_ai._ranking_final_rescue_ai_v1 = True  # type: ignore[attr-defined]
            patched_ai._ranking_final_rescue_ai_v11 = True  # type: ignore[attr-defined]
            patched_ai._ranking_final_rescue_ai_v12 = True  # type: ignore[attr-defined]
            patched_ai._original_ai_final_entry_check = orig  # type: ignore[attr-defined]
            ec.ai_final_entry_check = patched_ai
            ok_any = True
            logger.warning("[RANKING FINAL RESCUE] patched entry_controller.ai_final_entry_check v1.2 failopen=%s", os.environ.get("RANKING_FINAL_RESCUE_AI_FAILOPEN"))
    except Exception:
        logger.exception("[RANKING FINAL RESCUE] ai patch failed")

    return ok_any


def install() -> bool:
    global _DONE
    if _DONE:
        return _patch_entry_controller()
    os.environ.setdefault("RANKING_FINAL_RESCUE_MIN_SCORE", "55")
    os.environ.setdefault("RANKING_FINAL_RESCUE_MIN_VOLUME", "30000")
    os.environ.setdefault("RANKING_FINAL_RESCUE_ATR_FAILOPEN", "1")
    os.environ.setdefault("RANKING_FINAL_RESCUE_ATR_MIN_RATIO", "0.0005")
    os.environ.setdefault("RANKING_FINAL_RESCUE_RANGE_ERROR_FAILOPEN", "1")
    old_ai = os.environ.get("RANKING_FINAL_RESCUE_AI_FAILOPEN")
    os.environ["RANKING_FINAL_RESCUE_AI_FAILOPEN"] = "0"
    if old_ai != "0":
        logger.warning("[RANKING FINAL RESCUE] force AI fail-open %s->0", old_ai)
    os.environ.setdefault("RANKING_FINAL_RESCUE_AI_CONFIDENCE", "0.72")
    os.environ.setdefault("RANKING_FINAL_RESCUE_AI_REASONS", "model not found,ranking entry model not found")
    ok = _patch_entry_controller()
    _DONE = True
    logger.warning("[RANKING FINAL RESCUE] installed v1.2 ok=%s min_score=%s min_volume=%s atr_min_ratio=%s ai_failopen=%s", ok, os.environ.get("RANKING_FINAL_RESCUE_MIN_SCORE"), os.environ.get("RANKING_FINAL_RESCUE_MIN_VOLUME"), os.environ.get("RANKING_FINAL_RESCUE_ATR_MIN_RATIO"), os.environ.get("RANKING_FINAL_RESCUE_AI_FAILOPEN"))
    return ok


try:
    install()
except Exception:
    logger.exception("[RANKING FINAL RESCUE] auto install failed")

__all__ = ["install"]
