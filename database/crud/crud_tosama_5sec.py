# ============================================================
# database/crud/crud_tosama_5sec.py
# ------------------------------------------------------------
# ✔ tosama_5sec_snapshot 保存専用
# ✔ WAL / LOCK 耐性
# ============================================================

import time
import logging
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from database.session import Session_tosama

logger = logging.getLogger(__name__)


def insert_tosama_5sec(snapshot: dict, retries: int = 5):
    """
    snapshot:
      {
        symbol, price, volume,
        fast_ret, accel,
        snapshot_time, source
      }
    """

    sql = text("""
        INSERT INTO tosama_5sec_snapshot (
            symbol,
            price,
            volume,
            fast_ret,
            accel,
            snapshot_time,
            source
        ) VALUES (
            :symbol,
            :price,
            :volume,
            :fast_ret,
            :accel,
            :snapshot_time,
            :source
        )
    """)

    session = Session_tosama()
    try:
        for i in range(retries):
            try:
                session.execute(sql, snapshot)
                session.commit()
                return
            except OperationalError as e:
                session.rollback()
                if "locked" in str(e).lower():
                    time.sleep(0.2)
                else:
                    raise
    except Exception:
        session.rollback()
        logger.exception("[TOSAMA] 5sec insert failed")
    finally:
        session.close()
