# ============================================================
# File   : trading/ranking/summary/history_loader.py
# Version: PRODUCTION-STABLE-REV1.0-RANKING-SUMMARY-HISTORY-LOADER
# ------------------------------------------------------------
# Purpose:
#   ranking_summary 作成前に、既存 summary DB から 1分足履歴を読む。
#
# Why:
#   ranking_snapshot_1min だけで MA5/25/75, RSI, MACD を計算すると
#   履歴不足でテクニカルが未成熟になる。
#
# Flow:
#   stock_summary_1min の履歴
#      +
#   ranking_snapshot 由来の疑似1分足
#      ↓
#   builder 側で concat してテクニカル計算
#
# Notes:
#   - このファイルは DB 読み込み専用
#   - PUSH / Yahoo 由来の summary_1min を区別せず読む
#   - 直近N分だけでなく、MA75/MACD用に十分な履歴を読む
#   - SQLite lock 時も runtime を落とさない
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Optional

import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# defaults
# ============================================================

DEFAULT_BASE_DIR = r"\\192.168.0.22\AutoStockBuyAndSell"
DEFAULT_SUMMARY_DIR = os.path.join(
    DEFAULT_BASE_DIR,
    "raw_data",
    "kabu_station",
    "summary",
)

SUMMARY_1MIN_TABLE = "stock_summary_1min"

# MA75 + MACD + 少し余裕
DEFAULT_HISTORY_MINUTES = 420

SQLITE_TIMEOUT_SEC = 30.0
SQLITE_BUSY_TIMEOUT_MS = 30000


# ============================================================
# date / path helpers
# ============================================================

def normalize_trade_date(
    trade_date: Optional[str | int | dt.date | dt.datetime] = None,
) -> str:
    """
    trade_date を YYYYMMDD 文字列へ正規化する。
    None の場合は今日。
    """
    if trade_date is None:
        return dt.datetime.now().strftime("%Y%m%d")

    if isinstance(trade_date, dt.datetime):
        return trade_date.strftime("%Y%m%d")

    if isinstance(trade_date, dt.date):
        return trade_date.strftime("%Y%m%d")

    s = str(trade_date).strip()
    if not s:
        return dt.datetime.now().strftime("%Y%m%d")

    s = s.replace("-", "").replace("/", "")

    if len(s) >= 8:
        return s[:8]

    raise ValueError(f"invalid trade_date: {trade_date!r}")


def yyyymmdd_to_date_str(yyyymmdd: str) -> str:
    s = normalize_trade_date(yyyymmdd)
    return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"


def get_summary_db_path(
    trade_date: Optional[str | int | dt.date | dt.datetime] = None,
    *,
    summary_dir: str = DEFAULT_SUMMARY_DIR,
) -> str:
    """
    summaryYYYYMMDD.db のパスを返す。
    """
    d = normalize_trade_date(trade_date)
    return os.path.join(summary_dir, f"summary{d}.db")


def path_exists(path: str | os.PathLike[str] | None) -> bool:
    if not path:
        return False
    try:
        return Path(str(path)).exists()
    except Exception:
        return False


def previous_calendar_date_yyyymmdd(yyyymmdd: str) -> str:
    """
    単純に前日を返す。
    取引日カレンダーが未導入でも動かすため、営業日判定はここではしない。
    """
    d = dt.datetime.strptime(normalize_trade_date(yyyymmdd), "%Y%m%d").date()
    return (d - dt.timedelta(days=1)).strftime("%Y%m%d")


def resolve_existing_summary_db_paths(
    *,
    trade_date: Optional[str | int | dt.date | dt.datetime] = None,
    summary_dir: str = DEFAULT_SUMMARY_DIR,
    include_previous: bool = True,
) -> list[str]:
    """
    当日 summary DB と、必要なら前日 summary DB の存在確認をして返す。

    Returns:
        存在する DB パスの list
    """
    d = normalize_trade_date(trade_date)

    candidates: list[str] = []

    if include_previous:
        prev_d = previous_calendar_date_yyyymmdd(d)
        candidates.append(get_summary_db_path(prev_d, summary_dir=summary_dir))

    candidates.append(get_summary_db_path(d, summary_dir=summary_dir))

    paths: list[str] = []
    for p in candidates:
        if path_exists(p):
            paths.append(p)
        else:
            logger.info(
                "[RANKING SUMMARY HISTORY] summary db not found skip path=%s",
                p,
            )

    # 重複除去
    seen: set[str] = set()
    unique_paths: list[str] = []
    for p in paths:
        key = os.path.normcase(os.path.abspath(p))
        if key in seen:
            continue
        seen.add(key)
        unique_paths.append(p)

    return unique_paths


# ============================================================
# symbol helpers
# ============================================================

def normalize_symbol(v: Any) -> str:
    if v is None:
        return ""

    s = str(v).strip()

    if s.endswith(".0"):
        s = s[:-2]

    return s


def normalize_symbols(symbols: Optional[Iterable[Any]]) -> list[str]:
    if symbols is None:
        return []

    out: list[str] = []

    for x in symbols:
        s = normalize_symbol(x)
        if s:
            out.append(s)

    return sorted(set(out))


# ============================================================
# sqlite helpers
# ============================================================

def connect_readonly(db_path: str) -> sqlite3.Connection:
    """
    SQLite を読み取り用に開く。
    UNC パスでも安定しやすいように通常 connect を使う。
    """
    con = sqlite3.connect(
        db_path,
        timeout=SQLITE_TIMEOUT_SEC,
        isolation_level=None,
    )

    try:
        con.execute(f"PRAGMA busy_timeout={int(SQLITE_BUSY_TIMEOUT_MS)}")
        con.execute("PRAGMA query_only=ON")
    except Exception:
        pass

    return con


def table_exists(
    con: sqlite3.Connection,
    table_name: str,
) -> bool:
    try:
        row = con.execute(
            """
            SELECT name
              FROM sqlite_master
             WHERE type='table'
               AND name=?
             LIMIT 1
            """,
            (table_name,),
        ).fetchone()
        return row is not None
    except Exception:
        logger.warning(
            "[RANKING SUMMARY HISTORY] table_exists failed table=%s",
            table_name,
            exc_info=True,
        )
        return False


def get_table_columns(
    con: sqlite3.Connection,
    table_name: str,
) -> list[str]:
    try:
        rows = con.execute(f'PRAGMA table_info("{table_name}")').fetchall()
        return [str(r[1]) for r in rows if len(r) >= 2]
    except Exception:
        logger.warning(
            "[RANKING SUMMARY HISTORY] get_table_columns failed table=%s",
            table_name,
            exc_info=True,
        )
        return []


def first_existing_col(
    columns: Iterable[str],
    candidates: Iterable[str],
) -> Optional[str]:
    colset = set(columns)
    for c in candidates:
        if c in colset:
            return c
    return None


# ============================================================
# normalize summary history
# ============================================================

def normalize_summary_history_df(df: pd.DataFrame) -> pd.DataFrame:
    """
    stock_summary_1min の列名差を吸収し、builder で使える形へ正規化する。
    """
    if df is None or df.empty:
        return pd.DataFrame()

    out = df.copy()

    # datetime
    if "datetime" in out.columns:
        out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
    elif "date" in out.columns and "time" in out.columns:
        out["datetime"] = pd.to_datetime(
            out["date"].astype(str) + " " + out["time"].astype(str),
            errors="coerce",
        )
    else:
        logger.warning("[RANKING SUMMARY HISTORY] no datetime/date/time columns")
        return pd.DataFrame()

    out = out.dropna(subset=["datetime"])

    if out.empty:
        return pd.DataFrame()

    # symbol
    if "symbol" not in out.columns:
        logger.warning("[RANKING SUMMARY HISTORY] no symbol column")
        return pd.DataFrame()

    out["symbol"] = out["symbol"].map(normalize_symbol)
    out = out[out["symbol"] != ""]

    if out.empty:
        return pd.DataFrame()

    # symbolname
    if "symbolname" not in out.columns:
        if "name" in out.columns:
            out["symbolname"] = out["name"].astype(str)
        elif "symbol_name" in out.columns:
            out["symbolname"] = out["symbol_name"].astype(str)
        else:
            out["symbolname"] = ""

    # OHLC aliases
    alias_pairs = [
        ("open", ["open", "open_price"]),
        ("high", ["high", "high_price"]),
        ("low", ["low", "low_price"]),
        ("close", ["close", "close_price", "current_price"]),
        ("volume", ["volume", "trading_volume", "Volume"]),
    ]

    for target, aliases in alias_pairs:
        if target in out.columns:
            continue

        src = None
        for a in aliases:
            if a in out.columns:
                src = a
                break

        if src is not None:
            out[target] = out[src]
        else:
            out[target] = 0.0 if target == "volume" else pd.NA

    # price aliases
    if "open_price" not in out.columns:
        out["open_price"] = out["open"]
    if "high_price" not in out.columns:
        out["high_price"] = out["high"]
    if "low_price" not in out.columns:
        out["low_price"] = out["low"]
    if "close_price" not in out.columns:
        out["close_price"] = out["close"]

    for c in [
        "open",
        "high",
        "low",
        "close",
        "open_price",
        "high_price",
        "low_price",
        "close_price",
        "volume",
    ]:
        out[c] = pd.to_numeric(out[c], errors="coerce")

    out = out.dropna(subset=["close"])
    out = out[out["close"] > 0]

    if out.empty:
        return pd.DataFrame()

    out["date"] = out["datetime"].dt.strftime("%Y-%m-%d")
    out["time"] = out["datetime"].dt.strftime("%H:%M:%S")
    out["source"] = out.get("source", "summary_history")

    # ranking 側と concat しやすいように最低限の列を揃える
    defaults = {
        "ranking_type": "",
        "market": "",
        "rank": pd.NA,
        "current_price": out["close"],
        "price_source": "summary_history",
    }

    for c, v in defaults.items():
        if c not in out.columns:
            out[c] = v

    keep_cols = [
        "symbol",
        "symbolname",
        "datetime",
        "date",
        "time",
        "ranking_type",
        "market",
        "rank",
        "open",
        "high",
        "low",
        "close",
        "open_price",
        "high_price",
        "low_price",
        "close_price",
        "current_price",
        "volume",
        "source",
        "price_source",
    ]

    for c in keep_cols:
        if c not in out.columns:
            out[c] = pd.NA

    out = out[keep_cols].copy()
    out = out.sort_values(["symbol", "datetime"])
    out = out.drop_duplicates(["symbol", "datetime"], keep="last")

    return out


# ============================================================
# main loader
# ============================================================

def load_summary_1min_history_from_db(
    *,
    db_path: str,
    symbols: Optional[Iterable[Any]] = None,
    table_name: str = SUMMARY_1MIN_TABLE,
    start_datetime: Optional[str | dt.datetime | pd.Timestamp] = None,
    end_datetime: Optional[str | dt.datetime | pd.Timestamp] = None,
    limit_per_symbol: Optional[int] = None,
) -> pd.DataFrame:
    """
    1つの summary DB から stock_summary_1min 履歴を読む。
    """
    if not path_exists(db_path):
        logger.info(
            "[RANKING SUMMARY HISTORY] db not found path=%s",
            db_path,
        )
        return pd.DataFrame()

    symbol_list = normalize_symbols(symbols)

    con: Optional[sqlite3.Connection] = None

    try:
        con = connect_readonly(db_path)

        if not table_exists(con, table_name):
            logger.warning(
                "[RANKING SUMMARY HISTORY] table not found db=%s table=%s",
                db_path,
                table_name,
            )
            return pd.DataFrame()

        cols = get_table_columns(con, table_name)

        if not cols:
            logger.warning(
                "[RANKING SUMMARY HISTORY] no columns db=%s table=%s",
                db_path,
                table_name,
            )
            return pd.DataFrame()

        dt_col = first_existing_col(cols, ["datetime", "date"])
        time_col = first_existing_col(cols, ["time"])

        if dt_col is None:
            logger.warning(
                "[RANKING SUMMARY HISTORY] no datetime/date column db=%s table=%s cols=%s",
                db_path,
                table_name,
                cols,
            )
            return pd.DataFrame()

        where: list[str] = []
        params: dict[str, Any] = {}

        if symbol_list:
            placeholders: list[str] = []
            for i, sym in enumerate(symbol_list):
                key = f"sym_{i}"
                placeholders.append(f":{key}")
                params[key] = sym

            where.append(f"CAST(symbol AS TEXT) IN ({','.join(placeholders)})")

        # datetime 条件
        # datetime列がある場合のみSQLで絞る。date/time型だけの場合は読み込み後に絞る。
        if "datetime" in cols:
            if start_datetime is not None:
                params["start_dt"] = pd.to_datetime(start_datetime).strftime("%Y-%m-%d %H:%M:%S")
                where.append("datetime >= :start_dt")

            if end_datetime is not None:
                params["end_dt"] = pd.to_datetime(end_datetime).strftime("%Y-%m-%d %H:%M:%S")
                where.append("datetime <= :end_dt")

        sql = f'SELECT * FROM "{table_name}"'

        if where:
            sql += "\nWHERE " + " AND ".join(where)

        if "datetime" in cols:
            sql += "\nORDER BY symbol ASC, datetime ASC"
        elif "date" in cols and time_col:
            sql += "\nORDER BY symbol ASC, date ASC, time ASC"
        else:
            sql += f"\nORDER BY symbol ASC, {dt_col} ASC"

        raw = pd.read_sql_query(sql, con, params=params)

        if raw.empty:
            logger.info(
                "[RANKING SUMMARY HISTORY] empty db=%s table=%s symbols=%s",
                db_path,
                table_name,
                len(symbol_list),
            )
            return pd.DataFrame()

        df = normalize_summary_history_df(raw)

        if df.empty:
            logger.warning(
                "[RANKING SUMMARY HISTORY] normalized empty db=%s raw_rows=%s",
                db_path,
                len(raw),
            )
            return pd.DataFrame()

        # date/time型だけだった場合、ここで datetime 条件を適用
        if start_datetime is not None:
            start_ts = pd.to_datetime(start_datetime, errors="coerce")
            if pd.notna(start_ts):
                df = df[df["datetime"] >= start_ts]

        if end_datetime is not None:
            end_ts = pd.to_datetime(end_datetime, errors="coerce")
            if pd.notna(end_ts):
                df = df[df["datetime"] <= end_ts]

        if limit_per_symbol is not None and int(limit_per_symbol) > 0:
            n = int(limit_per_symbol)
            df = (
                df.sort_values(["symbol", "datetime"])
                .groupby("symbol", group_keys=False)
                .tail(n)
            )

        logger.info(
            "[RANKING SUMMARY HISTORY] loaded db=%s rows=%s symbols=%s dt_min=%s dt_max=%s",
            db_path,
            len(df),
            df["symbol"].nunique() if "symbol" in df.columns else 0,
            df["datetime"].min() if "datetime" in df.columns and not df.empty else None,
            df["datetime"].max() if "datetime" in df.columns and not df.empty else None,
        )

        return df

    except Exception:
        logger.exception(
            "[RANKING SUMMARY HISTORY] load failed db=%s table=%s",
            db_path,
            table_name,
        )
        return pd.DataFrame()

    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass


def load_summary_1min_history(
    *,
    trade_date: Optional[str | int | dt.date | dt.datetime] = None,
    symbols: Optional[Iterable[Any]] = None,
    summary_db_path: Optional[str] = None,
    summary_dir: str = DEFAULT_SUMMARY_DIR,
    include_previous: bool = True,
    history_minutes: int = DEFAULT_HISTORY_MINUTES,
    end_datetime: Optional[str | dt.datetime | pd.Timestamp] = None,
    table_name: str = SUMMARY_1MIN_TABLE,
) -> pd.DataFrame:
    """
    ranking_summary 計算用に stock_summary_1min 履歴を読む。

    Args:
        trade_date:
            対象日。Noneなら今日。
        symbols:
            読み込み対象銘柄。Noneなら全銘柄。
        summary_db_path:
            指定された場合はこのDBだけを読む。
        summary_dir:
            summaryYYYYMMDD.db の格納ディレクトリ。
        include_previous:
            Trueなら前日DBも読む。
        history_minutes:
            各銘柄について最大何分ぶん残すか。
            MA75/MACD用に 300〜450 程度を推奨。
        end_datetime:
            この時刻以前の履歴を読む。Noneなら制限なし。
        table_name:
            通常 stock_summary_1min。

    Returns:
        正規化済みの 1分足履歴 DataFrame
    """
    d = normalize_trade_date(trade_date)

    symbol_list = normalize_symbols(symbols)

    if summary_db_path:
        db_paths = [summary_db_path]
    else:
        db_paths = resolve_existing_summary_db_paths(
            trade_date=d,
            summary_dir=summary_dir,
            include_previous=include_previous,
        )

    if not db_paths:
        logger.warning(
            "[RANKING SUMMARY HISTORY] no summary db paths found trade_date=%s summary_dir=%s",
            d,
            summary_dir,
        )
        return pd.DataFrame()

    # end_datetime がなければ対象日の 23:59:59 まで
    if end_datetime is None:
        end_ts = pd.to_datetime(yyyymmdd_to_date_str(d) + " 23:59:59")
    else:
        end_ts = pd.to_datetime(end_datetime, errors="coerce")
        if pd.isna(end_ts):
            end_ts = pd.to_datetime(yyyymmdd_to_date_str(d) + " 23:59:59")

    # 前日DBも読むため、SQL段階では緩く読む
    # 最終的に tail(history_minutes) で絞る
    start_ts = end_ts - pd.Timedelta(minutes=max(int(history_minutes) * 2, int(history_minutes) + 120))

    frames: list[pd.DataFrame] = []

    for p in db_paths:
        x = load_summary_1min_history_from_db(
            db_path=p,
            symbols=symbol_list,
            table_name=table_name,
            start_datetime=start_ts,
            end_datetime=end_ts,
            limit_per_symbol=None,
        )

        if x is not None and not x.empty:
            frames.append(x)

    if not frames:
        logger.warning(
            "[RANKING SUMMARY HISTORY] history empty trade_date=%s symbols=%s dbs=%s",
            d,
            len(symbol_list),
            db_paths,
        )
        return pd.DataFrame()

    out = pd.concat(frames, ignore_index=True)
    out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
    out = out.dropna(subset=["symbol", "datetime", "close"])
    out["symbol"] = out["symbol"].map(normalize_symbol)
    out = out[out["symbol"] != ""]

    out = out.sort_values(["symbol", "datetime"])
    out = out.drop_duplicates(["symbol", "datetime"], keep="last")

    if history_minutes and int(history_minutes) > 0:
        out = (
            out.groupby("symbol", group_keys=False)
            .tail(int(history_minutes))
            .sort_values(["symbol", "datetime"])
        )

    logger.info(
        "[RANKING SUMMARY HISTORY] final rows=%s symbols=%s dt_min=%s dt_max=%s include_previous=%s history_minutes=%s",
        len(out),
        out["symbol"].nunique() if "symbol" in out.columns else 0,
        out["datetime"].min() if not out.empty else None,
        out["datetime"].max() if not out.empty else None,
        include_previous,
        history_minutes,
    )

    return out.reset_index(drop=True)


# ============================================================
# utility for builder integration
# ============================================================

def merge_history_and_ranking_pseudo(
    *,
    history_df: pd.DataFrame,
    ranking_pseudo_df: pd.DataFrame,
    keep_only_ranking_rows_after_calc: bool = False,
) -> pd.DataFrame:
    """
    builder.py 側で使う補助関数。

    history_df:
        stock_summary_1min から読んだ履歴

    ranking_pseudo_df:
        ranking_snapshot から作った疑似OHLC

    keep_only_ranking_rows_after_calc:
        False:
            テクニカル計算前の結合済み全体を返す。
        True:
            通常は使わない。
            テクニカル計算後に ranking 行だけ抽出する処理は builder 側で行う方が安全。

    Returns:
        履歴 + ランキング疑似足 の結合DataFrame
    """
    frames: list[pd.DataFrame] = []

    if history_df is not None and not history_df.empty:
        h = normalize_summary_history_df(history_df)
        if not h.empty:
            frames.append(h)

    if ranking_pseudo_df is not None and not ranking_pseudo_df.empty:
        r = ranking_pseudo_df.copy()
        r["datetime"] = pd.to_datetime(r["datetime"], errors="coerce")
        r = r.dropna(subset=["symbol", "datetime"])
        r["symbol"] = r["symbol"].map(normalize_symbol)
        r = r[r["symbol"] != ""]
        if "source" not in r.columns:
            r["source"] = "ranking_snapshot"
        if "price_source" not in r.columns:
            r["price_source"] = "ranking_snapshot"
        frames.append(r)

    if not frames:
        return pd.DataFrame()

    out = pd.concat(frames, ignore_index=True, sort=False)
    out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
    out = out.dropna(subset=["symbol", "datetime", "close"])
    out["symbol"] = out["symbol"].map(normalize_symbol)
    out = out[out["symbol"] != ""]

    for c in ["open", "high", "low", "close", "open_price", "high_price", "low_price", "close_price", "volume"]:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce")

    out = out.sort_values(["symbol", "datetime"])

    # 同一 symbol/datetime がある場合はランキング疑似足を優先したい。
    # source に ranking が含まれる行を後ろへ置く。
    if "source" in out.columns:
        out["_ranking_priority"] = (
            out["source"]
            .fillna("")
            .astype(str)
            .str.contains("ranking", case=False, na=False)
            .astype(int)
        )
        out = out.sort_values(["symbol", "datetime", "_ranking_priority"])
        out = out.drop_duplicates(["symbol", "datetime"], keep="last")
        out = out.drop(columns=["_ranking_priority"], errors="ignore")
    else:
        out = out.drop_duplicates(["symbol", "datetime"], keep="last")

    if keep_only_ranking_rows_after_calc:
        if "source" in out.columns:
            out = out[
                out["source"]
                .fillna("")
                .astype(str)
                .str.contains("ranking", case=False, na=False)
            ].copy()

    return out.reset_index(drop=True)


__all__ = [
    "DEFAULT_BASE_DIR",
    "DEFAULT_SUMMARY_DIR",
    "SUMMARY_1MIN_TABLE",
    "DEFAULT_HISTORY_MINUTES",
    "normalize_trade_date",
    "get_summary_db_path",
    "resolve_existing_summary_db_paths",
    "normalize_symbol",
    "normalize_symbols",
    "normalize_summary_history_df",
    "load_summary_1min_history_from_db",
    "load_summary_1min_history",
    "merge_history_and_ranking_pseudo",
]