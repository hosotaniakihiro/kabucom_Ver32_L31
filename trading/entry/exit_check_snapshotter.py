# ============================================================
# File   : trading/entry/exit_check_snapshotter.py
# Ver1.0-FINAL-EXIT-CHECK-SNAPSHOTTER
# ------------------------------------------------------------
# ✔ EXIT判定ごとのスナップショットをDBに追記保存
# ✔ 未EXIT（HOLD）/ EXIT の両方を記録可能
# ✔ ai_entry_events.db をそのまま使用
# ✔ Runtime を止めない（失敗しても例外を投げない）
# ============================================================

import sqlite3
import json
import logging
from pathlib import Path
from datetime import datetime
from typing import Dict, Any

logger = logging.getLogger(__name__)

# ============================================================
# PATH
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DB_FILE = PROJECT_ROOT / "AI" / "data" / "ai_entry_events.db"
TABLE = "entry_events"


# ============================================================
# SNAPSHOT APPENDER（公開API）
# ============================================================

def append_exit_snapshot(
    symbol: str,
    snapshot: Dict[str, Any],
):
    """
    EXIT 判定スナップショットを entry_events に追記する

    対象：
        ・同一 symbol の最新レコード
        ・exit_time IS NULL のもの

    Args:
        symbol (str):
            銘柄コード
        snapshot (dict):
            例：
            {
                "ts": "2026-01-28T14:32:05",
                "decision": "HOLD" | "AI_TAKE_PROFIT" | "AI_COLLAPSE" | "AI_HOLDTIME",
                "takeprofit_prob": 0.41,
                "collapse_prob": 0.18,
                "expected_hold": 120,
                "elapsed": 54,
                "hold_ratio": 0.45
            }
    """

    if not DB_FILE.exists():
        logger.debug("[EXIT SNAPSHOT] db not found")
        return

    con = None

    try:
        con = sqlite3.connect(DB_FILE)
        cur = con.cursor()

        # --------------------------------------------
        # 最新の未確定 entry を取得
        # --------------------------------------------
        cur.execute(
            f"""
            SELECT
                id,
                exit_check_snapshots
            FROM {TABLE}
            WHERE
                symbol = ?
                AND exit_time IS NULL
            ORDER BY id DESC
            LIMIT 1
            """,
            (symbol,),
        )

        row = cur.fetchone()
        if not row:
            logger.debug(
                f"[EXIT SNAPSHOT] no open entry for symbol={symbol}"
            )
            return

        event_id, raw = row

        # --------------------------------------------
        # 既存スナップショットをロード
        # --------------------------------------------
        snapshots = []

        if raw:
            try:
                snapshots = json.loads(raw)
                if not isinstance(snapshots, list):
                    snapshots = []
            except Exception:
                logger.warning(
                    "[EXIT SNAPSHOT] invalid json detected, reset snapshots"
                )
                snapshots = []

        # --------------------------------------------
        # スナップショット整形（最低限の安全ガード）
        # --------------------------------------------
        snap = dict(snapshot)
        snap.setdefault("ts", datetime.now().isoformat())

        snapshots.append(snap)

        # --------------------------------------------
        # DB更新
        # --------------------------------------------
        cur.execute(
            f"""
            UPDATE {TABLE}
            SET exit_check_snapshots = ?
            WHERE id = ?
            """,
            (json.dumps(snapshots, ensure_ascii=False), event_id),
        )

        con.commit()

        logger.debug(
            f"[EXIT SNAPSHOT] appended symbol={symbol} "
            f"decision={snap.get('decision')}"
        )

    except Exception:
        logger.exception("[EXIT SNAPSHOT] failed")

    finally:
        if con:
            con.close()
