# ============================================================
# File   : trading/exit/partial_exit_engine.py
# Version: V65-PARTIAL-EXIT-ENGINE
# ------------------------------------------------------------
# ✔ collapse段階利確
# ✔ inago対応
# ✔ regime対応
# ✔ フルEXIT互換
# ✔ execute_exit非破壊
# ✔ thread safe
# ✔ フェイルセーフ
# ============================================================

from __future__ import annotations
import logging
import math
from threading import Lock

from core.global_context.context import global_context as GC
from trading.exit.executor import execute_exit

logger = logging.getLogger(__name__)


# ============================================================
# 安全数値
# ============================================================

def _safe(v, default=0.0):
    try:
        v = float(v)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except Exception:
        return default


# ============================================================
# PartialExitEngine
# ============================================================

class PartialExitEngine:

    def __init__(self):
        self._lock = Lock()

    # ========================================================
    # collapseベース決済比率
    # ========================================================

    def _collapse_ratio(self, collapse_strength: float) -> float:

        collapse_strength = _safe(collapse_strength)

        if collapse_strength > 0.95:
            return 1.0
        if collapse_strength > 0.85:
            return 0.7
        if collapse_strength > 0.75:
            return 0.5
        if collapse_strength > 0.65:
            return 0.3

        return 0.0

    # ========================================================
    # メイン処理
    # ========================================================

    def process_exit(
        self,
        *,
        symbol: str,
        price: float,
        reason: str,
        collapse_strength: float,
        regime: int,
        inago_state: int,
        force_full: bool = False,
    ) -> bool:
        """
        return:
            True  = フルEXIT
            False = 継続
        """

        with self._lock:

            try:

                positions = GC.positions.snapshot_open()
                if symbol not in positions:
                    return False

                pos = positions[symbol]
                qty = _safe(pos.get("qty"))

                if qty <= 0:
                    return False

                # 強制フルEXIT
                if force_full:
                    execute_exit(symbol, price, reason)
                    return True

                # collapse比率
                ratio = self._collapse_ratio(collapse_strength)

                # regime crashは最低50%
                if regime == 3:
                    ratio = max(ratio, 0.5)

                # inago exhaustは最低50%
                if inago_state == 2:
                    ratio = max(ratio, 0.5)

                if ratio <= 0:
                    return False

                exit_qty = int(qty * ratio)

                if exit_qty <= 0:
                    return False

                # フルになる場合
                if exit_qty >= qty:
                    execute_exit(symbol, price, reason)
                    return True

                # 部分決済
                execute_exit(symbol, price, f"{reason}_PARTIAL")

                logger.info(
                    "[PARTIAL EXIT] %s ratio=%.2f qty=%d/%d",
                    symbol,
                    ratio,
                    exit_qty,
                    qty,
                )

                return False

            except Exception:
                logger.exception("[PARTIAL_EXIT_ENGINE_ERROR]")
                return False


# ============================================================
# Global accessor
# ============================================================

def get_partial_exit_engine() -> PartialExitEngine:

    engine = getattr(GC.exit, "partial_exit_engine", None)

    if engine is None:
        engine = PartialExitEngine()
        GC.exit.partial_exit_engine = engine

    return engine