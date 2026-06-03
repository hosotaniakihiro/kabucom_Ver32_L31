# ============================================================
# File   : core/startup/exit_tuning_defaults_patch.py
# Version: V1.1-FAST-PROFIT-PROTECTION
# ------------------------------------------------------------
# Purpose:
#   プラスだった建玉が利確遅れでマイナス化する問題を防ぐ。
#   既に環境変数で明示指定されている値は上書きしない。
#
# Tuning V1.1:
#   - 通常一部利確: +0.30% -> +0.18%
#   - 吹き上げ小ロット全利確: +0.25% -> +0.15%
#   - 吹き上げ一部利確: +0.30% -> +0.20%
#   - 吹き上げ全利確: +0.60% -> +0.35%
#   - トレール: 0.30% -> 0.18%
#   - 3分撤退: 240s -> 120s, flat ±0.08%, target +0.15%
#   - 損切り: -0.40%維持
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
        # 損切りは板ブレ対策で少し広め。ただし利益保護は早める。
        _setdefault("ABSOLUTE_ENTRY_STOP_LOSS_PCT", "0.40")
        _setdefault("TRAILING_DRAWDOWN_PCT", "0.0030")

        # 利確遅れ対策: +0.18%で半分利確。
        _setdefault("PARTIAL_PROFIT_ENABLED", "1")
        _setdefault("PARTIAL_PROFIT_TRIGGER_PCT", "0.18")
        _setdefault("PARTIAL_PROFIT_RATIO", "0.50")
        _setdefault("PARTIAL_PROFIT_MIN_QTY", "200")

        # 吹き上げ利確をかなり早める。
        _setdefault("BLOWOFF_PROFIT_TAKE_ENABLED", "1")
        _setdefault("BLOWOFF_SMALL_QTY_FULL_TAKE_PCT", "0.15")
        _setdefault("BLOWOFF_PARTIAL_TAKE_PCT", "0.20")
        _setdefault("BLOWOFF_FULL_TAKE_PCT", "0.35")
        _setdefault("BLOWOFF_SMALL_QTY_MAX", "199")
        _setdefault("BLOWOFF_PARTIAL_RATIO", "0.50")
        _setdefault("BLOWOFF_CONFIRM_ENABLED", "1")
        _setdefault("BLOWOFF_REQUIRE_VOLUME_AND_SLOPE", "0")
        _setdefault("BLOWOFF_CONFIRM_FAIL_OPEN", "1")

        # トレールも浅くしてプラスを守る。
        _setdefault("ENTRY_TRAIL_RETRACE_EXIT_PCT", "0.18")
        _setdefault("THREE_MIN_PROFIT_ESCAPE_SEC", "120")
        _setdefault("THREE_MIN_PROFIT_ESCAPE_TARGET_PCT", "0.15")
        _setdefault("THREE_MIN_PROFIT_ESCAPE_MIN_CURRENT_PCT", "0.00")
        _setdefault("THREE_MIN_FLAT_EXIT_ABS_PCT", "0.08")

        # 停滞撤退を早める。
        _setdefault("EARLY_PROFIT_GUARD_ENABLED", "1")
        _setdefault("EARLY_NO_PROGRESS_SECONDS", "180")
        _setdefault("EARLY_NO_PROGRESS_NEED_PCT", "0.0004")
        _setdefault("EARLY_STAGNATION_EXIT_ENABLED", "1")
        _setdefault("EARLY_STAGNATION_SECONDS", "180")
        _setdefault("EARLY_STAGNATION_NEED_PCT", "0.0004")

        # 利益ロック用の環境値。専用patchが入る環境ではこれを使う。
        _setdefault("EXIT_PROFIT_LOCK_ENABLED", "1")
        _setdefault("EXIT_PROFIT_LOCK_MIN_MFE_PCT", "0.12")
        _setdefault("EXIT_PROFIT_LOCK_FLOOR_PCT", "0.02")
        _setdefault("EXIT_PROFIT_LOCK_RETRACE_MIN_MFE_PCT", "0.20")
        _setdefault("EXIT_PROFIT_LOCK_RETRACE_PCT", "0.10")
        _setdefault("EXIT_PROFIT_LOCK_SMALL_QTY_MAX", "199")
        _setdefault("EXIT_PROFIT_LOCK_SMALL_FULL_TAKE_PCT", "0.15")

        # Tonosama SELL EXIT を有効化。
        _setdefault("TONOSAMA_EXIT_ENABLED", "1")
        _setdefault("TONOSAMA_EXIT_SELL_ENABLED", "1")
        _setdefault("TONOSAMA_EXIT_DRY_RUN", "0")
        _setdefault("TONOSAMA_EXIT_USE_5SEC", "1")
        _setdefault("TONOSAMA_EXIT_5SEC_VWAP_BREAK_SINGLE", "0")

        _INSTALLED = True
        logger.warning(
            "[EXIT TUNING DEFAULTS] installed FAST_PROFIT stop=%s partial=%s blowoff_small=%s blowoff_partial=%s blowoff_full=%s trail=%s three_sec=%s flat=%s early_sec=%s tonosama_sell=%s profit_lock=%s",
            os.environ.get("ABSOLUTE_ENTRY_STOP_LOSS_PCT"),
            os.environ.get("PARTIAL_PROFIT_TRIGGER_PCT"),
            os.environ.get("BLOWOFF_SMALL_QTY_FULL_TAKE_PCT"),
            os.environ.get("BLOWOFF_PARTIAL_TAKE_PCT"),
            os.environ.get("BLOWOFF_FULL_TAKE_PCT"),
            os.environ.get("ENTRY_TRAIL_RETRACE_EXIT_PCT"),
            os.environ.get("THREE_MIN_PROFIT_ESCAPE_SEC"),
            os.environ.get("THREE_MIN_FLAT_EXIT_ABS_PCT"),
            os.environ.get("EARLY_NO_PROGRESS_SECONDS"),
            os.environ.get("TONOSAMA_EXIT_SELL_ENABLED"),
            os.environ.get("EXIT_PROFIT_LOCK_ENABLED"),
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
