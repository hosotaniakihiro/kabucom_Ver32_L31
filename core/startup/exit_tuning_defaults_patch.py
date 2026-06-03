# ============================================================
# File   : core/startup/exit_tuning_defaults_patch.py
# Version: V1.2-SCALPING-EXIT-TUNING
# ------------------------------------------------------------
# Purpose:
#   EXIT条件をよりスキャルピング寄りへ調整する。
#   既に環境変数で明示指定されている値は上書きしない。
#
# Tuning V1.2:
#   - 損切り: -0.40% -> -0.25%
#   - 通常一部利確: +0.18% -> +0.12%
#   - 吹き上げ小ロット全利確: +0.15% -> +0.10%
#   - 吹き上げ一部利確: +0.20% -> +0.12%
#   - 吹き上げ全利確: +0.35% -> +0.22%
#   - トレール: 0.18% -> 0.10%
#   - 時間撤退: 120s -> 60s, flat ±0.05%, target +0.10%
#   - 停滞撤退: 180s -> 90s
# ============================================================

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)
_INSTALLED = False


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
        # スキャル用。逆行は早めに切る。
        _setdefault("ABSOLUTE_ENTRY_STOP_LOSS_PCT", "0.25")
        _setdefault("TRAILING_DRAWDOWN_PCT", "0.0025")

        # +0.12%で半分利確。200株未満は吹き上げ小ロット全利確で逃げる。
        _setdefault("PARTIAL_PROFIT_ENABLED", "1")
        _setdefault("PARTIAL_PROFIT_TRIGGER_PCT", "0.12")
        _setdefault("PARTIAL_PROFIT_RATIO", "0.50")
        _setdefault("PARTIAL_PROFIT_MIN_QTY", "200")

        # 薄利即利確。
        _setdefault("BLOWOFF_PROFIT_TAKE_ENABLED", "1")
        _setdefault("BLOWOFF_SMALL_QTY_FULL_TAKE_PCT", "0.10")
        _setdefault("BLOWOFF_PARTIAL_TAKE_PCT", "0.12")
        _setdefault("BLOWOFF_FULL_TAKE_PCT", "0.22")
        _setdefault("BLOWOFF_SMALL_QTY_MAX", "199")
        _setdefault("BLOWOFF_PARTIAL_RATIO", "0.50")
        _setdefault("BLOWOFF_CONFIRM_ENABLED", "1")
        _setdefault("BLOWOFF_REQUIRE_VOLUME_AND_SLOPE", "0")
        _setdefault("BLOWOFF_CONFIRM_FAIL_OPEN", "1")

        # 戻りは浅く撤退。
        _setdefault("ENTRY_TRAIL_RETRACE_EXIT_PCT", "0.10")
        _setdefault("THREE_MIN_PROFIT_ESCAPE_SEC", "60")
        _setdefault("THREE_MIN_PROFIT_ESCAPE_TARGET_PCT", "0.10")
        _setdefault("THREE_MIN_PROFIT_ESCAPE_MIN_CURRENT_PCT", "0.00")
        _setdefault("THREE_MIN_FLAT_EXIT_ABS_PCT", "0.05")

        # 停滞撤退も短くする。
        _setdefault("EARLY_PROFIT_GUARD_ENABLED", "1")
        _setdefault("EARLY_NO_PROGRESS_SECONDS", "90")
        _setdefault("EARLY_NO_PROGRESS_NEED_PCT", "0.0003")
        _setdefault("EARLY_STAGNATION_EXIT_ENABLED", "1")
        _setdefault("EARLY_STAGNATION_SECONDS", "90")
        _setdefault("EARLY_STAGNATION_NEED_PCT", "0.0003")

        # 利益ロック。プラスを建値付近で失わない。
        _setdefault("EXIT_PROFIT_LOCK_ENABLED", "1")
        _setdefault("EXIT_PROFIT_LOCK_MIN_MFE_PCT", "0.08")
        _setdefault("EXIT_PROFIT_LOCK_FLOOR_PCT", "0.01")
        _setdefault("EXIT_PROFIT_LOCK_RETRACE_MIN_MFE_PCT", "0.12")
        _setdefault("EXIT_PROFIT_LOCK_RETRACE_PCT", "0.05")
        _setdefault("EXIT_PROFIT_LOCK_SMALL_QTY_MAX", "199")
        _setdefault("EXIT_PROFIT_LOCK_SMALL_FULL_TAKE_PCT", "0.10")

        # Tonosama SELL EXIT を有効化。
        _setdefault("TONOSAMA_EXIT_ENABLED", "1")
        _setdefault("TONOSAMA_EXIT_SELL_ENABLED", "1")
        _setdefault("TONOSAMA_EXIT_DRY_RUN", "0")
        _setdefault("TONOSAMA_EXIT_USE_5SEC", "1")
        _setdefault("TONOSAMA_EXIT_5SEC_VWAP_BREAK_SINGLE", "0")

        _INSTALLED = True
        logger.warning(
            "[EXIT TUNING DEFAULTS] installed SCALPING stop=%s partial=%s blowoff_small=%s blowoff_partial=%s blowoff_full=%s trail=%s escape_sec=%s flat=%s early_sec=%s lock_min_mfe=%s lock_floor=%s tonosama_sell=%s",
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
