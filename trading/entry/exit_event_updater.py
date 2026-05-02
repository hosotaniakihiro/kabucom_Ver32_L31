# ============================================================
# File   : trading/entry/exit_event_updater.py
# Ver1.1-FINAL-EXIT-EVENT-UPDATER-PATHS-ALIGNED
# ------------------------------------------------------------
# ✔ EXIT後に entry_events を後追い更新
# ✔ 最新の未確定 ENTRY（exit_time IS NULL）のみ対象
# ✔ symbol 単位で安全に1件だけ更新
# ✔ 失敗しても Runtime を止めない
# ✔ config/paths.py による DB パス一元管理
# ============================================================

import sqlite3
import logging
from datetime import datetime
from typing import Optional

from config.paths import get_path, ensure_dirs

logger = logging.getLogger(__name__)

# ============================================================
# DB
# ============================================================

DB_FILE = get_path("ai_entry_events_db")
TABLE = "entry_events"


# ============================================================
# EXIT EVENT UPDATE（公開API）
# ============================================================

def update_exit_event(
    symbol: str,
    exit_price: float,
    exit_reason: str,
    exit_confidence: float,
    max_mfe: Optional[float] = None,
    max_mae: Optional[float] = None,
):
    """
    EXIT 後に entry_events を更新する

    対象：
        ・同一 symbol の最新レコード
        ・exit_time IS NULL のもの

    Args:
        symbol (str): 銘柄コード
        exit_price (float): EXIT価格
        exit_reason (str): AI_TAKE_PROFIT / AI_HOLDTIME / AI_COLLAPSE
        exit_confidence (float): 0.0 - 1.0
        max_mfe (float | None): 任意（あれば更新）
        max_mae (float | None): 任意（あれば更新）
    """

    try:
        # ディレクトリ保証（raw_data/AI 等）
        ensure_dirs()

        if not DB_FILE.exists():
            logger.warning(
                "[EXIT UPDATE] ai_entry_events.db not found: %s",
                DB_FILE,
            )
            return

        con = sqlite3.connect(DB_FILE)
        cur = con.cursor()

        now = datetime.now().isoformat()

        # ----------------------------------------------------
        # UPDATE 文（必要なものだけ更新）
        # ----------------------------------------------------
        sql = f"""
        UPDATE {TABLE}
        SET
            exit_price = ?,
            exit_time = ?,
            exit_reason = ?,
            exit_confidence = ?
            {", max_mfe = ?" if max_mfe is not None else ""}
            {", max_mae = ?" if max_mae is not None else ""}
        WHERE
            symbol = ?
            AND exit_time IS NULL
        ORDER BY id DESC
        LIMIT 1
        """

        params = [
            exit_price,
            now,
            exit_reason,
            exit_confidence,
        ]

        if max_mfe is not None:
            params.append(max_mfe)
        if max_mae is not None:
            params.append(max_mae)

        params.append(symbol)

        cur.execute(sql, params)
        con.commit()

        if cur.rowcount == 0:
            logger.info(
                "[EXIT UPDATE] no open entry found for symbol=%s",
                symbol,
            )
        else:
            logger.info(
                "[EXIT UPDATE] updated symbol=%s reason=%s conf=%.3f",
                symbol,
                exit_reason,
                exit_confidence,
            )

    except Exception:
        logger.exception("[EXIT UPDATE] failed")

    finally:
        try:
            con.close()
        except Exception:
            pass
