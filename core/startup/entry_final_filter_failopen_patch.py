# ============================================================
# File   : core/startup/entry_final_filter_failopen_patch.py
# Version: V1.2-DIRECTION-FAIL-CLOSED
# ------------------------------------------------------------
# 【目的】
#   候補・AI・pending までは通るのに、最後で全落ちする問題の緩和。
#
# 【対象】
#   - range_5m_filter が 5分足欠損/未完成で False を返すケース
#   - final_entry_safety_guard の board_missing で全落ちするケース
#   - SYMBOL_DAILY_ENTRY_LIMIT が 1回固定で強すぎるケース
#
# 【方針】
#   - 5分足元フィルタNGは、発注停止ではなく既存の低変動ガード側に任せる
#   - 板が取れない場合は entry_order_builder / buy_sell_entry の reference_price に任せる
#   - 同一銘柄の当日発注済みカウントはデフォルト2回まで許可する
#   - 方向確認の再帰/例外は fail-open しない。安全側NGにする。
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)
_PATCHED = False


def _setdefault_env(name: str, value: str) -> None:
    """bat や .env で明示指定されていなければ安全側の緩和値を入れる。"""
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
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}
    except Exception:
        return bool(default)


def install() -> bool:
    global _PATCHED
    if _PATCHED:
        return True

    # 既存 runtime patch は判定時に os.getenv を読むため、ここでデフォルトを補完する。
    # ユーザーが bat 側で明示指定している値は上書きしない。
    _setdefault_env("ENTRY_ALLOW_ENTRY_WITHOUT_BOARD", "1")
    _setdefault_env("ENTRY_MAX_DAILY_ENTRIES_PER_SYMBOL", "2")
    _setdefault_env("ENTRY_COUNT_SENT_ORDER_AS_DAILY_ENTRY", "1")
    _setdefault_env("RANGE_5M_FILTER_NG_FAIL_OPEN", "1")

    # 方向確認は fail-open しない。明示的に安全側NGへ固定する。
    _force_env("ENTRY_DIRECTION_CONFIRM_RECURSION_FAIL_OPEN", "0")
    _force_env("ENTRY_DIRECTION_CONFIRM_ERROR_FAIL_OPEN", "0")

    try:
        import trading.handlers.entry_controller as ec
    except Exception:
        logger.exception("[ENTRY FINAL FILTER FAILOPEN] entry_controller import failed")
        return False

    # --------------------------------------------------------
    # 1) range_5m_filter fail-open wrapper
    # --------------------------------------------------------
    try:
        orig_range = getattr(ec, "range_5m_filter", None)
        if callable(orig_range) and not getattr(orig_range, "_range5m_failopen_wrapper", False):

            def _range5m_failopen(entry_row: Any = None, *args, **kwargs):
                try:
                    ret = orig_range(entry_row, *args, **kwargs)
                    if isinstance(ret, tuple):
                        return ret
                    if ret is False and _env_bool("RANGE_5M_FILTER_NG_FAIL_OPEN", True):
                        logger.warning(
                            "[ENTRY FINAL FILTER FAILOPEN] range_5m_filter returned NG -> fail-open. Other guards still apply."
                        )
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

    # --------------------------------------------------------
    # 2) pure direction confirm は fail-closed
    # --------------------------------------------------------
    try:
        from core.startup.entry_direction_failclosed_patch import install as install_direction_failclosed
        ok = install_direction_failclosed()
        logger.warning("[ENTRY FINAL FILTER FAILOPEN] direction fail-closed patch installed=%s", ok)
    except Exception:
        logger.exception("[ENTRY FINAL FILTER FAILOPEN] direction fail-closed patch install failed")

    _PATCHED = True
    logger.warning(
        "[ENTRY FINAL FILTER FAILOPEN] installed range_fail_open=%s direction_recursion_fail_open=%s direction_error_fail_open=%s allow_without_board=%s max_symbol_entries=%s",
        _env_bool("RANGE_5M_FILTER_NG_FAIL_OPEN", True),
        _env_bool("ENTRY_DIRECTION_CONFIRM_RECURSION_FAIL_OPEN", False),
        _env_bool("ENTRY_DIRECTION_CONFIRM_ERROR_FAIL_OPEN", False),
        os.getenv("ENTRY_ALLOW_ENTRY_WITHOUT_BOARD"),
        os.getenv("ENTRY_MAX_DAILY_ENTRIES_PER_SYMBOL"),
    )
    return True


try:
    install()
except Exception:
    logger.exception("[ENTRY FINAL FILTER FAILOPEN] auto install failed")

__all__ = ["install"]
