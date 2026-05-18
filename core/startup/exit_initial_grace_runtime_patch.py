# ============================================================
# File   : core/startup/exit_initial_grace_runtime_patch.py
# Version: Ver01-EXIT-INITIAL-GRACE-ANTI-INSTANT-LOSSCUT
# ------------------------------------------------------------
# エントリー直後にスプレッド負け・1ティック不利で即損切りされる問題を緩和する。
#
# 変更内容:
#   - 絶対損切り幅 0.30% -> 0.60%
#   - エントリー後トレーリング戻り幅 0.25% -> 0.50%
#   - 3分停滞EXITは維持
#   - collapse / AI exit / 明確な危険判定は維持
#
# 注意:
#   損切りを無効化するのではなく、成行約定直後のノイズで即切られにくくする。
# ============================================================

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)
_INSTALLED = False


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        import trading.exit.exit_position_runner as epr

        old_abs = getattr(epr, "ABSOLUTE_ENTRY_STOP_LOSS_PCT", None)
        old_trail = getattr(epr, "ENTRY_TRAIL_RETRACE_EXIT_PCT", None)
        old_flat = getattr(epr, "THREE_MIN_FLAT_EXIT_ABS_PCT", None)

        new_abs = _env_float("ABSOLUTE_ENTRY_STOP_LOSS_PCT", 0.60)
        new_trail = _env_float("ENTRY_TRAIL_RETRACE_EXIT_PCT", 0.50)
        new_flat = _env_float("THREE_MIN_FLAT_EXIT_ABS_PCT", 0.12)

        epr.ABSOLUTE_ENTRY_STOP_LOSS_PCT = float(new_abs)
        epr.ENTRY_TRAIL_RETRACE_EXIT_PCT = float(new_trail)
        epr.THREE_MIN_FLAT_EXIT_ABS_PCT = float(new_flat)

        os.environ.setdefault("ABSOLUTE_ENTRY_STOP_LOSS_PCT", str(new_abs))
        os.environ.setdefault("ENTRY_TRAIL_RETRACE_EXIT_PCT", str(new_trail))
        os.environ.setdefault("THREE_MIN_FLAT_EXIT_ABS_PCT", str(new_flat))

        _INSTALLED = True
        logger.warning(
            "[EXIT INITIAL GRACE PATCH] installed abs_stop_pct %s->%.3f trail_retrace_pct %s->%.3f flat_exit_abs_pct %s->%.3f",
            old_abs, epr.ABSOLUTE_ENTRY_STOP_LOSS_PCT,
            old_trail, epr.ENTRY_TRAIL_RETRACE_EXIT_PCT,
            old_flat, epr.THREE_MIN_FLAT_EXIT_ABS_PCT,
        )
        return True
    except Exception:
        logger.exception("[EXIT INITIAL GRACE PATCH] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[EXIT INITIAL GRACE PATCH] auto install failed")

__all__ = ["install"]
