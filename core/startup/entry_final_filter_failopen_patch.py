# ============================================================
# File   : core/startup/entry_final_filter_failopen_patch.py
# Version: V1.8-TONOSAMA-ATR-HISTORY-FAILOPEN
# ------------------------------------------------------------
# 【目的】
#   候補・AI・pending までは通るのに、最後で全落ちする問題の緩和。
#
# 【対象】
#   - atr_1m_filter が TONOSAMA の起動直後/履歴不足で False を返すケース
#   - range_5m_filter が 5分足欠損/未完成で False を返すケース
#   - final_entry_safety_guard の board_missing で全落ちするケース
#   - SYMBOL_DAILY_ENTRY_LIMIT が 1回固定で強すぎるケース
#   - A/B PUSHローテーションで候補銘柄が反対面にいて板が取れないケース
#   - 日足MTFだけで発注停止してしまうケース
#   - BUY/SELLで3分足・5分足のMA5抜け後の本数を見たいケース
#
# 【方針】
#   - TONOSAMA は候補生成時に出来高/値幅/傾き/5秒足を見ているため、
#     発注直前ATRが「未生成・本数不足・ATRなし」だけなら fail-open する。
#   - ATRが明確に小さいケースは、本体filter結果を尊重して落とす。
#   - 5分足元フィルタNGは、発注停止ではなく既存の低変動ガード側に任せる。
#   - 方向確認の再帰/例外は fail-open しない。安全側NGにする。
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


def _force_env(name: str, value: str) -> None:
    try:
        os.environ[name] = str(value)
        logger.warning("[ENTRY FINAL FILTER FAILOPEN] env force set %s=%s", name, value)
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


def _is_tonosama_entry(entry_row: Any) -> bool:
    try:
        row = entry_row if isinstance(entry_row, dict) else (entry_row.to_dict() if hasattr(entry_row, "to_dict") else {})
        src = _safe_str(row.get("source")).upper()
        et = _safe_str(row.get("entry_type")).upper()
        return src == "TONOSAMA" or et == "TONOSAMA"
    except Exception:
        return False


def _has_explicit_atr(entry_row: Any) -> bool:
    try:
        row = entry_row if isinstance(entry_row, dict) else (entry_row.to_dict() if hasattr(entry_row, "to_dict") else {})
        price = _safe_float(row.get("close_price") or row.get("close") or row.get("price") or row.get("current_price"), 0.0)
        atr = _safe_float(row.get("atr_1m") or row.get("atr") or row.get("ATR") or row.get("atr14") or row.get("atr_14"), 0.0)
        return price > 0 and atr > 0
    except Exception:
        return False


def _looks_atr_history_gap(entry_row: Any = None, detail: Any = None) -> bool:
    try:
        # entry_rowにATRが明示的にあり、それでNGなら「履歴不足」ではなく低ATR扱い。
        if _has_explicit_atr(entry_row):
            return False
        text = _safe_str(detail)
        if not text:
            return True
        return any(w in text for w in _ATR_INSUFFICIENT_WORDS)
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

    _force_env("ENTRY_DIRECTION_CONFIRM_RECURSION_FAIL_OPEN", "0")
    _force_env("ENTRY_DIRECTION_CONFIRM_ERROR_FAIL_OPEN", "0")

    try:
        import trading.handlers.entry_controller as ec
    except Exception:
        logger.exception("[ENTRY FINAL FILTER FAILOPEN] entry_controller import failed")
        return False

    try:
        orig_atr = getattr(ec, "atr_1m_filter", None)
        if callable(orig_atr) and not getattr(orig_atr, "_tonosama_atr_failopen_wrapper", False):
            def _atr_tonosama_failopen(entry_row: Any = None, *args, **kwargs):
                try:
                    ret = orig_atr(entry_row, *args, **kwargs)
                    if isinstance(ret, tuple):
                        # 旧API: (ng, detail)。entry_controllerの呼び方では通常使わない。
                        return ret
                    if ret is False and _is_tonosama_entry(entry_row) and _env_bool("ATR_1M_FILTER_TONOSAMA_HISTORY_FAIL_OPEN", True):
                        if _looks_atr_history_gap(entry_row=entry_row, detail=None):
                            logger.warning(
                                "[ENTRY FINAL FILTER FAILOPEN] atr_1m_filter TONOSAMA history gap -> fail-open. Other guards still apply. symbol=%s",
                                (entry_row or {}).get("symbol") if isinstance(entry_row, dict) else None,
                            )
                            return True
                    return ret
                except Exception as e:
                    allow = _is_tonosama_entry(entry_row) and _env_bool("ATR_1M_FILTER_TONOSAMA_ERROR_FAIL_OPEN", False)
                    logger.warning("[ENTRY FINAL FILTER FAILOPEN] atr_1m_filter error. tonosama_fail_open=%s err=%s", allow, e, exc_info=False)
                    return bool(allow)

            _atr_tonosama_failopen._tonosama_atr_failopen_wrapper = True  # type: ignore[attr-defined]
            _atr_tonosama_failopen._original_atr_1m_filter = orig_atr  # type: ignore[attr-defined]
            setattr(ec, "atr_1m_filter", _atr_tonosama_failopen)
            logger.warning("[ENTRY FINAL FILTER FAILOPEN] atr_1m_filter TONOSAMA history-gap wrapper installed")
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
        ("core.startup.entry_direction_failclosed_patch", "direction fail-closed patch"),
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
        "[ENTRY FINAL FILTER FAILOPEN] installed v1.8 atr_tonosama_history_fail_open=%s range_fail_open=%s allow_without_board=%s max_symbol_entries=%s pending_protect_push=%s board_retry=%s short_mtf_required=%s daily_mtf_optional=%s ma5_breakout=%s",
        _env_bool("ATR_1M_FILTER_TONOSAMA_HISTORY_FAIL_OPEN", True),
        _env_bool("RANGE_5M_FILTER_NG_FAIL_OPEN", True),
        os.getenv("ENTRY_ALLOW_ENTRY_WITHOUT_BOARD"),
        os.getenv("ENTRY_MAX_DAILY_ENTRIES_PER_SYMBOL"),
        os.getenv("PENDING_PROTECT_PUSH_SYMBOLS"),
        os.getenv("ENTRY_BOARD_RETRY_ENABLED"),
        os.getenv("ENTRY_SHORT_MTF_REQUIRED"),
        os.getenv("ENTRY_DAILY_MTF_OPTIONAL"),
        os.getenv("ENTRY_MA5_BREAKOUT_ENABLED"),
    )
    return True


try:
    install()
except Exception:
    logger.exception("[ENTRY FINAL FILTER FAILOPEN] auto install failed")

__all__ = ["install"]
