# ============================================================
# trading/entry/entry_event_updater.py
# ------------------------------------------------------------
# ✔ EXIT後に ENTRY_EVENT を後追い更新
# ✔ pnl / holding_seconds / index_shock を反映
# ✔ 最新の未確定 ENTRY を安全に特定
# ✔ Runtime を止めない（失敗しても例外を投げない）
# ✔ DB / TABLE / COLUMN 欠損完全耐性
# ============================================================

from __future__ import annotations

import sqlite3
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ============================================================
# PATH 解決（cwd / frozen / test すべて耐性）
# ============================================================

def _resolve_project_root() -> Path:
    try:
        return Path(__file__).resolve().parents[2]
    except Exception:
        return Path.cwd()

PROJECT_ROOT = _resolve_project_root()
DB_FILE = PROJECT_ROOT / "AI" / "data" / "ai_entry_events.db"
TABLE = "entry_events"

# ============================================================
# 内部 util
# ============================================================

def _table_exists(cur: sqlite3.Cursor, table: str) -> bool:
    row = cur.execute(
        """
        SELECT name FROM sqlite_master
        WHERE type='table' AND name=?
        """,
        (table,),
    ).fetchone()
    return row is not None


def _column_exists(cur: sqlite3.Cursor, table: str, column: str) -> bool:
    rows = cur.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == column for r in rows)


# ============================================================
# ENTRY_EVENT 更新（唯一の公開 API）
# ============================================================

def update_entry_result(
    *,
    symbol: str,
    pnl: float,
    holding_seconds: int,
    index_shock: Optional[int] = None,
) -> None:
    """
    EXIT 後に ENTRY_EVENT を更新する

    対象:
    - 同一 symbol
    - 最新（datetime DESC）
    - pnl が NULL（未確定）

    重要:
    - Runtime を絶対に落とさない
    - 更新失敗はログのみ
    """

    if not DB_FILE.exists():
        logger.debug(
            "[ENTRY_EVENT_UPDATE] DB not found → skip (%s)",
            DB_FILE,
        )
        return

    try:
        with sqlite3.connect(DB_FILE) as con:
            con.row_factory = sqlite3.Row
            cur = con.cursor()

            # ------------------------------
            # TABLE / COLUMN ガード
            # ------------------------------
            if not _table_exists(cur, TABLE):
                logger.warning(
                    "[ENTRY_EVENT_UPDATE] table not found → skip (%s)",
                    TABLE,
                )
                return

            required_cols = {
                "id",
                "symbol",
                "datetime",
                "pnl",
                "holding_seconds",
                "index_shock",
            }

            for col in required_cols:
                if not _column_exists(cur, TABLE, col):
                    logger.warning(
                        "[ENTRY_EVENT_UPDATE] column missing %s → skip",
                        col,
                    )
                    return

            # ------------------------------
            # 最新・未確定 ENTRY を特定
            # ------------------------------
            row = cur.execute(
                f"""
                SELECT id
                FROM {TABLE}
                WHERE symbol = ?
                  AND pnl IS NULL
                ORDER BY datetime DESC
                LIMIT 1
                """,
                (str(symbol),),
            ).fetchone()

            if not row:
                logger.debug(
                    "[ENTRY_EVENT_UPDATE] no pending entry: %s",
                    symbol,
                )
                return

            entry_id = row["id"]

            # ------------------------------
            # UPDATE（index_shock は NULL のみ反映）
            # ------------------------------
            cur.execute(
                f"""
                UPDATE {TABLE}
                SET
                    pnl = ?,
                    holding_seconds = ?,
                    index_shock = COALESCE(index_shock, ?)
                WHERE id = ?
                """,
                (
                    float(pnl),
                    int(holding_seconds),
                    index_shock,
                    entry_id,
                ),
            )

            con.commit()

            logger.debug(
                "[ENTRY_EVENT_UPDATED] symbol=%s pnl=%.2f hold=%ss shock=%s",
                symbol,
                pnl,
                holding_seconds,
                index_shock,
            )

    except Exception:
        # ★ Runtime 絶対防衛
        logger.exception("[ENTRY_EVENT_UPDATE_ERROR]")
        return

