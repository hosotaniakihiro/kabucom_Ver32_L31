# ============================================================
# File   : trading/ranking/ranking_technical_store.py
# Ver    : RANKING-TECH-STORE-v2.0.0-INLINE-FAST-READONLY-AND-ALIAS
# ------------------------------------------------------------
# Purpose:
#   ランキング由来の現在値を疑似終値として扱い、サマリー本体とは別に
#   ランキング専用テクニカル指標を計算・DB保存する。
#
# Output DB:
#   \\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\ranking\rankingYYYYMMDD.db
#
# Output table:
#   ranking_technical_1min
#
# Design:
#   - ランキングの current_price / price を close とみなす
#   - 同一 symbol + datetime_minute は疑似OHLCへ集約
#   - open=初値, high=最大, low=最小, close=終値
#   - volume / turnover / rank 情報も保存
#   - ma5/ma25/ma75/rsi/macd/signal/hist/atr/slope/vwap 等を計算
#   - entry_from_ranking.py から呼ばれ、最新テクニカルを row に戻す
#
# v2.0.0:
#   - 旧 core/startup/ranking_entry_fast_runtime_patch.py の
#     readonly/メモリキャッシュ付き技術lookup (_patched_save_ranking_pseudo_technicals)
#     とバッチ_load_historyを本文へインライン化。既定でDB書き込み計算をせず、
#     既存テーブルの直近値を読むだけの高速パスを使う
#     (RANKING_ENTRY_SKIP_TECH_SAVE=0で従来の書き込みモードに戻せる)。
#   - 旧 core/startup/ranking_entry_snapshot_technical_alias_patch.py の
#     snapshot技術列エイリアスコピーを attach_ranking_technicals へインライン化。
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

DEFAULT_RANKING_DIR = r"\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\ranking"
TABLE_NAME = "ranking_technical_1min"


TECH_COLUMNS = [
    "ma5",
    "ma25",
    "ma75",
    "rsi",
    "macd",
    "signal",
    "macd_hist",
    "atr",
    "slope",
    "slope_atr_scaled",
    "vwap",
    "score_buy",
    "score_sell",
    "score_total",
    "ranking_tech_score",
    "ranking_tech_ready",
    "ranking_tech_reason",
]


def _today_yyyymmdd() -> str:
    return dt.datetime.now().strftime("%Y%m%d")


def _db_path() -> str:
    explicit = os.environ.get("RANKING_TECH_DB_PATH") or os.environ.get("RANKING_DB_PATH")
    if explicit:
        return explicit
    base = os.environ.get("RANKING_DB_DIR") or DEFAULT_RANKING_DIR
    return str(Path(base) / f"ranking{_today_yyyymmdd()}.db")


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        s = str(v).strip()
        if not s or s.lower() in ("nan", "none", "nat"):
            return default
        return float(s.replace(",", "").replace("%", ""))
    except Exception:
        return default


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        return int(float(str(v).replace(",", "")))
    except Exception:
        return default


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


# ============================================================
# readonly/cached technical lookup (旧 ranking_entry_fast_runtime_patch.py インライン化)
# ============================================================
# entry_from_ranking の1回の実行が数秒のtimeout budget制約下で動くため、
# 既定では save_ranking_pseudo_technicals は疑似足のDB書き込み計算をせず、
# 既存のranking_technical_1minテーブルから直近値を読むだけ(readonly)にする。
# RANKING_ENTRY_SKIP_TECH_SAVE=0 にすると従来の書き込みモードへ戻せる。

os.environ.setdefault("RANKING_ENTRY_SKIP_TECH_SAVE", "1")
os.environ.setdefault("RANKING_ENTRY_TECH_READONLY", "1")
os.environ.setdefault("RANKING_ENTRY_TECH_MEMORY_CACHE", "1")
os.environ.setdefault("RANKING_ENTRY_TECH_CACHE_TTL_SEC", "90")
os.environ.setdefault("RANKING_ENTRY_TECH_READ_BATCH_SIZE", "40")
os.environ.setdefault("RANKING_ENTRY_FAST_MAX_PREFILTER_ROWS", "40")
os.environ.setdefault("RANKING_ENTRY_FAST_MAX_SYMBOLS", "40")
os.environ.setdefault("RANKING_ENTRY_FAST_MAX_PER_SIDE", "22")
os.environ.setdefault("RANKING_ENTRY_FAST_MAX_PER_TYPE", "10")

_TECH_MEMORY_CACHE: Dict[str, Dict[str, Any]] = {}


def _readonly_side(row: Dict[str, Any]) -> str:
    s = str(row.get("side") or row.get("entry_decision") or "").upper().strip()
    if s in {"BUY", "SELL"}:
        return s
    rt = str(row.get("rank_type") or "")
    if "値下" in rt or "下落" in rt:
        return "SELL"
    day = _safe_float(row.get("day_change_pct"), 0.0)
    return "SELL" if day < 0 else "BUY"


def _readonly_row_priority(row: Dict[str, Any]) -> tuple:
    rank = _safe_int(row.get("rank_position") or row.get("rank"), 999999)
    turnover = _safe_float(row.get("turnover") or row.get("trading_value"), 0.0)
    volume = _safe_float(row.get("volume") or row.get("trading_volume"), 0.0)
    day_abs = abs(_safe_float(row.get("day_change_pct"), 0.0))
    return (rank, -turnover, -volume, -day_abs)


def _cap_rows_for_readonly_lookup(rows: List[Dict[str, Any]], *, context: str) -> List[Dict[str, Any]]:
    max_rows = max(5, _env_int("RANKING_ENTRY_FAST_MAX_PREFILTER_ROWS", 40))
    max_symbols = max(5, _env_int("RANKING_ENTRY_FAST_MAX_SYMBOLS", 40))
    max_per_side = max(3, _env_int("RANKING_ENTRY_FAST_MAX_PER_SIDE", 22))
    if len(rows) <= max_rows:
        return rows
    ordered = sorted([dict(r) for r in rows], key=_readonly_row_priority)
    kept: List[Dict[str, Any]] = []
    seen_symbols: set = set()
    seen_symbol_side: set = set()
    per_side: Dict[str, int] = {}
    for row in ordered:
        symbol = str(row.get("symbol") or "").strip()
        side = _readonly_side(row)
        if not symbol:
            continue
        if (symbol, side) in seen_symbol_side:
            continue
        if len(seen_symbols) >= max_symbols and symbol not in seen_symbols:
            continue
        if per_side.get(side, 0) >= max_per_side:
            continue
        kept.append(row)
        seen_symbols.add(symbol)
        seen_symbol_side.add((symbol, side))
        per_side[side] = per_side.get(side, 0) + 1
        if len(kept) >= max_rows:
            break
    logger.info("[RANKING TECH] readonly lookup row cap context=%s before=%s after=%s", context, len(rows), len(kept))
    return kept


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    try:
        r = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        return bool(r)
    except Exception:
        return False


def _db_mtime(path: str) -> float:
    try:
        return float(os.path.getmtime(path))
    except Exception:
        return 0.0


def _tech_cache_get(db: str, symbols: List[str]) -> tuple:
    if not _env_bool("RANKING_ENTRY_TECH_MEMORY_CACHE", True):
        return {}, symbols, "disabled"
    now = dt.datetime.now().timestamp()
    ttl = _env_float("RANKING_ENTRY_TECH_CACHE_TTL_SEC", 90.0)
    mtime = _db_mtime(db)
    cache = _TECH_MEMORY_CACHE.get(db)
    if not cache:
        return {}, symbols, "empty"
    if now - float(cache.get("ts") or 0.0) > ttl:
        return {}, symbols, "ttl_expired"
    if float(cache.get("mtime") or 0.0) != mtime:
        return {}, symbols, "db_mtime_changed"
    items = cache.get("items") or {}
    if not isinstance(items, dict):
        return {}, symbols, "invalid"
    hits = {s: dict(items[s]) for s in symbols if s in items}
    missing = [s for s in symbols if s not in hits]
    return hits, missing, "hit" if not missing else "partial"


def _tech_cache_put(db: str, items: Dict[str, Dict[str, Any]]) -> None:
    if not _env_bool("RANKING_ENTRY_TECH_MEMORY_CACHE", True):
        return
    try:
        now = dt.datetime.now().timestamp()
        mtime = _db_mtime(db)
        cur = _TECH_MEMORY_CACHE.get(db)
        if not cur or float(cur.get("mtime") or 0.0) != mtime:
            cur = {"ts": now, "mtime": mtime, "items": {}}
        cur_items = cur.setdefault("items", {})
        if isinstance(cur_items, dict):
            cur_items.update({str(k): dict(v) for k, v in (items or {}).items()})
        cur["ts"] = now
        cur["mtime"] = mtime
        _TECH_MEMORY_CACHE[db] = cur
    except Exception:
        logger.debug("[RANKING TECH] cache put failed", exc_info=True)


def _rows_to_tech_map(df: pd.DataFrame, db: str) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    if df is None or df.empty:
        return result
    if "datetime" in df.columns:
        df = df.copy()
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
        df = df.sort_values(["symbol", "datetime"], kind="stable").groupby("symbol", as_index=False).tail(1)
    for _, r in df.iterrows():
        sym = str(r.get("symbol") or "").strip()
        if not sym:
            continue
        item = {c: r.get(c) for c in TECH_COLUMNS if c in r.index}
        item["ranking_tech_datetime"] = r.get("datetime")
        item["ranking_tech_db"] = db
        item["ranking_tech_readonly"] = True
        item["ranking_tech_cacheable"] = True
        result[sym] = item
    return result


def _read_latest_tech_from_db(db: str, symbols: List[str]) -> Dict[str, Dict[str, Any]]:
    if not symbols:
        return {}
    batch_size = max(10, _env_int("RANKING_ENTRY_TECH_READ_BATCH_SIZE", 40))
    chunks = []
    with sqlite3.connect(db, timeout=1.5) as conn:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA busy_timeout=1200")
        if not _table_exists(conn, TABLE_NAME):
            logger.warning("[RANKING TECH] readonly tech skipped reason=table_missing db=%s table=%s", db, TABLE_NAME)
            return {}
        for i in range(0, len(symbols), batch_size):
            batch = symbols[i:i + batch_size]
            placeholders = ",".join(["?"] * len(batch))
            q = f"""
                SELECT t.*
                FROM {TABLE_NAME} t
                JOIN (
                    SELECT symbol, MAX(datetime) AS max_dt
                    FROM {TABLE_NAME}
                    WHERE symbol IN ({placeholders})
                    GROUP BY symbol
                ) m
                  ON t.symbol = m.symbol
                 AND t.datetime = m.max_dt
                WHERE t.symbol IN ({placeholders})
            """
            params = tuple(batch) + tuple(batch)
            part = pd.read_sql_query(q, conn, params=params)
            if not part.empty:
                chunks.append(part)
    if not chunks:
        return {}
    return _rows_to_tech_map(pd.concat(chunks, ignore_index=True), db)


def _latest_existing_technicals(rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    try:
        rows = _cap_rows_for_readonly_lookup(rows, context="before_readonly_tech")
        symbols = [str(r.get("symbol") or "").strip() for r in rows]
        symbols = [s for s in dict.fromkeys(symbols) if s]
        if not symbols:
            return {}
        db = _db_path()
        if not db or not os.path.exists(db):
            logger.warning("[RANKING TECH] readonly tech skipped reason=db_missing db=%s symbols=%s", db, len(symbols))
            return {}
        t0 = dt.datetime.now().timestamp()
        cached, missing, cache_state = _tech_cache_get(db, symbols)
        if cached and not missing:
            logger.info("[RANKING TECH] readonly tech cache hit symbols=%s hit=%s db=%s", len(symbols), len(cached), db)
            return cached
        db_items = _read_latest_tech_from_db(db, missing if cached else symbols)
        if db_items:
            _tech_cache_put(db, db_items)
        result = dict(cached)
        result.update(db_items)
        logger.info(
            "[RANKING TECH] readonly tech loaded symbols=%s hit=%s cache=%s db_read=%s db=%s elapsed=%.3fs",
            len(symbols), len(result), cache_state, len(db_items), db, dt.datetime.now().timestamp() - t0,
        )
        return result
    except Exception:
        logger.exception("[RANKING TECH] readonly tech load failed")
        return {}


def _first(row: Dict[str, Any], keys: Iterable[str], default: Any = None) -> Any:
    for k in keys:
        if k in row:
            v = row.get(k)
            if v is not None and str(v).strip() != "":
                return v
    return default


def _minute_floor(v: Any) -> dt.datetime:
    try:
        ts = pd.to_datetime(v, errors="coerce")
        if pd.notna(ts):
            return ts.to_pydatetime().replace(second=0, microsecond=0)
    except Exception:
        pass
    return dt.datetime.now().replace(second=0, microsecond=0)


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
            symbol TEXT NOT NULL,
            datetime TEXT NOT NULL,
            symbolname TEXT,
            source TEXT DEFAULT 'RANKING',
            rank_type TEXT,
            rank_position INTEGER,
            side TEXT,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            pseudo_close REAL,
            current_price REAL,
            volume REAL,
            turnover REAL,
            day_change_pct REAL,
            ma5 REAL,
            ma25 REAL,
            ma75 REAL,
            rsi REAL,
            macd REAL,
            signal REAL,
            macd_hist REAL,
            atr REAL,
            slope REAL,
            slope_atr_scaled REAL,
            vwap REAL,
            score_buy REAL,
            score_sell REAL,
            score_total REAL,
            ranking_tech_score REAL,
            ranking_tech_ready INTEGER DEFAULT 0,
            ranking_tech_reason TEXT,
            created_at TEXT,
            updated_at TEXT,
            PRIMARY KEY(symbol, datetime)
        )
        """
    )
    conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{TABLE_NAME}_dt ON {TABLE_NAME}(datetime)")
    conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{TABLE_NAME}_symbol_dt ON {TABLE_NAME}(symbol, datetime)")
    conn.commit()


def _rows_to_bars(rows: List[Dict[str, Any]]) -> pd.DataFrame:
    normalized: List[Dict[str, Any]] = []
    now_iso = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for row in rows:
        symbol = str(_first(row, ("symbol", "Symbol", "code", "コード"), "")).strip()
        if not symbol:
            continue
        price = _safe_float(
            _first(row, ("current_price", "close_price", "price", "close", "現在値"), 0.0),
            0.0,
        )
        if price <= 0:
            continue

        volume = _safe_float(_first(row, ("volume", "trading_volume", "出来高", "売買高"), 0.0), 0.0)
        turnover = _safe_float(
            _first(row, ("turnover", "trading_value", "売買代金", "value", "Value"), 0.0),
            0.0,
        )
        if turnover <= 0 and price > 0 and volume > 0:
            turnover = price * volume

        minute = _minute_floor(_first(row, ("datetime", "snapshot_time", "time", "created_at"), None))
        normalized.append(
            {
                "symbol": symbol,
                "datetime": minute.strftime("%Y-%m-%d %H:%M:%S"),
                "symbolname": str(_first(row, ("symbolname", "SymbolName", "銘柄名"), "") or ""),
                "source": "RANKING",
                "rank_type": str(_first(row, ("rank_type", "ranking_type", "type", "ランキング種別"), "") or ""),
                "rank_position": _safe_int(_first(row, ("rank_position", "rank", "順位", "Rank"), 999999), 999999),
                "side": str(row.get("side") or ""),
                "price": price,
                "volume": volume,
                "turnover": turnover,
                "day_change_pct": _safe_float(row.get("day_change_pct"), 0.0),
                "created_at": now_iso,
                "updated_at": now_iso,
            }
        )

    if not normalized:
        return pd.DataFrame()

    df = pd.DataFrame(normalized).sort_values(["symbol", "datetime"], kind="stable")
    grouped = []
    for (symbol, datetime_s), g in df.groupby(["symbol", "datetime"], sort=False):
        first = g.iloc[0]
        last = g.iloc[-1]
        grouped.append(
            {
                "symbol": symbol,
                "datetime": datetime_s,
                "symbolname": last.get("symbolname") or first.get("symbolname"),
                "source": "RANKING",
                "rank_type": last.get("rank_type") or first.get("rank_type"),
                "rank_position": int(pd.to_numeric(g["rank_position"], errors="coerce").min()),
                "side": last.get("side") or first.get("side"),
                "open": float(g["price"].iloc[0]),
                "high": float(g["price"].max()),
                "low": float(g["price"].min()),
                "close": float(g["price"].iloc[-1]),
                "pseudo_close": float(g["price"].iloc[-1]),
                "current_price": float(g["price"].iloc[-1]),
                "volume": float(pd.to_numeric(g["volume"], errors="coerce").max()),
                "turnover": float(pd.to_numeric(g["turnover"], errors="coerce").max()),
                "day_change_pct": float(pd.to_numeric(g["day_change_pct"], errors="coerce").iloc[-1]),
                "created_at": last.get("created_at"),
                "updated_at": last.get("updated_at"),
            }
        )
    return pd.DataFrame(grouped)


def _upsert_basic_bars(conn: sqlite3.Connection, bars: pd.DataFrame) -> None:
    if bars.empty:
        return
    cols = [
        "symbol",
        "datetime",
        "symbolname",
        "source",
        "rank_type",
        "rank_position",
        "side",
        "open",
        "high",
        "low",
        "close",
        "pseudo_close",
        "current_price",
        "volume",
        "turnover",
        "day_change_pct",
        "created_at",
        "updated_at",
    ]
    sql = f"""
        INSERT INTO {TABLE_NAME} ({','.join(cols)})
        VALUES ({','.join(['?'] * len(cols))})
        ON CONFLICT(symbol, datetime) DO UPDATE SET
            symbolname=excluded.symbolname,
            source=excluded.source,
            rank_type=excluded.rank_type,
            rank_position=excluded.rank_position,
            side=excluded.side,
            open=excluded.open,
            high=excluded.high,
            low=excluded.low,
            close=excluded.close,
            pseudo_close=excluded.pseudo_close,
            current_price=excluded.current_price,
            volume=excluded.volume,
            turnover=excluded.turnover,
            day_change_pct=excluded.day_change_pct,
            updated_at=excluded.updated_at
    """
    values = [tuple(row.get(c) for c in cols) for _, row in bars.iterrows()]
    conn.executemany(sql, values)
    conn.commit()


def _load_history(conn: sqlite3.Connection, symbols: List[str], lookback_rows: int = 120) -> pd.DataFrame:
    """symbolごとに1クエリではなく、バッチIN句でまとめて読む高速版。

    旧 core/startup/ranking_entry_fast_runtime_patch.py の_patched_load_historyを
    インライン化。symbol数が多いとsymbolごとの逐次クエリが遅くなるため、
    RANKING_ENTRY_FAST_HISTORY_BATCH_SIZE件ずつIN句でまとめて読む。
    """
    try:
        symbols = [str(s) for s in dict.fromkeys(symbols or []) if str(s).strip()]
        if not symbols:
            return pd.DataFrame()
        batch_size = max(10, _env_int("RANKING_ENTRY_FAST_HISTORY_BATCH_SIZE", 40))
        lookback_rows = min(int(lookback_rows or 30), _env_int("RANKING_ENTRY_FAST_TECH_LOOKBACK_ROWS", 30))
        chunks = []
        for i in range(0, len(symbols), batch_size):
            batch = symbols[i:i + batch_size]
            placeholders = ",".join(["?"] * len(batch))
            q = f"SELECT * FROM {TABLE_NAME} WHERE symbol IN ({placeholders}) ORDER BY symbol ASC, datetime DESC"
            part = pd.read_sql_query(q, conn, params=tuple(batch))
            if not part.empty:
                chunks.append(part.groupby("symbol", group_keys=False).head(int(lookback_rows)))
        if not chunks:
            return pd.DataFrame()
        df = pd.concat(chunks, ignore_index=True)
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
        return df.dropna(subset=["datetime"]).sort_values(["symbol", "datetime"], kind="stable")
    except Exception:
        logger.exception("[RANKING TECH] batch load_history failed")
        return pd.DataFrame()


def _calc_one_symbol(g: pd.DataFrame) -> pd.DataFrame:
    g = g.copy().sort_values("datetime", kind="stable")
    close = pd.to_numeric(g["close"], errors="coerce")
    high = pd.to_numeric(g["high"], errors="coerce").fillna(close)
    low = pd.to_numeric(g["low"], errors="coerce").fillna(close)
    volume = pd.to_numeric(g.get("volume", 0), errors="coerce").fillna(0)

    g["ma5"] = close.rolling(5, min_periods=1).mean()
    g["ma25"] = close.rolling(25, min_periods=1).mean()
    g["ma75"] = close.rolling(75, min_periods=1).mean()

    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14, min_periods=1).mean()
    loss = (-delta.clip(upper=0)).rolling(14, min_periods=1).mean()
    rs = gain / loss.replace(0, np.nan)
    g["rsi"] = (100 - (100 / (1 + rs))).fillna(50.0)

    ema12 = close.ewm(span=12, adjust=False, min_periods=1).mean()
    ema26 = close.ewm(span=26, adjust=False, min_periods=1).mean()
    g["macd"] = ema12 - ema26
    g["signal"] = g["macd"].ewm(span=9, adjust=False, min_periods=1).mean()
    g["macd_hist"] = g["macd"] - g["signal"]

    prev_close = close.shift(1)
    tr = pd.concat([(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    g["atr"] = tr.rolling(14, min_periods=1).mean().fillna(0.0)

    g["slope"] = close.diff(3) / close.shift(3).replace(0, np.nan)
    g["slope"] = g["slope"].fillna(0.0)
    atr_pct = (g["atr"] / close.replace(0, np.nan)).replace([np.inf, -np.inf], np.nan)
    g["slope_atr_scaled"] = (g["slope"] / atr_pct.replace(0, np.nan)).replace([np.inf, -np.inf], np.nan).fillna(0.0)

    pv = close * volume
    cum_vol = volume.cumsum().replace(0, np.nan)
    g["vwap"] = (pv.cumsum() / cum_vol).fillna(close)

    buy = pd.Series(0.0, index=g.index)
    sell = pd.Series(0.0, index=g.index)

    buy += (close > g["ma5"]).astype(float) * 1.0
    buy += (g["ma5"] >= g["ma25"]).astype(float) * 1.0
    buy += (g["ma25"] >= g["ma75"]).astype(float) * 0.5
    buy += (g["macd"] >= g["signal"]).astype(float) * 1.0
    buy += (g["slope"] > 0).astype(float) * 1.0
    buy += ((g["rsi"] >= 45) & (g["rsi"] <= 75)).astype(float) * 0.5

    sell += (close < g["ma5"]).astype(float) * 1.0
    sell += (g["ma5"] <= g["ma25"]).astype(float) * 1.0
    sell += (g["ma25"] <= g["ma75"]).astype(float) * 0.5
    sell += (g["macd"] <= g["signal"]).astype(float) * 1.0
    sell += (g["slope"] < 0).astype(float) * 1.0
    sell += ((g["rsi"] >= 25) & (g["rsi"] <= 55)).astype(float) * 0.5

    g["score_buy"] = buy
    g["score_sell"] = sell
    g["score_total"] = buy - sell
    g["ranking_tech_score"] = g["score_total"]
    g["ranking_tech_ready"] = (g.groupby("symbol").cumcount() >= 2).astype(int)
    g["ranking_tech_reason"] = np.where(
        g["ranking_tech_ready"].astype(int) == 1,
        "OK",
        "SHORT_HISTORY",
    )
    return g


def _calculate_technicals(history: pd.DataFrame) -> pd.DataFrame:
    if history.empty:
        return history
    out = []
    for _, g in history.groupby("symbol", sort=False):
        out.append(_calc_one_symbol(g))
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


def _upsert_technicals(conn: sqlite3.Connection, df: pd.DataFrame) -> None:
    if df.empty:
        return
    cols = [
        "symbol",
        "datetime",
        "ma5",
        "ma25",
        "ma75",
        "rsi",
        "macd",
        "signal",
        "macd_hist",
        "atr",
        "slope",
        "slope_atr_scaled",
        "vwap",
        "score_buy",
        "score_sell",
        "score_total",
        "ranking_tech_score",
        "ranking_tech_ready",
        "ranking_tech_reason",
        "updated_at",
    ]
    now_iso = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    work = df.copy()
    work["datetime"] = pd.to_datetime(work["datetime"], errors="coerce").dt.strftime("%Y-%m-%d %H:%M:%S")
    work["updated_at"] = now_iso
    sql = f"""
        UPDATE {TABLE_NAME}
        SET ma5=?, ma25=?, ma75=?, rsi=?, macd=?, signal=?, macd_hist=?, atr=?,
            slope=?, slope_atr_scaled=?, vwap=?, score_buy=?, score_sell=?, score_total=?,
            ranking_tech_score=?, ranking_tech_ready=?, ranking_tech_reason=?, updated_at=?
        WHERE symbol=? AND datetime=?
    """
    values = []
    for _, r in work.iterrows():
        values.append(
            (
                r.get("ma5"),
                r.get("ma25"),
                r.get("ma75"),
                r.get("rsi"),
                r.get("macd"),
                r.get("signal"),
                r.get("macd_hist"),
                r.get("atr"),
                r.get("slope"),
                r.get("slope_atr_scaled"),
                r.get("vwap"),
                r.get("score_buy"),
                r.get("score_sell"),
                r.get("score_total"),
                r.get("ranking_tech_score"),
                int(_safe_int(r.get("ranking_tech_ready"), 0)),
                r.get("ranking_tech_reason"),
                now_iso,
                r.get("symbol"),
                r.get("datetime"),
            )
        )
    conn.executemany(sql, values)
    conn.commit()


def _latest_map(calc: pd.DataFrame, symbols: List[str]) -> Dict[str, Dict[str, Any]]:
    if calc.empty:
        return {}
    work = calc[calc["symbol"].astype(str).isin([str(s) for s in symbols])].copy()
    if work.empty:
        return {}
    work = work.sort_values(["symbol", "datetime"], kind="stable").groupby("symbol", as_index=False).tail(1)
    result: Dict[str, Dict[str, Any]] = {}
    for _, r in work.iterrows():
        symbol = str(r.get("symbol") or "").strip()
        if not symbol:
            continue
        result[symbol] = {c: r.get(c) for c in TECH_COLUMNS if c in r.index}
        result[symbol]["ranking_tech_datetime"] = r.get("datetime")
        result[symbol]["ranking_tech_db"] = _db_path()
    return result


def save_ranking_pseudo_technicals(rows: List[Dict[str, Any]], lookback_rows: int = 120) -> Dict[str, Dict[str, Any]]:
    """
    ランキング rows から疑似終値テクニカルを計算・保存し、最新値を symbol map で返す。
    失敗してもエントリー本体を止めない。

    旧 core/startup/ranking_entry_fast_runtime_patch.py の
    _patched_save_ranking_pseudo_technicals をインライン化。entry_from_ranking の
    1回の実行がtimeout budget制約下で動くため、既定(RANKING_ENTRY_SKIP_TECH_SAVE=1)では
    DB書き込み計算をせず、既存テーブルから直近値を読むだけ(readonly, メモリキャッシュ付き)にする。
    """
    rows = _cap_rows_for_readonly_lookup(rows, context="before_save_technical")
    if _env_bool("RANKING_ENTRY_SKIP_TECH_SAVE", True):
        t0 = dt.datetime.now().timestamp()
        ret = _latest_existing_technicals(rows) if _env_bool("RANKING_ENTRY_TECH_READONLY", True) else {}
        logger.info("[RANKING TECH] technical save skipped rows=%s readonly_hit=%s elapsed=%.3fs", len(rows), len(ret or {}), dt.datetime.now().timestamp() - t0)
        return ret
    try:
        bars = _rows_to_bars(rows)
        if bars.empty:
            return {}

        path = _db_path()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        symbols = sorted(set(bars["symbol"].astype(str)))

        with sqlite3.connect(path, timeout=30.0) as conn:
            _ensure_schema(conn)
            _upsert_basic_bars(conn, bars)
            history = _load_history(conn, symbols, lookback_rows=lookback_rows)
            calc = _calculate_technicals(history)
            if not calc.empty:
                # 直近履歴だけ更新してDB負荷を抑える
                cutoff = pd.to_datetime(bars["datetime"], errors="coerce").min()
                if pd.notna(cutoff):
                    target = calc[pd.to_datetime(calc["datetime"], errors="coerce") >= cutoff - pd.Timedelta(minutes=80)].copy()
                else:
                    target = calc.copy()
                _upsert_technicals(conn, target)

        latest = _latest_map(calc, symbols) if not calc.empty else {}
        logger.info(
            "[RANKING TECH] saved table=%s bars=%s symbols=%s latest_map=%s db=%s",
            TABLE_NAME,
            len(bars),
            len(symbols),
            len(latest),
            path,
        )
        return latest
    except Exception as e:
        logger.warning(
            "[RANKING TECH] failed err=%s: %s",
            type(e).__name__,
            str(e)[:300],
            exc_info=False,
        )
        return {}


_SNAPSHOT_ALIAS_MAP = {
    "ma5": ("ma5_1m", "disp_ma5", "display_ma5"),
    "ma25": ("ma25_1m", "disp_ma25", "display_ma25"),
    "ma75": ("ma75_1m", "disp_ma75", "display_ma75"),
    "rsi": ("rsi_1m", "disp_rsi"),
    "macd": ("macd_1m", "disp_macd"),
    "signal": ("signal_1m", "macd_signal_1m", "disp_signal"),
    "macd_hist": ("macd_hist_1m", "hist_1m", "disp_macd_hist"),
    "atr": ("atr_1m", "disp_atr"),
    "slope": ("slope_1m", "ma5_slope_1m", "slope_pct_1m", "disp_slope"),
    "slope_atr_scaled": ("slope_atr_scaled_1m", "disp_slope_atr_scaled"),
    "vwap": ("vwap_1m", "disp_vwap"),
    "price_change_pct": ("price_change_pct_1m", "change_percentage", "change_rate"),
    "volume_ratio5": ("volume_ratio5_1m",),
}


def _snapshot_alias_is_blank(v: Any) -> bool:
    try:
        if v is None:
            return True
        s = str(v).strip()
        return s == "" or s.lower() in {"nan", "none", "nat", "<na>"}
    except Exception:
        return True


def _copy_snapshot_technical_aliases(row: Dict[str, Any]) -> Dict[str, Any]:
    """旧 core/startup/ranking_entry_snapshot_technical_alias_patch.py をインライン化。

    ranking_technical_1min テーブルが無い/未整備な日でも、ranking_snapshot_1min 側に
    既に ma5_1m/ma25_1m/macd_1m/slope_1m 等の技術列があれば、それを entry_controller の
    ガードが参照する無サフィックス名 (ma5/ma25/...) へエイリアスコピーする。
    """
    out = dict(row or {})
    for dst, srcs in _SNAPSHOT_ALIAS_MAP.items():
        cur = out.get(dst)
        if not _snapshot_alias_is_blank(cur) and _safe_float(cur, 0.0) != 0.0:
            continue
        for src in srcs:
            if src in out and not _snapshot_alias_is_blank(out.get(src)):
                out[dst] = out.get(src)
                break

    close = _safe_float(out.get("close") or out.get("close_price") or out.get("current_price") or out.get("price"), 0.0)
    ma5 = _safe_float(out.get("ma5"), 0.0)
    ma25 = _safe_float(out.get("ma25"), 0.0)
    slope = _safe_float(out.get("slope"), 0.0)
    macd = _safe_float(out.get("macd"), 0.0)
    signal = _safe_float(out.get("signal"), 0.0)
    atr = _safe_float(out.get("atr"), 0.0)
    rsi = _safe_float(out.get("rsi"), 50.0)

    ready = int(close > 0 and (ma5 > 0 or ma25 > 0 or abs(slope) > 0 or abs(macd) > 0 or atr > 0))
    if ready:
        out["ranking_tech_ready"] = 1
        out["ranking_tech_reason"] = "snapshot_alias"
        score = 0.0
        if close > 0 and ma5 > 0:
            score += min(20.0, abs(close - ma5) / close * 1000.0)
        if ma5 > 0 and ma25 > 0:
            score += min(20.0, abs(ma5 - ma25) / close * 1000.0 if close > 0 else 0.0)
        score += min(25.0, abs(slope) * 10000.0)
        score += min(15.0, abs(macd - signal) * 10.0)
        score += min(10.0, atr / close * 1000.0 if close > 0 else 0.0)
        if rsi != 50.0:
            score += min(10.0, abs(rsi - 50.0) / 5.0)
        out["ranking_tech_score"] = max(_safe_float(out.get("ranking_tech_score"), 0.0), round(score, 4))
        out["ranking_tech_source"] = "ranking_snapshot_alias"

    return out


def attach_ranking_technicals(row: Dict[str, Any], tech_map: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    try:
        symbol = str(row.get("symbol") or "").strip()
        tech = tech_map.get(symbol)
        if not isinstance(tech, dict):
            row["ranking_tech_ready"] = 0
            row["ranking_tech_reason"] = "NO_TECH"
            return _copy_snapshot_technical_aliases(row)
        for k, v in tech.items():
            row[k] = v
        return _copy_snapshot_technical_aliases(row)
    except Exception:
        return row


__all__ = [
    "TABLE_NAME",
    "TECH_COLUMNS",
    "save_ranking_pseudo_technicals",
    "attach_ranking_technicals",
]
