# =========================================================
# trading/handlers/exit_monitor.py
# Ver15.0-FINAL-EXIT-MONITOR-UNIFIED
# ---------------------------------------------------------
# ✔ 通常 EXIT は exit_controller に完全委譲
# ✔ CRASH_SHORT は summary 非依存で即時判定
# ✔ EXIT 実行は _execute_exit() に完全統一
# ✔ 旧 check_and_execute_exit / send_exit_order 不使用
# =========================================================

import time
import threading
import logging
import datetime as dt
import pandas as pd

from global_state import global_data

# 通常 EXIT（本流）
from trading.handlers.exit_controller import exit_loop_5s, _execute_exit

# CRASH_SHORT 判定
from trading.exit.crash_short_exit import should_exit_crash_short

# DB
from database import Session_position
from database.models import Position

# ExitContext
from trading.exit.exit_context import ExitContext

logger = logging.getLogger(__name__)


def start_exit_monitor(interval_sec: int = 10):
    """
    EXIT を背景監視するスレッド。

    ① 通常 EXIT
        - exit_controller.exit_loop_5s() に完全委譲

    ② CRASH_SHORT EXIT
        - summary 非依存
        - 時間・価格・地合いで即時判定
        - _execute_exit() に直接接続
    """

    def loop():
        logger.info(
            "🟢 EXIT MONITOR STARTED (interval=%ss)",
            interval_sec,
        )

        while True:
            try:
                # =================================================
                # ① 通常 EXIT（最優先・本流）
                # =================================================
                exit_loop_5s()

                # =================================================
                # ② CRASH_SHORT 専用 EXIT
                # =================================================
                session = Session_position()

                try:
                    rows = session.query(Position).filter_by(
                        status="OPEN",
                        entry_type="CRASH_SHORT",
                    ).all()

                finally:
                    session.close()

                if not rows:
                    time.sleep(interval_sec)
                    continue

                now = dt.datetime.now()

                for pos in rows:
                    symbol = str(pos.symbol)

                    # 現在値
                    tick = global_data.get_latest_tick(symbol)
                    if not tick:
                        continue

                    price = tick.get("price")
                    if not price:
                        continue

                    # EXIT 判定
                    if not should_exit_crash_short(
                        entry_price=pos.avg_price,
                        current_price=price,
                        entry_time=pos.entry_time,
                    ):
                        continue

                    logger.warning(
                        "🚨 CRASH_SHORT EXIT %s price=%.2f",
                        symbol,
                        price,
                    )

                    # ExitContext（最小構成）
                    ctx = ExitContext(
                        symbol=symbol,
                        side="SELL",  # CRASH_SHORT は必ず SELL
                        entry_price=pos.avg_price,
                        atr_1min=0.0,
                        entry_time=pos.entry_time,
                    )

                    _execute_exit(
                        symbol=symbol,
                        exit_price=price,
                        pos_list=[{"id": pos.id}],
                        ctx=ctx,
                        exit_reason="CRASH_SHORT_EXIT",
                        index_shock=0,
                    )

            except Exception:
                logger.exception("❌ EXIT MONITOR LOOP ERROR")

            time.sleep(interval_sec)

    th = threading.Thread(
        target=loop,
        daemon=True,
        name="ExitMonitorThread",
    )
    th.start()

    return th
