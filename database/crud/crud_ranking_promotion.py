# ============================================================
# database/crud/crud_ranking_promotion.py
# ------------------------------------------------------------
# ✔ ranking → ENTRY 昇格 成否ラベル保存
# ✔ tosama DB 使用（学習専用）
# ✔ INSERT ONLY（UPDATE / DELETE なし）
# ✔ テーブル自動生成（安全）
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
from typing import Optional

from sqlalchemy import text

from database.session import tosama_engine

logger = logging.getLogger(__name__)

TABLE_NAME = "ranking_promotion_label"


# ============================================================
# 内部：テーブル存在保証（ADD ONLY）
# ============================================================

def _ensure_table() -> None:
    """
    ranking 昇格ラベル保存テーブルを保証する。

    注意:
      - 学習用の履歴テーブル
      - 既存行は破壊しない
      - INSERT ONLY
    """
    with tosama_engine.begin() as conn:
        conn.execute(text("PRAGMA busy_timeout=30000"))

        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                triggered_at TEXT NOT NULL,
                result INTEGER NOT NULL,
                reason TEXT,
                created_at TEXT NOT NULL
            )
        """))


# ============================================================
# 公開API：ランキング昇格ラベル保存
# ============================================================

def save_ranking_promotion_label(
    *,
    symbol: str,
    result: int,
    reason: str,
    triggered_at: Optional[str] = None,
) -> None:
    """
    ranking 昇格 成否ラベルを保存（学習用）

    Parameters
    ----------
    symbol : str
        銘柄コード

    result : int
        1 = ENTRY成功
        0 = 失敗（expire / ai_block / risk_ng 等）

    reason : str
        失敗・成功理由（学習特徴として使用）

    triggered_at : str | None
        ranking 発火時刻（ISO8601）
        None の場合は現在時刻
    """
    symbol_text = str(symbol or "").strip()
    if not symbol_text:
        logger.warning("[ranking_label] skipped: empty symbol")
        return

    result_value = 1 if int(result or 0) == 1 else 0
    reason_text = str(reason or "").strip()

    triggered_time = (
        str(triggered_at)
        if triggered_at is not None
        else dt.datetime.now().isoformat()
    )

    created_time = dt.datetime.now().isoformat()

    sql = text(f"""
        INSERT INTO {TABLE_NAME} (
            symbol,
            triggered_at,
            result,
            reason,
            created_at
        )
        VALUES (
            :symbol,
            :triggered_at,
            :result,
            :reason,
            :created_at
        )
    """)

    try:
        _ensure_table()

        with tosama_engine.begin() as conn:
            conn.execute(text("PRAGMA busy_timeout=30000"))
            conn.execute(sql, {
                "symbol": symbol_text,
                "triggered_at": triggered_time,
                "result": result_value,
                "reason": reason_text,
                "created_at": created_time,
            })

        logger.debug(
            "[ranking_label] saved symbol=%s result=%s reason=%s",
            symbol_text,
            result_value,
            reason_text,
        )

    except Exception:
        logger.exception(
            "❌ save_ranking_promotion_label failed: %s",
            symbol_text,
        )


__all__ = [
    "save_ranking_promotion_label",
]