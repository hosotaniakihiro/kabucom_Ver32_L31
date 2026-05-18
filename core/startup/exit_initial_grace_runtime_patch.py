# ============================================================
# File   : core/startup/exit_initial_grace_runtime_patch.py
# Version: Ver02-QUICK-PROFIT-REVERSE-EXIT
# ------------------------------------------------------------
# エントリー直後のノイズ損切りを避けつつ、
# 含み益が出た後に反対方向へ動いたら早めに利確する。
#
# 仕様:
#   - 絶対損切り幅は 0.60% のまま維持
#   - トレーリング戻り幅は 0.15% に縮小
#       BUY : 高値から -0.15% 戻ったら利確
#       SELL: 安値から +0.15% 戻ったら利確
#   - 一部利確は +0.15% 到達で発火しやすくする
#   - 3分停滞EXITは維持
#
# 注意:
#   損切りを早めるのではなく、含み益後の反転利確だけ早める。
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
        old_partial = getattr(epr, "PARTIAL_PROFIT_TRIGGER_PCT", None)

        new_abs = _env_float("ABSOLUTE_ENTRY_STOP_LOSS_PCT", 0.60)
        new_trail = _env_float("ENTRY_TRAIL_RETRACE_EXIT_PCT", 0.15)
        new_flat = _env_float("THREE_MIN_FLAT_EXIT_ABS_PCT", 0.12)
        new_partial = _env_float("PARTIAL_PROFIT_TRIGGER_PCT", 0.15)

        epr.ABSOLUTE_ENTRY_STOP_LOSS_PCT = float(new_abs)
        epr.ENTRY_TRAIL_RETRACE_EXIT_PCT = float(new_trail)
        epr.THREE_MIN_FLAT_EXIT_ABS_PCT = float(new_flat)
        epr.PARTIAL_PROFIT_TRIGGER_PCT = float(new_partial)

        os.environ.setdefault("ABSOLUTE_ENTRY_STOP_LOSS_PCT", str(new_abs))
        os.environ.setdefault("ENTRY_TRAIL_RETRACE_EXIT_PCT", str(new_trail))
        os.environ.setdefault("THREE_MIN_FLAT_EXIT_ABS_PCT", str(new_flat))
        os.environ.setdefault("PARTIAL_PROFIT_TRIGGER_PCT", str(new_partial))

        _INSTALLED = True
        logger.warning(
            "[EXIT INITIAL GRACE PATCH] installed quick_profit_reverse abs_stop_pct %s->%.3f trail_retrace_pct %s->%.3f partial_profit_pct %s->%.3f flat_exit_abs_pct %s->%.3f",
            old_abs, epr.ABSOLUTE_ENTRY_STOP_LOSS_PCT,
            old_trail, epr.ENTRY_TRAIL_RETRACE_EXIT_PCT,
            old_partial, epr.PARTIAL_PROFIT_TRIGGER_PCT,
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
