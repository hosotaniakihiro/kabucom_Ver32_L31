# ============================================================
# File   : core/startup/entry_final_filter_failopen_patch.py
# Version: V2.2-TONOSAMA-ATR-NESTED-SOURCE-FAILOPEN
# ------------------------------------------------------------
# 【目的】
#   候補・AI・pending までは通るのに、最後で全落ちする問題の緩和。
#
# V2.2:
#   - 最新ログでは PENDING_BUCKET は source=TONOSAMA なのに、ATR判定では
#       ENTRY_SKIP reason=ATR_1M_FILTER_NG
#       detail={'reason':'1m本数不足', 'atr':None, 'bars':14}
#     で止まっていた。
#   - entry_controller 内で行が変換されると top-level source が落ちることがあるため、
#     _raw / raw / entry_conditions / conditions / pipeline_source まで見て TONOSAMA 判定する。
#   - reason=1m本数不足 / atr=None / no_atr の場合は bars=14 ちょうどでも fail-open。
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)
_PATCHED = False


_ATR_INSUFFICIENT_WORDS = (
    "1m未生成",
    "1m本数不足",
    "ATR計算不可",
    "symbol列なし",
    "OHLC列不足",
    "no_atr_data",
    "no_atr",
    "atr=None",
    "'atr': None",
    '"atr": None',
    "bars",
    "本数不足",
    "未生成",
)


def _setdefault_env(name: str, value: str) -> None:
    try:
        cur = os.getenv(name)
        if cur is None or str(cur).strip() == "":
            os.environ[name] = str(value)
            logger.warning("[ENTRY FINAL FILTER FAILOPEN] env default set %s=%s", name, value)
    except Exception:
        pass


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _safe_str(v: Any) -> str:
    try:
        return str(v or "").strip()
    except Exception:
        return ""


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _row_dict(entry_row: Any) -> dict:
    try:
        if isinstance(entry_row, dict):
            return entry_row
        if hasattr(entry_row, "to_dict"):
            d = entry_row.to_dict()
            return d if isinstance(d, dict) else {}
    except Exception:
        pass
    return {}


def _dicts(entry_row: Any) -> list[dict]:
    base = _row_dict(entry_row)
    out: list[dict] = []
    if base:
        out.append(base)
    for k in ("_raw", "raw", "source_row", "candidate_raw", "entry_conditions", "conditions", "metrics", "features", "detail", "ai_detail"):
        try:
            v = base.get(k) if isinstance(base, dict) else None
            d = v if isinstance(v, dict) else (v.to_dict() if hasattr(v, "to_dict") else None)
            if isinstance(d, dict):
                out.append(d)
                for kk in ("_raw", "raw", "entry_conditions", "conditions", "metrics", "features", "detail", "ai_detail"):
                    vv = d.get(kk)
                    dd = vv if isinstance(vv, dict) else (vv.to_dict() if hasattr(vv, "to_dict") else None)
                    if isinstance(dd, dict):
                        out.append(dd)
        except Exception:
            pass
    return out


def _is_tonosama_entry(entry_row: Any) -> bool:
    for row in _dicts(entry_row):
        src = _safe_str(row.get("source") or row.get("pipeline_source") or row.get("entry_source")).upper()
        et = _safe_str(row.get("entry_type") or row.get("type") or row.get("strategy")).upper()
        reason = _safe_str(row.get("ai_reason") or row.get("reason") or row.get("source_reason")).upper()
        if src == "TONOSAMA" or et == "TONOSAMA" or "TONOSAMA" in reason:
            return True
    return False


def _has_explicit_atr(entry_row: Any) -> bool:
    for row in _dicts(entry_row):
        price = _safe_float(row.get("close_price") or row.get("close") or row.get("price") or row.get("current_price"), 0.0)
        atr = _safe_float(row.get("atr_1m") or row.get("atr") or row.get("ATR") or row.get("atr14") or row.get("atr_14"), 0.0)
        if price > 0 and atr > 0:
            return True
    return False


def _ret_ok(ret: Any) -> bool:
    try:
        if isinstance(ret, tuple) and len(ret) > 0:
            return bool(ret[0])
        return bool(ret)
    except Exception:
        return False


def _ret_detail(ret: Any) -> Any:
    try:
        if isinstance(ret, tuple) and len(ret) > 1:
            return ret[1]
    except Exception:
        pass
    return None


def _detail_bars(detail: Any) -> float:
    try:
        if isinstance(detail, dict):
            return _safe_float(detail.get("bars"), -1.0)
    except Exception:
        pass
    return -1.0


def _detail_atr_missing(detail: Any) -> bool:
    try:
        if isinstance(detail, dict):
            if detail.get("atr") is None or detail.get("atr_1m") is None:
                return True
            reason = _safe_str(detail.get("reason"))
            if any(w in reason for w in _ATR_INSUFFICIENT_WORDS):
                return True
    except Exception:
        pass
    text = _safe_str(detail)
    return any(w in text for w in _ATR_INSUFFICIENT_WORDS)


def _looks_atr_history_gap(entry_row: Any = None, detail: Any = None) -> bool:
    try:
        if _has_explicit_atr(entry_row):
            return False
        if detail is None:
            return True
        if _detail_atr_missing(detail):
            return True
        bars = _detail_bars(detail)
        # 14本ちょうどでもATRがNone/本数不足ならfail-open対象。
        if 0 <= bars <= _safe_float(os.getenv("ATR_1M_FILTER_TONOSAMA_MIN_BARS"), 14.0):
            return True
        return False
    except Exception:
        return False


def install() -> bool:
    global _PATCHED
    if _PATCHED:
        return True

    _setdefault_env("ENTRY_ALLOW_ENTRY_WITHOUT_BOARD", "1")
    _setdefault_env("ENTRY_MAX_DAILY_ENTRIES_PER_SYMBOL", "2")
    _setdefault_env("ENTRY_COUNT_SENT_ORDER_AS_DAILY_ENTRY", "1")
    _setdefault_env("ATR_1M_FILTER_TONOSAMA_HISTORY_FAIL_OPEN", "1")
    _setdefault_env("ATR_1M_FILTER_TONOSAMA_MIN_BARS", "14")
    _setdefault_env("RANGE_5M_FILTER_NG_FAIL_OPEN", "1")
    _setdefault_env("PENDING_PROTECT_PUSH_SYMBOLS", "1")
    _setdefault_env("PENDING_PROTECT_PUSH_MAX_KEEP", "50")
    _setdefault_env("ENTRY_BOARD_RETRY_ENABLED", "1")
    _setdefault_env("ENTRY_BOARD_RETRY_WAIT_SEC", "4.5")
    _setdefault_env("ENTRY_BOARD_RETRY_COUNT", "1")
    _setdefault_env("ENTRY_BOARD_RETRY_EXTRA_WAIT_SEC", "0.3")
    _setdefault_env("ENTRY_BOARD_RETRY_EXTRA_COUNT", "1")
    _setdefault_env("ENTRY_BOARD_RETRY_SYMBOLS_ONLY_PENDING", "0")
    _setdefault_env("ENTRY_SHORT_MTF_REQUIRED", "1")
    _setdefault_env("ENTRY_SHORT_MTF_REQUIRE_ALL", "1")
    _setdefault_env("ENTRY_SHORT_MTF_SLOPE_EPS", "0.0")
    _setdefault_env("ENTRY_DAILY_MTF_OPTIONAL", "1")
    _setdefault_env("ENTRY_MA5_BREAKOUT_ENABLED", "1")
    _setdefault_env("ENTRY_MA5_BREAKOUT_TFS", "3,5")
    _setdefault_env("ENTRY_MA5_BREAKOUT_MIN_BAR", "1")
    _setdefault_env("ENTRY_MA5_BREAKOUT_MAX_BAR", "3")
    _setdefault_env("ENTRY_MA5_BREAKOUT_LOOKBACK", "20")
    _setdefault_env("ENTRY_MA5_BREAKOUT_REQUIRE_DATA", "1")
    _setdefault_env("ENTRY_MA5_BREAKOUT_DB_BACKFILL", "1")
    _setdefault_env("ENTRY_DIRECTION_CONFIRM_RECURSION_FAIL_OPEN", "1")
    _setdefault_env("ENTRY_DIRECTION_CONFIRM_ERROR_FAIL_OPEN", "0")

    try:
        import trading.handlers.entry_controller as ec
    except Exception:
        logger.exception("[ENTRY FINAL FILTER FAILOPEN] entry_controller import failed")
        return False

    try:
        orig_atr = getattr(ec, "atr_1m_filter", None)
        if callable(orig_atr) and not getattr(orig_atr, "_tonosama_atr_failopen_wrapper_v22", False):
            def _atr_tonosama_failopen(entry_row: Any = None, *args, **kwargs):
                try:
                    ret = orig_atr(entry_row, *args, **kwargs)
                    if (not _ret_ok(ret)) and _env_bool("ATR_1M_FILTER_TONOSAMA_HISTORY_FAIL_OPEN", True):
                        detail = _ret_detail(ret)
                        if _is_tonosama_entry(entry_row) and _looks_atr_history_gap(entry_row=entry_row, detail=detail):
                            logger.warning(
                                "[ENTRY FINAL FILTER FAILOPEN] atr_1m_filter TONOSAMA history gap -> fail-open. symbol=%s ret=%s detail=%s nested_tonosama=True",
                                _row_dict(entry_row).get("symbol"), ret, detail,
                            )
                            return True
                    return ret
                except Exception as e:
                    allow = _is_tonosama_entry(entry_row) and _env_bool("ATR_1M_FILTER_TONOSAMA_ERROR_FAIL_OPEN", False)
                    logger.warning("[ENTRY FINAL FILTER FAILOPEN] atr_1m_filter error. tonosama_fail_open=%s err=%s", allow, e, exc_info=False)
                    return bool(allow)

            _atr_tonosama_failopen._tonosama_atr_failopen_wrapper = True  # type: ignore[attr-defined]
            _atr_tonosama_failopen._tonosama_atr_failopen_wrapper_v2 = True  # type: ignore[attr-defined]
            _atr_tonosama_failopen._tonosama_atr_failopen_wrapper_v22 = True  # type: ignore[attr-defined]
            _atr_tonosama_failopen._original_atr_1m_filter = orig_atr  # type: ignore[attr-defined]
            setattr(ec, "atr_1m_filter", _atr_tonosama_failopen)
            logger.warning("[ENTRY FINAL FILTER FAILOPEN] atr_1m_filter TONOSAMA nested-source history-gap wrapper installed v2.2")
    except Exception:
        logger.exception("[ENTRY FINAL FILTER FAILOPEN] atr_1m wrapper install failed")

    try:
        orig_range = getattr(ec, "range_5m_filter", None)
        if callable(orig_range) and not getattr(orig_range, "_range5m_failopen_wrapper", False):
            def _range5m_failopen(entry_row: Any = None, *args, **kwargs):
                try:
                    ret = orig_range(entry_row, *args, **kwargs)
                    if isinstance(ret, tuple):
                        return ret
                    if ret is False and _env_bool("RANGE_5M_FILTER_NG_FAIL_OPEN", True):
                        logger.warning("[ENTRY FINAL FILTER FAILOPEN] range_5m_filter returned NG -> fail-open. Other guards still apply.")
                        return True
                    return ret
                except RecursionError:
                    allow = _env_bool("RANGE_5M_FILTER_RECURSION_FAIL_OPEN", True)
                    logger.error("[ENTRY FINAL FILTER FAILOPEN] range_5m_filter recursion. fail_open=%s", allow, exc_info=False)
                    return bool(allow)
                except Exception as e:
                    allow = _env_bool("RANGE_5M_FILTER_ERROR_FAIL_OPEN", True)
                    logger.warning("[ENTRY FINAL FILTER FAILOPEN] range_5m_filter error. fail_open=%s err=%s", allow, e, exc_info=False)
                    return bool(allow)

            _range5m_failopen._range5m_failopen_wrapper = True  # type: ignore[attr-defined]
            _range5m_failopen._original_range_5m_filter = orig_range  # type: ignore[attr-defined]
            setattr(ec, "range_5m_filter", _range5m_failopen)
            logger.warning("[ENTRY FINAL FILTER FAILOPEN] range_5m_filter wrapper installed")
    except Exception:
        logger.exception("[ENTRY FINAL FILTER FAILOPEN] range_5m wrapper install failed")

    for mod_name, label in [
        ("core.startup.entry_direction_failclosed_patch", "direction guarded patch"),
        ("core.startup.pending_protect_push_patch", "pending protect push patch"),
        ("core.startup.board_retry_patch", "board retry patch"),
        ("core.startup.entry_mtf_short_required_daily_optional_patch", "short MTF daily optional patch"),
        ("core.startup.entry_ma5_breakout_count_patch", "MA5 breakout count patch"),
    ]:
        try:
            mod = __import__(mod_name, fromlist=["install"])
            fn = getattr(mod, "install", None)
            ok = fn() if callable(fn) else False
            logger.warning("[ENTRY FINAL FILTER FAILOPEN] %s installed=%s", label, ok)
        except Exception:
            logger.exception("[ENTRY FINAL FILTER FAILOPEN] %s install failed", label)

    _PATCHED = True
    logger.warning(
        "[ENTRY FINAL FILTER FAILOPEN] installed v2.2 atr_tonosama_history_fail_open=%s atr_min_bars=%s range_fail_open=%s allow_without_board=%s max_symbol_entries=%s pending_protect_push=%s board_retry=%s short_mtf_required=%s daily_mtf_optional=%s ma5_breakout=%s direction_recursion_fail_open=%s direction_error_fail_open=%s",
        _env_bool("ATR_1M_FILTER_TONOSAMA_HISTORY_FAIL_OPEN", True),
        os.getenv("ATR_1M_FILTER_TONOSAMA_MIN_BARS"),
        _env_bool("RANGE_5M_FILTER_NG_FAIL_OPEN", True),
        os.getenv("ENTRY_ALLOW_ENTRY_WITHOUT_BOARD"),
        os.getenv("ENTRY_MAX_DAILY_ENTRIES_PER_SYMBOL"),
        os.getenv("PENDING_PROTECT_PUSH_SYMBOLS"),
        os.getenv("ENTRY_BOARD_RETRY_ENABLED"),
        os.getenv("ENTRY_SHORT_MTF_REQUIRED"),
        os.getenv("ENTRY_DAILY_MTF_OPTIONAL"),
        os.getenv("ENTRY_MA5_BREAKOUT_ENABLED"),
        os.getenv("ENTRY_DIRECTION_CONFIRM_RECURSION_FAIL_OPEN"),
        os.getenv("ENTRY_DIRECTION_CONFIRM_ERROR_FAIL_OPEN"),
    )
    return True


try:
    install()
except Exception:
    logger.exception("[ENTRY FINAL FILTER FAILOPEN] auto install failed")


__all__ = ["install"]
