# ============================================================
# File   : core/startup/exit_tuning_defaults_patch.py
# Version: V1.3-FAST-CUT-LOSS-SCALPING
# ------------------------------------------------------------
# Purpose:
#   EXIT条件をよりスキャルピング寄りへ調整する。
#
# V1.3:
#   - 損失拡大後に返済して、その後戻る問題への対策。
#   - 損切りを -0.25% -> -0.15% へ強制。
#   - 逆行時の時間撤退を 60s -> 20s、停滞撤退を 90s -> 30s へ短縮。
#   - 利益ロックも早める。
#   - 既存環境変数が古い値を持っていても、明示的に上書きする。
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
        # 逆行は早めに切る。0.25%では遅いので0.15%へ。
        _force("ABSOLUTE_ENTRY_STOP_LOSS_PCT", "0.15")
        _force("TRAILING_DRAWDOWN_PCT", "0.0015")

        # 一度も利益方向へ進まない銘柄は待たない。
        _force("THREE_MIN_PROFIT_ESCAPE_SEC", "20")
        _force("THREE_MIN_PROFIT_ESCAPE_TARGET_PCT", "0.06")
        _force("THREE_MIN_PROFIT_ESCAPE_MIN_CURRENT_PCT", "-0.03")
        _force("THREE_MIN_FLAT_EXIT_ABS_PCT", "0.03")

        # 停滞・伸びない銘柄は30秒で撤退寄り。
        _force("EARLY_PROFIT_GUARD_ENABLED", "1")
        _force("EARLY_NO_PROGRESS_SECONDS", "30")
        _force("EARLY_NO_PROGRESS_NEED_PCT", "0.0002")
        _force("EARLY_STAGNATION_EXIT_ENABLED", "1")
        _force("EARLY_STAGNATION_SECONDS", "30")
        _force("EARLY_STAGNATION_NEED_PCT", "0.0002")

        # +0.10%で半分利確。200株未満は小ロット全利確で逃げる。
        _force("PARTIAL_PROFIT_ENABLED", "1")
        _force("PARTIAL_PROFIT_TRIGGER_PCT", "0.10")
        _force("PARTIAL_PROFIT_RATIO", "0.50")
        _setdefault("PARTIAL_PROFIT_MIN_QTY", "200")

        # 薄利即利確も少し早める。
        _force("BLOWOFF_PROFIT_TAKE_ENABLED", "1")
        _force("BLOWOFF_SMALL_QTY_FULL_TAKE_PCT", "0.08")
        _force("BLOWOFF_PARTIAL_TAKE_PCT", "0.10")
        _force("BLOWOFF_FULL_TAKE_PCT", "0.18")
        _setdefault("BLOWOFF_SMALL_QTY_MAX", "199")
        _force("BLOWOFF_PARTIAL_RATIO", "0.50")
        _force("BLOWOFF_CONFIRM_ENABLED", "1")
        _force("BLOWOFF_REQUIRE_VOLUME_AND_SLOPE", "0")
        _force("BLOWOFF_CONFIRM_FAIL_OPEN", "1")

        # 利益ロック。少しでも伸びたあとに建値割れまで待たない。
        _force("ENTRY_TRAIL_RETRACE_EXIT_PCT", "0.06")
        _force("EXIT_PROFIT_LOCK_ENABLED", "1")
        _force("EXIT_PROFIT_LOCK_MIN_MFE_PCT", "0.05")
        _force("EXIT_PROFIT_LOCK_FLOOR_PCT", "0.00")
        _force("EXIT_PROFIT_LOCK_RETRACE_MIN_MFE_PCT", "0.08")
        _force("EXIT_PROFIT_LOCK_RETRACE_PCT", "0.03")
        _setdefault("EXIT_PROFIT_LOCK_SMALL_QTY_MAX", "199")
        _force("EXIT_PROFIT_LOCK_SMALL_FULL_TAKE_PCT", "0.08")

        # EXITループ自体も早く回す/詰まりにくくする。
        _force("EXIT_LOOP_RUN_TIMEOUT_SEC", "2.0")
        _force("EXIT_LOOP_STUCK_WARN_SEC", "4.0")

        # Tonosama SELL EXIT を有効化。
        _force("TONOSAMA_EXIT_ENABLED", "1")
        _force("TONOSAMA_EXIT_SELL_ENABLED", "1")
        _force("TONOSAMA_EXIT_DRY_RUN", "0")
        _force("TONOSAMA_EXIT_USE_5SEC", "1")
        _force("TONOSAMA_EXIT_5SEC_VWAP_BREAK_SINGLE", "1")

        _INSTALLED = True
        logger.warning(
            "[EXIT TUNING DEFAULTS] installed FAST_CUT stop=%s partial=%s blowoff_small=%s blowoff_partial=%s blowoff_full=%s trail=%s escape_sec=%s flat=%s early_sec=%s lock_min_mfe=%s lock_floor=%s loop_timeout=%s tonosama_sell=%s",
            os.environ.get("ABSOLUTE_ENTRY_STOP_LOSS_PCT"),
            os.environ.get("PARTIAL_PROFIT_TRIGGER_PCT"),
            os.environ.get("BLOWOFF_SMALL_QTY_FULL_TAKE_PCT"),
            os.environ.get("BLOWOFF_PARTIAL_TAKE_PCT"),
            os.environ.get("BLOWOFF_FULL_TAKE_PCT"),
            os.environ.get("ENTRY_TRAIL_RETRACE_EXIT_PCT"),
            os.environ.get("THREE_MIN_PROFIT_ESCAPE_SEC"),
            os.environ.get("THREE_MIN_FLAT_EXIT_ABS_PCT"),
            os.environ.get("EARLY_NO_PROGRESS_SECONDS"),
            os.environ.get("EXIT_PROFIT_LOCK_MIN_MFE_PCT"),
            os.environ.get("EXIT_PROFIT_LOCK_FLOOR_PCT"),
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
