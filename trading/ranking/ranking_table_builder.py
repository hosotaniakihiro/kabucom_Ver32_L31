# ============================================================
# File   : trading/ranking/ranking_table_builder.py
# Ver    : FINAL-SNAPSHOT-TO-RANKING-TABLE-DAILY-DB-SCHEMA-SAFE-v8
# ------------------------------------------------------------
# ✔ ranking_snapshot_1min → legacy ranking tables 展開
# ✔ ranking_snapshot_1min → ranking_ma_1min 更新
# ✔ 日付別 rankingYYYYMMDD.db / summaryYYYYMMDD.db 対応
# ✔ snapshot 実スキーマ(price / volume / turnover) に追従
# ✔ 再実行安全（同一 minute は DELETE → INSERT）
# ✔ symbol_master から symbolname 補完
# ✔ summary 1min と JOIN して close / score / slope / rsi / macd を補完
# ✔ best_rank / ma_rank_position / ma_volume_speed / trend_score 計算
# ✔ ranking_ma_1min の UNIQUE を (symbol, rank_type, market, datetime) へ統一
# ✔ DB を唯一の正本とする
# ============================================================

from __future__ import annotations

import logging
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from config.paths import get_path

logger = logging.getLogger(__name__)

# ============================================================
# DB path helpers
# ============================================================


def _today_ymd() -> str:
    return date.today().strftime("%Y%m%d")


def _safe_get_path(name: str) -> Optional[Path]:
    try:
        p = get_path(name)
        if p is None:
            return None
        return Path(p)
    except Exception:
        return None


RANKING_DB_DIR: Path = _safe_get_path("raw_ranking") or Path(
    r"\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\ranking"
)
RANKING_DB: Path = RANKING_DB_DIR / f"ranking{_today_ymd()}.db"

SUMMARY_DB_DIR: Path = (
    _safe_get_path("raw_summary")
    or _safe_get_path("summary")
    or Path(r"\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\summary")
)
SUMMARY_DB: Path = SUMMARY_DB_DIR / f"summary{_today_ymd()}.db"

MASTER_DB: Path = _safe_get_path("symbol_master_db") or Path(
    r"\\192.168.0.22\AutoStockBuyAndSell\Basic\symbol_master.db"
)

SNAPSHOT_TABLE = "ranking_snapshot_1min"
SUMMARY_1MIN_TABLE = "stock_summary_1min"
RANKING_MA_TABLE = "ranking_ma_1min"

OLD_UNIQUE_INDEX_CANDIDATES = [
    "uq_ranking_ma_1min_symbol_datetime",
    "idx_ranking_ma_1min_symbol_datetime_unique",
]

NEW_UNIQUE_INDEX_NAME = "uq_ranking_ma_1min_symbol_ranktype_market_datetime"

# ============================================================
# ランキング種別 → 旧テーブル名
# ============================================================

RANK_TABLE_MAP = {
    "値上がり率": "値上がり率",
    "値下がり率": "値下がり率",
    "売買高上位": "売買高上位",
    "売買代金": "売買代金",
    "TICK回数": "TICK回数",
    "売買高急増": "売買高急増",
    "売買代金急増": "売買代金急増",
}

MARKETS = ["ALL", "TP", "TS", "TG"]

# ============================================================
# utility
# ============================================================


def _table_exists(cur: sqlite3.Cursor, table: str, schema: str = "main") -> bool:
    cur.execute(
        f"""
        SELECT 1
        FROM {schema}.sqlite_master
        WHERE type='table' AND name=?
        LIMIT 1
        """,
        (table,),
    )
    return cur.fetchone() is not None


def _get_table_columns(cur: sqlite3.Cursor, table: str, schema: str = "main") -> set[str]:
    cur.execute(f"PRAGMA {schema}.table_info({table})")
    return {row[1] for row in cur.fetchall()}


def _quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _coalesce_expr(candidates: Sequence[str], fallback: str = "NULL") -> str:
    cols = [c for c in candidates if c]
    if not cols:
        return fallback
    if len(cols) == 1:
        return cols[0]
    return f"COALESCE({', '.join(cols)})"


def _load_snapshot_rows(cur: sqlite3.Cursor, snapshot_time: str) -> List[Dict[str, Any]]:
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


def _load_latest_snapshot_time(cur: sqlite3.Cursor) -> Optional[str]:
    cur.execute(f"SELECT MAX(snapshot_time) FROM {SNAPSHOT_TABLE}")
    row = cur.fetchone()
    return row[0] if row and row[0] else None


def _normalize_num(v: Any, default: Optional[float] = None) -> Optional[float]:
    if v is None or v == "":
        return default
    try:
        return float(v)
    except Exception:
        return default


def _normalize_rank(v: Any) -> Optional[int]:
    if v is None or v == "":
        return None
    try:
        return int(float(v))
    except Exception:
        return None


def _index_columns(cur: sqlite3.Cursor, index_name: str) -> list[str]:
    try:
        cur.execute(f"PRAGMA index_info({_quote_ident(index_name)})")
        rows = cur.fetchall()
        return [str(r[2]) for r in rows if len(r) >= 3]
    except Exception:
        return []


def _drop_old_unique_indexes(cur: sqlite3.Cursor) -> None:
    try:
        cur.execute(f"PRAGMA index_list({RANKING_MA_TABLE})")
        idx_rows = cur.fetchall()
    except Exception:
        logger.exception("[RANKING MA][SCHEMA] failed PRAGMA index_list")
        return

    for row in idx_rows:
        try:
            idx_name = str(row[1])
            is_unique = int(row[2]) == 1
        except Exception:
            continue

        if not is_unique:
            continue

        cols = _index_columns(cur, idx_name)

        # 古い symbol+datetime UNIQUE を削除
        if cols == ["symbol", "datetime"] or idx_name in OLD_UNIQUE_INDEX_CANDIDATES:
            try:
                cur.execute(f"DROP INDEX IF EXISTS {_quote_ident(idx_name)}")
                logger.info(
                    "[RANKING MA][SCHEMA] dropped old unique index name=%s cols=%s",
                    idx_name,
                    cols,
                )
            except Exception:
                logger.exception(
                    "[RANKING MA][SCHEMA] failed drop old unique index name=%s cols=%s",
                    idx_name,
                    cols,
                )


# ============================================================
# schema bootstrap
# ============================================================


def _ensure_ranking_ma_table(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {RANKING_MA_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT
        )
        """
    )

    required_columns: dict[str, str] = {
        "symbol": "TEXT",
        "rank_type": "TEXT",
        "market": "TEXT",
        "ma_rank_position": "REAL",
        "ma_volume_speed": "REAL",
        "trend_score": "REAL",
        "snapshot_time": "TIMESTAMP",
        "created_at": "TIMESTAMP",
        "datetime": "TIMESTAMP",
        "symbolname": "TEXT",
        "close": "REAL",
        "score": "REAL",
        "slope": "REAL",
        "rsi": "REAL",
        "macd": "REAL",
        "macd_signal": "REAL",
        "macd_hist": "REAL",
        "best_rank": "INTEGER",
        "source": "TEXT",
        "updated_at": "TIMESTAMP",
    }

    existing = _get_table_columns(cur, RANKING_MA_TABLE)
    for col, col_type in required_columns.items():
        if col in existing:
            continue
        try:
            cur.execute(
                f'ALTER TABLE {RANKING_MA_TABLE} ADD COLUMN "{col}" {col_type}'
            )
            logger.info(
                "[RANKING MA][SCHEMA] added column table=%s column=%s type=%s",
                RANKING_MA_TABLE,
                col,
                col_type,
            )
        except Exception:
            logger.exception(
                "[RANKING MA][SCHEMA] failed add column table=%s column=%s type=%s",
                RANKING_MA_TABLE,
                col,
                col_type,
            )

    try:
        cur.execute(
            f"""
            CREATE INDEX IF NOT EXISTS idx_{RANKING_MA_TABLE}_dt
            ON {RANKING_MA_TABLE}(datetime)
            """
        )
    except Exception:
        logger.exception("[RANKING MA][SCHEMA] failed create datetime index")

    try:
        cur.execute(
            f"""
            CREATE INDEX IF NOT EXISTS idx_{RANKING_MA_TABLE}_sym_dt
            ON {RANKING_MA_TABLE}(symbol, datetime)
            """
        )
    except Exception:
        logger.exception("[RANKING MA][SCHEMA] failed create symbol-datetime index")

    try:
        cur.execute(
            f"""
            CREATE INDEX IF NOT EXISTS idx_{RANKING_MA_TABLE}_sym_type_mkt_dt
            ON {RANKING_MA_TABLE}(symbol, rank_type, market, datetime)
            """
        )
    except Exception:
        logger.exception("[RANKING MA][SCHEMA] failed create composite index")

    _drop_old_unique_indexes(cur)

    try:
        cur.execute(
            f"""
            CREATE UNIQUE INDEX IF NOT EXISTS {NEW_UNIQUE_INDEX_NAME}
            ON {RANKING_MA_TABLE}(symbol, rank_type, market, datetime)
            """
        )
        logger.info(
            "[RANKING MA][SCHEMA] ensured composite unique index name=%s",
            NEW_UNIQUE_INDEX_NAME,
        )
    except Exception:
        logger.exception("[RANKING MA][SCHEMA] failed create composite unique index")

    conn.commit()


# ============================================================
# legacy table build
# ============================================================


def _build_legacy_tables_from_snapshot(
    conn: sqlite3.Connection,
    snapshot_time: str,
) -> int:
    cur = conn.cursor()

    if not _table_exists(cur, SNAPSHOT_TABLE):
        logger.warning("[RANKING] snapshot table not found: %s", SNAPSHOT_TABLE)
        return 0

    snapshot_cols = _get_table_columns(cur, SNAPSHOT_TABLE)

    price_expr = (
        "s.price"
        if "price" in snapshot_cols
        else "s.current_price"
        if "current_price" in snapshot_cols
        else "NULL"
    )
    volume_expr = (
        "s.volume"
        if "volume" in snapshot_cols
        else "s.trading_volume"
        if "trading_volume" in snapshot_cols
        else "NULL"
    )
    turnover_expr = (
        "s.turnover"
        if "turnover" in snapshot_cols
        else "s.trading_value"
        if "trading_value" in snapshot_cols
        else "NULL"
    )
    tick_count_expr = (
        "s.tick_count"
        if "tick_count" in snapshot_cols
        else "NULL"
    )

    total_inserted = 0

    for rank_type, base_name in RANK_TABLE_MAP.items():
        for market in MARKETS:
            table_name = f"{base_name}_{market}"

            if not _table_exists(cur, table_name):
                logger.warning("[RANKING LEGACY BUILD] table missing: %s", table_name)
                continue

            try:
                cur.execute(
                    f"""
                    DELETE FROM {_quote_ident(table_name)}
                    WHERE inserted_at >= ?
                      AND inserted_at < datetime(?, '+1 minute')
                    """,
                    (snapshot_time, snapshot_time),
                )
            except Exception:
                logger.exception(
                    "[RANKING LEGACY BUILD] delete failed table=%s time=%s",
                    table_name,
                    snapshot_time,
                )
                continue

            sql = f"""
                INSERT INTO {_quote_ident(table_name)} (
                    symbol,
                    symbolname,
                    current_price,
                    trading_volume,
                    trading_value,
                    tick_count,
                    inserted_at
                )
                SELECT
                    s.symbol,
                    COALESCE(m.symbolname, s.symbolname),
                    {price_expr},
                    {volume_expr},
                    {turnover_expr},
                    {tick_count_expr},
                    s.snapshot_time
                FROM {SNAPSHOT_TABLE} s
                LEFT JOIN master.symbol_master m
                  ON s.symbol = m.symbol
                WHERE s.snapshot_time = ?
                  AND s.rank_type = ?
                  AND s.market = ?
            """

            try:
                cur.execute(sql, (snapshot_time, rank_type, market))
                inserted = cur.rowcount or 0
                total_inserted += inserted
                logger.info(
                    "[RANKING LEGACY BUILD] table=%s type=%s market=%s inserted=%s time=%s",
                    table_name,
                    rank_type,
                    market,
                    inserted,
                    snapshot_time,
                )
            except Exception:
                logger.exception(
                    "[RANKING LEGACY BUILD] insert failed table=%s type=%s market=%s time=%s",
                    table_name,
                    rank_type,
                    market,
                    snapshot_time,
                )

    return total_inserted


# ============================================================
# ranking_ma_1min build
# ============================================================


def _load_summary_rows_for_minute(
    conn: sqlite3.Connection,
    snapshot_time: str,
) -> dict[str, Dict[str, Any]]:
    cur = conn.cursor()

    if not _table_exists(cur, SUMMARY_1MIN_TABLE, schema="summary"):
        logger.warning("[RANKING MA] summary table not found: summary.%s", SUMMARY_1MIN_TABLE)
        return {}

    cols = _get_table_columns(cur, SUMMARY_1MIN_TABLE, schema="summary")

    symbol_expr = "symbol" if "symbol" in cols else None
    dt_expr = "datetime" if "datetime" in cols else None
    if not symbol_expr or not dt_expr:
        logger.warning(
            "[RANKING MA] summary table missing required cols symbol/datetime cols=%s",
            sorted(cols),
        )
        return {}

    close_expr = _coalesce_expr(
        [c for c in ["close", "current_price", "price"] if c in cols],
        "NULL",
    )
    score_expr = _coalesce_expr(
        [c for c in ["score", "score_total", "display_score", "final_score"] if c in cols],
        "NULL",
    )
    slope_expr = _coalesce_expr(
        [c for c in ["slope", "score_slope", "slope_atr_scaled"] if c in cols],
        "NULL",
    )
    rsi_expr = _coalesce_expr([c for c in ["rsi"] if c in cols], "NULL")
    macd_expr = _coalesce_expr([c for c in ["macd"] if c in cols], "NULL")
    macd_signal_expr = _coalesce_expr(
        [c for c in ["signal", "macd_signal"] if c in cols],
        "NULL",
    )
    macd_hist_expr = _coalesce_expr(
        [c for c in ["macd_hist", "hist"] if c in cols],
        "NULL",
    )

    sql = f"""
        SELECT
            {symbol_expr} AS symbol,
            {dt_expr} AS datetime,
            {close_expr} AS close,
            {score_expr} AS score,
            {slope_expr} AS slope,
            {rsi_expr} AS rsi,
            {macd_expr} AS macd,
            {macd_signal_expr} AS macd_signal,
            {macd_hist_expr} AS macd_hist
        FROM summary.{SUMMARY_1MIN_TABLE}
        WHERE datetime = ?
    """

    out: dict[str, Dict[str, Any]] = {}
    try:
        cur.execute(sql, (snapshot_time,))
        rows = cur.fetchall()
        cols2 = [d[0] for d in cur.description]
        for row in rows:
            item = dict(zip(cols2, row))
            symbol = str(item.get("symbol") or "").strip()
            if symbol:
                out[symbol] = item
    except Exception:
        logger.exception("[RANKING MA] failed load summary rows snapshot_time=%s", snapshot_time)

    logger.info(
        "[RANKING MA] summary rows loaded time=%s rows=%s",
        snapshot_time,
        len(out),
    )
    return out


def _calc_best_rank(
    cur: sqlite3.Cursor,
    symbol: str,
    rank_type: str,
    market: str,
    snapshot_time: str,
) -> Optional[int]:
    try:
        cur.execute(
            f"""
            SELECT MIN(rank)
            FROM {SNAPSHOT_TABLE}
            WHERE symbol = ?
              AND rank_type = ?
              AND market = ?
              AND snapshot_time <= ?
            """,
            (symbol, rank_type, market, snapshot_time),
        )
        row = cur.fetchone()
        if row and row[0] is not None:
            return int(row[0])
    except Exception:
        logger.exception(
            "[RANKING MA] calc best_rank failed symbol=%s type=%s market=%s time=%s",
            symbol,
            rank_type,
            market,
            snapshot_time,
        )
    return None


def _calc_ma_rank_position(
    cur: sqlite3.Cursor,
    symbol: str,
    rank_type: str,
    market: str,
    snapshot_time: str,
    lookback: int = 5,
) -> Optional[float]:
    try:
        cur.execute(
            f"""
            SELECT rank
            FROM {SNAPSHOT_TABLE}
            WHERE symbol = ?
              AND rank_type = ?
              AND market = ?
              AND snapshot_time <= ?
            ORDER BY snapshot_time DESC
            LIMIT ?
            """,
            (symbol, rank_type, market, snapshot_time, lookback),
        )
        vals = [_normalize_num(r[0]) for r in cur.fetchall()]
        vals = [v for v in vals if v is not None]
        if vals:
            return round(sum(vals) / len(vals), 4)
    except Exception:
        logger.exception(
            "[RANKING MA] calc ma_rank_position failed symbol=%s type=%s market=%s time=%s",
            symbol,
            rank_type,
            market,
            snapshot_time,
        )
    return None


def _calc_ma_volume_speed(
    cur: sqlite3.Cursor,
    symbol: str,
    rank_type: str,
    market: str,
    snapshot_time: str,
    current_volume: Optional[float],
    lookback: int = 5,
) -> Optional[float]:
    if current_volume is None:
        return None

    snapshot_cols = _get_table_columns(cur, SNAPSHOT_TABLE)
    volume_col = (
        "volume"
        if "volume" in snapshot_cols
        else "trading_volume"
        if "trading_volume" in snapshot_cols
        else None
    )
    if volume_col is None:
        return None

    try:
        cur.execute(
            f"""
            SELECT {volume_col}
            FROM {SNAPSHOT_TABLE}
            WHERE symbol = ?
              AND rank_type = ?
              AND market = ?
              AND snapshot_time < ?
            ORDER BY snapshot_time DESC
            LIMIT ?
            """,
            (symbol, rank_type, market, snapshot_time, lookback),
        )
        hist = [_normalize_num(r[0]) for r in cur.fetchall()]
        hist = [v for v in hist if v is not None]
        if not hist:
            return None
        avg_hist = sum(hist) / len(hist)
        if avg_hist == 0:
            return None
        return round(current_volume / avg_hist, 4)
    except Exception:
        logger.exception(
            "[RANKING MA] calc ma_volume_speed failed symbol=%s type=%s market=%s time=%s",
            symbol,
            rank_type,
            market,
            snapshot_time,
        )
    return None


def _calc_trend_score(
    rank_now: Optional[int],
    ma_rank_position: Optional[float],
    score: Optional[float],
    slope: Optional[float],
    ma_volume_speed: Optional[float],
) -> Optional[float]:
    if (
        rank_now is None
        and ma_rank_position is None
        and score is None
        and slope is None
        and ma_volume_speed is None
    ):
        return None

    rank_component = 0.0
    if rank_now is not None and ma_rank_position is not None and ma_rank_position > 0:
        rank_component = (ma_rank_position - float(rank_now)) / ma_rank_position * 100.0

    score_component = float(score or 0.0)
    slope_component = float(slope or 0.0) * 10.0
    volume_component = 0.0
    if ma_volume_speed is not None:
        volume_component = (float(ma_volume_speed) - 1.0) * 20.0

    return round(rank_component + score_component + slope_component + volume_component, 4)


def _build_ranking_ma_rows(
    conn: sqlite3.Connection,
    snapshot_time: str,
) -> List[Dict[str, Any]]:
    cur = conn.cursor()

    snapshot_rows = _load_snapshot_rows(cur, snapshot_time)
    if not snapshot_rows:
        logger.warning("[RANKING MA] no snapshot rows time=%s", snapshot_time)
        return []

    summary_map = _load_summary_rows_for_minute(conn, snapshot_time)

    out: List[Dict[str, Any]] = []
    seen_keys: set[tuple[str, str, str, str]] = set()

    close_nonnull = 0
    score_nonnull = 0
    slope_nonnull = 0
    rsi_nonnull = 0
    macd_nonnull = 0

    for r in snapshot_rows:
        symbol = str(r.get("symbol") or "").strip()
        if not symbol:
            continue

        rank_type = str(
            r.get("rank_type") or r.get("ranking_type") or r.get("category") or "不明"
        ).strip()
        market = str(r.get("market") or "ALL").strip() or "ALL"
        symbolname = str(r.get("symbolname") or "").strip()
        rank_now = _normalize_rank(r.get("rank"))
        volume_now = _normalize_num(r.get("volume"))

        dedup_key = (symbol, rank_type, market, str(snapshot_time))
        if dedup_key in seen_keys:
            continue
        seen_keys.add(dedup_key)

        s = summary_map.get(symbol, {})
        close_v = _normalize_num(s.get("close"))
        score_v = _normalize_num(s.get("score"))
        slope_v = _normalize_num(s.get("slope"))
        rsi_v = _normalize_num(s.get("rsi"))
        macd_v = _normalize_num(s.get("macd"))
        macd_signal_v = _normalize_num(s.get("macd_signal"))
        macd_hist_v = _normalize_num(s.get("macd_hist"))

        if close_v is not None:
            close_nonnull += 1
        if score_v is not None:
            score_nonnull += 1
        if slope_v is not None:
            slope_nonnull += 1
        if rsi_v is not None:
            rsi_nonnull += 1
        if macd_v is not None:
            macd_nonnull += 1

        best_rank = _calc_best_rank(cur, symbol, rank_type, market, snapshot_time)
        ma_rank_position = _calc_ma_rank_position(cur, symbol, rank_type, market, snapshot_time)
        ma_volume_speed = _calc_ma_volume_speed(
            cur,
            symbol,
            rank_type,
            market,
            snapshot_time,
            current_volume=volume_now,
        )
        trend_score = _calc_trend_score(
            rank_now=rank_now,
            ma_rank_position=ma_rank_position,
            score=score_v,
            slope=slope_v,
            ma_volume_speed=ma_volume_speed,
        )

        out.append(
            {
                "symbol": symbol,
                "rank_type": rank_type,
                "market": market,
                "ma_rank_position": ma_rank_position,
                "ma_volume_speed": ma_volume_speed,
                "trend_score": trend_score,
                "snapshot_time": snapshot_time,
                "created_at": snapshot_time,
                "datetime": snapshot_time,
                "symbolname": symbolname,
                "close": close_v,
                "score": score_v,
                "slope": slope_v,
                "rsi": rsi_v,
                "macd": macd_v,
                "macd_signal": macd_signal_v,
                "macd_hist": macd_hist_v,
                "best_rank": best_rank,
                "source": f"RANKING_{rank_type}",
                "updated_at": snapshot_time,
            }
        )

    logger.info(
        "[RANKING MA] build rows=%s time=%s close_nonnull=%s score_nonnull=%s slope_nonnull=%s rsi_nonnull=%s macd_nonnull=%s",
        len(out),
        snapshot_time,
        close_nonnull,
        score_nonnull,
        slope_nonnull,
        rsi_nonnull,
        macd_nonnull,
    )
    return out


def _save_ranking_ma_rows(
    conn: sqlite3.Connection,
    rows: List[Dict[str, Any]],
    snapshot_time: str,
) -> int:
    if not rows:
        logger.warning("[RANKING MA] save skipped: empty rows time=%s", snapshot_time)
        return 0

    cur = conn.cursor()
    _ensure_ranking_ma_table(conn)

    try:
        cur.execute(
            f"""
            DELETE FROM {RANKING_MA_TABLE}
            WHERE datetime >= ?
              AND datetime < datetime(?, '+1 minute')
            """,
            (snapshot_time, snapshot_time),
        )
    except Exception:
        logger.exception("[RANKING MA] delete existing minute failed time=%s", snapshot_time)

    sql = f"""
        INSERT INTO {RANKING_MA_TABLE} (
            symbol,
            rank_type,
            market,
            ma_rank_position,
            ma_volume_speed,
            trend_score,
            snapshot_time,
            created_at,
            datetime,
            symbolname,
            close,
            score,
            slope,
            rsi,
            macd,
            macd_signal,
            macd_hist,
            best_rank,
            source,
            updated_at
        ) VALUES (
            :symbol,
            :rank_type,
            :market,
            :ma_rank_position,
            :ma_volume_speed,
            :trend_score,
            :snapshot_time,
            :created_at,
            :datetime,
            :symbolname,
            :close,
            :score,
            :slope,
            :rsi,
            :macd,
            :macd_signal,
            :macd_hist,
            :best_rank,
            :source,
            :updated_at
        )
    """

    try:
        cur.executemany(sql, rows)
        inserted = cur.rowcount if cur.rowcount is not None else len(rows)
        logger.info(
            "[RANKING MA] inserted rows=%s time=%s",
            inserted,
            snapshot_time,
        )
        return inserted
    except Exception:
        logger.exception("[RANKING MA] insert failed rows=%s time=%s", len(rows), snapshot_time)
        return 0


# ============================================================
# public entrypoint
# ============================================================


def build_ranking_tables_from_snapshot(snapshot_time: Optional[str]):
    """
    指定 snapshot_time の ranking_snapshot_1min を
    1) 旧 ranking テーブル群
    2) ranking_ma_1min
    へ展開する
    """

    if not RANKING_DB.exists():
        logger.warning("[RANKING] DB not found: %s", RANKING_DB)
        return

    conn = sqlite3.connect(RANKING_DB)
    conn.execute("PRAGMA busy_timeout=15000")
    cur = conn.cursor()

    try:
        if MASTER_DB.exists():
            cur.execute(f"ATTACH DATABASE '{MASTER_DB}' AS master")
        else:
            logger.warning("[RANKING] MASTER_DB not found: %s", MASTER_DB)
            cur.execute("ATTACH DATABASE ':memory:' AS master")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS master.symbol_master (
                    symbol TEXT PRIMARY KEY,
                    symbolname TEXT
                )
                """
            )

        if SUMMARY_DB.exists():
            cur.execute(f"ATTACH DATABASE '{SUMMARY_DB}' AS summary")
        else:
            logger.warning("[RANKING] SUMMARY_DB not found: %s", SUMMARY_DB)
            cur.execute("ATTACH DATABASE ':memory:' AS summary")

        if not _table_exists(cur, SNAPSHOT_TABLE):
            logger.warning("[RANKING] snapshot table not found: %s", SNAPSHOT_TABLE)
            return

        if not snapshot_time:
            snapshot_time = _load_latest_snapshot_time(cur)

        if not snapshot_time:
            logger.warning("[RANKING] latest snapshot_time not found")
            return

        legacy_inserted = _build_legacy_tables_from_snapshot(conn, snapshot_time)
        ma_rows = _build_ranking_ma_rows(conn, snapshot_time)
        ma_inserted = _save_ranking_ma_rows(conn, ma_rows, snapshot_time)

        snapshot_rows = _load_snapshot_rows(cur, snapshot_time)
        ranking_types = sorted(
            {str(r.get("rank_type")) for r in snapshot_rows if r.get("rank_type")}
        )

        conn.commit()

        logger.info(
            "[RANKING SNAPSHOT READY] snapshot=%s types=%s time=%s legacy_inserted=%s ma_inserted=%s",
            len(snapshot_rows),
            ranking_types,
            snapshot_time,
            legacy_inserted,
            ma_inserted,
        )

    except Exception:
        logger.exception("[RANKING] snapshot expand failed time=%s", snapshot_time)
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass