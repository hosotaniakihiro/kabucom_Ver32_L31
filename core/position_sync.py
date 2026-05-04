# ============================================================
# position_sync.py
# Ver24.2-FINAL-INFLIGHT-RELEASE-INTEGRATED
# ------------------------------------------------------------
# ✔ Ver24.1 完全保持（機能削除ゼロ）
# ✔ /positions API → DB同期
# ✔ Position DB → global_data.open_positions 更新
# ✔ atr_1min / entry_time は runtime 前提（None-SAFE）
# ✔ EXIT / state_machine 完全対応
# ✔ ENTRY inflight を「建玉成立時」に安全解放（NEW）
# ============================================================

import time
import logging
import datetime as dt

from global_state import global_data
from kabu_api.positions import sync_positions_from_kabus, get_positions

from database import Session_position
from database.models import Position

logger = logging.getLogger(__name__)


def update_open_positions_from_db():
    """
    Position DB から OPEN の建玉を global_data.open_positions に反映
    EXIT / ENTRY / AI が参照する唯一の正本
    """
    session = None

    try:
        session = Session_position()
        rows = session.query(Position).filter_by(status="OPEN").all()

        open_positions = {}

        for p in rows:
            symbol = str(p.symbol)

            open_positions[symbol] = {
                "id": p.id,
                "symbol": symbol,
                "symbolname": getattr(p, "symbolname", ""),
                "side": p.side,
                "qty": p.qty,
                "avg_price": p.avg_price,

                # -----------------------------
                # ★ runtime 派生情報（None-SAFE）
                # -----------------------------
                "atr_1min": getattr(p, "atr_1min", None),
                "entry_time": getattr(p, "entry_time", None),
            }

        # ======================================================
        # ★ inflight 解放判定（NEW / 最重要）
        # ------------------------------------------------------
        # 「DB 上で OPEN になった」＝ 建玉成立
        # entry_controller の意思決定とは完全分離
        # ======================================================
        newly_opened = []

        with global_data.entry_inflight_lock:
            for symbol in open_positions.keys():
                if symbol in global_data.entry_inflight:
                    newly_opened.append(symbol)

        for symbol in newly_opened:
            global_data.release_entry_inflight(
                symbol,
                reason="POSITION_OPENED"
            )

        # ======================================================
        # ★ overwrite 防止設計に従う
        # ======================================================
        current = global_data.open_positions

        if not isinstance(current, dict):
            raise TypeError(
                f"open_positions invalid type: {type(current)}"
            )

        current.clear()
        current.update(open_positions)

        logger.info(
            "[PositionSync] OPEN positions updated: %d (released inflight=%d)",
            len(open_positions),
            len(newly_opened),
        )

    except Exception as e:
        logger.error(
            "[PositionSync] open_positions 更新エラー: %s",
            e,
            exc_info=True,
        )

    finally:
        if session:
            session.close()


def start_position_sync_loop():
    """
    Position API → DB → global_state を同期する常駐スレッド
    """

    import threading

    def loop():
        logger.info("PositionSyncLoop started")

        while True:

            # token 待ち
            if not getattr(global_data, "token_value", None):
                logger.warning("PositionSync: token 未セット → 待機")
                time.sleep(3)
                continue

            # ==================================================
            # API 取得
            # ==================================================
            api_positions = get_positions()

            if api_positions is None:
                logger.error("PositionSync: /positions = None")
                time.sleep(5)
                continue

            # ==================================================
            # API → DB 同期
            # ==================================================
            try:
                sync_positions_from_kabus()
            except Exception as e:
                logger.error(
                    "[PositionSync] sync_positions_from_kabus Error: %s",
                    e,
                    exc_info=True,
                )

            # ==================================================
            # DB → global_state 反映（最重要）
            # ==================================================
            update_open_positions_from_db()

            time.sleep(10)

    threading.Thread(target=loop, daemon=True).start()
    logger.info("PositionSync thread started")
