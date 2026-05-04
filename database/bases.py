# ============================================================
# database/bases.py
# Ver30.0-FINAL-LEGACY-SAFE-ENGINE-ISOLATED
# ------------------------------------------------------------
# ✔ Base 定義（既存互換100%）
# ✔ 用途別 Base 完全分離
# ✔ 動的ランキングテーブル生成
# ✔ ORM 管理テーブル完全分離
# ✔ legacy engine は明示実行時のみ生成
# ✔ import 時に engine を絶対生成しない（最重要修正）
# ✔ MetaData 二重定義事故を構造的に完全防止
# ============================================================

import os
import datetime as dt
import configparser
import logging
from pathlib import Path

from sqlalchemy import (
    create_engine,
    Column,
    Integer,
    String,
    Float,
    DateTime,
)
from sqlalchemy.orm import sessionmaker, declarative_base

# ----------------------------------------------------------
# Logger
# ----------------------------------------------------------
logger = logging.getLogger(__name__)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("[%(asctime)s] %(levelname)s - %(message)s")
    )
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

# ==========================================================
# Base 定義（用途別・完全分離）
# ==========================================================

Base_push = declarative_base()
Base_summary = declarative_base()
Base_position = declarative_base()
Base_trade_history = declarative_base()
Base_ranking = declarative_base()
Base_tosama = declarative_base()
Base_ai = declarative_base()
# ==========================================================
# ORM 管理テーブル（models.py 側のみで定義される正本）
# ==========================================================

ORM_DEFINED_RANKING_TABLES = {
    "ranking_snapshot_1min",
    "ranking_raw_1min",
    "ranking_ma_1min",
    "yahoo_ma_1min",
}

# ==========================================================
# ランキング分類定義
# ==========================================================

TYPE_TO_TABLE = {
    1: "値上がり率",
    2: "値下がり率",
    3: "売買高上位",
    4: "売買代金",
    5: "TICK回数",
    6: "売買高急増",
    7: "売買代金急増",
}

EXCHANGE_DIVISIONS = {
    "ALL": "全市場",
    "TP": "東証プライム",
    "TS": "東証スタンダード",
    "TG": "東証グロース",
}

# ==========================================================
# 動的ランキングテーブル生成
# ==========================================================

def _create_ranking_tables():
    """
    TYPE_TO_TABLE × EXCHANGE_DIVISIONS の組み合わせで
    動的ランキングテーブルを生成する

    ⚠ ルール
    - ORM 管理テーブルは絶対に生成しない
    - 既存 MetaData / Class 登録があれば即スキップ
    """

    for _, type_name in TYPE_TO_TABLE.items():
        for ex_key in EXCHANGE_DIVISIONS.keys():

            table_name = f"{type_name}_{ex_key}"

            if table_name in ORM_DEFINED_RANKING_TABLES:
                continue

            if table_name in Base_ranking.metadata.tables:
                continue

            if table_name in globals():
                continue

            attrs = {
                "__tablename__": table_name,
                "__table_args__": {"extend_existing": True},
                "id": Column(Integer, primary_key=True, autoincrement=True),
                "symbol": Column(String, nullable=False, index=True),
                "symbolname": Column(String),
                "current_price": Column(Float),
                "change_percentage": Column(Float),
                "change_ratio": Column(Float),
                "trading_volume": Column(Float),
                "trading_value": Column(Float),
                "turnover": Column(Float),
                "tick_count": Column(Integer),
                "inserted_at": Column(
                    DateTime,
                    default=dt.datetime.utcnow,
                    index=True,
                ),
            }

            type(table_name, (Base_ranking,), attrs)

# import 時に動的ランキング登録（engineは作らない）
_create_ranking_tables()

# ==========================================================
# Legacy Engine（明示実行時のみ生成）
# ==========================================================

def _create_legacy_engine():
    """
    legacy main.db 用 engine を明示的に生成する。
    import 時には絶対呼ばれない。
    """

    conf = configparser.ConfigParser()
    conf.read("settings.ini", encoding="utf-8")

    db_path = conf.get("paths", "base_path", fallback="y:/stock_price_data/")
    db_path = Path(db_path)
    db_path.mkdir(parents=True, exist_ok=True)

    db_file = db_path / "main.db"

    logger.warning(f"[LEGACY] main.db -> {db_file}")

    engine = create_engine(
        f"sqlite:///{db_file}",
        echo=False,
        connect_args={"check_same_thread": False},
    )

    return engine

def create_legacy_session():
    """
    旧コード互換用 Session。
    明示呼び出し時のみ engine 作成。
    """

    engine = _create_legacy_engine()

    return sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
    )

# ==========================================================
# DB 初期化（legacy専用）
# ==========================================================

def init_db():
    """
    legacy main.db 用 create_all
    通常運用では使用しない。
    """

    try:
        engine = _create_legacy_engine()

        from database import models  # noqa: F401
        try:
            from database import tosama_models  # noqa
        except Exception:
            pass

        _create_ranking_tables()

        for base in (
            Base_push,
            Base_summary,
            Base_position,
            Base_trade_history,
            Base_ranking,
            Base_tosama,
        ):
            base.metadata.create_all(bind=engine)

        logger.info("✅ 全テーブル初期化完了（legacy）")

    except Exception:
        logger.exception("❌ init_db error")

# ==========================================================
# Script 実行時のみ legacy 実行
# ==========================================================

if __name__ == "__main__":
    init_db()