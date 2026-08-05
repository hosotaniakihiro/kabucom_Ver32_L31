# ============================================================
# File   : trading/ranking/ranking_entry_high_low.py
# ------------------------------------------------------------
# ランキング由来エントリー行の high/low を ranking_snapshot から補完する。
#
# 旧 core/startup/ranking_entry_high_low_from_snapshot_patch.py が
# trading/ranking/entry_from_ranking.build_entry_row (AI.entry_row_builder からの
# import エイリアス) を差し替えて行っていた処理を本文側へ移設した。
#
# 補完対象は「close > 0 なのに high/low が欠損 or high < low」の行だけ。
# 既に値が入っているカラムは上書きしない。
#
# 旧パッチは sitecustomize の BACKGROUND_MAIN_PATCHES 経由 = main.py 専用
# だったため、high_low_fill_enabled() で同じスコープを保つ。
# ============================================================
from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

_INSTALLED = False
_ORIGINAL_BUILD_ENTRY_ROW = None
_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def _env_bool(name: str, default: bool) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, str(default))))
    except Exception:
        return int(default)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return float(default)


ENABLED = _env_bool("RANKING_ENTRY_HIGH_LOW_SNAPSHOT_PATCH_ENABLED", True)
LOOKBACK_ROWS = _env_int("RANKING_ENTRY_HIGH_LOW_SNAPSHOT_LOOKBACK_ROWS", 12)
MAX_AGE_MIN = _env_float("RANKING_ENTRY_HIGH_LOW_SNAPSHOT_MAX_AGE_MIN", 30.0)
CACHE_TTL_SEC = _env_float("RANKING_ENTRY_HIGH_LOW_SNAPSHOT_CACHE_TTL_SEC", 3.0)

_PRICE_COL_CANDIDATES = ("price", "current_price", "CurrentPrice", "close", "close_price", "last_price")
_TIME_COL_CANDIDATES = ("datetime", "snapshot_time", "received_at", "created_at", "inserted_at", "updated_at", "time")
_SYMBOL_COL_CANDIDATES = ("symbol", "Symbol", "code", "銘柄コード", "stock_code")


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        s = str(v).strip().replace(",", "").replace("%", "")
        if not s or s.lower() in {"nan", "none", "nat", "<na>"}:
            return default
        return float(s)
    except Exception:
        return default


def _normalize_symbol(symbol: Any) -> str:
    try:
        s = str(symbol or "").strip()
        if s.endswith(".0") and s[:-2].isdigit():
            s = s[:-2]
        return s
    except Exception:
        return ""


def _quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _resolve_db_path() -> str | None:
    try:
        env_path = os.getenv("RANKING_DB_PATH") or os.getenv("KABU_RANKING_DB_PATH") or os.getenv("KABUCOM_RANKING_DB_PATH")
        if env_path and str(env_path).strip():
            return str(env_path).strip()
        from database.paths.ranking_paths import resolve_ranking_db_path
        return str(resolve_ranking_db_path())
    except Exception:
        logger.debug("[RANKING ENTRY HL PATCH] resolve db path failed", exc_info=True)
        return None


def _snapshot_table() -> str:
    try:
        from database.schema.ranking_snapshot_schema import SNAPSHOT_TABLE
        return str(SNAPSHOT_TABLE)
    except Exception:
        return "ranking_snapshot_1min"


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    try:
        return {str(r[1]) for r in conn.execute(f"PRAGMA table_info({_quote_ident(table_name)})").fetchall()}
    except Exception:
        return set()


def _first_existing(cols: set[str], candidates: tuple[str, ...]) -> str | None:
    for c in candidates:
        if c in cols:
            return c
    lower = {c.lower(): c for c in cols}
    for c in candidates:
        if c.lower() in lower:
            return lower[c.lower()]
    return None


def _calc_from_snapshot(symbol: str) -> dict[str, Any]:
    sym = _normalize_symbol(symbol)
    if not sym:
        return {}
    now_ts = dt.datetime.now().timestamp()
    cached = _CACHE.get(sym)
    if cached and (now_ts - cached[0]) <= CACHE_TTL_SEC:
        return dict(cached[1])

    db_path = _resolve_db_path()
    table = _snapshot_table()
    if not db_path:
        return {}
    try:
        with sqlite3.connect(str(db_path), timeout=1.0) as conn:
            cols = _table_columns(conn, table)
            sym_col = _first_existing(cols, _SYMBOL_COL_CANDIDATES)
            time_col = _first_existing(cols, _TIME_COL_CANDIDATES)
            price_col = _first_existing(cols, _PRICE_COL_CANDIDATES)
            if not sym_col or not time_col or not price_col:
                logger.warning(
                    "[RANKING ENTRY HL PATCH] required cols missing table=%s sym_col=%s time_col=%s price_col=%s cols=%s",
                    table, sym_col, time_col, price_col, sorted(cols),
                )
                return {}
            q = (
                f"SELECT {_quote_ident(time_col)} AS dt, {_quote_ident(price_col)} AS price "
                f"FROM {_quote_ident(table)} "
                f"WHERE CAST({_quote_ident(sym_col)} AS TEXT)=? "
                f"ORDER BY {_quote_ident(time_col)} DESC "
                f"LIMIT ?"
            )
            df = pd.read_sql_query(q, conn, params=(sym, max(3, int(LOOKBACK_ROWS))))
        if df.empty:
            return {}
        df["dt"] = pd.to_datetime(df["dt"], errors="coerce")
        df["price"] = pd.to_numeric(df["price"], errors="coerce")
        df = df.dropna(subset=["dt", "price"])
        df = df[df["price"] > 0].sort_values("dt")
        if df.empty:
            return {}
        latest_dt = pd.to_datetime(df["dt"].max(), errors="coerce")
        age_min = None
        if pd.notna(latest_dt):
            age_min = (pd.Timestamp(dt.datetime.now()) - latest_dt).total_seconds() / 60.0
        if age_min is not None and age_min > MAX_AGE_MIN:
            logger.warning(
                "[RANKING ENTRY HL PATCH] snapshot stale symbol=%s latest=%s age_min=%.1f max=%.1f",
                sym, latest_dt, age_min, MAX_AGE_MIN,
            )
            return {}
        open_price = float(df["price"].iloc[0])
        close_price = float(df["price"].iloc[-1])
        high = float(df["price"].max())
        low = float(df["price"].min())
        range_pct = ((high - low) / close_price * 100.0) if close_price > 0 else 0.0
        out = {
            "open": open_price,
            "high": high,
            "low": low,
            "close": close_price,
            "open_price": open_price,
            "high_price": high,
            "low_price": low,
            "close_price": close_price,
            "ranking_snapshot_high": high,
            "ranking_snapshot_low": low,
            "ranking_snapshot_open": open_price,
            "ranking_snapshot_close": close_price,
            "ranking_snapshot_range_pct": range_pct,
            "row_range_pct": range_pct,
            "intrabar_range_pct": range_pct,
            "ranking_snapshot_hl_rows": int(len(df)),
            "ranking_snapshot_hl_latest_dt": str(latest_dt) if pd.notna(latest_dt) else None,
            "ranking_snapshot_hl_age_min": round(float(age_min), 3) if age_min is not None else None,
            "ranking_snapshot_hl_source": "ranking_snapshot_1min",
        }
        _CACHE[sym] = (now_ts, out)
        logger.info(
            "[RANKING ENTRY HL PATCH] attached symbol=%s rows=%s open=%.2f high=%.2f low=%.2f close=%.2f range_pct=%.3f latest=%s age_min=%s",
            sym, len(df), open_price, high, low, close_price, range_pct, out["ranking_snapshot_hl_latest_dt"], out["ranking_snapshot_hl_age_min"],
        )
        return out
    except Exception:
        logger.debug("[RANKING ENTRY HL PATCH] calc failed symbol=%s db=%s", sym, db_path, exc_info=True)
        return {}


def _needs_high_low(entry_row: dict[str, Any]) -> bool:
    high = _safe_float(entry_row.get("high") or entry_row.get("high_price"), 0.0)
    low = _safe_float(entry_row.get("low") or entry_row.get("low_price"), 0.0)
    close = _safe_float(entry_row.get("close") or entry_row.get("close_price") or entry_row.get("price"), 0.0)
    return close > 0 and (high <= 0 or low <= 0 or high < low)



# ============================================================
# スコープ判定 + 公開入口
# ============================================================

import sys  # noqa: E402

_HL_DB_PROCESS_NAMES = (
    "main_database.py",
    "data_collectors_runner.py",
    "db_prepare_runner.py",
    "push_receiver_runner.py",
    "yahoo_complement_runner.py",
    "summary_database_runner.py",
    "ranking_collector_runner.py",
)


def high_low_fill_enabled() -> bool:
    """このプロセスで ranking_snapshot からの high/low 補完を行うか。

    無効化は従来どおり RANKING_ENTRY_HIGH_LOW_SNAPSHOT_PATCH_ENABLED=0 (ENABLED)。
    スコープは RANKING_ENTRY_HIGH_LOW_SCOPE_ENABLED で上書きできる。
    """
    try:
        if not ENABLED:
            return False
        raw = os.getenv("RANKING_ENTRY_HIGH_LOW_SCOPE_ENABLED")
        if raw is not None and str(raw).strip() != "":
            return str(raw).strip().lower() in {"1", "true", "yes", "y", "on", "enable", "enabled"}
        if str(os.getenv("CORE_STARTUP_FORCE_FULL_INIT_PATCHES", "")).strip().lower() in {
            "1", "true", "yes", "y", "on", "enable", "enabled",
        }:
            return True
        argv_text = " ".join(str(x) for x in sys.argv).replace("\\", "/").lower()
        return not any(name in argv_text for name in _HL_DB_PROCESS_NAMES)
    except Exception:
        return False


def fill_high_low_from_snapshot(entry_row: Any, source_row: Any = None) -> Any:
    """entry_row の high/low を ranking_snapshot から補完して返す。

    補完不要 / 対象外 / 失敗時は entry_row をそのまま返す。
    """
    try:
        if not high_low_fill_enabled() or not isinstance(entry_row, dict):
            return entry_row
        src = str(
            entry_row.get("source")
            or (source_row.get("source") if isinstance(source_row, dict) else "")
            or ""
        ).upper()
        symbol = _normalize_symbol(
            entry_row.get("symbol")
            or (source_row.get("symbol") if isinstance(source_row, dict) else "")
        )
        if not symbol or not _needs_high_low(entry_row):
            return entry_row
        hl = _calc_from_snapshot(symbol)
        if not hl:
            return entry_row
        for k, v in hl.items():
            if k in {"open", "high", "low", "close", "open_price", "high_price", "low_price", "close_price"}:
                if _safe_float(entry_row.get(k), 0.0) <= 0:
                    entry_row[k] = v
            else:
                entry_row[k] = v
        entry_row["ranking_high_low_filled"] = True
        if not entry_row.get("source") and src:
            entry_row["source"] = src
        return entry_row
    except Exception:
        logger.debug("[RANKING ENTRY HL] fill from snapshot failed", exc_info=True)
        return entry_row


__all__ = ["high_low_fill_enabled", "fill_high_low_from_snapshot"]
