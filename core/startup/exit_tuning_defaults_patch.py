# ============================================================
# File   : core/startup/exit_tuning_defaults_patch.py
# Version: V1.0-SAFER-EXIT-TUNING-DEFAULTS
# ------------------------------------------------------------
# Purpose:
#   EXIT条件の推奨値を起動時に安全側へ調整する。
#   既に環境変数で明示指定されている値は上書きしない。
#
# Tuning:
#   - 損切り: -0.30% -> -0.40%
#   - 通常一部利確: +0.20% -> +0.30%
#   - 吹き上げ利確: small +0.25%, partial +0.30%, full +0.60%
#   - トレール: 0.25% -> 0.30%
#   - 3分撤退: 180s -> 240s, flat ±0.15%, target +0.25%
#   - Tonosama SELL EXIT enabled
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
        # 損切りを少し広げる。一瞬の板ブレ/スプレッドで切られにくくする。
        _setdefault("ABSOLUTE_ENTRY_STOP_LOSS_PCT", "0.40")
        _setdefault("TRAILING_DRAWDOWN_PCT", "0.0040")

        # 通常一部利確は +0.30%。+0.20%は薄すぎるため少し待つ。
        _setdefault("PARTIAL_PROFIT_ENABLED", "1")
        _setdefault("PARTIAL_PROFIT_TRIGGER_PCT", "0.30")
        _setdefault("PARTIAL_PROFIT_RATIO", "0.50")
        _setdefault("PARTIAL_PROFIT_MIN_QTY", "200")

        # 吹き上げ利確は通常利確より早く、ただし全利確は +0.60%まで伸ばす。
        _setdefault("BLOWOFF_PROFIT_TAKE_ENABLED", "1")
        _setdefault("BLOWOFF_SMALL_QTY_FULL_TAKE_PCT", "0.25")
        _setdefault("BLOWOFF_PARTIAL_TAKE_PCT", "0.30")
        _setdefault("BLOWOFF_FULL_TAKE_PCT", "0.60")
        _setdefault("BLOWOFF_SMALL_QTY_MAX", "199")
        _setdefault("BLOWOFF_PARTIAL_RATIO", "0.50")
        _setdefault("BLOWOFF_CONFIRM_ENABLED", "1")
        _setdefault("BLOWOFF_REQUIRE_VOLUME_AND_SLOPE", "1")
        _setdefault("BLOWOFF_CONFIRM_FAIL_OPEN", "0")

        # トレールと時間撤退。
        _setdefault("ENTRY_TRAIL_RETRACE_EXIT_PCT", "0.30")
        _setdefault("THREE_MIN_PROFIT_ESCAPE_SEC", "240")
        _setdefault("THREE_MIN_PROFIT_ESCAPE_TARGET_PCT", "0.25")
        _setdefault("THREE_MIN_PROFIT_ESCAPE_MIN_CURRENT_PCT", "0.00")
        _setdefault("THREE_MIN_FLAT_EXIT_ABS_PCT", "0.15")

        # 停滞撤退は維持。進展幅は0.05%、5分停止で撤退。
        _setdefault("EARLY_PROFIT_GUARD_ENABLED", "1")
        _setdefault("EARLY_NO_PROGRESS_SECONDS", "300")
        _setdefault("EARLY_NO_PROGRESS_NEED_PCT", "0.0005")
        _setdefault("EARLY_STAGNATION_EXIT_ENABLED", "1")
        _setdefault("EARLY_STAGNATION_SECONDS", "300")
        _setdefault("EARLY_STAGNATION_NEED_PCT", "0.0005")

        # Tonosama SELL EXIT を有効化。BUY policyへミラー変換して使う。
        _setdefault("TONOSAMA_EXIT_ENABLED", "1")
        _setdefault("TONOSAMA_EXIT_SELL_ENABLED", "1")
        _setdefault("TONOSAMA_EXIT_DRY_RUN", "0")

        # 5秒足EXITは使うが、単独VWAP割れ/上抜けだけでは逃げない。
        _setdefault("TONOSAMA_EXIT_USE_5SEC", "1")
        _setdefault("TONOSAMA_EXIT_5SEC_VWAP_BREAK_SINGLE", "0")

        _INSTALLED = True
        logger.warning(
            "[EXIT TUNING DEFAULTS] installed stop=%s partial=%s blowoff_small=%s blowoff_partial=%s blowoff_full=%s trail=%s three_sec=%s flat=%s tonosama_sell=%s",
            os.environ.get("ABSOLUTE_ENTRY_STOP_LOSS_PCT"),
            os.environ.get("PARTIAL_PROFIT_TRIGGER_PCT"),
            os.environ.get("BLOWOFF_SMALL_QTY_FULL_TAKE_PCT"),
            os.environ.get("BLOWOFF_PARTIAL_TAKE_PCT"),
            os.environ.get("BLOWOFF_FULL_TAKE_PCT"),
            os.environ.get("ENTRY_TRAIL_RETRACE_EXIT_PCT"),
            os.environ.get("THREE_MIN_PROFIT_ESCAPE_SEC"),
            os.environ.get("THREE_MIN_FLAT_EXIT_ABS_PCT"),
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
