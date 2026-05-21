# ============================================================
# File   : core/startup/entry_final_filter_failopen_patch.py
# Version: V1.6-SHORT-MTF-REQUIRED-DAILY-OPTIONAL
# ------------------------------------------------------------
# 【目的】
#   候補・AI・pending までは通るのに、最後で全落ちする問題の緩和。
#
# 【対象】
#   - range_5m_filter が 5分足欠損/未完成で False を返すケース
#   - final_entry_safety_guard の board_missing で全落ちするケース
#   - SYMBOL_DAILY_ENTRY_LIMIT が 1回固定で強すぎるケース
#   - A/B PUSHローテーションで候補銘柄が反対面にいて板が取れないケース
#   - 日足MTFだけで発注停止してしまうケース
#
# 【方針】
#   - 5分足元フィルタNGは、発注停止ではなく既存の低変動ガード側に任せる
#   - 板が取れない場合は4.5秒待って再取得し、境界対策で0.3秒追加確認する
#   - 同一銘柄の当日発注済みカウントはデフォルト2回まで許可する
#   - 方向確認の再帰/例外は fail-open しない。安全側NGにする。
#   - pending化した候補銘柄をPUSH protectedへ渡し、A/B両面登録されやすくする。
#   - 発注直前MTFは 1分/3分/5分の傾きを必須、日足MTFはオプショナルにする。
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

    # --------------------------------------------------------
    # 3) pending候補銘柄をPUSH protectedへ渡す
    # --------------------------------------------------------
    try:
        from core.startup.pending_protect_push_patch import install as install_pending_protect_push
        ok = install_pending_protect_push()
        logger.warning("[ENTRY FINAL FILTER FAILOPEN] pending protect push patch installed=%s", ok)
    except Exception:
        logger.exception("[ENTRY FINAL FILTER FAILOPEN] pending protect push patch install failed")

    # --------------------------------------------------------
    # 4) 板が無い場合、A/Bローテーションを考慮して4.5秒後に再取得
    # --------------------------------------------------------
    try:
        from core.startup.board_retry_patch import install as install_board_retry
        ok = install_board_retry()
        logger.warning("[ENTRY FINAL FILTER FAILOPEN] board retry patch installed=%s", ok)
    except Exception:
        logger.exception("[ENTRY FINAL FILTER FAILOPEN] board retry patch install failed")

    # --------------------------------------------------------
    # 5) 発注直前MTFは短期1/3/5分を必須、日足MTFはオプショナル
    # --------------------------------------------------------
    try:
        from core.startup.entry_mtf_short_required_daily_optional_patch import install as install_short_mtf
        ok = install_short_mtf()
        logger.warning("[ENTRY FINAL FILTER FAILOPEN] short MTF daily optional patch installed=%s", ok)
    except Exception:
        logger.exception("[ENTRY FINAL FILTER FAILOPEN] short MTF daily optional patch install failed")

    _PATCHED = True
    logger.warning(
        "[ENTRY FINAL FILTER FAILOPEN] installed range_fail_open=%s direction_recursion_fail_open=%s direction_error_fail_open=%s allow_without_board=%s max_symbol_entries=%s pending_protect_push=%s board_retry=%s board_retry_wait=%s board_retry_extra_wait=%s board_retry_extra_count=%s short_mtf_required=%s short_mtf_require_all=%s daily_mtf_optional=%s",
        _env_bool("RANGE_5M_FILTER_NG_FAIL_OPEN", True),
        _env_bool("ENTRY_DIRECTION_CONFIRM_RECURSION_FAIL_OPEN", False),
        _env_bool("ENTRY_DIRECTION_CONFIRM_ERROR_FAIL_OPEN", False),
        os.getenv("ENTRY_ALLOW_ENTRY_WITHOUT_BOARD"),
        os.getenv("ENTRY_MAX_DAILY_ENTRIES_PER_SYMBOL"),
        os.getenv("PENDING_PROTECT_PUSH_SYMBOLS"),
        os.getenv("ENTRY_BOARD_RETRY_ENABLED"),
        os.getenv("ENTRY_BOARD_RETRY_WAIT_SEC"),
        os.getenv("ENTRY_BOARD_RETRY_EXTRA_WAIT_SEC"),
        os.getenv("ENTRY_BOARD_RETRY_EXTRA_COUNT"),
        os.getenv("ENTRY_SHORT_MTF_REQUIRED"),
        os.getenv("ENTRY_SHORT_MTF_REQUIRE_ALL"),
        os.getenv("ENTRY_DAILY_MTF_OPTIONAL"),
    )
    return True


try:
    install()
except Exception:
    logger.exception("[ENTRY FINAL FILTER FAILOPEN] auto install failed")

__all__ = ["install"]