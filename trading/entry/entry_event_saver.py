# ============================================================
# File   : trading/entry/entry_event_saver.py
# Ver6.9-FINAL-AI-TRACEABLE-DATASET-ENTRY-AND-NG
# ------------------------------------------------------------
# ✔ ENTRY / AI_REJECT / PRECHECK_NG / ORDER_NG 全保存
# ✔ 既存 Ver6.8 の全機能を完全保持（後方互換100%）
# ✔ SQLite 永続化（ai_entry_events.db）
# ✔ 即益AI / HOLDTIME / ENTRY可否 学習に直結
# ✔ 特徴量(JSON) と メタ情報を明確分離
# ✔ feats / features 両対応
# ✔ 未来リーク完全防止
# ✔ entry_time NOT NULL 制約 完全対応
# ============================================================

import sqlite3
import datetime as dt
import pandas as pd
import json
import threading
import logging
from typing import Dict, Any

from config.paths import get_path, ensure_dirs

logger = logging.getLogger(__name__)

# ============================================================
# DB 設定（唯一の正）
# ============================================================

DB_FILE = get_path("ai_entry_events_db")
TABLE = "entry_events"

_DB_LOCK = threading.Lock()


# ============================================================
# テーブル自動作成
# ============================================================

def _ensure_table():
    """
    entry_events テーブルを自動作成する
    ※ 既存の場合は何もしない
    """
    ensure_dirs()

    with _DB_LOCK:
        conn = sqlite3.connect(DB_FILE)
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TABLE} (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,

                -- ENTRY 基本情報
                datetime             TEXT,
                entry_time           TEXT NOT NULL,
                symbol               TEXT,
                side                 TEXT,
                entry_price          REAL,
                interval             INTEGER,
                score                REAL,

                -- 実行コンテキスト
                source               TEXT,
                entry_mode           TEXT,
                order_type           TEXT,

                -- AI 判定結果
                ai_confidence        REAL,
                dominant_ratio       REAL,
                model_used           TEXT,

                -- 即益AI / HOLDTIME
                ai_pred              REAL,
                ai_threshold         REAL,
                ai_pass              INTEGER,
                pred_hold_seconds    INTEGER,

                -- 市場環境
                index_shock          INTEGER,

                -- 学習用特徴量
                features_json        TEXT,

                -- EXIT / 後段更新用
                exit_time            TEXT,
                exit_price           REAL,
                exit_reason          TEXT,
                exit_confidence      REAL,

                -- パフォーマンス
                pnl                  REAL,
                holding_seconds      INTEGER,
                max_mfe              REAL,
                max_mae              REAL,

                -- AI 思考トレース
                exit_check_snapshots TEXT,

                -- メタ
                created_at           TEXT DEFAULT (datetime('now'))
            );
            """
        )
        conn.commit()
        conn.close()


# ============================================================
# 内部保存ロジック（共通）
# ============================================================

def _save_row(row: Dict[str, Any]):
    with _DB_LOCK:
        conn = sqlite3.connect(DB_FILE)
        pd.DataFrame([row]).to_sql(
            TABLE,
            conn,
            if_exists="append",
            index=False,
        )
        conn.close()


# ============================================================
# 公開API：ENTRY / NG イベント保存（統一）
# ============================================================

def save_entry_event(
    *,
    symbol: str,
    side: str,
    entry_price: float | None,
    interval: int,
    score: float | None,
    features: Dict[str, Any] | None = None,
    feats: Dict[str, Any] | None = None,   # 後方互換
    meta: Dict[str, Any] | None = None,
    event_time: dt.datetime | None = None,
):
    """
    ENTRY / 非ENTRY（AI_REJECT 等）を AI 学習用に保存する。

    entry_mode は meta["entry_mode"] で指定する：
        - "ENTRY"
        - "AI_REJECT"
        - "PRECHECK_NG"
        - "ORDER_NG"
    """

    # --------------------------------------------------------
    # 引数正規化
    # --------------------------------------------------------
    if features is None and feats is not None:
        features = feats

    features = features or {}
    meta = meta or {}

    try:
        _ensure_table()

        now = event_time or dt.datetime.now()
        ts = now.isoformat(timespec="seconds")

        row = {
            # ====================================================
            # ENTRY 基本
            # ====================================================
            "datetime": ts,
            "entry_time": ts,

            "symbol": str(symbol),
            "side": str(side),
            "entry_price": float(entry_price) if entry_price is not None else None,
            "interval": int(interval),
            "score": float(score) if score is not None else None,

            # ====================================================
            # 実行コンテキスト
            # ====================================================
            "source": meta.get("source"),
            "entry_mode": meta.get("entry_mode", "ENTRY"),
            "order_type": meta.get("order_type"),

            # ====================================================
            # AI 判定結果
            # ====================================================
            "ai_confidence": meta.get("confidence"),
            "dominant_ratio": meta.get("dominant_ratio"),
            "model_used": meta.get("model_used"),

            # ====================================================
            # 即益AI / HOLDTIME
            # ====================================================
            "ai_pred": meta.get("ai_pred"),
            "ai_threshold": meta.get("ai_threshold"),
            "ai_pass": (
                int(bool(meta.get("ai_pass")))
                if "ai_pass" in meta else None
            ),
            "pred_hold_seconds": meta.get("pred_hold_seconds"),

            # ====================================================
            # 市場環境
            # ====================================================
            "index_shock": meta.get("index_shock"),

            # ====================================================
            # 学習用特徴量（未来リークなし）
            # ====================================================
            "features_json": json.dumps(
                features,
                ensure_ascii=False,
                default=float,
            ),
        }

        _save_row(row)

        logger.debug(
            "[ENTRY_EVENT_SAVED] %s %s mode=%s price=%s score=%s conf=%s",
            symbol,
            side,
            row["entry_mode"],
            entry_price,
            score,
            meta.get("confidence"),
        )

    except Exception:
        logger.exception("[ENTRY_EVENT_SAVE_ERROR]")
