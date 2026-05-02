# ============================================================
# duckdb_manager.py
# PRODUCTION-ANALYTICS-LAYER-FINAL-REBUILD-STABLE
# ------------------------------------------------------------
# ✔ SQLite → DuckDB 差分ロード
# ✔ ticks 永続テーブル
# ✔ summary 互換テーブル保持
# ✔ summary_1m / 3m / 5m フル再構築方式
# ✔ metadata差分管理
# ✔ 集計API提供
# ✔ AI特徴量生成基盤
# ✔ ANALYZE最適化
# ✔ 本番安定
# ============================================================

from __future__ import annotations

import duckdb
import logging
import datetime as dt
from pathlib import Path
from typing import Optional

from config.paths import get_path

logger = logging.getLogger(__name__)

DUCKDB_PATH = Path("intraday.duckdb")


# ============================================================
# DuckDB Manager
# ============================================================

class DuckDBManager:

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or DUCKDB_PATH
        self.conn = None
        self._connect()
        self._initialize_tables()

    # --------------------------------------------------------
    # 接続
    # --------------------------------------------------------

    def _connect(self):
        try:
            self.conn = duckdb.connect(str(self.db_path))
            logger.info(f"[DuckDB] connected → {self.db_path}")
        except Exception:
            logger.exception("[DuckDB] connection failed")

    # --------------------------------------------------------
    # 初期テーブル生成
    # --------------------------------------------------------

    def _initialize_tables(self):

        # ticks
        self.conn.execute("""
        CREATE TABLE IF NOT EXISTS ticks (
            symbol TEXT,
            datetime TIMESTAMP,
            price DOUBLE,
            volume DOUBLE,
            trading_value DOUBLE,
            vwap DOUBLE,
            previousclose DOUBLE
        );
        """)

        # SQLite互換summary（互換用途のみ）
        self.conn.execute("""
        CREATE TABLE IF NOT EXISTS summary (
            symbol TEXT,
            datetime TIMESTAMP,
            interval TEXT,
            open DOUBLE,
            high DOUBLE,
            low DOUBLE,
            close DOUBLE,
            volume DOUBLE
        );
        """)

        # MTF summaryテーブル
        for tf in [1, 3, 5]:
            self.conn.execute(f"""
            CREATE TABLE IF NOT EXISTS summary_{tf}m (
                symbol TEXT,
                datetime TIMESTAMP,
                open DOUBLE,
                high DOUBLE,
                low DOUBLE,
                close DOUBLE,
                volume DOUBLE
            );
            """)

        # metadata
        self.conn.execute("""
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        """)

        logger.info("[DuckDB] tables initialized")

    # --------------------------------------------------------
    # metadata
    # --------------------------------------------------------

    def _get_metadata(self, key: str) -> Optional[str]:
        result = self.conn.execute(
            "SELECT value FROM metadata WHERE key = ?",
            [key]
        ).fetchone()
        return result[0] if result else None

    def _set_metadata(self, key: str, value: str):
        self.conn.execute("""
        INSERT OR REPLACE INTO metadata (key, value)
        VALUES (?, ?)
        """, [key, value])

    # --------------------------------------------------------
    # SQLite tick 差分ロード
    # --------------------------------------------------------

    def load_ticks_from_sqlite(self, sqlite_path: Path):

        try:
            if not sqlite_path.exists():
                logger.warning(f"[DuckDB] sqlite not found: {sqlite_path}")
                return

            logger.info(f"[DuckDB] incremental load from {sqlite_path}")

            last_dt = self._get_metadata("last_tick_datetime")

            condition = ""
            if last_dt:
                condition = f"WHERE datetime > '{last_dt}'"

            self.conn.execute(f"""
            INSERT INTO ticks
            SELECT
                symbol,
                datetime,
                price,
                volume,
                trading_value,
                vwap,
                previousclose
            FROM sqlite_scan('{sqlite_path}', 'stream_data')
            {condition};
            """)

            result = self.conn.execute(
                "SELECT MAX(datetime) FROM ticks"
            ).fetchone()

            if result and result[0]:
                self._set_metadata("last_tick_datetime", str(result[0]))

            self.conn.execute("ANALYZE ticks;")

            logger.info("[DuckDB] ticks incremental load complete")

        except Exception:
            logger.exception("[DuckDB] load_ticks_from_sqlite failed")

    # --------------------------------------------------------
    # SQLite summary互換ロード（任意）
    # --------------------------------------------------------

    def load_summary_from_sqlite(self, sqlite_path: Path):

        try:
            if not sqlite_path.exists():
                return

            logger.info(f"[DuckDB] loading legacy summary from {sqlite_path}")

            self.conn.execute("DELETE FROM summary;")

            self.conn.execute(f"""
            INSERT INTO summary
            SELECT *
            FROM sqlite_scan('{sqlite_path}', 'summary');
            """)

            self.conn.execute("ANALYZE summary;")

        except Exception:
            logger.exception("[DuckDB] load_summary_from_sqlite failed")

    # --------------------------------------------------------
    # 🔥 フル再構築方式（推奨）
    # --------------------------------------------------------

    def rebuild_all_summaries(self):

        try:
            logger.info("🔥 DuckDB summary full rebuild")

            # 1分足
            self.conn.execute("""
            CREATE OR REPLACE TABLE summary_1m AS
            SELECT
                symbol,
                date_trunc('minute', datetime) AS datetime,
                FIRST(price) AS open,
                MAX(price) AS high,
                MIN(price) AS low,
                LAST(price) AS close,
                SUM(volume) AS volume
            FROM ticks
            GROUP BY symbol, date_trunc('minute', datetime);
            """)

            # 3分足
            self.conn.execute("""
            CREATE OR REPLACE TABLE summary_3m AS
            SELECT
                symbol,
                date_trunc('minute', datetime)
                    - INTERVAL (EXTRACT(MINUTE FROM datetime) % 3) MINUTE
                    AS datetime,
                FIRST(open) AS open,
                MAX(high) AS high,
                MIN(low) AS low,
                LAST(close) AS close,
                SUM(volume) AS volume
            FROM summary_1m
            GROUP BY symbol,
                date_trunc('minute', datetime)
                    - INTERVAL (EXTRACT(MINUTE FROM datetime) % 3) MINUTE;
            """)

            # 5分足
            self.conn.execute("""
            CREATE OR REPLACE TABLE summary_5m AS
            SELECT
                symbol,
                date_trunc('minute', datetime)
                    - INTERVAL (EXTRACT(MINUTE FROM datetime) % 5) MINUTE
                    AS datetime,
                FIRST(open) AS open,
                MAX(high) AS high,
                MIN(low) AS low,
                LAST(close) AS close,
                SUM(volume) AS volume
            FROM summary_1m
            GROUP BY symbol,
                date_trunc('minute', datetime)
                    - INTERVAL (EXTRACT(MINUTE FROM datetime) % 5) MINUTE;
            """)

            self.conn.execute("ANALYZE summary_1m;")
            self.conn.execute("ANALYZE summary_3m;")
            self.conn.execute("ANALYZE summary_5m;")

            logger.info("✅ DuckDB summaries rebuilt")

        except Exception:
            logger.exception("DuckDB rebuild failed")

    # --------------------------------------------------------
    # AI特徴量
    # --------------------------------------------------------

    def build_features(self):

        try:
            return self.conn.execute("""
            SELECT
                symbol,
                datetime,
                close,
                close - LAG(close)
                    OVER (PARTITION BY symbol ORDER BY datetime) AS delta,
                AVG(close)
                    OVER (PARTITION BY symbol
                          ORDER BY datetime
                          ROWS BETWEEN 10 PRECEDING AND CURRENT ROW) AS ma10,
                SUM(volume)
                    OVER (PARTITION BY symbol
                          ORDER BY datetime
                          ROWS BETWEEN 20 PRECEDING AND CURRENT ROW) AS vol20
            FROM summary_1m
            """).fetchdf()

        except Exception:
            logger.exception("[DuckDB] build_features failed")
            return None

    # --------------------------------------------------------
    # symbol集計
    # --------------------------------------------------------

    def aggregate_by_symbol(self):

        try:
            return self.conn.execute("""
            SELECT
                symbol,
                COUNT(*) AS tick_count,
                AVG(price) AS avg_price,
                SUM(volume) AS total_volume,
                MAX(price) AS high,
                MIN(price) AS low
            FROM ticks
            GROUP BY symbol
            """).fetchdf()

        except Exception:
            logger.exception("[DuckDB] aggregate_by_symbol failed")
            return None

    # --------------------------------------------------------
    # 本日ロード＋再構築
    # --------------------------------------------------------

    def load_today_and_build(self):

        today = dt.datetime.now().strftime("%Y%m%d")

        push_path = get_path("raw_push") / f"push{today}.db"
        summary_path = get_path("raw_summary") / f"summary{today}.db"

        self.load_ticks_from_sqlite(push_path)
        self.load_summary_from_sqlite(summary_path)
        self.rebuild_all_summaries()

    # --------------------------------------------------------
    # ユーティリティ
    # --------------------------------------------------------

    def list_tables(self):
        return self.conn.execute("""
            SELECT table_name
            FROM information_schema.tables
        """).fetchall()

    def close(self):
        if self.conn:
            self.conn.close()
            logger.info("[DuckDB] connection closed")


# ============================================================
# Singleton
# ============================================================

duck_manager = DuckDBManager()