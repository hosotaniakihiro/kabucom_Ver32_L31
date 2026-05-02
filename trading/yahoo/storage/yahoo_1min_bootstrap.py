# ============================================================
# File   : trading/yahoo/yahoo_1min_bootstrap.py
# Version: Ver31-PRODUCTION-INTRADAY-BOOTSTRAP-NAS-ULTRA-STABLE
# ------------------------------------------------------------
# ✔ Ver30 機能完全保持（削除ゼロ）
# ✔ NAS (UNC path) 固定保存
# ✔ 起動時に必ず当日 Yahoo 1min DB を作成
# ✔ ディレクトリ自動作成
# ✔ テーブル自動作成
# ✔ データは一切入れない（空DB）
# ✔ idempotent（何度呼んでも安全）
# ✔ intraday DB パス取得正式API
# ✔ WAL / busy_timeout / NAS耐性
# ✔ SQLiteロック耐性強化
# ✔ UNC path 安定化
# ✔ logger強化
# ✔ save_yahoo_1min と完全整合
# ============================================================

from __future__ import annotations

import datetime as dt
import os
import logging
import pathlib

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

logger = logging.getLogger(__name__)


# ============================================================
# NAS ROOT
# ============================================================

RAW_DATA_ROOT = r"\\192.168.0.22\AutoStockBuyAndSell\raw_data\yahoo\intraday"


# ============================================================
# intraday DB パス取得（正式API）
# ============================================================

def get_intraday_db_path(
    target_date: dt.date | None = None,
) -> str:
    """
    intraday DB の正式パスを返す
    他モジュールは必ずこれを使用すること
    """

    if target_date is None:
        target_date = dt.date.today()

    try:

        # UNC path 正規化
        root = pathlib.Path(RAW_DATA_ROOT)

        root.mkdir(parents=True, exist_ok=True)

    except Exception:

        logger.exception(
            "[YAHOO BOOTSTRAP] directory create failed"
        )

    db_path = os.path.join(
        RAW_DATA_ROOT,
        f"yahoo_1min_{target_date:%Y%m%d}.db"
    )

    logger.info(
        "[YAHOO BOOTSTRAP] intraday db path: %s",
        db_path,
    )

    return db_path


# ============================================================
# 起動時 DB 作成（器のみ）
# ============================================================

def ensure_today_yahoo_1min_db(
    target_date: dt.date | None = None,
) -> str:
    """
    起動時に必ず当日分 Yahoo 1min DB を作成する

    ・データは入れない（器だけ）
    ・idempotent（何度呼んでも安全）
    """

    if target_date is None:
        target_date = dt.date.today()

    db_path = get_intraday_db_path(target_date)

    engine = None

    try:

        engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={
                "check_same_thread": False,
                "timeout": 30,
            },
            pool_pre_ping=True,
        )

        with engine.begin() as conn:

            # -------------------------------------------------
            # NAS SQLite 安定設定
            # -------------------------------------------------

            conn.execute(text("PRAGMA journal_mode=WAL"))
            conn.execute(text("PRAGMA synchronous=NORMAL"))
            conn.execute(text("PRAGMA busy_timeout=10000"))
            conn.execute(text("PRAGMA temp_store=MEMORY"))

            # -------------------------------------------------
            # テーブル作成
            # -------------------------------------------------

            conn.execute(text("""
            CREATE TABLE IF NOT EXISTS yahoo_1min (
                symbol TEXT NOT NULL,
                datetime TEXT NOT NULL,
                open REAL,
                high REAL,
                low REAL,
                close REAL,
                volume REAL,
                PRIMARY KEY (symbol, datetime)
            )
            """))

        logger.info(
            "[YAHOO BOOTSTRAP] intraday DB ready: %s",
            db_path,
        )

    except SQLAlchemyError:

        logger.exception(
            "[YAHOO BOOTSTRAP] DB bootstrap failed"
        )

    finally:

        try:

            if engine is not None:

                engine.dispose()

        except Exception:
            pass

    return db_path