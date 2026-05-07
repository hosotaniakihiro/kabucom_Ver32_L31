from __future__ import annotations

import datetime as dt
import glob
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd

from trading.yahoo.download.download_runner import resolve_target_symbols
from .logging_utils import log_step

logger = logging.getLogger(__name__)

try:
    from trading.yahoo.symbol.yahoo_symbol_provider import get_yahoo_target_symbols
    _HAS_YAHOO_SYMBOL_PROVIDER = True
except Exception:
    _HAS_YAHOO_SYMBOL_PROVIDER = False

    def get_yahoo_target_symbols(*args, **kwargs):
        return []

try:
    from trading.ranking.runtime_symbols import normalize_symbols
except Exception:
    def normalize_symbols(symbols: Iterable[object]) -> set[str]:
        out = set()
        for s in symbols or []:
            if s is None:
                continue
            ss = str(s).strip()
            if ss.endswith(".0"):
                ss = ss[:-2]
            if ss:
                out.add(ss)
        return out


DEFAULT_NAS_ROOT = r"\\192.168.0.22\AutoStockBuyAndSell"
DEFAULT_RANKING_DIR = r"\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\ranking"


def _env_bool(name: str, default: bool = False) -> bool:
    try:
        v = os.environ.get(name)
        if v is None:
            return bool(default)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on"}:
            return True
        if s in {"0", "false", "no", "n", "off", ""}:
            return False
        return bool(default)
    except Exception:
        return bool(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


def sanitize_symbols(symbols: Iterable[object]) -> list[str]:
    out, seen = [], set()
    for s in symbols or []:
        if s is None:
            continue
        sym = str(s).strip()
        if sym.endswith(".0"):
            sym = sym[:-2]
        if sym and sym not in seen:
            seen.add(sym)
            out.append(sym)
    return out


def extract_success_symbols_from_df(df: pd.DataFrame) -> set[str]:
    if df is None or df.empty or "symbol" not in df.columns:
        return set()
    try:
        return normalize_symbols(df["symbol"].tolist())
    except Exception:
        return {str(s).strip() for s in df["symbol"].astype(str).tolist() if str(s).strip()}


def build_rows_by_symbol(df: pd.DataFrame) -> dict[str, int]:
    if df is None or df.empty or "symbol" not in df.columns:
        return {}
    try:
        return {
            str(k).strip(): int(v)
            for k, v in df.groupby("symbol", dropna=True).size().items()
            if str(k).strip()
        }
    except Exception:
        logger.exception("[YAHOO COMPLEMENT] build rows_by_symbol failed")
        return {}


def build_last_bar_by_symbol(df: pd.DataFrame) -> dict[str, object]:
    if df is None or df.empty or "symbol" not in df.columns or "datetime" not in df.columns:
        return {}
    try:
        tmp = df.copy()
        tmp["datetime"] = pd.to_datetime(tmp["datetime"], errors="coerce")
        tmp = tmp.dropna(subset=["symbol", "datetime"])
        return {
            str(sym).strip(): g["datetime"].max()
            for sym, g in tmp.groupby("symbol", sort=False)
            if str(sym).strip() and not g.empty
        }
    except Exception:
        logger.exception("[YAHOO COMPLEMENT] build last_bar_by_symbol failed")
        return {}


def _resolve_ranking_db_path(target_date: dt.date) -> Optional[str]:
    ymd = target_date.strftime("%Y%m%d")

    candidates = []
    for env_name in (
        "YAHOO_RANKING_DB_PATH",
        "RANKING_DB_PATH",
        "KABU_RANKING_DB_PATH",
    ):
        v = os.environ.get(env_name)
        if v:
            candidates.append(v)

    ranking_dir = os.environ.get("YAHOO_RANKING_DB_DIR") or os.environ.get("RANKING_DB_DIR") or DEFAULT_RANKING_DIR
    candidates.append(str(Path(ranking_dir) / f"ranking{ymd}.db"))
    candidates.extend(glob.glob(str(Path(ranking_dir) / f"*{ymd}*.db")))

    for p in candidates:
        try:
            if p and os.path.exists(p):
                return p
        except Exception:
            continue

    return None


def _read_table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    try:
        cur = conn.execute(f"PRAGMA table_info({table})")
        return [str(r[1]) for r in cur.fetchall()]
    except Exception:
        return []


def _pick_time_col(columns: list[str]) -> Optional[str]:
    for c in ("datetime", "snapshot_time", "received_at", "inserted_at"):
        if c in columns:
            return c
    return None


def _read_current_ranking_snapshot_symbols(
    *,
    target_date: dt.date,
    lookback_minutes: Optional[int] = None,
    max_symbols: Optional[int] = None,
) -> list[str]:
    """
    ranking_snapshot_1min の「最新時刻」に入っている銘柄だけを返す。

    以前の実装は include_today_all_rankings=True により、
    その日にランキングへ一度でも入った銘柄を全てYahoo補完対象にしていた。
    場中はこれが1550銘柄以上になり重すぎるため、現在ランキングだけに絞る。
    """
    db_path = _resolve_ranking_db_path(target_date)
    if not db_path:
        logger.warning("[YAHOO CURRENT RANKING TARGET] ranking db not found target_date=%s", target_date)
        return []

    table = "ranking_snapshot_1min"
    lookback = _env_int("YAHOO_CURRENT_RANKING_LOOKBACK_MINUTES", 2) if lookback_minutes is None else int(lookback_minutes)
    lookback = max(0, lookback)

    try:
        with sqlite3.connect(db_path, timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA busy_timeout=30000")
            except Exception:
                pass

            columns = _read_table_columns(conn, table)
            if not columns or "symbol" not in columns:
                logger.warning(
                    "[YAHOO CURRENT RANKING TARGET] invalid schema db=%s table=%s columns=%s",
                    db_path,
                    table,
                    columns,
                )
                return []

            time_col = _pick_time_col(columns)
            if not time_col:
                logger.warning(
                    "[YAHOO CURRENT RANKING TARGET] time column missing db=%s table=%s columns=%s",
                    db_path,
                    table,
                    columns,
                )
                return []

            ymd_dash = target_date.strftime("%Y-%m-%d")
            latest_sql = f"""
                SELECT MAX({time_col}) AS latest_dt
                  FROM {table}
                 WHERE {time_col} IS NOT NULL
                   AND substr(CAST({time_col} AS TEXT), 1, 10) = ?
            """
            latest = conn.execute(latest_sql, (ymd_dash,)).fetchone()
            latest_dt_raw = latest["latest_dt"] if latest else None
            if latest_dt_raw is None:
                logger.warning(
                    "[YAHOO CURRENT RANKING TARGET] no latest snapshot db=%s table=%s date=%s time_col=%s",
                    db_path,
                    table,
                    target_date,
                    time_col,
                )
                return []

            latest_ts = pd.to_datetime(latest_dt_raw, errors="coerce")
            if pd.isna(latest_ts):
                latest_text = str(latest_dt_raw)
                where_time = f"CAST({time_col} AS TEXT) = ?"
                params: tuple[object, ...] = (latest_text,)
                threshold_text = latest_text
            elif lookback <= 0:
                latest_text = latest_ts.strftime("%Y-%m-%d %H:%M:%S")
                where_time = f"CAST({time_col} AS TEXT) = ?"
                params = (latest_text,)
                threshold_text = latest_text
            else:
                threshold_ts = latest_ts.to_pydatetime() - dt.timedelta(minutes=lookback)
                threshold_text = threshold_ts.strftime("%Y-%m-%d %H:%M:%S")
                latest_text = latest_ts.strftime("%Y-%m-%d %H:%M:%S")
                where_time = f"CAST({time_col} AS TEXT) >= ? AND CAST({time_col} AS TEXT) <= ?"
                params = (threshold_text, latest_text)

            # rankなどがあれば上位順をできるだけ維持する。
            order_cols = []
            for c in ("rank", "rank_position", "No", "no"):
                if c in columns:
                    order_cols.append(c)
                    break
            order_sql = f"ORDER BY {order_cols[0]} ASC" if order_cols else "ORDER BY symbol ASC"

            sql = f"""
                SELECT DISTINCT CAST(symbol AS TEXT) AS symbol
                  FROM {table}
                 WHERE symbol IS NOT NULL
                   AND TRIM(CAST(symbol AS TEXT)) <> ''
                   AND {where_time}
                 {order_sql}
            """
            rows = conn.execute(sql, params).fetchall()
            symbols = sanitize_symbols([r["symbol"] for r in rows])

            if max_symbols is None:
                max_symbols_env = _env_int("YAHOO_CURRENT_RANKING_MAX_SYMBOLS", 0)
                max_symbols = max_symbols_env if max_symbols_env > 0 else None
            if max_symbols is not None and int(max_symbols) > 0:
                symbols = symbols[: int(max_symbols)]

            logger.info(
                "[YAHOO CURRENT RANKING TARGET] symbols=%d target_date=%s db=%s table=%s time_col=%s latest=%s threshold=%s lookback_min=%s sample=%s",
                len(symbols),
                target_date,
                db_path,
                table,
                time_col,
                latest_dt_raw,
                threshold_text,
                lookback,
                symbols[:30],
            )
            return symbols

    except Exception:
        logger.exception(
            "[YAHOO CURRENT RANKING TARGET] read failed target_date=%s db=%s",
            target_date,
            db_path,
        )
        return []


def resolve_cached_target_symbols(*, target_date: dt.date) -> list[str]:
    ts = time.time()
    try:
        symbols = sanitize_symbols(resolve_target_symbols(target_date=target_date, use_ranking_cache=True))
        logger.info("[YAHOO COMPLEMENT] resolved cached targets=%d target_date=%s sample=%s", len(symbols), target_date, symbols[:20])
        log_step("resolve_cached_target_symbols_done", ts, count=len(symbols))
        return symbols
    except TypeError:
        symbols = sanitize_symbols(resolve_target_symbols(target_date=target_date))
        logger.warning("[YAHOO COMPLEMENT] fallback legacy resolve target_date=%s symbols=%d sample=%s", target_date, len(symbols), symbols[:20])
        return symbols
    except Exception:
        logger.exception("[YAHOO COMPLEMENT] resolve cached targets failed")
        return []


def resolve_all_ranking_symbols_for_reflect(*, target_date: dt.date) -> list[str]:
    """
    Yahoo反映/ダウンロード対象を解決する。

    既定は「現在ランキングに入っている銘柄のみ」。
    旧仕様へ戻したい場合だけ:
      YAHOO_USE_ALL_DAY_RANKING_SYMBOLS=1
    """
    ts = time.time()

    use_all_day = _env_bool("YAHOO_USE_ALL_DAY_RANKING_SYMBOLS", False)

    if not use_all_day:
        symbols = _read_current_ranking_snapshot_symbols(target_date=target_date)
        if symbols:
            logger.info(
                "[YAHOO REFLECT TARGET] current ranking symbols=%d target_date=%s source=ranking_snapshot_latest all_day=False sample=%s",
                len(symbols),
                target_date,
                symbols[:20],
            )
            log_step("resolve_reflect_current_ranking_symbols_done", ts, count=len(symbols))
            return symbols

        logger.warning(
            "[YAHOO REFLECT TARGET] current ranking snapshot empty -> fallback all-day target_date=%s",
            target_date,
        )

    symbols: list[str] = []
    if _HAS_YAHOO_SYMBOL_PROVIDER:
        try:
            symbols = sanitize_symbols(
                get_yahoo_target_symbols(
                    max_symbols=None,
                    include_today_all_rankings=True,
                    target_date=target_date,
                    include_active=False,
                    include_light=False,
                    include_universe=False,
                )
            )
        except Exception:
            logger.exception("[YAHOO REFLECT TARGET] provider failed")
            symbols = []

    if not symbols:
        try:
            symbols = sanitize_symbols(resolve_target_symbols(target_date=target_date, use_ranking_cache=False))
        except Exception:
            logger.exception("[YAHOO REFLECT TARGET] fallback resolve_target_symbols failed")
            symbols = []

    logger.info(
        "[YAHOO REFLECT TARGET] all ranking symbols=%d target_date=%s source=ranking_raw_all_day all_day=%s sample=%s",
        len(symbols),
        target_date,
        use_all_day,
        symbols[:20],
    )
    log_step("resolve_reflect_all_ranking_symbols_done", ts, count=len(symbols))
    return symbols


def resolve_download_symbols_from_reflect_symbols(*, target_date: dt.date, reflect_symbols: Iterable[object]) -> list[str]:
    symbols = sanitize_symbols(reflect_symbols)
    logger.info(
        "[YAHOO DOWNLOAD TARGET] symbols=%d target_date=%s derived_from=current_ranking_snapshot sample=%s",
        len(symbols),
        target_date,
        symbols[:20],
    )
    return symbols


__all__ = [
    "sanitize_symbols",
    "extract_success_symbols_from_df",
    "build_rows_by_symbol",
    "build_last_bar_by_symbol",
    "resolve_cached_target_symbols",
    "resolve_all_ranking_symbols_for_reflect",
    "resolve_download_symbols_from_reflect_symbols",
    "normalize_symbols",
]
