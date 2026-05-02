# ============================================================
# trading/handlers/entry_precheck_ranking.py
# Ver: FINAL-RANKING-PRECHECK-PRODUCTION-ELITE
# ------------------------------------------------------------
# ✔ 既存機能100%保持（削除ゼロ）
# ✔ DB WAL / timeout対応
# ✔ snapshot鮮度チェック（NEW）
# ✔ global_data復元（強化）
# ✔ ロック耐性
# ✔ 異常データ防止
# ✔ production hardened
# ============================================================

import sqlite3
import logging
from typing import Dict, Any, Optional, List
from datetime import datetime, timedelta

from config.paths import get_path
from global_state import global_data

logger = logging.getLogger(__name__)

RANKING_DB_DIR = get_path("raw_ranking")
SNAPSHOT_TABLE = "ranking_snapshot_1min"

# ------------------------------------------------------------
# 許容遅延（秒）
# ------------------------------------------------------------
MAX_SNAPSHOT_AGE_SEC = 90


# ============================================================
# debug
# ============================================================

print(
    "### ENTRY_PRECHECK_RANKING LOADED FROM:",
    __file__,
    "PID:",
    __import__("os").getpid(),
    flush=True
)


# ============================================================
# utils
# ============================================================

def _today_ranking_db_path() -> Optional[str]:
    try:
        ymd = datetime.now().strftime("%Y%m%d")
        return str(RANKING_DB_DIR / f"ranking{ymd}.db")
    except Exception:
        return None


def _connect_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(
        path,
        timeout=5,
        check_same_thread=False
    )
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=3000;")
    return conn


def _load_latest_snapshot_time(cur: sqlite3.Cursor) -> Optional[str]:
    cur.execute(f"SELECT MAX(snapshot_time) FROM {SNAPSHOT_TABLE}")
    row = cur.fetchone()
    return row[0] if row and row[0] else None


def _load_snapshot_rows(
    cur: sqlite3.Cursor,
    snapshot_time: str,
) -> List[Dict[str, Any]]:

    cur.execute(
        f"""
        SELECT *
        FROM {SNAPSHOT_TABLE}
        WHERE snapshot_time = ?
        """,
        (snapshot_time,),
    )

    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _is_snapshot_fresh(snapshot_time: str) -> bool:
    try:
        ts = datetime.fromisoformat(snapshot_time)
        return (datetime.now() - ts) <= timedelta(seconds=MAX_SNAPSHOT_AGE_SEC)
    except Exception:
        return False


# ============================================================
# core
# ============================================================

def _check_ranking_ready() -> Dict[str, Any]:

    # ========================================================
    # ① global_data
    # ========================================================

    snapshot = getattr(global_data, "latest_ranking_snapshot", None)
    ranking_type = getattr(global_data, "latest_ranking_type", None)

    if isinstance(snapshot, list) and len(snapshot) > 0:
        return {
            "is_ready": True,
            "explicit_ready": True,
            "derived_ready": True,
            "has_snapshot": True,
            "snapshot_count": len(snapshot),
            "ranking_type": ranking_type,
            "source": "cache",
        }

    # ========================================================
    # ② DB fallback
    # ========================================================

    ranking_db_path = _today_ranking_db_path()

    if not ranking_db_path:
        return _not_ready()

    try:
        conn = _connect_db(ranking_db_path)
        cur = conn.cursor()

        latest_time = _load_latest_snapshot_time(cur)

        if not latest_time:
            conn.close()
            return _not_ready()

        # ----------------------------------------------------
        # 🔥 鮮度チェック
        # ----------------------------------------------------
        if not _is_snapshot_fresh(latest_time):
            logger.warning(
                "[RANKING STALE] snapshot_time=%s",
                latest_time
            )
            conn.close()
            return _not_ready()

        snapshot_rows = _load_snapshot_rows(cur, latest_time)
        conn.close()

        if not snapshot_rows:
            return _not_ready()

        # ----------------------------------------------------
        # type抽出
        # ----------------------------------------------------
        ranking_types = {
            r.get("rank_type")
            for r in snapshot_rows
            if r.get("rank_type")
        }

        resolved_type = (
            ranking_types.pop()
            if len(ranking_types) == 1
            else None
        )

        # ----------------------------------------------------
        # 🔥 global_data復元（重要）
        # ----------------------------------------------------
        try:
            global_data.latest_ranking_snapshot = snapshot_rows
            global_data.latest_ranking_type = resolved_type
        except Exception:
            logger.warning("[RANKING CACHE RESTORE FAILED]")

        logger.info(
            "[RANKING READY][DB] snapshot=%d type=%s time=%s",
            len(snapshot_rows),
            resolved_type,
            latest_time,
        )

        return {
            "is_ready": True,
            "explicit_ready": True,
            "derived_ready": True,
            "has_snapshot": True,
            "snapshot_count": len(snapshot_rows),
            "ranking_type": resolved_type,
            "source": "db",
        }

    except Exception as e:
        logger.exception("[RANKING PRECHECK ERROR] %s", e)
        return _not_ready()


# ============================================================
# helper
# ============================================================

def _not_ready() -> Dict[str, Any]:
    return {
        "is_ready": False,
        "explicit_ready": False,
        "derived_ready": False,
        "has_snapshot": False,
        "snapshot_count": 0,
        "ranking_type": None,
        "source": None,
    }


# ============================================================
# public API
# ============================================================

def precheck_ranking_entry() -> Dict[str, Any]:

    result = _check_ranking_ready()

    if not result["is_ready"]:
        logger.warning("[RANKING PRECHECK NG] %s", result)

    return result


# ============================================================
# log helper
# ============================================================

def log_precheck_result(result: Dict[str, Any]):

    if result.get("is_ready"):
        logger.info(
            "[RANKING PRECHECK OK] type=%s snapshot=%s source=%s",
            result.get("ranking_type"),
            result.get("snapshot_count"),
            result.get("source"),
        )
    else:
        logger.warning("[RANKING PRECHECK NG] %s", result)