# ============================================================
# File   : scripts/night_yahoo_full_summary_batch.py
# Version: V1-NIGHT-YAHOO-FULL-SUMMARY-BATCH
# ------------------------------------------------------------
# 【概要】
#   夜間にYahoo Financeから全銘柄の最新営業日1分足を取得し、
#   1分/3分/5分のsummaryを計算して summaryYYYYMMDD.db に保存する。
#
# 【目的】
#   翌営業日の起動時点で、最新マーケット日のテクニカル・スコア・MTF等が
#   summary DBに揃っている状態にする。
#
# 【特徴】
#   - symbol_flags.db / CSV / 環境変数から銘柄一覧を取得
#   - yfinance 1mデータをバッチ取得
#   - 1mから3m/5mを生成
#   - 既存のYahoo補完compute/saveを使用し、summary DB schemaに合わせて保存
#   - 失敗銘柄があっても継続
#
# 【実行例】
#   python scripts/night_yahoo_full_summary_batch.py
#   python scripts/night_yahoo_full_summary_batch.py --date 2026-06-24 --batch-size 80
# ============================================================

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd

try:
    import yfinance as yf
except Exception:  # pragma: no cover
    yf = None

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from trading.yahoo.pipeline.complement.compute import compute_summary_frame
from trading.yahoo.pipeline.complement.save import save_summary_df
from trading.yahoo.pipeline.complement.constants import DEFAULT_BASE_DIR

LOG = logging.getLogger("night_yahoo_full_summary_batch")
VERSION = "V1-NIGHT-YAHOO-FULL-SUMMARY-BATCH"


# ============================================================
# logging
# ============================================================

def _setup_logging() -> None:
    level = os.environ.get("NIGHT_YAHOO_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


# ============================================================
# market date
# ============================================================

def _parse_date(value: str | None) -> Optional[pd.Timestamp]:
    if not value:
        return None
    ts = pd.to_datetime(value, errors="coerce")
    if pd.isna(ts):
        raise ValueError(f"invalid date: {value!r}")
    return pd.Timestamp(ts).normalize()


def latest_market_date(now: Optional[dt.datetime] = None) -> pd.Timestamp:
    """最新の日本市場営業日を返す。pandas_market_calendarsがあればJPXを使う。"""
    now = now or dt.datetime.now()
    today = pd.Timestamp(now).normalize()

    try:
        import pandas_market_calendars as mcal

        cal = mcal.get_calendar("JPX")
        start = (today - pd.Timedelta(days=14)).strftime("%Y-%m-%d")
        end = today.strftime("%Y-%m-%d")
        sched = cal.schedule(start_date=start, end_date=end)
        if not sched.empty:
            return pd.Timestamp(sched.index[-1]).normalize()
    except Exception:
        LOG.warning("[NIGHT YAHOO] JPX calendar unavailable -> weekday fallback", exc_info=False)

    d = today
    while d.weekday() >= 5:
        d -= pd.Timedelta(days=1)
    return d


# ============================================================
# symbol loading
# ============================================================

def _base_dir() -> Path:
    return Path(os.environ.get("AUTOSTOCK_BASE_DIR") or DEFAULT_BASE_DIR)


def _normalize_symbol(value) -> str:
    s = str(value or "").strip().upper()
    if not s or s in {"NAN", "NONE", "NULL", "-"}:
        return ""
    if s.endswith(".T"):
        s = s[:-2]
    if s.endswith(".0"):
        s = s[:-2]
    return s


def _to_yahoo_ticker(symbol: str) -> str:
    s = _normalize_symbol(symbol)
    return f"{s}.T" if s else ""


def _load_symbols_from_env() -> list[str]:
    raw = os.environ.get("NIGHT_YAHOO_SYMBOLS", "").strip()
    if not raw:
        return []
    out: list[str] = []
    for x in raw.replace("\n", ",").split(","):
        s = _normalize_symbol(x)
        if s:
            out.append(s)
    return sorted(set(out))


def _load_symbols_from_csv(path: Path) -> list[str]:
    if not path.exists():
        return []
    try:
        df = pd.read_csv(path, dtype=str, encoding="utf-8-sig")
    except UnicodeDecodeError:
        df = pd.read_csv(path, dtype=str, encoding="cp932")
    except Exception:
        LOG.warning("[NIGHT YAHOO] csv load failed path=%s", path, exc_info=True)
        return []

    candidate_cols = ["symbol", "code", "コード", "銘柄コード", "Symbol"]
    col = next((c for c in candidate_cols if c in df.columns), None)
    if col is None and len(df.columns) > 0:
        col = str(df.columns[0])
    if not col:
        return []
    return sorted({s for s in (_normalize_symbol(v) for v in df[col].tolist()) if s})


def _load_symbols_from_symbol_flags_db(path: Path) -> list[str]:
    if not path.exists():
        return []
    con = None
    try:
        con = sqlite3.connect(str(path), timeout=10)
        tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        preferred_tables = ["symbol_flags", "symbols", "watchlist"]
        ordered_tables = [t for t in preferred_tables if t in tables] + [t for t in tables if t not in preferred_tables]

        for table in ordered_tables:
            cols = [str(r[1]) for r in con.execute(f"PRAGMA table_info({table})").fetchall()]
            symbol_col = next((c for c in ["symbol", "code", "銘柄コード", "Symbol"] if c in cols), None)
            if not symbol_col:
                continue
            rows = con.execute(f"SELECT DISTINCT {symbol_col} FROM {table}").fetchall()
            symbols = sorted({s for s in (_normalize_symbol(r[0]) for r in rows) if s})
            if symbols:
                LOG.info("[NIGHT YAHOO] loaded symbols from db table=%s col=%s count=%s", table, symbol_col, len(symbols))
                return symbols
    except Exception:
        LOG.warning("[NIGHT YAHOO] symbol_flags db load failed path=%s", path, exc_info=True)
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass
    return []


def load_all_symbols() -> list[str]:
    # 1. 明示指定
    symbols = _load_symbols_from_env()
    if symbols:
        LOG.info("[NIGHT YAHOO] loaded symbols from env count=%s", len(symbols))
        return symbols

    # 2. symbol_flags.db
    base = _base_dir()
    db_candidates = [
        Path(os.environ.get("NIGHT_YAHOO_SYMBOL_DB", "")) if os.environ.get("NIGHT_YAHOO_SYMBOL_DB") else None,
        base / "Basic" / "symbol_flags.db",
        base / "basic" / "symbol_flags.db",
    ]
    for p in db_candidates:
        if p:
            symbols = _load_symbols_from_symbol_flags_db(p)
            if symbols:
                return symbols

    # 3. CSV候補
    csv_env = os.environ.get("NIGHT_YAHOO_SYMBOL_CSV")
    csv_candidates = [Path(csv_env)] if csv_env else []
    csv_candidates += [
        base / "Basic" / "symbols.csv",
        base / "Basic" / "watchlist.csv",
        PROJECT_ROOT / "symbols.csv",
        PROJECT_ROOT / "watchlist.csv",
    ]
    for p in csv_candidates:
        symbols = _load_symbols_from_csv(p)
        if symbols:
            LOG.info("[NIGHT YAHOO] loaded symbols from csv path=%s count=%s", p, len(symbols))
            return symbols

    raise RuntimeError("No symbols found. Set NIGHT_YAHOO_SYMBOLS or NIGHT_YAHOO_SYMBOL_DB / CSV.")


# ============================================================
# yahoo fetch
# ============================================================

def _chunks(seq: list[str], size: int) -> Iterable[list[str]]:
    for i in range(0, len(seq), max(1, int(size))):
        yield seq[i:i + max(1, int(size))]


def _normalize_yfinance_frame(raw: pd.DataFrame, symbols: list[str], market_date: pd.Timestamp) -> pd.DataFrame:
    if raw is None or raw.empty:
        return pd.DataFrame()

    frames: list[pd.DataFrame] = []
    date0 = market_date.normalize()
    date1 = date0 + pd.Timedelta(days=1)

    # yfinance download(group_by='ticker') は複数銘柄でMultiIndexになる。
    if isinstance(raw.columns, pd.MultiIndex):
        for sym in symbols:
            ticker = _to_yahoo_ticker(sym)
            if not ticker:
                continue
            try:
                if ticker not in raw.columns.get_level_values(0):
                    continue
                part = raw[ticker].copy()
            except Exception:
                continue
            if part.empty:
                continue
            part = _standardize_ohlcv(part, sym, date0, date1)
            if not part.empty:
                frames.append(part)
    else:
        # 1銘柄バッチの場合
        sym = symbols[0] if symbols else ""
        part = _standardize_ohlcv(raw.copy(), sym, date0, date1)
        if not part.empty:
            frames.append(part)

    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    out = out.sort_values(["symbol", "datetime"], kind="stable").reset_index(drop=True)
    return out


def _standardize_ohlcv(df: pd.DataFrame, symbol: str, date0: pd.Timestamp, date1: pd.Timestamp) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    work = df.copy()
    work = work.reset_index()
    # Datetime列名は環境で Datetime / Date などになる。
    dt_col = next((c for c in work.columns if str(c).lower() in {"datetime", "date", "index"}), work.columns[0])
    work["datetime"] = pd.to_datetime(work[dt_col], errors="coerce")
    try:
        if getattr(work["datetime"].dt, "tz", None) is not None:
            work["datetime"] = work["datetime"].dt.tz_convert("Asia/Tokyo").dt.tz_localize(None)
    except Exception:
        try:
            work["datetime"] = work["datetime"].dt.tz_localize(None)
        except Exception:
            pass

    rename = {}
    for c in work.columns:
        lc = str(c).strip().lower().replace(" ", "_")
        if lc == "open":
            rename[c] = "open"
        elif lc == "high":
            rename[c] = "high"
        elif lc == "low":
            rename[c] = "low"
        elif lc == "close":
            rename[c] = "close"
        elif lc == "volume":
            rename[c] = "volume"
    work = work.rename(columns=rename)

    required = ["open", "high", "low", "close", "volume"]
    if not all(c in work.columns for c in required):
        return pd.DataFrame()

    work = work[(work["datetime"] >= date0) & (work["datetime"] < date1)].copy()
    if work.empty:
        return pd.DataFrame()

    for c in required:
        work[c] = pd.to_numeric(work[c], errors="coerce")
    work = work.dropna(subset=["datetime", "open", "high", "low", "close"])
    if work.empty:
        return pd.DataFrame()

    work["volume"] = work["volume"].fillna(0.0)
    work["symbol"] = _normalize_symbol(symbol)
    return work[["symbol", "datetime", "open", "high", "low", "close", "volume"]].copy()


def fetch_1m_for_symbols(symbols: list[str], market_date: pd.Timestamp, *, batch_size: int, pause_sec: float) -> pd.DataFrame:
    if yf is None:
        raise RuntimeError("yfinance is not installed")

    all_frames: list[pd.DataFrame] = []
    period = os.environ.get("NIGHT_YAHOO_PERIOD", "5d")

    total = len(symbols)
    for idx, chunk in enumerate(_chunks(symbols, batch_size), start=1):
        tickers = [_to_yahoo_ticker(s) for s in chunk if _to_yahoo_ticker(s)]
        if not tickers:
            continue
        LOG.info("[NIGHT YAHOO] fetch batch %s symbols=%s/%s first=%s", idx, len(chunk), total, chunk[:5])
        try:
            raw = yf.download(
                tickers=" ".join(tickers),
                period=period,
                interval="1m",
                group_by="ticker",
                auto_adjust=False,
                prepost=False,
                threads=True,
                progress=False,
            )
            df = _normalize_yfinance_frame(raw, chunk, market_date)
            LOG.info("[NIGHT YAHOO] fetch batch done %s rows=%s symbols=%s", idx, len(df), df["symbol"].nunique() if not df.empty else 0)
            if not df.empty:
                all_frames.append(df)
        except Exception:
            LOG.warning("[NIGHT YAHOO] fetch batch failed idx=%s size=%s", idx, len(chunk), exc_info=True)
        if pause_sec > 0:
            time.sleep(float(pause_sec))

    if not all_frames:
        return pd.DataFrame()
    out = pd.concat(all_frames, ignore_index=True)
    out = out.drop_duplicates(subset=["symbol", "datetime"], keep="last")
    out = out.sort_values(["symbol", "datetime"], kind="stable").reset_index(drop=True)
    return out


# ============================================================
# resample
# ============================================================

def resample_ohlcv(df_1m: pd.DataFrame, interval: int) -> pd.DataFrame:
    if df_1m is None or df_1m.empty:
        return pd.DataFrame()
    if int(interval) == 1:
        return df_1m.copy()

    frames: list[pd.DataFrame] = []
    for symbol, g in df_1m.groupby("symbol", sort=False):
        work = g.copy()
        work["datetime"] = pd.to_datetime(work["datetime"], errors="coerce")
        work = work.dropna(subset=["datetime"]).set_index("datetime").sort_index()
        if work.empty:
            continue
        rule = f"{int(interval)}min"
        agg = work.resample(rule, label="right", closed="right").agg(
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            volume=("volume", "sum"),
        )
        agg = agg.dropna(subset=["open", "high", "low", "close"]).reset_index()
        if agg.empty:
            continue
        agg["symbol"] = symbol
        frames.append(agg[["symbol", "datetime", "open", "high", "low", "close", "volume"]])

    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True).sort_values(["symbol", "datetime"], kind="stable").reset_index(drop=True)


# ============================================================
# run
# ============================================================

def build_and_save(df_1m: pd.DataFrame, intervals: tuple[int, ...]) -> dict[int, dict[str, int]]:
    result: dict[int, dict[str, int]] = {}
    for interval in intervals:
        raw = resample_ohlcv(df_1m, interval)
        if raw.empty:
            LOG.warning("[NIGHT YAHOO] no raw bars interval=%s", interval)
            result[int(interval)] = {"raw": 0, "computed": 0, "saved": 0}
            continue

        computed = compute_summary_frame(raw, interval=int(interval))
        if computed.empty:
            LOG.warning("[NIGHT YAHOO] computed empty interval=%s raw=%s", interval, len(raw))
            result[int(interval)] = {"raw": len(raw), "computed": 0, "saved": 0}
            continue

        saved = save_summary_df(computed, interval=int(interval))
        LOG.warning(
            "[NIGHT YAHOO] saved interval=%s raw=%s computed=%s saved=%s symbols=%s latest=%s",
            interval,
            len(raw),
            len(computed),
            saved,
            computed["symbol"].nunique() if "symbol" in computed.columns else 0,
            computed["datetime"].max() if "datetime" in computed.columns else None,
        )
        result[int(interval)] = {"raw": len(raw), "computed": len(computed), "saved": int(saved or 0)}
    return result


def main(argv: Optional[list[str]] = None) -> int:
    _setup_logging()
    parser = argparse.ArgumentParser(description="Nightly Yahoo full summary batch")
    parser.add_argument("--date", dest="date", default=os.environ.get("NIGHT_YAHOO_TARGET_DATE"), help="target market date YYYY-MM-DD")
    parser.add_argument("--batch-size", type=int, default=int(os.environ.get("NIGHT_YAHOO_BATCH_SIZE", "80")))
    parser.add_argument("--pause-sec", type=float, default=float(os.environ.get("NIGHT_YAHOO_PAUSE_SEC", "1.0")))
    parser.add_argument("--limit", type=int, default=int(os.environ.get("NIGHT_YAHOO_LIMIT", "0")), help="debug symbol limit")
    parser.add_argument("--intervals", default=os.environ.get("NIGHT_YAHOO_INTERVALS", "1,3,5"))
    args = parser.parse_args(argv)

    market_date = _parse_date(args.date) or latest_market_date()
    intervals = tuple(int(x) for x in str(args.intervals).replace(" ", "").split(",") if x)
    intervals = tuple(x for x in intervals if x in {1, 3, 5}) or (1, 3, 5)

    LOG.warning("[NIGHT YAHOO] START version=%s market_date=%s intervals=%s", VERSION, market_date.date(), intervals)

    symbols = load_all_symbols()
    if args.limit and args.limit > 0:
        symbols = symbols[: int(args.limit)]
    LOG.warning("[NIGHT YAHOO] symbols loaded count=%s head=%s", len(symbols), symbols[:20])

    df_1m = fetch_1m_for_symbols(symbols, market_date, batch_size=args.batch_size, pause_sec=args.pause_sec)
    if df_1m.empty:
        LOG.error("[NIGHT YAHOO] no 1m data fetched market_date=%s symbols=%s", market_date.date(), len(symbols))
        return 2

    LOG.warning(
        "[NIGHT YAHOO] fetched 1m rows=%s symbols=%s latest=%s",
        len(df_1m),
        df_1m["symbol"].nunique(),
        df_1m["datetime"].max(),
    )

    result = build_and_save(df_1m, intervals)
    LOG.warning("[NIGHT YAHOO] DONE result=%s", result)

    # 保存件数0のintervalがあれば異常扱い。ただし取得できたintervalだけ見る。
    if any(v.get("computed", 0) > 0 and v.get("saved", 0) <= 0 for v in result.values()):
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
