# ============================================================
# File   : scripts/night_yahoo_full_summary_batch.py
# Version: V2-NIGHT-YAHOO-FULL-SUMMARY-SAVE-BY-BAR-DATE
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
#   - NIGHT_YAHOO_MINUTE_SAVE_BY_BAR_DATE=1 の場合、足の日付ごとに summaryYYYYMMDD.db へ分割保存
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
from trading.yahoo.pipeline.complement.db import get_summary_db_path
from trading.yahoo.pipeline.complement.constants import DEFAULT_BASE_DIR

LOG = logging.getLogger("night_yahoo_full_summary_batch")
VERSION = "V3-NIGHT-YAHOO-FULL-SUMMARY-CATCHUP-RANGE"

# ------------------------------------------------------------
# Yahoo Finance の分足取得制限 (2026-08-06 実測)
#   - 1m は約30日前までしか取得できない
#     (それ以前は "1m data not available for startTime")
#   - 1リクエストで取得できるのは最大8日分
# ここでは安全側に倒して 7 日窓で分割し、30 日を上限とする。
# ------------------------------------------------------------
YAHOO_1M_MAX_LOOKBACK_DAYS = 30
YAHOO_1M_MAX_SPAN_DAYS = 7


# ============================================================
# logging
# ============================================================

def _setup_logging() -> None:
    level = os.environ.get("NIGHT_YAHOO_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None or str(v).strip() == "":
        return bool(default)
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}


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
# catch-up: summary DB に格納済みの最新データを調べる
# ============================================================

def latest_stored_bar_date(*, max_lookback_days: int = 60) -> Optional[pd.Timestamp]:
    """summaryYYYYMMDD.db を新しい順に見て、実際に足が入っている最新日を返す。

    ファイルが存在するだけ・中身が空、というケースがあるため
    stock_summary_1min の件数まで確認する。
    見つからなければ None。
    """
    today = pd.Timestamp.now().normalize()
    for i in range(int(max_lookback_days) + 1):
        d = today - pd.Timedelta(days=i)
        path = get_summary_db_path(date_yyyymmdd=d.strftime("%Y%m%d"))
        if not os.path.exists(path):
            continue
        conn = None
        try:
            # 稼働中プロセスが書き込み中でもロックしないよう read-only で開く。
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='stock_summary_1min'"
            ).fetchone()
            if not row:
                continue
            n = conn.execute("SELECT COUNT(*) FROM stock_summary_1min").fetchone()[0]
            if int(n or 0) <= 0:
                continue
            LOG.info("[NIGHT YAHOO CATCHUP] latest stored summary date=%s rows=%s db=%s", d.date(), n, path)
            return d
        except Exception:
            LOG.warning("[NIGHT YAHOO CATCHUP] summary db read failed db=%s", path, exc_info=True)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
    return None


def _stored_volume(date: pd.Timestamp) -> Optional[int]:
    """指定日の summaryYYYYMMDD.db の格納量を表す指標を返す。

    完全な日と欠損した日を見分けるのが目的。

    NAS上の 500MB 級DBに COUNT(DISTINCT symbol) を投げると全表スキャンで
    1ファイル数十秒かかる。MAX(rowid) は速いが upsert の都合で日々の
    ばらつきが大きく判定に使えなかった (実測 51万〜257万)。
    ファイルサイズは os.stat だけで済むうえ分離がよい
    (実測: 正常日 568〜658MB に対し、途中停止した 2026-08-05 は 115MB)。

    DBが無ければ None (= 未取得)。
    """
    path = get_summary_db_path(date_yyyymmdd=pd.Timestamp(date).strftime("%Y%m%d"))
    try:
        if not os.path.exists(path):
            return None
        return int(os.path.getsize(path))
    except Exception:
        LOG.warning("[NIGHT YAHOO CATCHUP] size check failed db=%s", path, exc_info=True)
        return None


def find_incomplete_start(end: pd.Timestamp) -> Optional[pd.Timestamp]:
    """Yahooの取得可能範囲内で、格納量が不足している最も古い日を返す。

    「格納済みの最新日」だけを見ると、日中に停止して途中までしか
    保存されていない日 (例: 通常の2割しか入っていない日) を取りこぼす。
    取得可能範囲内の最大件数を基準に、その一定割合を下回る日を
    未完了とみなして取り直し対象にする。
    """
    ratio = float(os.environ.get("NIGHT_YAHOO_CATCHUP_MIN_RATIO", "0.5"))
    floor = pd.Timestamp.now().normalize() - pd.Timedelta(days=YAHOO_1M_MAX_LOOKBACK_DAYS)

    counts: dict[pd.Timestamp, Optional[int]] = {}
    d = floor
    while d <= end:
        if d.weekday() < 5:  # 土日は市場休みなので見ない
            counts[d] = _stored_volume(d)
        d += pd.Timedelta(days=1)

    if not counts:
        return None

    known = [c for c in counts.values() if c]
    if not known:
        # 範囲内に1件も入っていない -> 取得可能な最古日から
        LOG.warning("[NIGHT YAHOO CATCHUP] 取得可能範囲内に格納済みデータなし -> %s から取得する", floor.date())
        return floor

    reference = max(known)
    threshold = int(reference * ratio)

    incomplete = [d for d, c in sorted(counts.items()) if (c or 0) < threshold]
    for d in incomplete:
        LOG.warning(
            "[NIGHT YAHOO CATCHUP] 未完了と判定 date=%s 件数=%s (基準=%s 閾値=%s)",
            d.date(), counts[d] if counts[d] is not None else "DBなし", reference, threshold,
        )
    if not incomplete:
        LOG.info("[NIGHT YAHOO CATCHUP] 未完了の日なし (基準銘柄数=%s)", reference)
        return None
    return incomplete[0]


def resolve_catchup_range(
    *,
    explicit_start: Optional[pd.Timestamp] = None,
    explicit_end: Optional[pd.Timestamp] = None,
) -> tuple[pd.Timestamp, pd.Timestamp]:
    """取得すべき期間 [start, end] (両端含む・日付単位) を決める。

    start は「銘柄数が不足している最も古い日」。無ければ
    「格納済みの最新日」。その日も部分的な可能性があるため含めて
    取り直す (保存は upsert なので重複しない)。
    Yahoo が 1m を約30日前までしか返さないため、それより古い場合は
    30日前に切り上げ、切り捨てた期間をログに残す。
    """
    end = explicit_end or latest_market_date()

    if explicit_start is not None:
        start = explicit_start
    else:
        start = find_incomplete_start(end)
        if start is None:
            stored = latest_stored_bar_date()
            if stored is None:
                start = end
                LOG.warning(
                    "[NIGHT YAHOO CATCHUP] 格納済みsummaryが見つからないため最新営業日のみ対象にする end=%s",
                    end.date(),
                )
            else:
                start = stored

    if start > end:
        start = end

    floor = pd.Timestamp.now().normalize() - pd.Timedelta(days=YAHOO_1M_MAX_LOOKBACK_DAYS)
    if start < floor:
        LOG.warning(
            "[NIGHT YAHOO CATCHUP] Yahooの1m取得上限(%s日)により %s 〜 %s は取得できないためスキップする",
            YAHOO_1M_MAX_LOOKBACK_DAYS,
            start.date(),
            (floor - pd.Timedelta(days=1)).date(),
        )
        start = floor

    return start, end


def _split_windows(start: pd.Timestamp, end: pd.Timestamp) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """[start, end] を Yahoo の1リクエスト上限に収まる窓へ分割する。"""
    span = max(1, int(YAHOO_1M_MAX_SPAN_DAYS))
    windows: list[tuple[pd.Timestamp, pd.Timestamp]] = []
    cur = start
    while cur <= end:
        last = min(end, cur + pd.Timedelta(days=span - 1))
        windows.append((cur, last))
        cur = last + pd.Timedelta(days=1)
    return windows


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


def _normalize_yfinance_frame(
    raw: pd.DataFrame,
    symbols: list[str],
    market_date: pd.Timestamp,
    end_date: Optional[pd.Timestamp] = None,
) -> pd.DataFrame:
    """market_date から end_date までの足を取り出す。

    end_date 省略時は market_date 当日のみ (従来動作)。
    """
    if raw is None or raw.empty:
        return pd.DataFrame()

    frames: list[pd.DataFrame] = []
    date0 = market_date.normalize()
    last = (end_date or market_date).normalize()
    date1 = last + pd.Timedelta(days=1)

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


def fetch_1m_for_symbols(
    symbols: list[str],
    market_date: pd.Timestamp,
    *,
    batch_size: int,
    pause_sec: float,
    end_date: Optional[pd.Timestamp] = None,
) -> pd.DataFrame:
    """market_date 〜 end_date の1分足を取得する。

    end_date 省略時は market_date 当日のみ (従来動作)。
    期間が Yahoo の1リクエスト上限を超える場合は窓に分割して取得する。
    """
    if yf is None:
        raise RuntimeError("yfinance is not installed")

    start = pd.Timestamp(market_date).normalize()
    last = pd.Timestamp(end_date or market_date).normalize()
    windows = _split_windows(start, last)

    all_frames: list[pd.DataFrame] = []
    total = len(symbols)

    for w_idx, (w_start, w_end) in enumerate(windows, start=1):
        # end は排他指定のため翌日を渡す。
        yf_start = w_start.strftime("%Y-%m-%d")
        yf_end = (w_end + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        LOG.warning(
            "[NIGHT YAHOO] fetch window %s/%s %s .. %s",
            w_idx, len(windows), w_start.date(), w_end.date(),
        )

        for idx, chunk in enumerate(_chunks(symbols, batch_size), start=1):
            tickers = [_to_yahoo_ticker(s) for s in chunk if _to_yahoo_ticker(s)]
            if not tickers:
                continue
            LOG.info(
                "[NIGHT YAHOO] fetch window %s batch %s symbols=%s/%s first=%s",
                w_idx, idx, len(chunk), total, chunk[:5],
            )
            try:
                raw = yf.download(
                    tickers=" ".join(tickers),
                    start=yf_start,
                    end=yf_end,
                    interval="1m",
                    group_by="ticker",
                    auto_adjust=False,
                    prepost=False,
                    threads=True,
                    progress=False,
                )
                df = _normalize_yfinance_frame(raw, chunk, w_start, w_end)
                LOG.info(
                    "[NIGHT YAHOO] fetch window %s batch %s done rows=%s symbols=%s",
                    w_idx, idx, len(df), df["symbol"].nunique() if not df.empty else 0,
                )
                if not df.empty:
                    all_frames.append(df)
            except Exception:
                LOG.warning(
                    "[NIGHT YAHOO] fetch failed window=%s batch=%s size=%s",
                    w_idx, idx, len(chunk), exc_info=True,
                )
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
# save helpers
# ============================================================

def _date_series_for_save(df: pd.DataFrame) -> pd.Series:
    """保存先 summaryYYYYMMDD.db を決めるための足の日付Seriesを返す。"""
    if df is None or df.empty:
        return pd.Series(dtype="object")

    if "datetime" in df.columns:
        s = pd.to_datetime(df["datetime"], errors="coerce")
    elif "date" in df.columns:
        s = pd.to_datetime(df["date"], errors="coerce")
    else:
        return pd.Series([None] * len(df), index=df.index, dtype="object")

    try:
        return s.dt.strftime("%Y%m%d")
    except Exception:
        return pd.Series([None] * len(df), index=df.index, dtype="object")


def save_summary_df_by_bar_date(computed: pd.DataFrame, *, interval: int) -> int:
    """
    computedを足の日付ごとに分割して保存する。

    save_summary_df() 側はDF内の最大datetimeから summaryYYYYMMDD.db を決めるため、
    5日分をまとめて渡すと実行日/最新日のDBに過去日分まで混ざる。
    ここで日付単位に分割してから保存することで、
    2026-06-23 の足は summary20260623.db へ、2026-06-24 の足は summary20260624.db へ入る。
    """
    out = computed.copy() if computed is not None else pd.DataFrame()
    if out.empty:
        return 0

    save_dates = _date_series_for_save(out)
    if save_dates.empty or save_dates.isna().all():
        LOG.warning("[NIGHT YAHOO] save_by_bar_date no usable date interval=%s rows=%s -> normal save", interval, len(out))
        return int(save_summary_df(out, interval=int(interval)) or 0)

    total_saved = 0
    valid_dates = sorted({str(x) for x in save_dates.dropna().unique() if str(x) and str(x).lower() != "nat"})
    for yyyymmdd in valid_dates:
        part = out.loc[save_dates == yyyymmdd].copy()
        if part.empty:
            continue
        saved = int(save_summary_df(part, interval=int(interval)) or 0)
        total_saved += saved
        LOG.warning(
            "[NIGHT YAHOO] saved by bar date interval=%s save_date=%s rows=%s saved=%s symbols=%s latest=%s",
            interval,
            yyyymmdd,
            len(part),
            saved,
            part["symbol"].nunique() if "symbol" in part.columns else 0,
            part["datetime"].max() if "datetime" in part.columns else None,
        )
    return int(total_saved)


# ============================================================
# run
# ============================================================

def build_and_save(df_1m: pd.DataFrame, intervals: tuple[int, ...]) -> dict[int, dict[str, int]]:
    result: dict[int, dict[str, int]] = {}
    save_by_bar_date = _env_bool("NIGHT_YAHOO_MINUTE_SAVE_BY_BAR_DATE", True)

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

        if save_by_bar_date:
            saved = save_summary_df_by_bar_date(computed, interval=int(interval))
        else:
            saved = save_summary_df(computed, interval=int(interval))

        LOG.warning(
            "[NIGHT YAHOO] saved interval=%s raw=%s computed=%s saved=%s symbols=%s latest=%s save_by_bar_date=%s",
            interval,
            len(raw),
            len(computed),
            saved,
            computed["symbol"].nunique() if "symbol" in computed.columns else 0,
            computed["datetime"].max() if "datetime" in computed.columns else None,
            int(save_by_bar_date),
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
    parser.add_argument(
        "--start", dest="start", default=os.environ.get("NIGHT_YAHOO_START_DATE"),
        help="取得開始日 YYYY-MM-DD (省略時は summary DB の最新日から)",
    )
    parser.add_argument(
        "--no-catchup", dest="no_catchup", action="store_true",
        default=_env_bool("NIGHT_YAHOO_DISABLE_CATCHUP", False),
        help="catch-up を無効化し、最新営業日1日だけ取得する (従来動作)",
    )
    args = parser.parse_args(argv)

    intervals = tuple(int(x) for x in str(args.intervals).replace(" ", "").split(",") if x)
    intervals = tuple(x for x in intervals if x in {1, 3, 5}) or (1, 3, 5)

    explicit_date = _parse_date(args.date)
    explicit_start = _parse_date(args.start)

    if args.no_catchup:
        # 従来動作: 単日のみ
        market_date = explicit_date or latest_market_date()
        end_date = market_date
    else:
        market_date, end_date = resolve_catchup_range(
            explicit_start=explicit_start or explicit_date,
            explicit_end=None,
        )

    days = int((end_date - market_date).days) + 1
    LOG.warning(
        "[NIGHT YAHOO] START version=%s range=%s..%s (%s日) intervals=%s save_by_bar_date=%s catchup=%s",
        VERSION,
        market_date.date(),
        end_date.date(),
        days,
        intervals,
        int(_env_bool("NIGHT_YAHOO_MINUTE_SAVE_BY_BAR_DATE", True)),
        int(not args.no_catchup),
    )

    symbols = load_all_symbols()
    if args.limit and args.limit > 0:
        symbols = symbols[: int(args.limit)]
    LOG.warning("[NIGHT YAHOO] symbols loaded count=%s head=%s", len(symbols), symbols[:20])

    df_1m = fetch_1m_for_symbols(
        symbols,
        market_date,
        batch_size=args.batch_size,
        pause_sec=args.pause_sec,
        end_date=end_date,
    )
    if df_1m.empty:
        LOG.error(
            "[NIGHT YAHOO] no 1m data fetched range=%s..%s symbols=%s",
            market_date.date(), end_date.date(), len(symbols),
        )
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
