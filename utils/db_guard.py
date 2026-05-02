# ============================================================
# File   : utils/db_guard.py
# Version: Ver3.0-ULTRA-STABLE-DB-GUARD
# ------------------------------------------------------------
# ✔ SQLite connection guard
# ✔ WAL auto enable
# ✔ busy_timeout
# ✔ retry execution
# ✔ transaction safety
# ✔ executemany safe
# ✔ database locked protection
# ============================================================

from __future__ import annotations

import sqlite3
import logging
import time

logger = logging.getLogger(__name__)


# ============================================================
# create connection
# ============================================================

def create_connection(db_path: str):

    conn = sqlite3.connect(
        db_path,
        timeout=30,
        check_same_thread=False
    )

    try:

        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA busy_timeout=5000")

    except Exception:

        logger.warning("[DB GUARD] pragma setup failed")

    return conn


# ============================================================
# execute with retry
# ============================================================

def execute_with_retry(conn, query, params=None, retries=5, delay=0.2):

    for i in range(retries):

        try:

            cur = conn.cursor()

            if params is None:
                cur.execute(query)
            else:
                cur.execute(query, params)

            conn.commit()

            return cur

        except sqlite3.OperationalError as e:

            if "locked" in str(e):

                logger.warning(
                    "[DB GUARD] database locked retry %s",
                    i + 1
                )

                time.sleep(delay)

            else:
                raise

    raise RuntimeError("DB locked too many times")


# ============================================================
# executemany safe
# ============================================================

def executemany_with_retry(conn, query, data, retries=5):

    for i in range(retries):

        try:

            cur = conn.cursor()

            cur.executemany(query, data)

            conn.commit()

            return

        except sqlite3.OperationalError as e:

            if "locked" in str(e):

                logger.warning(
                    "[DB GUARD] executemany retry %s",
                    i + 1
                )

                time.sleep(0.2)

            else:
                raise

    raise RuntimeError("executemany failed due to lock")


# ============================================================
# safe close
# ============================================================

def safe_close(conn):

    try:
        conn.close()
    except Exception:
        pass