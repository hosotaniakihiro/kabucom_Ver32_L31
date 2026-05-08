# ============================================================
# File   : core/startup/startup_push_incremental_ma75.py
# Version: PRODUCTION-STABLE-V1-PUSH-INCREMENTAL-MA75
# ------------------------------------------------------------
# Purpose:
#   起動時に、銘柄ごとの保存済み 1分/3分/5分サマリー最新時刻以降の
#   PUSHだけを読み込み、既存tailと結合して MA75 を含む指標を作る。
#
# Why:
#   - PUSH bootstrap は生PUSHを復元するだけ
#   - startup_summary_restore は既存3/5分足tailを使うが、PUSH time列が
#     HH:MM:SS / HHMMSS の時刻のみの場合に差分抽出が弱い
#   - 起動直後から 75MA を途切れさせないため、保存済みsummary tailを
#     75本以上読み、最新以降のPUSHだけで追加足を作る
#
# Design:
#   1. summaryYYYYMMDD.db の stock_summary_1min/3min/5min tail を読む
#   2. 銘柄ごとの latest datetime を作る
#   3. pushYYYYMMDD.db の stream_data から直近PUSHを読む
#   4. PUSHを1分足へ丸め、銘柄ごとに latest_1min より後だけ採用
#   5. 1分足から3分足/5分足を作り、銘柄ごとの latest_3/5 より後だけ採用
#   6. 既存tail + 新規足で indicator_pipeline を通し、MA75を作る
#   7. global_data の merged summary cache へ反映する
#
# Notes:
#   - DB保存は既存scheduler/summary_saverに任せる。ここでは起動直後の
#     global cache 復元・MA75連続性を優先する。
#   - 失敗してもstartupを止めない。
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

DEFAULT_BASE_DIR = r"\\192.168.0.22\AutoStockBuyAndSell"
DEFAULT_SUMMARY_DIR = rf"{DEFAULT_BASE_DIR}\raw_data\kabu_station\summary"
DEFAULT_PUSH_DIR = rf"{DEFAULT_BASE_DIR}\raw_data\kabu_station\push"

SUMMARY_TABLE_BY_INTERVAL = {
    1: "stock_summary_1min",
    3: "stock_summary_3min",
    5: "stock_summary_5min",
}

TAIL_ROWS_PER_SYMBOL = int(os.environ.get("PUSH_INCREMENTAL_MA75_TAIL_ROWS", "120"))
PUSH_MAX_ROWS = int(os.environ.get("PUSH_INCREMENTAL_MA75_PUSH_MAX_ROWS", "50000"))
PUSH_LOOKBACK_MINUTES = int(os.environ.get("PUSH_INCREMENTAL_MA75_LOOKBACK_MINUTES", "240"))

MARKET_OPEN_TIME = dt.time(9, 0)
MARKET_CLOSE_TIME = dt.time(15, 30)


@dataclass
class PushIncrementalMA75Result:
    ok: bool
    summary_db: Optional[str] = None
    push_db: Optional[str] = None
    loaded_summary_rows: dict[int, int] = field(default_factory=dict)
    existing_latest: dict[int, Optional[str]] = field(default_factory=dict)
    push_rows: int = 0
    new_rows: dict[int, int] = field(default_factory=dict)
    cache_rows: dict[int, int] = field(default_factory=dict)
    ma75_nonnull: dict[int, int] = field(default_factory=dict)
    latest: dict[int, Optional[str]] = field(default_factory=dict)
    message: str = ""


# ============================================================
# path / sqlite helpers
# ============================================================

def _trade_date_str(trade_date: dt.date | str | None = None) -> str:
    if trade_date is None:
        return dt.datetime.now().strftime("%Y%m%d")
    if isinstance(trade_date, dt.date):
        return trade_date.strftime("%Y%m%d")
    s = str(trade_date).strip().replace("-", "")
    if len(s) >= 8:
        return s[:8]
    return dt.datetime.now().strftime("%Y%m%d")


def _trade_date_obj(trade_date: dt.date | str | None = None) -> dt.date:
    ymd = _trade_date_str(trade_date)
    return dt.datetime.strptime(ymd, "%Y%m%d").date()


def _resolve_db(dir_path: str, prefix: str, trade_date: dt.date | str | None = None) -> Optional[str]:
    ymd = _trade_date_str(trade_date)
    direct = Path(dir_path) / f"{prefix}{ymd}.db"
    if direct.exists():
        return str(direct)
    base = Path(dir_path)
    if not base.exists():
        return None
    files = sorted(base.glob(f"{prefix}*.db"), reverse=True)
    return str(files[0]) if files else None


def _table_exists(db_path: str, table: str) -> bool:
    try:
        with sqlite3.connect(db_path, timeout=5) as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
        return row is not None
    except Exception:
        return False


def _table_columns(db_path: str, table: str) -> list[str]:
    try:
        with sqlite3.connect(db_path, timeout=5) as conn:
            rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        return [str(r[1]) for r in rows]
    except Exception:
        return []


def _read_sql(db_path: str, sql: str, params: tuple[Any, ...] = ()) -> pd.DataFrame:
    try:
        with sqlite3.connect(db_path, timeout=5) as conn:
            try:
                conn.execute("PRAGMA busy_timeout=5000")
                conn.execute("PRAGMA query_only=ON")
            except Exception:
                pass
            return pd.read_sql(sql, conn, params=params)
    except Exception:
        logger.exception("[PUSH INCR MA75] read_sql failed db=%s sql=%s", db_path, sql[:200])
        return pd.DataFrame()


# ============================================================
# datetime / column normalization
# ============================================================

def _parse_time_only(value: Any, trade_day: dt.date) -> Optional[dt.datetime]:
    if value is None:
        return None
    try:
        s = str(value).strip()
        if not s or s.lower() in {"nan", "none", "nat", "<na>"}:
            return None
        if re.search(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", s):
            return None

        m = re.match(r"^(\d{1,2}):(\d{2})(?::(\d{2})(?:\.(\d{1,6}))?)?$", s)
        if m:
            hh = int(m.group(1))
            mm = int(m.group(2))
            ss = int(m.group(3) or 0)
            micros = int((m.group(4) or "0").ljust(6, "0")[:6])
            if 0 <= hh <= 23 and 0 <= mm <= 59 and 0 <= ss <= 59:
                return dt.datetime.combine(trade_day, dt.time(hh, mm, ss, micros))
            return None

        s2 = s.split(".", 1)[0]
        if s2.isdigit() and 3 <= len(s2) <= 6:
            s2 = s2.zfill(6)
            hh = int(s2[:2])
            mm = int(s2[2:4])
            ss = int(s2[4:6])
            if 0 <= hh <= 23 and 0 <= mm <= 59 and 0 <= ss <= 59:
                return dt.datetime.combine(trade_day, dt.time(hh, mm, ss))
    except Exception:
        return None
    return None


def _parse_datetime_series(s: pd.Series, trade_day: dt.date) -> pd.Series:
    def one(v: Any):
        t = _parse_time_only(v, trade_day)
        if t is not None:
            return t
        try:
            ts = pd.to_datetime(str(v), errors="coerce")
            if pd.isna(ts):
                return pd.NaT
            try:
                if getattr(ts, "tzinfo", None) is not None:
                    ts = ts.tz_convert("Asia/Tokyo").tz_localize(None)
            except Exception:
                try:
                    ts = ts.tz_localize(None)
                except Exception:
                    pass
            py = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
            if isinstance(py, dt.datetime) and py.year < 2000:
                return pd.NaT
            return py
        except Exception:
            return pd.NaT

    return pd.to_datetime(s.apply(one), errors="coerce")


def _first_existing_col(df: pd.DataFrame, candidates: tuple[str, ...]) -> Optional[str]:
    lower_map = {str(c).strip().lower(): c for c in df.columns}
    for c in candidates:
        if c.lower() in lower_map:
            return lower_map[c.lower()]
    return None


def _num(df: pd.DataFrame, col: str, default=np.nan) -> pd.Series:
    if col not in df.columns:
        return pd.Series(default, index=df.index, dtype="float64")
    return pd.to_numeric(df[col], errors="coerce").replace([np.inf, -np.inf], np.nan)


def _normalize_symbol(s: pd.Series) -> pd.Series:
    return (
        s.astype(str)
        .str.strip()
        .str.upper()
        .str.replace(r"\.T$", "", regex=True)
        .str.replace(r"\.0$", "", regex=True)
    )


# ============================================================
# summary tail load
# ============================================================

def _datetime_expr(cols: list[str]) -> Optional[str]:
    lower = {c.lower(): c for c in cols}
    if "datetime" in lower:
        return f'"{lower["datetime"]}"'
    if "date" in lower and "time" in lower:
        return f'("{lower["date"]}" || " " || "{lower["time"]}")'
    if "inserted_at" in lower:
        return f'"{lower["inserted_at"]}"'
    if "updated_at" in lower:
        return f'"{lower["updated_at"]}"'
    return None


def _load_summary_tail(summary_db: str, interval: int, *, tail_rows: int, trade_day: dt.date) -> pd.DataFrame:
    table = SUMMARY_TABLE_BY_INTERVAL[int(interval)]
    if not _table_exists(summary_db, table):
        return pd.DataFrame()

    cols = _table_columns(summary_db, table)
    dt_expr = _datetime_expr(cols)
    if not dt_expr:
        logger.warning("[PUSH INCR MA75] datetime expr unavailable table=%s cols=%s", table, cols)
        return pd.DataFrame()

    # 多めに読み、pandas側で銘柄別tailにする。
    limit = max(int(tail_rows) * 1200, int(tail_rows))
    sql = f"""
        SELECT *
        FROM "{table}"
        ORDER BY {dt_expr} DESC
        LIMIT ?
    """
    df = _read_sql(summary_db, sql, (limit,))
    if df.empty:
        return df

    df.columns = [str(c).strip() for c in df.columns]

    sym_col = _first_existing_col(df, ("symbol", "Symbol", "code", "銘柄コード"))
    if sym_col is None:
        return pd.DataFrame()
    df["symbol"] = _normalize_symbol(df[sym_col])

    if "datetime" in df.columns:
        df["datetime"] = _parse_datetime_series(df["datetime"], trade_day)
    elif "date" in df.columns and "time" in df.columns:
        df["datetime"] = _parse_datetime_series(df["date"].astype(str) + " " + df["time"].astype(str), trade_day)
    elif "time" in df.columns:
        df["datetime"] = _parse_datetime_series(df["time"], trade_day)
    else:
        return pd.DataFrame()

    df = df[df["symbol"].ne("") & df["datetime"].notna()].copy()
    if df.empty:
        return df

    for srcs, dst in [
        (("open", "open_price", "Open", "始値"), "open"),
        (("high", "high_price", "High", "高値"), "high"),
        (("low", "low_price", "Low", "安値"), "low"),
        (("close", "close_price", "price", "current_price", "CurrentPrice", "終値", "現在値"), "close"),
        (("volume", "trading_volume", "TradingVolume", "出来高"), "volume"),
    ]:
        c = _first_existing_col(df, srcs)
        if c is not None:
            df[dst] = pd.to_numeric(df[c], errors="coerce")

    if "close" not in df.columns:
        return pd.DataFrame()
    for c in ("open", "high", "low"):
        if c not in df.columns:
            df[c] = df["close"]
    if "volume" not in df.columns:
        df["volume"] = 0.0

    df["interval"] = int(interval)
    df["source"] = df.get("source", "summary_db_tail")

    df = df.sort_values(["symbol", "datetime"])
    df = df.drop_duplicates(["symbol", "datetime"], keep="last")
    df = df.groupby("symbol", group_keys=False).tail(int(tail_rows))
    return df.reset_index(drop=True)


def _latest_map(df: pd.DataFrame) -> dict[str, pd.Timestamp]:
    if df is None or df.empty or "symbol" not in df.columns or "datetime" not in df.columns:
        return {}
    tmp = df[["symbol", "datetime"]].copy()
    tmp["datetime"] = pd.to_datetime(tmp["datetime"], errors="coerce")
    tmp = tmp.dropna(subset=["datetime"])
    if tmp.empty:
        return {}
    return tmp.groupby("symbol")["datetime"].max().to_dict()


# ============================================================
# push load and aggregate
# ============================================================

def _resolve_push_table(push_db: str) -> Optional[str]:
    preferred = ("stream_data", "push_stream", "push_ticks", "push_data", "ticks")
    try:
        with sqlite3.connect(push_db, timeout=5) as conn:
            rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        tables = [str(r[0]) for r in rows]
    except Exception:
        return None
    for t in preferred:
        if t in tables:
            return t
    for t in tables:
        low = t.lower()
        if "stream" in low or "push" in low or "tick" in low:
            return t
    return tables[0] if tables else None


def _load_recent_push_rows(push_db: str) -> pd.DataFrame:
    table = _resolve_push_table(push_db)
    if not table:
        return pd.DataFrame()
    sql = f"""
        SELECT *
        FROM "{table}"
        ORDER BY rowid DESC
        LIMIT ?
    """
    return _read_sql(push_db, sql, (int(PUSH_MAX_ROWS),))


def _normalize_push_ticks(raw: pd.DataFrame, trade_day: dt.date) -> pd.DataFrame:
    if raw is None or raw.empty:
        return pd.DataFrame()

    df = raw.copy()
    df.columns = [str(c).strip() for c in df.columns]

    sym_col = _first_existing_col(df, ("symbol", "Symbol", "code", "symbol_code", "銘柄コード"))
    time_col = _first_existing_col(df, ("datetime", "time", "timestamp", "CurrentPriceTime", "current_price_time", "received_at", "inserted_at"))
    price_col = _first_existing_col(df, ("current_price", "CurrentPrice", "price", "Price", "close", "Close", "last_price", "LastPrice", "現在値"))
    vol_col = _first_existing_col(df, ("volume", "TradingVolume", "trading_volume", "Volume", "出来高"))
    name_col = _first_existing_col(df, ("symbolname", "symbol_name", "name", "銘柄名", "SymbolName"))

    if sym_col is None or time_col is None or price_col is None:
        logger.warning(
            "[PUSH INCR MA75] push required columns missing sym=%s time=%s price=%s cols=%s",
            sym_col,
            time_col,
            price_col,
            list(df.columns),
        )
        return pd.DataFrame()

    out = pd.DataFrame(index=df.index)
    out["symbol"] = _normalize_symbol(df[sym_col])
    out["datetime"] = _parse_datetime_series(df[time_col], trade_day)
    out["price"] = pd.to_numeric(df[price_col], errors="coerce").replace([np.inf, -np.inf], np.nan)
    out["volume"] = pd.to_numeric(df[vol_col], errors="coerce").replace([np.inf, -np.inf], np.nan) if vol_col else 0.0
    if name_col:
        out["symbolname"] = df[name_col].astype(str)

    out = out[out["symbol"].ne("") & out["datetime"].notna() & out["price"].notna()].copy()
    out = out[out["price"] > 0].copy()
    try:
        out = out[out["datetime"].dt.time.between(MARKET_OPEN_TIME, MARKET_CLOSE_TIME)]
    except Exception:
        pass

    if out.empty:
        return out

    # 起動時処理を軽くするため、過去N分に制限
    cutoff = pd.Timestamp(dt.datetime.now() - dt.timedelta(minutes=max(PUSH_LOOKBACK_MINUTES, 1)))
    out = out[out["datetime"] >= cutoff].copy()
    return out.sort_values(["symbol", "datetime"]).reset_index(drop=True)


def _ticks_to_1min(ticks: pd.DataFrame) -> pd.DataFrame:
    if ticks is None or ticks.empty:
        return pd.DataFrame()

    df = ticks.copy()
    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce").dt.floor("min")
    df = df.dropna(subset=["datetime"])
    if df.empty:
        return df

    agg_kwargs = dict(
        open=("price", "first"),
        high=("price", "max"),
        low=("price", "min"),
        close=("price", "last"),
        volume=("volume", "last"),
    )
    if "symbolname" in df.columns:
        agg_kwargs["symbolname"] = ("symbolname", "last")

    out = (
        df.sort_values(["symbol", "datetime"])
        .groupby(["symbol", "datetime"], as_index=False)
        .agg(**agg_kwargs)
    )
    out["interval"] = 1
    out["source"] = "push_incremental_after_summary_latest"
    return out.reset_index(drop=True)


def _filter_after_latest_by_symbol(df: pd.DataFrame, latest: dict[str, pd.Timestamp]) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    if not latest:
        return df.copy()
    out = df.copy()
    out["_latest"] = out["symbol"].map(latest)
    mask = out["_latest"].isna() | (pd.to_datetime(out["datetime"], errors="coerce") > pd.to_datetime(out["_latest"], errors="coerce"))
    out = out.loc[mask].drop(columns=["_latest"], errors="ignore")
    return out.reset_index(drop=True)


# ============================================================
# resample / indicators / cache
# ============================================================

def _resample_from_1min(df1: pd.DataFrame, interval: int) -> pd.DataFrame:
    if df1 is None or df1.empty:
        return pd.DataFrame()
    if int(interval) == 1:
        return df1.copy()

    frames: list[pd.DataFrame] = []
    for symbol, g in df1.groupby("symbol", sort=False):
        x = g.copy()
        x["datetime"] = pd.to_datetime(x["datetime"], errors="coerce")
        x = x.dropna(subset=["datetime"]).sort_values("datetime")
        if x.empty:
            continue
        x = x.set_index("datetime")

        rule = f"{int(interval)}min"
        agg = {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "last",
        }
        y = x.resample(rule, label="right", closed="right").agg(agg)
        y = y.dropna(subset=["close"]).reset_index()
        if y.empty:
            continue
        y["symbol"] = symbol
        if "symbolname" in g.columns:
            try:
                y["symbolname"] = str(g["symbolname"].dropna().iloc[-1])
            except Exception:
                pass
        y["interval"] = int(interval)
        y["source"] = f"push_incremental_{interval}min_after_summary_latest"
        frames.append(y)

    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    return out.sort_values(["symbol", "datetime"]).reset_index(drop=True)


def _combine_tail_and_new(existing: pd.DataFrame, new: pd.DataFrame, interval: int) -> pd.DataFrame:
    frames = []
    if isinstance(existing, pd.DataFrame) and not existing.empty:
        frames.append(existing)
    if isinstance(new, pd.DataFrame) and not new.empty:
        frames.append(new)
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True, sort=False)
    out["symbol"] = _normalize_symbol(out["symbol"])
    out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
    out = out[out["symbol"].ne("") & out["datetime"].notna()].copy()
    out = out.sort_values(["symbol", "datetime"]).drop_duplicates(["symbol", "datetime"], keep="last")
    out["interval"] = int(interval)

    for c in ("open", "high", "low", "close", "volume"):
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce")
    for c in ("open", "high", "low"):
        if c not in out.columns:
            out[c] = out["close"]
    if "volume" not in out.columns:
        out["volume"] = 0.0

    return out.reset_index(drop=True)


def _apply_indicators(df: pd.DataFrame, interval: int) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    try:
        from trading.summary.pipeline.indicator_pipeline import run_indicator_pipeline
        out = run_indicator_pipeline(df, interval=int(interval), run_downstream_scoring=True)
        if isinstance(out, pd.DataFrame):
            return out
    except Exception:
        logger.exception("[PUSH INCR MA75] indicator pipeline failed interval=%s", interval)

    # fallback: at least ma75
    out = df.copy()
    out = out.sort_values(["symbol", "datetime"])
    out["ma75"] = out.groupby("symbol")["close"].transform(lambda x: pd.to_numeric(x, errors="coerce").rolling(75, min_periods=75).mean())
    return out


def _set_global_cache(df: pd.DataFrame, interval: int) -> None:
    if df is None or df.empty:
        return

    # まず既存 controller_cache を使う
    try:
        from trading.summary.controller_cache import safe_global_set_merged_summary
        safe_global_set_merged_summary(int(interval), "push", df)
        return
    except Exception:
        logger.debug("[PUSH INCR MA75] safe_global_set_merged_summary unavailable", exc_info=True)

    # fallback: global_dataへ直接
    try:
        from global_state import global_data
        setattr(global_data, f"summary_{int(interval)}min_df", df)
        setattr(global_data, f"merged_summary_{int(interval)}min", df)
    except Exception:
        logger.exception("[PUSH INCR MA75] global cache set failed interval=%s", interval)


def _latest_str(df: pd.DataFrame) -> Optional[str]:
    try:
        if df is None or df.empty or "datetime" not in df.columns:
            return None
        ts = pd.to_datetime(df["datetime"], errors="coerce").max()
        return str(ts) if pd.notna(ts) else None
    except Exception:
        return None


def _ma75_nonnull(df: pd.DataFrame) -> int:
    try:
        if df is None or df.empty or "ma75" not in df.columns:
            return 0
        return int(pd.to_numeric(df["ma75"], errors="coerce").notna().sum())
    except Exception:
        return 0


# ============================================================
# public
# ============================================================

def build_push_incremental_ma75_on_startup(
    *,
    summary_db_path: str | None = None,
    push_db_path: str | None = None,
    trade_date: dt.date | str | None = None,
    intervals: tuple[int, ...] = (1, 3, 5),
    update_global_cache: bool = True,
) -> PushIncrementalMA75Result:
    result = PushIncrementalMA75Result(ok=False)
    started = dt.datetime.now()
    trade_day = _trade_date_obj(trade_date)

    try:
        summary_db = summary_db_path or _resolve_db(DEFAULT_SUMMARY_DIR, "summary", trade_day)
        push_db = push_db_path or _resolve_db(DEFAULT_PUSH_DIR, "push", trade_day)
        result.summary_db = summary_db
        result.push_db = push_db

        logger.info(
            "[PUSH INCR MA75] start summary_db=%s push_db=%s tail=%d push_limit=%d lookback=%d intervals=%s",
            summary_db,
            push_db,
            TAIL_ROWS_PER_SYMBOL,
            PUSH_MAX_ROWS,
            PUSH_LOOKBACK_MINUTES,
            intervals,
        )

        if not summary_db or not Path(summary_db).exists():
            result.message = f"summary db not found: {summary_db}"
            logger.warning("[PUSH INCR MA75] %s", result.message)
            return result
        if not push_db or not Path(push_db).exists():
            result.message = f"push db not found: {push_db}"
            logger.warning("[PUSH INCR MA75] %s", result.message)
            return result

        existing: dict[int, pd.DataFrame] = {}
        latest_maps: dict[int, dict[str, pd.Timestamp]] = {}

        for iv in intervals:
            x = _load_summary_tail(summary_db, int(iv), tail_rows=TAIL_ROWS_PER_SYMBOL, trade_day=trade_day)
            existing[int(iv)] = x
            latest_maps[int(iv)] = _latest_map(x)
            result.loaded_summary_rows[int(iv)] = int(len(x))
            result.existing_latest[int(iv)] = _latest_str(x)
            logger.info(
                "[PUSH INCR MA75] existing interval=%s rows=%d symbols=%d latest=%s",
                iv,
                len(x),
                x["symbol"].nunique() if not x.empty and "symbol" in x.columns else 0,
                _latest_str(x),
            )

        raw_push = _load_recent_push_rows(push_db)
        ticks = _normalize_push_ticks(raw_push, trade_day)
        result.push_rows = int(len(ticks))
        logger.info(
            "[PUSH INCR MA75] push loaded raw_rows=%d tick_rows=%d latest=%s",
            len(raw_push),
            len(ticks),
            _latest_str(ticks),
        )

        if ticks.empty:
            result.ok = True
            result.message = "no push ticks to append"
            return result

        one_new_all = _ticks_to_1min(ticks)
        one_new = _filter_after_latest_by_symbol(one_new_all, latest_maps.get(1, {}))
        result.new_rows[1] = int(len(one_new))

        combined_1 = _combine_tail_and_new(existing.get(1, pd.DataFrame()), one_new, 1)
        cache_1 = _apply_indicators(combined_1, 1)
        result.cache_rows[1] = int(len(cache_1))
        result.ma75_nonnull[1] = _ma75_nonnull(cache_1)
        result.latest[1] = _latest_str(cache_1)
        if update_global_cache and not cache_1.empty:
            _set_global_cache(cache_1, 1)

        # 3分/5分は 1分足 tail + 新規PUSHから作る
        for iv in (3, 5):
            if iv not in intervals:
                continue
            base_for_resample = _combine_tail_and_new(existing.get(1, pd.DataFrame()), one_new, 1)
            generated = _resample_from_1min(base_for_resample, iv)
            new_iv = _filter_after_latest_by_symbol(generated, latest_maps.get(iv, {}))
            result.new_rows[iv] = int(len(new_iv))

            combined = _combine_tail_and_new(existing.get(iv, pd.DataFrame()), new_iv, iv)
            cache = _apply_indicators(combined, iv)
            result.cache_rows[iv] = int(len(cache))
            result.ma75_nonnull[iv] = _ma75_nonnull(cache)
            result.latest[iv] = _latest_str(cache)
            if update_global_cache and not cache.empty:
                _set_global_cache(cache, iv)

            logger.info(
                "[PUSH INCR MA75] interval=%s generated=%d new=%d cache=%d ma75_nonnull=%d latest=%s",
                iv,
                len(generated),
                len(new_iv),
                result.cache_rows.get(iv, 0),
                result.ma75_nonnull.get(iv, 0),
                result.latest.get(iv),
            )

        logger.info(
            "[PUSH INCR MA75] done ok=True new_rows=%s cache_rows=%s ma75_nonnull=%s latest=%s elapsed=%.3fs",
            result.new_rows,
            result.cache_rows,
            result.ma75_nonnull,
            result.latest,
            (dt.datetime.now() - started).total_seconds(),
        )

        result.ok = True
        result.message = "push incremental MA75 cache built"
        return result

    except Exception as e:
        logger.exception("[PUSH INCR MA75] failed")
        result.ok = False
        result.message = str(e)
        return result


__all__ = ["PushIncrementalMA75Result", "build_push_incremental_ma75_on_startup"]
