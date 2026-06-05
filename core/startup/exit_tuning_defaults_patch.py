# ============================================================
# File   : core/startup/exit_tuning_defaults_patch.py
# Version: V1.7-PROFIT-PROTECT-WITH-ORDER-WAIT
# ------------------------------------------------------------
# Purpose:
#   EXIT条件を「利が乗ったら損にしない」スキャルピング寄りへ戻す。
#
# V1.7:
#   - 現行V1.6は利益を伸ばす設定で、ユーザー要望
#       「利確も損にならないうちに撤退したい」
#     と逆方向になっていたため再調整。
#   - +0.03%から利益ロック、+0.06%で半分利確、小ロット+0.05%で全利確寄り。
#   - 損切りは -0.15%。
#   - EXIT注文の送信〜結果確認に数秒以上かかるため、EXIT_LOOP timeoutは2秒ではなく15秒。
#     2秒だと注文中に scheduler が解放され、previous worker still alive が連発する。
# ============================================================

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)
_INSTALLED = False


def _force(name: str, value: str) -> None:
    try:
        old = os.environ.get(name)
        os.environ[name] = str(value)
        if old != str(value):
            logger.warning("[EXIT TUNING DEFAULTS] env force %s %s->%s", name, old, value)
    except Exception:
        pass


def _setdefault(name: str, value: str) -> None:
    try:
        os.environ.setdefault(name, str(value))
    except Exception:
        pass


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        # 逆行は早めに切る。
        _force("ABSOLUTE_ENTRY_STOP_LOSS_PCT", "0.15")
        _force("TONOSAMA_STOP_LOSS_PCT", "0.18")
        _force("TRAILING_DRAWDOWN_PCT", "0.0015")

        # 一度も利益方向へ進まない銘柄は待ちすぎない。
        _force("THREE_MIN_PROFIT_ESCAPE_SEC", "20")
        _force("THREE_MIN_PROFIT_ESCAPE_TARGET_PCT", "0.04")
        _force("THREE_MIN_PROFIT_ESCAPE_MIN_CURRENT_PCT", "-0.02")
        _force("THREE_MIN_FLAT_EXIT_ABS_PCT", "0.02")

        # 停滞・伸びない銘柄は30秒で撤退寄り。
        _force("EARLY_PROFIT_GUARD_ENABLED", "1")
        _force("EARLY_NO_PROGRESS_SECONDS", "30")
        _force("EARLY_NO_PROGRESS_NEED_PCT", "0.00015")
        _force("EARLY_STAGNATION_EXIT_ENABLED", "1")
        _force("EARLY_STAGNATION_SECONDS", "30")
        _force("EARLY_STAGNATION_NEED_PCT", "0.00015")

        # 利確を早める。少しでも乗ったら半分逃がす。
        _force("PARTIAL_PROFIT_ENABLED", "1")
        _force("PARTIAL_PROFIT_TRIGGER_PCT", "0.06")
        _force("PARTIAL_PROFIT_RATIO", "0.50")
        _setdefault("PARTIAL_PROFIT_MIN_QTY", "200")

        # 小ロット/薄利は損になる前に逃げる。
        _force("BLOWOFF_PROFIT_TAKE_ENABLED", "1")
        _force("BLOWOFF_SMALL_QTY_FULL_TAKE_PCT", "0.05")
        _force("BLOWOFF_PARTIAL_TAKE_PCT", "0.06")
        _force("BLOWOFF_FULL_TAKE_PCT", "0.12")
        _setdefault("BLOWOFF_SMALL_QTY_MAX", "199")
        _force("BLOWOFF_PARTIAL_RATIO", "0.50")
        _force("BLOWOFF_CONFIRM_ENABLED", "1")
        _force("BLOWOFF_REQUIRE_VOLUME_AND_SLOPE", "0")
        _force("BLOWOFF_CONFIRM_FAIL_OPEN", "1")

        # 利益ロック。+0.03%でも一度プラスになったら、損になる前に撤退対象。
        _force("ENTRY_TRAIL_RETRACE_EXIT_PCT", "0.03")
        _force("EXIT_PROFIT_LOCK_ENABLED", "1")
        _force("EXIT_PROFIT_LOCK_MIN_MFE_PCT", "0.03")
        _force("EXIT_PROFIT_LOCK_FLOOR_PCT", "0.01")
        _force("EXIT_PROFIT_LOCK_RETRACE_MIN_MFE_PCT", "0.04")
        _force("EXIT_PROFIT_LOCK_RETRACE_PCT", "0.015")
        _setdefault("EXIT_PROFIT_LOCK_SMALL_QTY_MAX", "199")
        _force("EXIT_PROFIT_LOCK_SMALL_FULL_TAKE_PCT", "0.05")

        # 実装側で使われている場合に備えた別名系。未使用なら無害。
        _force("BREAKEVEN_PROTECT_ENABLED", "1")
        _force("BREAKEVEN_PROTECT_MIN_MFE_PCT", "0.03")
        _force("BREAKEVEN_PROTECT_EXIT_FLOOR_PCT", "0.005")
        _force("PROFIT_TO_LOSS_EXIT_ENABLED", "1")
        _force("PROFIT_TO_LOSS_MIN_MFE_PCT", "0.03")
        _force("PROFIT_TO_LOSS_EXIT_BEFORE_PCT", "0.005")

        # 別パッチ系の利益トレーリングも早める。
        _force("EXIT_PROFIT_TRAIL_START_PCT", "0.08")
        _force("EXIT_PROFIT_TRAIL_DRAWDOWN_PCT", "0.03")

        # EXIT注文送信〜結果確認には実測で10秒超かかることがある。
        # 2秒では worker timeout -> previous worker still alive が連発するため15秒へ。
        _force("EXIT_LOOP_RUN_TIMEOUT_SEC", "15.0")
        _force("EXIT_LOOP_STUCK_WARN_SEC", "20.0")

        # Tonosama SELL EXIT を維持。
        _force("TONOSAMA_EXIT_ENABLED", "1")
        _force("TONOSAMA_EXIT_SELL_ENABLED", "1")
        _force("TONOSAMA_EXIT_DRY_RUN", "0")
        _force("TONOSAMA_EXIT_USE_5SEC", "1")
        _force("TONOSAMA_EXIT_5SEC_VWAP_BREAK_SINGLE", "1")

        _INSTALLED = True
        logger.warning(
            "[EXIT TUNING DEFAULTS] installed PROFIT_PROTECT_WAIT stop=%s tonosama_stop=%s partial=%s blowoff_small=%s blowoff_partial=%s blowoff_full=%s trail_start=%s trail_dd=%s escape_sec=%s flat=%s early_sec=%s lock_min_mfe=%s lock_floor=%s lock_retrace=%s loop_timeout=%s stuck_warn=%s tonosama_sell=%s",
            os.environ.get("ABSOLUTE_ENTRY_STOP_LOSS_PCT"),
            os.environ.get("TONOSAMA_STOP_LOSS_PCT"),
            os.environ.get("PARTIAL_PROFIT_TRIGGER_PCT"),
            os.environ.get("BLOWOFF_SMALL_QTY_FULL_TAKE_PCT"),
            os.environ.get("BLOWOFF_PARTIAL_TAKE_PCT"),
            os.environ.get("BLOWOFF_FULL_TAKE_PCT"),
            os.environ.get("EXIT_PROFIT_TRAIL_START_PCT"),
            os.environ.get("EXIT_PROFIT_TRAIL_DRAWDOWN_PCT"),
            os.environ.get("THREE_MIN_PROFIT_ESCAPE_SEC"),
            os.environ.get("THREE_MIN_FLAT_EXIT_ABS_PCT"),
            os.environ.get("EARLY_NO_PROGRESS_SECONDS"),
            os.environ.get("EXIT_PROFIT_LOCK_MIN_MFE_PCT"),
            os.environ.get("EXIT_PROFIT_LOCK_FLOOR_PCT"),
            os.environ.get("EXIT_PROFIT_LOCK_RETRACE_PCT"),
            os.environ.get("EXIT_LOOP_RUN_TIMEOUT_SEC"),
            os.environ.get("EXIT_LOOP_STUCK_WARN_SEC"),
            os.environ.get("TONOSAMA_EXIT_SELL_ENABLED"),
        )
        return True
    except Exception:
        logger.exception("[EXIT TUNING DEFAULTS] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[EXIT TUNING DEFAULTS] auto install failed")


__all__ = ["install"]
