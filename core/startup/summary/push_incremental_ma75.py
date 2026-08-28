# ============================================================
# File   : core/startup/startup_push_incremental_ma75.py
# Version: PRODUCTION-STABLE-V2-PUSH-INCREMENTAL-MA75-MULTIDAY
# ------------------------------------------------------------
# Purpose:
#   起動時に、銘柄ごとの保存済み 1分/3分/5分サマリー最新時刻以降の
#   PUSHだけを読み込み、既存tailと結合して MA75 を含む指標を作る。
#
# Important:
#   - 5分足の75MAには 75本 * 5分 = 375分 が必要。
#   - 東証の1営業日は約330分なので、当日DBだけでは5分足75本に届かない。
#   - そのため summaryYYYYMMDD.db だけでなく、前日/前々日を含む
#     複数日のsummary DBから銘柄別tailを読む。
#
# Design:
#   1. summary DBを当日から最大3営業日相当分読む
#      例: summary20260508.db, summary20260507.db, summary20260506.db
#   2. stock_summary_1min/3min/5min の銘柄別tailを必要最小限の各75本を確保
#   3. pushYYYYMMDD.db のstream_dataから直近PUSHを読む
#   4. PUSHを1分足へ丸め、保存済みsummary最新以降のみ採用
#   5. 1分足から3分足/5分足を生成
#   6. 既存tail + 新規足でindicator_pipelineを通し、MA75を作る
#   7. global_data の merged summary cache へ反映する
#
# Env:
#   PUSH_INCREMENTAL_MA75_TAIL_ROWS=75
#   PUSH_INCREMENTAL_MA75_SUMMARY_LOOKBACK_DAYS=3
#   PUSH_INCREMENTAL_MA75_PUSH_MAX_ROWS=50000
#   PUSH_INCREMENTAL_MA75_LOOKBACK_MINUTES=240
# ============================================================

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import math
import os
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd
from sqlite_cm import committing_connect
from nas_root import nas_path, nas_root

logger = logging.getLogger(__name__)

DEFAULT_BASE_DIR = nas_root()
DEFAULT_SUMMARY_DIR = rf"{DEFAULT_BASE_DIR}\raw_data\kabu_station\summary"
DEFAULT_PUSH_DIR = rf"{DEFAULT_BASE_DIR}\raw_data\kabu_station\push"

SUMMARY_TABLE_BY_INTERVAL = {
    1: "stock_summary_1min",
    3: "stock_summary_3min",
    5: "stock_summary_5min",
}

MIN_MA75_BARS = 75
TAIL_ROWS_PER_SYMBOL = max(int(os.environ.get("PUSH_INCREMENTAL_MA75_TAIL_ROWS", str(MIN_MA75_BARS))), MIN_MA75_BARS)
SUMMARY_LOOKBACK_DAYS = int(os.environ.get("PUSH_INCREMENTAL_MA75_SUMMARY_LOOKBACK_DAYS", "3"))
PUSH_MAX_ROWS = int(os.environ.get("PUSH_INCREMENTAL_MA75_PUSH_MAX_ROWS", "50000"))
PUSH_LOOKBACK_MINUTES = int(os.environ.get("PUSH_INCREMENTAL_MA75_LOOKBACK_MINUTES", "240"))

MARKET_OPEN_TIME = dt.time(9, 0)
MARKET_CLOSE_TIME = dt.time(15, 30)

# 銘柄スコープ: "push" = PUSH受信銘柄 + ATS登録銘柄のみ (既定)
#               "all"  = summary DB の全銘柄 (従来動作)
SYMBOL_SCOPE = (os.environ.get("PUSH_INCREMENTAL_MA75_SYMBOL_SCOPE") or "push").strip().lower()
# 新しい日のDBだけで必要本数がそろったら残りの日を読まない
EARLY_STOP_DBS = (os.environ.get("PUSH_INCREMENTAL_MA75_EARLY_STOP", "1").strip() not in {"0", "false", "no", "off"})
# sqlite の host parameter 上限を避けるための分割数 (銘柄別SELECTでは未使用)
SYMBOL_CHUNK = max(50, int(os.environ.get("PUSH_INCREMENTAL_MA75_SYMBOL_CHUNK", "400")))


@dataclass
class PushIncrementalMA75Result:
    ok: bool
    summary_db: Optional[str] = None
    summary_dbs: list[str] = field(default_factory=list)
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


def _extract_ymd_from_path(path: str | Path, prefix: str = "summary") -> Optional[str]:
    name = Path(path).name
    m = re.search(rf"{re.escape(prefix)}(\d{{8}})\.db$", name, flags=re.IGNORECASE)
    return m.group(1) if m else None


def _ymd_to_date(ymd: str | None, fallback: dt.date) -> dt.date:
    try:
        if ymd and len(str(ymd)) == 8:
            return dt.datetime.strptime(str(ymd), "%Y%m%d").date()
    except Exception:
        pass
    return fallback


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


def _resolve_summary_dbs(
    dir_path: str,
    *,
    trade_date: dt.date | str | None = None,
    lookback_days: int = SUMMARY_LOOKBACK_DAYS,
) -> list[str]:
    """
    当日から過去方向にsummary DBを複数取得する。

    注意:
      - カレンダー営業日判定ではなく、実在するDBファイルを日付降順で採用する。
      - 祝日/休日をまたぐ場合でも、存在する直近DBを拾える。
      - 5分足75MAのため、標準3DBを読む。
    """
    base = Path(dir_path)
    if not base.exists():
        return []

    target_ymd = _trade_date_str(trade_date)
    rows: list[tuple[str, str]] = []
    for p in base.glob("summary*.db"):
        ymd = _extract_ymd_from_path(p, "summary")
        if not ymd:
            continue
        if ymd <= target_ymd:
            rows.append((ymd, str(p)))

    rows.sort(key=lambda x: x[0], reverse=True)
    out: list[str] = []
    for _, path in rows:
        if path not in out:
            out.append(path)
        if len(out) >= max(int(lookback_days), 1):
            break

    return out


def _table_exists(db_path: str, table: str) -> bool:
    try:
        with committing_connect(db_path, timeout=5) as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
        return row is not None
    except Exception:
        return False


def _table_columns(db_path: str, table: str) -> list[str]:
    try:
        with committing_connect(db_path, timeout=5) as conn:
            rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        return [str(r[1]) for r in rows]
    except Exception:
        return []


def _read_sql(db_path: str, sql: str, params: tuple[Any, ...] = ()) -> pd.DataFrame:
    try:
        with committing_connect(db_path, timeout=5) as conn:
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


def _symbol_col_name(cols: list[str]) -> Optional[str]:
    lower = {str(c).strip().lower(): c for c in cols}
    for c in ("symbol", "code"):
        if c in lower:
            return lower[c]
    return None


def _window_sql_enabled() -> bool:
    """window 関数経路。既定 OFF。

    EXPLAIN QUERY PLAN では symbol 単独 index + TEMP B-TREE ソートになり、
    (symbol, datetime) 複合 index を直撃する銘柄別クエリより遅かった
    (実測 80銘柄で 59.07s vs 21.60s)。検証用に env で有効化はできる。
    """
    return (os.environ.get("PUSH_INCREMENTAL_MA75_WINDOW_SQL", "0").strip().lower()
            in {"1", "true", "yes", "on"})


def _parallel_dbs_enabled() -> bool:
    return (os.environ.get("PUSH_INCREMENTAL_MA75_PARALLEL_DBS", "1").strip().lower()
            not in {"0", "false", "no", "off"})


def _tail_cache_enabled() -> bool:
    return (os.environ.get("PUSH_INCREMENTAL_MA75_TAIL_CACHE", "1").strip().lower()
            not in {"0", "false", "no", "off"})


def _tail_cache_dir() -> Path:
    v = os.environ.get("PUSH_INCREMENTAL_MA75_CACHE_DIR")
    if v and str(v).strip():
        return Path(str(v).strip())
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or "."
    return Path(base) / "kabu_autotrade" / "ma75_tail_cache"


def _tail_cache_key(summary_db: str, table: str, tail_rows: int, symbols=None) -> Optional[str]:
    """過去日 DB は内容が変わらないので (mtime, size) でキャッシュできる。

    銘柄集合はキーに入れない。配信銘柄は日中に増減する (実測 320->502->506) ため、
    集合をキーにすると 1 銘柄増えただけで全部読み直しになる。
    足りない銘柄は呼び出し側が差分で読み足す。
    """
    try:
        st = os.stat(summary_db)
        raw = "%s|%d|%d|%s|%d" % (Path(summary_db).name, int(st.st_mtime), int(st.st_size),
                                  table, int(tail_rows))
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()
    except Exception:
        logger.exception("[PUSH INCR MA75] cache key build failed db=%s", summary_db)
        return None


def _is_today_db(summary_db: str, trade_day: dt.date) -> bool:
    ymd = _extract_ymd_from_path(summary_db, "summary")
    return bool(ymd) and ymd == trade_day.strftime("%Y%m%d")


def _tail_cache_load(key: str) -> Optional[pd.DataFrame]:
    if not key or not _tail_cache_enabled():
        return None
    f = _tail_cache_dir() / (key + ".pkl")
    try:
        if not f.exists():
            return None
        df = pd.read_pickle(f)
        if isinstance(df, pd.DataFrame):
            return df
    except Exception:
        logger.exception("[PUSH INCR MA75] tail cache load failed file=%s", f)
    return None


def _tail_cache_save(key: str, df: pd.DataFrame) -> None:
    if not key or not _tail_cache_enabled() or not isinstance(df, pd.DataFrame) or df.empty:
        return
    d = _tail_cache_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / (key + ".pkl.tmp")
        df.to_pickle(tmp)
        os.replace(tmp, d / (key + ".pkl"))
    except Exception:
        logger.exception("[PUSH INCR MA75] tail cache save failed dir=%s", d)


def _sqlite_has_window() -> bool:
    try:
        parts = [int(x) for x in sqlite3.sqlite_version.split(".")[:2]]
        return (parts[0], parts[1]) >= (3, 25)
    except Exception:
        return False


def _read_tail_window(
    summary_db: str,
    table: str,
    dt_expr: str,
    sym_col: str,
    symbols: Optional[list[str]],
    tail_rows: int,
) -> pd.DataFrame:
    """1 クエリで「銘柄あたり末尾 tail_rows 本」を取る。

    ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY datetime DESC) を使う。
    銘柄別 SELECT を 502 回投げる方式より速く、502 個の小フレームを
    concat しないので FutureWarning も出ない。
    """
    frames: list[pd.DataFrame] = []
    try:
        with committing_connect(summary_db, timeout=120) as conn:
            try:
                conn.execute("PRAGMA busy_timeout=120000")
                conn.execute("PRAGMA query_only=ON")
                conn.execute("PRAGMA cache_size=-131072")
            except Exception:
                pass
            inner = (
                'SELECT *, ROW_NUMBER() OVER (PARTITION BY "%s" ORDER BY %s DESC) AS _ma75_rn'
                ' FROM "%s"' % (sym_col, dt_expr, table)
            )
            if symbols:
                for i in range(0, len(symbols), SYMBOL_CHUNK):
                    chunk = list(symbols[i:i + SYMBOL_CHUNK])
                    ph = ",".join("?" * len(chunk))
                    sql = ('SELECT * FROM (%s WHERE "%s" IN (%s)) WHERE _ma75_rn <= ?'
                           % (inner, sym_col, ph))
                    frames.append(pd.read_sql_query(sql, conn, params=chunk + [int(tail_rows)]))
            else:
                sql = 'SELECT * FROM (%s) WHERE _ma75_rn <= ?' % inner
                frames.append(pd.read_sql_query(sql, conn, params=(int(tail_rows),)))
    except Exception:
        logger.exception("[PUSH INCR MA75] window read failed db=%s table=%s", summary_db, table)
        return pd.DataFrame()
    frames = [f for f in frames if isinstance(f, pd.DataFrame) and not f.empty]
    if not frames:
        return pd.DataFrame()
    out = frames[0] if len(frames) == 1 else pd.concat(frames, ignore_index=True, sort=False)
    return out.drop(columns=["_ma75_rn"], errors="ignore")


def _bulk_today_enabled() -> bool:
    return (os.environ.get("PUSH_INCREMENTAL_MA75_BULK_TODAY", "1").strip().lower()
            not in {"0", "false", "no", "off"})


def _read_tail_bulk(
    summary_db: str,
    table: str,
    dt_expr: str,
    sym_col: str,
    symbols: list[str],
    tail_rows: int,
) -> pd.DataFrame:
    """symbol IN (...) をチャンクで投げて一括取得し、pandas 側で tail する。

    SMB は 1 クエリあたりの往復が高い。当日 DB は 1 銘柄あたり数十本しか
    無いので、506 回投げるより 3 回でまとめて取る方が速い
    (実測 47.6s -> 5.0s)。値は銘柄別読みと一致する。
    """
    frames: list[pd.DataFrame] = []
    try:
        with committing_connect(summary_db, timeout=60) as conn:
            try:
                conn.execute("PRAGMA busy_timeout=60000")
                conn.execute("PRAGMA query_only=ON")
                conn.execute("PRAGMA cache_size=-131072")
                conn.execute("PRAGMA mmap_size=536870912")
            except Exception:
                pass
            for i in range(0, len(symbols), SYMBOL_CHUNK):
                chunk = list(symbols[i:i + SYMBOL_CHUNK])
                ph = ",".join("?" * len(chunk))
                sql = 'SELECT * FROM "%s" WHERE "%s" IN (%s)' % (table, sym_col, ph)
                frames.append(pd.read_sql_query(sql, conn, params=chunk))
    except Exception:
        logger.exception("[PUSH INCR MA75] bulk read failed db=%s table=%s", summary_db, table)
        return pd.DataFrame()
    frames = [f for f in frames if isinstance(f, pd.DataFrame) and not f.empty]
    if not frames:
        return pd.DataFrame()
    out = frames[0] if len(frames) == 1 else pd.concat(frames, ignore_index=True, sort=False)
    dt_col = None
    for c in out.columns:
        if str(c).strip().lower() == "datetime":
            dt_col = c
            break
    if dt_col is None or sym_col not in out.columns:
        return out.reset_index(drop=True)
    try:
        out = (out.sort_values([sym_col, dt_col], kind="stable")
                  .groupby(sym_col, group_keys=False)
                  .tail(int(tail_rows)))
    except Exception:
        logger.exception("[PUSH INCR MA75] bulk tail trim failed db=%s table=%s", summary_db, table)
    return out.reset_index(drop=True)


def _read_tail_per_symbol(
    summary_db: str,
    table: str,
    dt_expr: str,
    sym_col: str,
    symbols: list[str],
    tail_rows: int,
) -> pd.DataFrame:
    """(symbol, datetime) index を直撃して銘柄ごとに末尾 tail_rows 本だけ読む。

    従来の `ORDER BY datetime DESC LIMIT tail*1600` は全銘柄 12 万行を
    読んでおり、SMB 越しで 1 テーブルあたり数分かかっていた。
    """
    sql = (
        'SELECT * FROM "%s" WHERE "%s" = ? ORDER BY %s DESC LIMIT ?'
        % (table, sym_col, dt_expr)
    )
    # 銘柄ごとに DataFrame を作って最後に concat すると、
    # 実測で pandas 側だけで 7.93 秒 (全体 11.34 秒の 70%) を使っていた。
    # 行はそのまま貯めて、最後に 1 回だけ DataFrame にする。
    # 502 フレームの concat が消えるので FutureWarning も出なくなる。
    cols: Optional[list[str]] = None
    all_rows: list[tuple] = []
    n_err = 0
    try:
        with committing_connect(summary_db, timeout=60) as conn:
            try:
                conn.execute("PRAGMA busy_timeout=60000")
                conn.execute("PRAGMA query_only=ON")
                conn.execute("PRAGMA cache_size=-131072")
                conn.execute("PRAGMA mmap_size=536870912")
            except Exception:
                pass
            for sym in symbols:
                try:
                    cur = conn.execute(sql, (str(sym), int(tail_rows)))
                    if cols is None:
                        cols = [d[0] for d in cur.description]
                    all_rows.extend(cur.fetchall())
                except Exception:
                    n_err += 1
                    if n_err <= 3:
                        logger.exception(
                            "[PUSH INCR MA75] per-symbol read failed db=%s table=%s symbol=%s",
                            summary_db, table, sym,
                        )
                    continue
    except Exception:
        logger.exception("[PUSH INCR MA75] per-symbol read open failed db=%s table=%s", summary_db, table)
        return pd.DataFrame()
    if n_err > 3:
        logger.warning(
            "[PUSH INCR MA75] per-symbol read errors db=%s table=%s count=%d (先頭3件のみ trace)",
            summary_db, table, n_err,
        )
    if not all_rows or not cols:
        return pd.DataFrame()
    return pd.DataFrame(all_rows, columns=cols)


def _load_summary_tail(
    summary_db: str,
    interval: int,
    *,
    tail_rows: int,
    trade_day: dt.date,
    symbols: Optional[list[str]] = None,
) -> pd.DataFrame:
    table = SUMMARY_TABLE_BY_INTERVAL[int(interval)]
    if not _table_exists(summary_db, table):
        return pd.DataFrame()

    cols = _table_columns(summary_db, table)
    dt_expr = _datetime_expr(cols)
    if not dt_expr:
        logger.warning("[PUSH INCR MA75] datetime expr unavailable table=%s cols=%s db=%s", table, cols, summary_db)
        return pd.DataFrame()

    sym_col_sql = _symbol_col_name(cols)
    cache_key = ""
    cached_part: Optional[pd.DataFrame] = None
    want = [str(s) for s in symbols] if symbols else None
    if sym_col_sql and not _is_today_db(summary_db, trade_day):
        # 過去日 DB は内容が変わらないのでローカルキャッシュが使える
        cache_key = _tail_cache_key(summary_db, table, int(tail_rows)) or ""
        cached = _tail_cache_load(cache_key)
        if cached is not None and not cached.empty and "symbol" in cached.columns:
            have = set(cached["symbol"].astype(str))
            if want is None:
                logger.info(
                    "[PUSH INCR MA75] tail cache hit db=%s table=%s rows=%d (全銘柄)",
                    Path(summary_db).name, table, len(cached),
                )
                return cached
            missing = [s for s in want if s not in have]
            if not missing:
                out = cached[cached["symbol"].astype(str).isin(set(want))].reset_index(drop=True)
                logger.info(
                    "[PUSH INCR MA75] tail cache hit db=%s table=%s rows=%d symbols=%d",
                    Path(summary_db).name, table, len(out), len(want),
                )
                return out
            logger.info(
                "[PUSH INCR MA75] tail cache partial db=%s table=%s cached_symbols=%d "
                "missing=%d -> 不足ぶんだけ読み足す",
                Path(summary_db).name, table, len(have), len(missing),
            )
            cached_part = cached
            symbols = missing

    is_today = _is_today_db(summary_db, trade_day)
    if symbols and sym_col_sql and is_today and _bulk_today_enabled():
        # 当日 DB は本数が少ないので一括取得のほうが往復が減って速い
        df = _read_tail_bulk(summary_db, table, dt_expr, sym_col_sql, list(symbols), int(tail_rows))
        if df.empty:
            logger.warning(
                "[PUSH INCR MA75] bulk read empty -> 銘柄別読みへフォールバック db=%s table=%s",
                Path(summary_db).name, table,
            )
            df = _read_tail_per_symbol(summary_db, table, dt_expr, sym_col_sql, list(symbols), int(tail_rows))
    elif sym_col_sql and _window_sql_enabled() and _sqlite_has_window():
        # 1 クエリで銘柄あたり tail_rows 本 (index 直撃)
        df = _read_tail_window(summary_db, table, dt_expr, sym_col_sql, list(symbols) if symbols else None, int(tail_rows))
        if df.empty and symbols:
            logger.warning(
                "[PUSH INCR MA75] window read empty -> 銘柄別読みへフォールバック db=%s table=%s",
                Path(summary_db).name, table,
            )
            df = _read_tail_per_symbol(summary_db, table, dt_expr, sym_col_sql, list(symbols), int(tail_rows))
    elif symbols and sym_col_sql:
        # 銘柄が絞れているときは index 直撃で必要本数だけ読む
        df = _read_tail_per_symbol(summary_db, table, dt_expr, sym_col_sql, list(symbols), int(tail_rows))
    else:
        if symbols and not sym_col_sql:
            logger.warning(
                "[PUSH INCR MA75] symbol column not found -> full scan fallback table=%s db=%s",
                table, summary_db,
            )
        # DB単位では多めに読む。複数日concat後に銘柄別tailへ削る。
        limit = max(int(tail_rows) * 1600, int(tail_rows))
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
    df["_source_db"] = str(summary_db)

    out = df.reset_index(drop=True)
    if cache_key:
        try:
            merged = out
            if cached_part is not None and not cached_part.empty:
                merged = pd.concat([cached_part, out], ignore_index=True, sort=False)
                if "symbol" in merged.columns and "datetime" in merged.columns:
                    merged = merged.drop_duplicates(subset=["symbol", "datetime"], keep="last")
                merged = merged.reset_index(drop=True)
            _tail_cache_save(cache_key, merged)
            if want is not None and "symbol" in merged.columns:
                out = merged[merged["symbol"].astype(str).isin(set(want))].reset_index(drop=True)
            else:
                out = merged
        except Exception:
            logger.exception("[PUSH INCR MA75] tail cache merge failed db=%s", summary_db)
    return out


def _load_summary_tail_from_dbs(
    summary_dbs: list[str],
    interval: int,
    *,
    tail_rows: int,
    fallback_trade_day: dt.date,
    symbols: Optional[list[str]] = None,
) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []

    def _one(db: str) -> tuple[str, pd.DataFrame, float]:
        ymd = _extract_ymd_from_path(db, "summary")
        trade_day = _ymd_to_date(ymd, fallback_trade_day)
        t_db = time.perf_counter()
        x = _load_summary_tail(
            db, interval, tail_rows=tail_rows, trade_day=trade_day, symbols=symbols,
        )
        return db, x, time.perf_counter() - t_db

    # SMB はレイテンシ律速なので DB ごとに並列で読む (別ファイル / read-only)
    results: list[tuple[str, pd.DataFrame, float]] = []
    if _parallel_dbs_enabled() and len(summary_dbs) > 1:
        try:
            with ThreadPoolExecutor(max_workers=min(3, len(summary_dbs)),
                                    thread_name_prefix="ma75-tail") as ex:
                results = list(ex.map(_one, summary_dbs))
        except Exception:
            logger.exception("[PUSH INCR MA75] parallel tail read failed -> 逐次で読み直す")
            results = []
    if not results:
        results = [_one(db) for db in summary_dbs]

    for db, x, el in results:
        logger.info(
            "[PUSH INCR MA75] tail read interval=%s db=%s rows=%d elapsed=%.3fs",
            interval, Path(db).name, len(x), el,
        )
        if isinstance(x, pd.DataFrame) and not x.empty:
            frames.append(x)

    if not frames:
        return pd.DataFrame()

    # 一部のフレームだけ all-NA になる列があると
    #   FutureWarning: The behavior of DataFrame concatenation with empty or all-NA entries
    # になる。object dtype の all-NA 列を float64 へ明示してから連結する。
    # (int64/bool は NaN を入れられないので触らない。値・列集合は不変)
    _aligned = []
    for _f in frames:
        try:
            _cast = {
                c: "float64"
                for c in _f.columns
                if _f[c].dtype == object and _f[c].isna().all()
            }
            _aligned.append(_f.astype(_cast) if _cast else _f)
        except Exception:
            _aligned.append(_f)

    df = pd.concat(_aligned, ignore_index=True, sort=False)
    df["symbol"] = _normalize_symbol(df["symbol"])
    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
    df = df[df["symbol"].ne("") & df["datetime"].notna()].copy()
    df = df.sort_values(["symbol", "datetime"])
    df = df.drop_duplicates(["symbol", "datetime"], keep="last")
    df = df.groupby("symbol", group_keys=False).tail(int(tail_rows))
    return df.reset_index(drop=True)


def _tail_coverage_satisfied(frames: list[pd.DataFrame], tail_rows: int) -> bool:
    """読み込み済みフレームで、出現した全銘柄が tail_rows 本以上そろったか。"""
    try:
        counts: dict[str, int] = {}
        for f in frames:
            if f is None or f.empty or "symbol" not in f.columns:
                continue
            for sym, n in f["symbol"].value_counts().items():
                counts[str(sym)] = counts.get(str(sym), 0) + int(n)
        if not counts:
            return False
        return min(counts.values()) >= int(tail_rows)
    except Exception:
        logger.exception("[PUSH INCR MA75] tail coverage check failed")
        return False


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
        with committing_connect(push_db, timeout=5) as conn:
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


def _fill_ma75_from_db_and_minimal_tail(calc_df: pd.DataFrame, source_df: pd.DataFrame, interval: int) -> pd.DataFrame:
    """
    起動時MA75用の最小履歴補正。

    前日までの summary DB には ma75 が格納済みという前提で、
    indicator_pipeline の再計算で 75本未満の既存行が NaN になった場合は
    DB格納値を復元する。

    新規PUSH足は、読み込んだ最小75本tailと結合した通常rollingで計算する。
    直近値のcarry/ffillは行わず、DB格納済みの同一足ma75か厳密75本rollingのみを採用する。
    """
    if calc_df is None or calc_df.empty:
        return pd.DataFrame()

    out = calc_df.copy()
    if "ma75" not in out.columns:
        out["ma75"] = np.nan

    try:
        out["symbol"] = _normalize_symbol(out["symbol"])
        out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
    except Exception:
        logger.debug("[PUSH INCR MA75] ma75 normalize calc failed interval=%s", interval, exc_info=True)

    try:
        if isinstance(source_df, pd.DataFrame) and not source_df.empty and "ma75" in source_df.columns:
            src = source_df[["symbol", "datetime", "ma75"]].copy()
            src["symbol"] = _normalize_symbol(src["symbol"])
            src["datetime"] = pd.to_datetime(src["datetime"], errors="coerce")
            src["__db_ma75"] = pd.to_numeric(src["ma75"], errors="coerce")
            src = (
                src.dropna(subset=["symbol", "datetime"])
                   .sort_values(["symbol", "datetime"])
                   .drop_duplicates(["symbol", "datetime"], keep="last")
                   [["symbol", "datetime", "__db_ma75"]]
            )

            before = int(pd.to_numeric(out["ma75"], errors="coerce").notna().sum())
            out = out.merge(src, on=["symbol", "datetime"], how="left")
            cur = pd.to_numeric(out["ma75"], errors="coerce")
            dbv = pd.to_numeric(out["__db_ma75"], errors="coerce")
            out["ma75"] = cur.combine_first(dbv)
            out = out.drop(columns=["__db_ma75"], errors="ignore")
            after = int(pd.to_numeric(out["ma75"], errors="coerce").notna().sum())
            if after > before:
                logger.info(
                    "[PUSH INCR MA75] restored stored ma75 interval=%s nonnull=%d->%d",
                    interval,
                    before,
                    after,
                )
    except Exception:
        logger.debug("[PUSH INCR MA75] stored ma75 restore failed interval=%s", interval, exc_info=True)

    try:
        out = out.sort_values(["symbol", "datetime"]).reset_index(drop=True)
        close_col = "close" if "close" in out.columns else "close_price" if "close_price" in out.columns else None
        if close_col is not None:
            def _repair_one(g: pd.DataFrame) -> pd.DataFrame:
                g = g.copy()
                close = pd.to_numeric(g[close_col], errors="coerce")
                strict = close.rolling(MIN_MA75_BARS, min_periods=MIN_MA75_BARS).mean()
                ma = pd.to_numeric(g["ma75"], errors="coerce").combine_first(strict)
                # ffill/carry は使わない。前日までの値は同一足のDB格納ma75として復元し、
                # 新規足は最小75本tailから厳密rollingで成立した場合だけ採用する。
                g["ma75"] = ma
                return g

            before = int(pd.to_numeric(out["ma75"], errors="coerce").notna().sum())
            out = out.groupby("symbol", group_keys=False).apply(_repair_one).reset_index(drop=True)
            after = int(pd.to_numeric(out["ma75"], errors="coerce").notna().sum())
            if after > before:
                logger.info(
                    "[PUSH INCR MA75] minimal-tail ma75 repaired interval=%s nonnull=%d->%d",
                    interval,
                    before,
                    after,
                )
    except Exception:
        logger.debug("[PUSH INCR MA75] minimal-tail ma75 repair failed interval=%s", interval, exc_info=True)

    return out


def _apply_indicators(df: pd.DataFrame, interval: int) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    t0 = time.perf_counter()
    try:
        from trading.summary.pipeline.indicator_pipeline import run_indicator_pipeline
        out = run_indicator_pipeline(df, interval=int(interval), run_downstream_scoring=True)
        if isinstance(out, pd.DataFrame):
            filled = _fill_ma75_from_db_and_minimal_tail(out, df, int(interval))
            logger.info(
                "[PUSH INCR MA75] indicators done interval=%s in_rows=%d out_rows=%d symbols=%d elapsed=%.3fs",
                interval, len(df), len(filled), _safe_nunique(filled, "symbol"),
                time.perf_counter() - t0,
            )
            return filled
    except Exception:
        logger.exception("[PUSH INCR MA75] indicator pipeline failed interval=%s", interval)

    out = df.copy()
    out = out.sort_values(["symbol", "datetime"])
    out["ma75"] = out.groupby("symbol")["close"].transform(
        lambda x: pd.to_numeric(x, errors="coerce").rolling(MIN_MA75_BARS, min_periods=MIN_MA75_BARS).mean()
    )
    return _fill_ma75_from_db_and_minimal_tail(out, df, int(interval))


def _latest_rows_per_symbol(df: pd.DataFrame) -> pd.DataFrame:
    try:
        if df is None or df.empty or "symbol" not in df.columns:
            return df if isinstance(df, pd.DataFrame) else pd.DataFrame()
        if "datetime" not in df.columns:
            return df.drop_duplicates(subset=["symbol"], keep="last").reset_index(drop=True)
        return (
            df.sort_values(["symbol", "datetime"], kind="stable")
            .groupby("symbol", as_index=False)
            .tail(1)
            .reset_index(drop=True)
        )
    except Exception:
        logger.exception("[PUSH INCR MA75] latest-per-symbol failed")
        return df


def _set_global_cache(df: pd.DataFrame, interval: int) -> None:
    """起動時に組んだ「銘柄あたり 75 本」を main.py 側キャッシュへ渡す。

    履歴は history cache へ、最新行だけを latest / merged へ入れる。
    merged に履歴をそのまま入れると「銘柄あたり 1 行」を前提にしている
    消費側が壊れるので分ける。
    履歴を history cache に入れることで merge_history_sources の
    hist_len が 1 -> 75 になり、RSI/MACD/ATR/MA25/MA75/slope が
    fallback ではなく実計算されるようになる。
    """
    if df is None or df.empty:
        return

    latest = _latest_rows_per_symbol(df)
    hist_len_max = 0
    try:
        if "symbol" in df.columns and "datetime" in df.columns:
            hist_len_max = int(df.groupby("symbol")["datetime"].nunique().max())
    except Exception:
        logger.exception("[PUSH INCR MA75] hist_len probe failed interval=%s", interval)

    try:
        from trading.summary.controller_cache import (
            safe_global_get_merged_summary,
            safe_global_set_history,
            safe_global_set_latest,
            safe_global_set_merged_summary,
            should_overwrite_merged_summary,
        )
    except Exception:
        logger.exception("[PUSH INCR MA75] controller_cache import failed interval=%s", interval)
        try:
            from global_state import global_data
            setattr(global_data, f"summary_{int(interval)}min_df", df)
            setattr(global_data, f"merged_summary_{int(interval)}min", df)
        except Exception:
            logger.exception("[PUSH INCR MA75] global cache set failed interval=%s", interval)
        return

    # 1) 履歴 -> history cache (指標計算に必要な過去本数はここから流れる)
    try:
        safe_global_set_history(int(interval), df)
    except Exception:
        logger.exception("[PUSH INCR MA75] set history failed interval=%s", interval)

    # 2) 最新行 -> latest cache
    #    ただし MA75 の "最新" は summary DB の最終書き込み時刻なので、
    #    ライブの merged より古いことがある。巻き戻さない。
    allow_backward = (os.environ.get("PUSH_INCREMENTAL_MA75_ALLOW_BACKWARD_PUBLISH", "0")
                      .strip().lower() in {"1", "true", "yes", "on"})
    ma75_latest_dt = None
    try:
        if "datetime" in latest.columns:
            ma75_latest_dt = pd.to_datetime(latest["datetime"], errors="coerce").max()
    except Exception:
        logger.exception("[PUSH INCR MA75] latest_dt probe failed interval=%s", interval)
    try:
        safe_global_set_latest(int(interval), latest)
    except Exception:
        logger.exception("[PUSH INCR MA75] set latest failed interval=%s", interval)

    # 3) 最新行 -> merged (既存のほうが新鮮/豊かなら上書きしない)
    overwrote = False
    try:
        from trading.summary.controller_utils import normalize_summary_df as _norm
    except Exception:
        _norm = None
    try:
        existing = (
            safe_global_get_merged_summary(int(interval), _norm)
            if _norm is not None
            else pd.DataFrame()
        )
        existing_latest_dt = None
        if isinstance(existing, pd.DataFrame) and not existing.empty and "datetime" in existing.columns:
            existing_latest_dt = pd.to_datetime(existing["datetime"], errors="coerce").max()

        backward = bool(
            existing_latest_dt is not None
            and ma75_latest_dt is not None
            and pd.notna(existing_latest_dt) and pd.notna(ma75_latest_dt)
            and ma75_latest_dt < existing_latest_dt
        )
        if backward and not allow_backward:
            # 巻き戻し禁止。黙って捨てず必ず残す。
            logger.warning(
                "[PUSH INCR MA75] merged publish skipped interval=%s reason=backward "
                "ma75_latest=%s < merged_latest=%s "
                "(summary DB の tail はライブより古いので merged を巻き戻さない)",
                interval, ma75_latest_dt, existing_latest_dt,
            )
        elif existing is None or not isinstance(existing, pd.DataFrame) or existing.empty \
                or should_overwrite_merged_summary(existing, latest):
            safe_global_set_merged_summary(int(interval), latest)
            overwrote = True
    except Exception:
        logger.exception("[PUSH INCR MA75] merged cache set failed interval=%s", interval)

    logger.warning(
        "[PUSH INCR MA75] cache published interval=%s hist_rows=%d hist_len_max=%d "
        "latest_rows=%d symbols=%d merged_overwritten=%s latest_dt=%s",
        interval, len(df), hist_len_max, len(latest),
        _safe_nunique(latest, "symbol"), overwrote, _latest_str(latest),
    )


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


def _safe_nunique(df: pd.DataFrame, col: str) -> int:
    try:
        if df is None or df.empty or col not in df.columns:
            return 0
        return int(df[col].nunique())
    except Exception:
        return 0


def _registered_scope_symbols() -> set[str]:
    """PUSH に出ていなくても実際に監視・登録している銘柄は落とさない。"""
    out: set[str] = set()
    try:
        from global_state import global_data
    except Exception:
        logger.debug("[PUSH INCR MA75] global_data unavailable for scope", exc_info=True)
        return out
    for attr in (
        "ats_registered_symbols",
        "ats_current_active_symbols",
        "ats_last_good_batch",
    ):
        try:
            vals = getattr(global_data, attr, None) or []
            for v in vals:
                s = str(v).strip().upper().replace(".0", "")
                if s and s.lower() not in {"nan", "none", "null", "<na>"}:
                    out.add(s)
        except Exception:
            logger.debug("[PUSH INCR MA75] scope attr read failed attr=%s", attr, exc_info=True)
    return out


def _resolve_scope_symbols(ticks: pd.DataFrame) -> Optional[list[str]]:
    """MA75 を作る対象銘柄を決める。None は全銘柄 (従来動作)。"""
    if SYMBOL_SCOPE == "all":
        logger.warning("[PUSH INCR MA75] symbol scope=all (全銘柄。重い)")
        return None

    scope: set[str] = set()
    try:
        if isinstance(ticks, pd.DataFrame) and not ticks.empty and "symbol" in ticks.columns:
            for v in _normalize_symbol(ticks["symbol"]).unique().tolist():
                s = str(v).strip()
                if s:
                    scope.add(s)
    except Exception:
        logger.exception("[PUSH INCR MA75] push symbol scope build failed")

    n_push = len(scope)
    reg = _registered_scope_symbols()
    scope |= reg

    if not scope:
        # 空スコープで走ると何も作れないので、握り潰さず全銘柄へ戻す。
        logger.warning(
            "[PUSH INCR MA75] symbol scope empty (push=%d registered=%d) -> 全銘柄で実行する",
            n_push, len(reg),
        )
        return None

    out = sorted(scope)
    logger.warning(
        "[PUSH INCR MA75] symbol scope=%s symbols=%d (push=%d registered=%d) "
        "reason=main.py が売買する銘柄だけに限定 (env PUSH_INCREMENTAL_MA75_SYMBOL_SCOPE=all で従来動作)",
        SYMBOL_SCOPE, len(out), n_push, len(reg),
    )
    return out


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
        if summary_db_path:
            summary_dbs = [summary_db_path]
        else:
            summary_dbs = _resolve_summary_dbs(
                DEFAULT_SUMMARY_DIR,
                trade_date=trade_day,
                lookback_days=SUMMARY_LOOKBACK_DAYS,
            )
        summary_db = summary_dbs[0] if summary_dbs else None
        push_db = push_db_path or _resolve_db(DEFAULT_PUSH_DIR, "push", trade_day)

        result.summary_db = summary_db
        result.summary_dbs = list(summary_dbs)
        result.push_db = push_db

        logger.info(
            "[PUSH INCR MA75] start summary_db=%s summary_dbs=%s summary_lookback_days=%d "
            "push_db=%s tail=%d push_limit=%d lookback=%d intervals=%s",
            summary_db,
            summary_dbs,
            SUMMARY_LOOKBACK_DAYS,
            push_db,
            TAIL_ROWS_PER_SYMBOL,
            PUSH_MAX_ROWS,
            PUSH_LOOKBACK_MINUTES,
            intervals,
        )

        if not summary_dbs:
            result.message = "summary dbs not found"
            logger.warning("[PUSH INCR MA75] %s", result.message)
            return result
        if not push_db or not Path(push_db).exists():
            result.message = f"push db not found: {push_db}"
            logger.warning("[PUSH INCR MA75] %s", result.message)
            return result

        # --- PUSH を先に読む (銘柄スコープの決定に使う) ---
        t_push = time.perf_counter()
        raw_push = _load_recent_push_rows(push_db)
        ticks = _normalize_push_ticks(raw_push, trade_day)
        result.push_rows = int(len(ticks))
        logger.info(
            "[PUSH INCR MA75] push loaded raw_rows=%d tick_rows=%d latest=%s elapsed=%.3fs",
            len(raw_push),
            len(ticks),
            _latest_str(ticks),
            time.perf_counter() - t_push,
        )

        scope_symbols = _resolve_scope_symbols(ticks)

        existing: dict[int, pd.DataFrame] = {}
        latest_maps: dict[int, dict[str, pd.Timestamp]] = {}

        for iv in intervals:
            t_iv = time.perf_counter()
            x = _load_summary_tail_from_dbs(
                summary_dbs,
                int(iv),
                tail_rows=TAIL_ROWS_PER_SYMBOL,
                fallback_trade_day=trade_day,
                symbols=scope_symbols,
            )
            existing[int(iv)] = x
            latest_maps[int(iv)] = _latest_map(x)
            result.loaded_summary_rows[int(iv)] = int(len(x))
            result.existing_latest[int(iv)] = _latest_str(x)
            logger.info(
                "[PUSH INCR MA75] existing interval=%s rows=%d symbols=%d latest=%s dbs=%d "
                "tail_per_symbol=%d scope=%s scope_symbols=%s elapsed=%.3fs",
                iv,
                len(x),
                _safe_nunique(x, "symbol"),
                _latest_str(x),
                len(summary_dbs),
                TAIL_ROWS_PER_SYMBOL,
                SYMBOL_SCOPE,
                len(scope_symbols) if scope_symbols else "all",
                time.perf_counter() - t_iv,
            )

        if ticks.empty:
            # PUSH差分がなくても、複数日tailからMA75を作ってcacheへ反映する。
            for iv in intervals:
                cache = _apply_indicators(existing.get(int(iv), pd.DataFrame()), int(iv))
                result.cache_rows[int(iv)] = int(len(cache))
                result.ma75_nonnull[int(iv)] = _ma75_nonnull(cache)
                result.latest[int(iv)] = _latest_str(cache)
                if update_global_cache and not cache.empty:
                    _set_global_cache(cache, int(iv))
            result.ok = True
            result.message = "no push ticks to append; multiday summary tail cached"
            logger.info(
                "[PUSH INCR MA75] done ok=True no_push cache_rows=%s ma75_nonnull=%s latest=%s elapsed=%.3fs",
                result.cache_rows,
                result.ma75_nonnull,
                result.latest,
                (dt.datetime.now() - started).total_seconds(),
            )
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

        # 3分/5分は 1分足 tail + 新規PUSHから生成した新規足も作る。
        base_for_resample = _combine_tail_and_new(existing.get(1, pd.DataFrame()), one_new, 1)
        for iv in (3, 5):
            if iv not in intervals:
                continue
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
            "[PUSH INCR MA75] done ok=True summary_dbs=%s new_rows=%s cache_rows=%s ma75_nonnull=%s latest=%s elapsed=%.3fs",
            summary_dbs,
            result.new_rows,
            result.cache_rows,
            result.ma75_nonnull,
            result.latest,
            (dt.datetime.now() - started).total_seconds(),
        )

        result.ok = True
        result.message = "push incremental MA75 cache built from multiday summary tail"
        return result

    except Exception as e:
        logger.exception("[PUSH INCR MA75] failed")
        result.ok = False
        result.message = str(e)
        return result


__all__ = ["PushIncrementalMA75Result", "build_push_incremental_ma75_on_startup"]
