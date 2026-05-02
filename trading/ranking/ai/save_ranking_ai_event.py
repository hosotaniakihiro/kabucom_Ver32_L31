# ============================================================
# trading/ai/save_ranking_ai_event.py
# Ver: RANKING-AI-EVENT-SAVER-FINAL
# ------------------------------------------------------------
# ✔ ranking 合議制 ENTRY 判定を AI 学習データとして保存
# ✔ ENTRY / NO ENTRY 両方保存
# ✔ JSON 特徴量保存（将来拡張対応）
# ✔ Runtime を止めない（完全 fail-safe）
# ============================================================

import sqlite3
import json
import datetime as dt
import logging
from pathlib import Path

from config.paths import get_path

logger = logging.getLogger(__name__)

# ============================================================
# DB 設定
# ============================================================

DB_PATH = get_path("ai_entry_events_db")
TABLE_NAME = "ranking_ai_events"

_DB_LOCK = False  # sqlite 軽量ロック用途（thread-safe 前提）


# ============================================================
# テーブル作成（初回のみ）
# ============================================================
def _ensure_table():
    """
    ranking_ai_events テーブルを作成（存在しなければ）
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()

        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                market TEXT,
                snapshot_time TEXT NOT NULL,
                score INTEGER,
                reasons TEXT,
                features TEXT,
                entry_ok INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )

        # index（検索・学習用）
        cur.execute(
            f"""
            CREATE INDEX IF NOT EXISTS idx_{TABLE_NAME}_symbol_time
            ON {TABLE_NAME} (symbol, snapshot_time)
            """
        )

        conn.commit()
        conn.close()

    except Exception:
        logger.exception("[AI EVENT] failed to ensure table")


# ============================================================
# 保存関数（本体）
# ============================================================
def save_ranking_ai_event(
    *,
    symbol: str,
    market: str,
    snapshot_time: str,
    score: int,
    reasons: list[str],
    features: dict,
    entry_ok: int,
):
    """
    ranking 判定結果を AI 学習用に保存する

    Parameters
    ----------
    symbol : str
        銘柄コード
    market : str
        市場区分（ALL / TP / TS / TG）
    snapshot_time : str
        ランキングスナップショット時刻（ISO）
    score : int
        合議制スコア
    reasons : list[str]
        ENTRY / NG の理由
    features : dict
        学習用特徴量（JSON 化）
    entry_ok : int
        1=ENTRY / 0=NG
    """

    # 念のため
    if not symbol or not snapshot_time:
        return

    try:
        _ensure_table()

        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()

        cur.execute(
            f"""
            INSERT INTO {TABLE_NAME} (
                symbol,
                market,
                snapshot_time,
                score,
                reasons,
                features,
                entry_ok,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(symbol),
                market,
                snapshot_time,
                int(score) if score is not None else None,
                json.dumps(reasons, ensure_ascii=False),
                json.dumps(features, ensure_ascii=False),
                int(entry_ok),
                dt.datetime.now().isoformat(timespec="seconds"),
            ),
        )

        conn.commit()
        conn.close()

    except Exception:
        # ★ 絶対に trading を止めない
        logger.exception(
            "[AI EVENT] failed to save ranking ai event "
            f"symbol={symbol} snapshot={snapshot_time}"
        )
