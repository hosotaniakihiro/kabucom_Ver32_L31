# ============================================================
# File   : trading/ranking/ranking_technical_store.py
# Ver    : RANKING-TECH-STORE-v1.0.0
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
    if not symbols:
        return pd.DataFrame()
    chunks = []
    for symbol in symbols:
        q = f"""
            SELECT * FROM {TABLE_NAME}
            WHERE symbol = ?
            ORDER BY datetime DESC
            LIMIT ?
        """
        part = pd.read_sql_query(q, conn, params=(symbol, int(lookback_rows)))
        if not part.empty:
            chunks.append(part)
    if not chunks:
        return pd.DataFrame()
    df = pd.concat(chunks, ignore_index=True)
    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
    return df.dropna(subset=["datetime"]).sort_values(["symbol", "datetime"], kind="stable")


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
    """
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


def attach_ranking_technicals(row: Dict[str, Any], tech_map: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    try:
        symbol = str(row.get("symbol") or "").strip()
        tech = tech_map.get(symbol)
        if not isinstance(tech, dict):
            row["ranking_tech_ready"] = 0
            row["ranking_tech_reason"] = "NO_TECH"
            return row
        for k, v in tech.items():
            row[k] = v
        return row
    except Exception:
        return row


__all__ = [
    "TABLE_NAME",
    "TECH_COLUMNS",
    "save_ranking_pseudo_technicals",
    "attach_ranking_technicals",
]
