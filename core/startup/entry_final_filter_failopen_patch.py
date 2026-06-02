# ============================================================
# File   : core/startup/entry_final_filter_failopen_patch.py
# Version: V2.0-TONOSAMA-ATR-TUPLE-HISTORY-FAILOPEN
# ------------------------------------------------------------
# 【目的】
#   候補・AI・pending までは通るのに、最後で全落ちする問題の緩和。
#
# V2.0:
#   - atr_1m_filter が False 単体ではなく
#       (False, {'reason': '1m本数不足', 'bars': 10, ...})
#     の tuple を返すケースがあり、V1.9では tuple をそのまま返していた。
#   - そのため TONOSAMA でも ENTRY_SKIP reason=ATR_1M_FILTER_NG になっていた。
#   - TONOSAMA限定で、tuple型NGでも reason/bars が履歴不足なら True へ fail-open。
#
# 【方針】
#   - TONOSAMA は候補生成時に出来高/値幅/傾き/5秒足を見ているため、
#     発注直前ATRが「未生成・本数不足・ATRなし」だけなら fail-open する。
#   - ATRが明確に小さいケースは、本体filter結果を尊重して落とす。
#   - 5分足元フィルタNGは、発注停止ではなく既存の低変動ガード側に任せる。
#   - 方向確認の通常例外は fail-open しない。RecursionErrorだけfail-open可能。
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


def _is_tonosama_entry(entry_row: Any) -> bool:
    row = _row_dict(entry_row)
    src = _safe_str(row.get("source")).upper()
    et = _safe_str(row.get("entry_type")).upper()
    return src == "TONOSAMA" or et == "TONOSAMA"


def _has_explicit_atr(entry_row: Any) -> bool:
    row = _row_dict(entry_row)
    price = _safe_float(row.get("close_price") or row.get("close") or row.get("price") or row.get("current_price"), 0.0)
    atr = _safe_float(row.get("atr_1m") or row.get("atr") or row.get("ATR") or row.get("atr14") or row.get("atr_14"), 0.0)
    return price > 0 and atr > 0


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


def _looks_atr_history_gap(entry_row: Any = None, detail: Any = None) -> bool:
    try:
        if _has_explicit_atr(entry_row):
            return False
        text = _safe_str(detail)
        if not text:
            return True
        if any(w in text for w in _ATR_INSUFFICIENT_WORDS):
            return True
        bars = _detail_bars(detail)
        if 0 <= bars < _safe_float(os.getenv("ATR_1M_FILTER_TONOSAMA_MIN_BARS"), 14.0):
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

    # RecursionError は wrapper衝突由来のため fail-open を既定にする。
    # 通常Exceptionは安全側NGを維持。
    _setdefault_env("ENTRY_DIRECTION_CONFIRM_RECURSION_FAIL_OPEN", "1")
    _setdefault_env("ENTRY_DIRECTION_CONFIRM_ERROR_FAIL_OPEN", "0")

    try:
        import trading.handlers.entry_controller as ec
    except Exception:
        logger.exception("[ENTRY FINAL FILTER FAILOPEN] entry_controller import failed")
        return False

    try:
        orig_atr = getattr(ec, "atr_1m_filter", None)
        if callable(orig_atr) and not getattr(orig_atr, "_tonosama_atr_failopen_wrapper_v2", False):
            def _atr_tonosama_failopen(entry_row: Any = None, *args, **kwargs):
                try:
                    ret = orig_atr(entry_row, *args, **kwargs)
                    if (not _ret_ok(ret)) and _is_tonosama_entry(entry_row) and _env_bool("ATR_1M_FILTER_TONOSAMA_HISTORY_FAIL_OPEN", True):
                        detail = _ret_detail(ret)
                        if _looks_atr_history_gap(entry_row=entry_row, detail=detail):
                            logger.warning(
                                "[ENTRY FINAL FILTER FAILOPEN] atr_1m_filter TONOSAMA history gap -> fail-open. symbol=%s ret=%s detail=%s",
                                _row_dict(entry_row).get("symbol"),
                                ret,
                                detail,
                            )
                            return True
                    return ret
                except Exception as e:
                    allow = _is_tonosama_entry(entry_row) and _env_bool("ATR_1M_FILTER_TONOSAMA_ERROR_FAIL_OPEN", False)
                    logger.warning("[ENTRY FINAL FILTER FAILOPEN] atr_1m_filter error. tonosama_fail_open=%s err=%s", allow, e, exc_info=False)
                    return bool(allow)

            _atr_tonosama_failopen._tonosama_atr_failopen_wrapper = True  # type: ignore[attr-defined]
            _atr_tonosama_failopen._tonosama_atr_failopen_wrapper_v2 = True  # type: ignore[attr-defined]
            _atr_tonosama_failopen._original_atr_1m_filter = orig_atr  # type: ignore[attr-defined]
            setattr(ec, "atr_1m_filter", _atr_tonosama_failopen)
            logger.warning("[ENTRY FINAL FILTER FAILOPEN] atr_1m_filter TONOSAMA history-gap tuple wrapper installed v2")
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
        "[ENTRY FINAL FILTER FAILOPEN] installed v2.0 atr_tonosama_history_fail_open=%s atr_min_bars=%s range_fail_open=%s allow_without_board=%s max_symbol_entries=%s pending_protect_push=%s board_retry=%s short_mtf_required=%s daily_mtf_optional=%s ma5_breakout=%s direction_recursion_fail_open=%s direction_error_fail_open=%s",
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
