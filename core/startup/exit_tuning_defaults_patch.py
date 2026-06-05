# ============================================================
# File   : core/startup/exit_tuning_defaults_patch.py
# Version: V1.8-LET-PROFIT-RUN-LONGER
# ------------------------------------------------------------
# Purpose:
#   EXIT条件を「早く逃げる」から「利益を伸ばす」方向へ再調整する。
#
# V1.8:
#   - 6323 が +0.3387% で保有115秒、idle92秒により
#     EARLY_STAGNATION_BUY で早期利確されたため緩和。
#   - 既存の V1.7-PROFIT-PROTECT-WITH-ORDER-WAIT が薄利撤退へ戻していたため、
#     ユーザー要望「利益は伸ばしたい」に合わせて上書き。
#   - 停滞撤退 90秒 -> 180秒。
#   - progress_need 0.05% -> 0.08%。
#   - +0.35%未満では停滞撤退しない floor を追加。
#   - EXIT_LOOP timeout は注文完了待ちのため 15秒を維持。
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
        # エントリー直後のブレで損切りしすぎない設定。
        _force("ABSOLUTE_ENTRY_STOP_LOSS_PCT", "0.35")
        _force("TONOSAMA_STOP_LOSS_PCT", "0.40")
        _force("TRAILING_DRAWDOWN_PCT", "0.0030")
        _force("EXIT_INITIAL_STOP_GRACE_SEC", "30")
        _force("EXIT_INITIAL_GRACE_HARD_STOP_PCT", "0.60")

        # 利益なし/小動き撤退は少なくとも90秒待つ。
        _force("THREE_MIN_PROFIT_ESCAPE_SEC", "90")
        _force("THREE_MIN_PROFIT_ESCAPE_TARGET_PCT", "0.15")
        _force("THREE_MIN_PROFIT_ESCAPE_MIN_CURRENT_PCT", "-0.10")
        _force("THREE_MIN_FLAT_EXIT_ABS_PCT", "0.06")

        # 利益を伸ばす: 停滞撤退は180秒まで待つ。
        _force("EARLY_PROFIT_GUARD_ENABLED", "1")
        _force("EARLY_NO_PROGRESS_SECONDS", "180")
        _force("EARLY_NO_PROGRESS_NEED_PCT", "0.00080")
        _force("EARLY_STAGNATION_EXIT_ENABLED", "1")
        _force("EARLY_STAGNATION_SECONDS", "180")
        _force("EARLY_STAGNATION_NEED_PCT", "0.00080")
        # early_profit_guard 側が対応していれば、+0.35%未満では停滞利確しない。
        _force("EARLY_STAGNATION_MIN_PROFIT_PCT", "0.35")
        _force("EARLY_NO_PROGRESS_MIN_PROFIT_PCT", "0.35")

        # 分割利確: +0.40%で半分だけ。
        _force("PARTIAL_PROFIT_ENABLED", "1")
        _force("PARTIAL_PROFIT_TRIGGER_PCT", "0.40")
        _force("PARTIAL_PROFIT_RATIO", "0.50")
        _setdefault("PARTIAL_PROFIT_MIN_QTY", "200")

        # 吹き上げ/小ロット全利確も後ろへ。+0.90%までは伸ばす。
        _force("BLOWOFF_PROFIT_TAKE_ENABLED", "1")
        _force("BLOWOFF_SMALL_QTY_FULL_TAKE_PCT", "0.50")
        _force("BLOWOFF_PARTIAL_TAKE_PCT", "0.40")
        _force("BLOWOFF_FULL_TAKE_PCT", "0.90")
        _setdefault("BLOWOFF_SMALL_QTY_MAX", "199")
        _force("BLOWOFF_PARTIAL_RATIO", "0.50")
        _force("BLOWOFF_CONFIRM_ENABLED", "1")
        _force("BLOWOFF_REQUIRE_VOLUME_AND_SLOPE", "0")
        _force("BLOWOFF_CONFIRM_FAIL_OPEN", "1")

        # 利益ロックは +0.25% から。床は +0.05%。
        _force("ENTRY_TRAIL_RETRACE_EXIT_PCT", "0.30")
        _force("EXIT_PROFIT_LOCK_ENABLED", "1")
        _force("EXIT_PROFIT_LOCK_MIN_MFE_PCT", "0.25")
        _force("EXIT_PROFIT_LOCK_FLOOR_PCT", "0.05")
        _force("EXIT_PROFIT_LOCK_RETRACE_MIN_MFE_PCT", "0.35")
        _force("EXIT_PROFIT_LOCK_RETRACE_PCT", "0.12")
        _setdefault("EXIT_PROFIT_LOCK_SMALL_QTY_MAX", "199")
        _force("EXIT_PROFIT_LOCK_SMALL_FULL_TAKE_PCT", "0.50")

        # 建値保護・利益から損失への撤退も +0.25% 以上乗ってから。
        _force("BREAKEVEN_PROTECT_ENABLED", "1")
        _force("BREAKEVEN_PROTECT_MIN_MFE_PCT", "0.25")
        _force("BREAKEVEN_PROTECT_EXIT_FLOOR_PCT", "0.02")
        _force("PROFIT_TO_LOSS_EXIT_ENABLED", "1")
        _force("PROFIT_TO_LOSS_MIN_MFE_PCT", "0.25")
        _force("PROFIT_TO_LOSS_EXIT_BEFORE_PCT", "0.02")

        # 利益トレーリング。
        _force("EXIT_PROFIT_TRAIL_START_PCT", "0.50")
        _force("EXIT_PROFIT_TRAIL_DRAWDOWN_PCT", "0.30")

        # EXIT注文の完了待ちに必要。2秒だと previous worker still alive が連発する。
        _force("EXIT_LOOP_RUN_TIMEOUT_SEC", "15.0")
        _force("EXIT_LOOP_STUCK_WARN_SEC", "20.0")

        # Tonosama SELL EXIT は維持。
        _force("TONOSAMA_EXIT_ENABLED", "1")
        _force("TONOSAMA_EXIT_SELL_ENABLED", "1")
        _force("TONOSAMA_EXIT_DRY_RUN", "0")
        _force("TONOSAMA_EXIT_USE_5SEC", "1")
        _force("TONOSAMA_EXIT_5SEC_VWAP_BREAK_SINGLE", "1")

        _INSTALLED = True
        logger.warning(
            "[EXIT TUNING DEFAULTS] installed LET_PROFIT_RUN_LONGER stop=%s tonosama_stop=%s partial=%s blowoff_small=%s blowoff_partial=%s blowoff_full=%s trail_start=%s trail_dd=%s escape_sec=%s early_sec=%s early_floor=%s lock_min_mfe=%s lock_floor=%s lock_retrace=%s loop_timeout=%s tonosama_sell=%s",
            os.environ.get("ABSOLUTE_ENTRY_STOP_LOSS_PCT"),
            os.environ.get("TONOSAMA_STOP_LOSS_PCT"),
            os.environ.get("PARTIAL_PROFIT_TRIGGER_PCT"),
            os.environ.get("BLOWOFF_SMALL_QTY_FULL_TAKE_PCT"),
            os.environ.get("BLOWOFF_PARTIAL_TAKE_PCT"),
            os.environ.get("BLOWOFF_FULL_TAKE_PCT"),
            os.environ.get("EXIT_PROFIT_TRAIL_START_PCT"),
            os.environ.get("EXIT_PROFIT_TRAIL_DRAWDOWN_PCT"),
            os.environ.get("THREE_MIN_PROFIT_ESCAPE_SEC"),
            os.environ.get("EARLY_NO_PROGRESS_SECONDS"),
            os.environ.get("EARLY_STAGNATION_MIN_PROFIT_PCT"),
            os.environ.get("EXIT_PROFIT_LOCK_MIN_MFE_PCT"),
            os.environ.get("EXIT_PROFIT_LOCK_FLOOR_PCT"),
            os.environ.get("EXIT_PROFIT_LOCK_RETRACE_PCT"),
            os.environ.get("EXIT_LOOP_RUN_TIMEOUT_SEC"),
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
