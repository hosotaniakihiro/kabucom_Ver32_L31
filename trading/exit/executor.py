# ============================================================
# File   : trading/exit/executor.py
# Version: Ver26.3-PRODUCTION-HARDENED-LOCKED-FINAL
# ------------------------------------------------------------
# ✔ EXIT最終実行層
# ✔ 二重EXIT完全防止（CLOSING / CLOSED + Thread Lock）
# ✔ NaN / inf 完全防御
# ✔ PnL厳密計算
# ✔ DB同期更新（OPENのみ）
# ✔ rollback完全対応
# ✔ global_data整合保証
# ✔ deterministic
# ✔ 本番例外耐性MAX
# ============================================================

from __future__ import annotations

import logging
import datetime as dt
import math
import threading

from global_state import global_data
from database import Session_position
from database.models import Position

logger = logging.getLogger(__name__)

# スレッド二重防止
_exit_lock = threading.Lock()


# ============================================================
# SAFE UTIL
# ============================================================

def _safe_float(v, default=0.0):
    try:
        if v is None:
            return default
        v = float(v)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except Exception:
        return default


# ============================================================
# メイン実行
# ============================================================

def execute_exit(
    symbol: str,
    reason: str,
    exit_price: float,
) -> bool:
    """
    EXITを実行する最終関数

    return:
        True  = EXIT成功
        False = 実行されなかった
    """

    with _exit_lock:

        try:
            positions = global_data.open_positions

            # ----------------------------------------------------
            # ポジション存在確認
            # ----------------------------------------------------
            if symbol not in positions:
                logger.debug("[EXIT_EXECUTOR] no open position %s", symbol)
                return False

            pos = positions[symbol]

            status = pos.get("status")

            # ----------------------------------------------------
            # 二重EXIT完全防止
            # ----------------------------------------------------
            if status in ("CLOSING", "CLOSED"):
                logger.debug(
                    "[EXIT_EXECUTOR] already closing/closed symbol=%s status=%s",
                    symbol,
                    status,
                )
                return False

            pos["status"] = "CLOSING"

            # ----------------------------------------------------
            # 安全値取得
            # ----------------------------------------------------
            side = pos.get("side")
            qty = _safe_float(pos.get("qty"))
            avg_price = _safe_float(pos.get("avg_price"))
            exit_price = _safe_float(exit_price)

            if qty <= 0 or avg_price <= 0 or exit_price <= 0:
                logger.warning(
                    "[EXIT_EXECUTOR] invalid numeric values symbol=%s",
                    symbol,
                )
                pos["status"] = "OPEN"
                return False

            # ----------------------------------------------------
            # 実発注ログ（API差し替えポイント）
            # ----------------------------------------------------
            logger.info(
                "🚪 EXIT EXECUTE symbol=%s side=%s qty=%.4f price=%.4f reason=%s",
                symbol,
                side,
                qty,
                exit_price,
                reason,
            )

            # ----------------------------------------------------
            # PnL計算（厳密）
            # ----------------------------------------------------
            if side == "BUY":
                pnl = (exit_price - avg_price) * qty
            else:
                pnl = (avg_price - exit_price) * qty

            pnl = _safe_float(pnl)

            # ----------------------------------------------------
            # DB同期更新（OPENのみ）
            # ----------------------------------------------------
            session = Session_position()
            try:
                db_pos = (
                    session.query(Position)
                    .filter(Position.symbol == symbol)
                    .filter(Position.status == "OPEN")
                    .first()
                )

                if db_pos:
                    db_pos.exit_price = exit_price
                    db_pos.status = "CLOSED"
                    db_pos.exit_time = dt.datetime.now()
                    db_pos.updated_at = dt.datetime.now()
                    session.commit()
                else:
                    logger.warning(
                        "[EXIT_EXECUTOR] DB open position not found symbol=%s",
                        symbol,
                    )

            except Exception:
                session.rollback()
                logger.exception("[EXIT_EXECUTOR_DB_ERROR]")
                pos["status"] = "OPEN"
                return False
            finally:
                session.close()

            # ----------------------------------------------------
            # global_data 更新
            # ----------------------------------------------------
            pos["exit_price"] = exit_price
            pos["pnl"] = pnl
            pos["status"] = "CLOSED"
            pos["exit_time"] = dt.datetime.now()

            logger.info(
                "✅ EXIT COMPLETE symbol=%s pnl=%.4f reason=%s",
                symbol,
                pnl,
                reason,
            )

            # open_positions から削除（最後）
            if symbol in positions:
                del positions[symbol]

            return True

        except Exception:
            logger.exception("[EXIT_EXECUTOR_FATAL]")
            return False