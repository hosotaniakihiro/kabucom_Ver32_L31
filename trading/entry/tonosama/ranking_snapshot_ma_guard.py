# ============================================================
# File   : trading/entry/tonosama/ranking_snapshot_ma_guard.py
# Version: Ver1.1-TONOSAMA-RANKING-SNAPSHOT-MA-GUARD-SHARED-RANKING-MODULES
# ------------------------------------------------------------
# 目的:
#   殿様イナゴの通知/pending直前で使う、ランキングスナップショット由来の
#   MA3/MA5方向判定。
#
# 方針:
#   - PUSH 1分履歴は使わない。
#   - ranking_snapshot_1min の価格履歴から、symbol別に直近MA3/MA5と傾きを算出する。
#   - trading/ranking/snapshot_writer.py が委譲している既存共通モジュールを使う。
#       database.paths.ranking_paths.resolve_ranking_db_path
#       database.schema.ranking_snapshot_schema.SNAPSHOT_TABLE
#   - ranking DBの場所やテーブル名を殿様側で独自に持たない。
#
# BUY拒否:
#   - ma3_slope < 0 または ma5_slope < 0
# SELL拒否:
#   - ma3_slope > 0 または ma5_slope > 0
# ============================================================
from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
from typing import Any

import pandas as pd

try:
    from database.paths.ranking_paths import resolve_ranking_db_path
except Exception:  # pragma: no cover
    resolve_ranking_db_path = None  # type: ignore

try:
    from database.schema.ranking_snapshot_schema import SNAPSHOT_TABLE
except Exception:  # pragma: no cover
    SNAPSHOT_TABLE = "ranking_snapshot_1min"

logger = logging.getLogger(__name__)


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


RANKING_MA_GUARD_ENABLED = _env_bool("TONOSAMA_RANKING_MA_GUARD_ENABLED", True)
RANKING_MA_LOOKBACK_ROWS = _env_int("TONOSAMA_RANKING_MA_LOOKBACK_ROWS", 30)
RANKING_MA_MAX_AGE_MIN = _env_float("TONOSAMA_RANKING_MA_MAX_AGE_MIN", 30.0)
RANKING_MA_FAIL_OPEN_IF_UNKNOWN = _env_bool("TONOSAMA_RANKING_MA_FAIL_OPEN_IF_UNKNOWN", True)

_PRICE_COL_CANDIDATES = [
    "current_price",
    "price",
    "CurrentPrice",
    "close",
    "close_price",
    "Close",
    "last_price",
    "last",
]
_TIME_COL_CANDIDATES = [
    "datetime",
    "snapshot_time",
    "received_at",
    "created_at",
    "inserted_at",
    "updated_at",
    "time",
]
_SYMBOL_COL_CANDIDATES = ["symbol", "Symbol", "code", "銘柄コード", "stock_code"]


def _normalize_symbol(symbol: Any) -> str:
    try:
        s = str(symbol or "").strip()
        if not s:
            return ""
        if s.endswith(".0"):
            s = s[:-2]
        return s
    except Exception:
        return ""


def _first_existing(df: pd.DataFrame, names: list[str]) -> str | None:
    for c in names:
        if c in df.columns:
            return c
    return None


def _quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _normalize_snapshot_df(df: pd.DataFrame, *, symbol: str | None = None) -> pd.DataFrame:
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame()
    x = df.copy()
    sym_col = _first_existing(x, _SYMBOL_COL_CANDIDATES)
    time_col = _first_existing(x, _TIME_COL_CANDIDATES)
    price_col = _first_existing(x, _PRICE_COL_CANDIDATES)
    if sym_col is None or time_col is None or price_col is None:
        logger.warning(
            "[TONOSAMA RANKING MA] normalize failed missing columns sym_col=%s time_col=%s price_col=%s columns=%s",
            sym_col,
            time_col,
            price_col,
            list(x.columns),
        )
        return pd.DataFrame()
    x["symbol"] = x[sym_col].map(_normalize_symbol)
    x["datetime"] = pd.to_datetime(x[time_col], errors="coerce")
    x["price"] = pd.to_numeric(x[price_col], errors="coerce")
    x = x.dropna(subset=["datetime", "price"])
    x = x[x["price"] > 0]
    if symbol:
        sym = _normalize_symbol(symbol)
        x = x[x["symbol"] == sym]
    if x.empty:
        return pd.DataFrame()
    x = x.sort_values(["symbol", "datetime"])
    x["datetime"] = pd.to_datetime(x["datetime"], errors="coerce").dt.floor("min")
    x = x.drop_duplicates(subset=["symbol", "datetime"], keep="last")
    return x[["symbol", "datetime", "price"]].sort_values(["symbol", "datetime"])


def _candidate_global_snapshot_frames() -> list[pd.DataFrame]:
    frames: list[pd.DataFrame] = []
    try:
        from global_state import global_data
    except Exception:
        return frames

    method_names = [
        "get_latest_ranking_snapshot",
        "get_ranking_snapshot",
        "get_ranking_snapshot_df",
        "snapshot_ranking",
    ]
    for name in method_names:
        try:
            fn = getattr(global_data, name, None)
            if callable(fn):
                df = fn()
                if isinstance(df, pd.DataFrame) and not df.empty:
                    frames.append(df)
        except Exception:
            logger.debug("[TONOSAMA RANKING MA] global_data.%s failed", name, exc_info=True)

    attr_names = [
        "latest_ranking_snapshot",
        "ranking_snapshot",
        "ranking_snapshot_1min",
        "_latest_ranking_snapshot",
        "_ranking_snapshot",
    ]
    for name in attr_names:
        try:
            df = getattr(global_data, name, None)
            if isinstance(df, pd.DataFrame) and not df.empty:
                frames.append(df)
        except Exception:
            logger.debug("[TONOSAMA RANKING MA] global_data attr %s failed", name, exc_info=True)
    return frames


def _resolve_today_ranking_db_path() -> str | None:
    # 環境変数が明示されている場合はそれを優先。
    env_path = os.getenv("RANKING_DB_PATH") or os.getenv("KABU_RANKING_DB_PATH") or os.getenv("KABUCOM_RANKING_DB_PATH")
    if env_path and str(env_path).strip():
        return str(env_path).strip()

    # 既存のranking共通パス解決へ委譲。
    try:
        if callable(resolve_ranking_db_path):
            return str(resolve_ranking_db_path())
    except Exception:
        logger.debug("[TONOSAMA RANKING MA] resolve_ranking_db_path failed", exc_info=True)
    return None


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    try:
        cur = conn.execute(f"PRAGMA table_info({_quote_ident(table_name)})")
        return {str(row[1]) for row in cur.fetchall()}
    except Exception:
        return set()


def _load_snapshot_from_db(symbol: str) -> pd.DataFrame:
    sym = _normalize_symbol(symbol)
    if not sym:
        return pd.DataFrame()

    db_path = _resolve_today_ranking_db_path()
    if not db_path:
        logger.warning("[TONOSAMA RANKING MA] ranking db path unresolved symbol=%s", sym)
        return pd.DataFrame()

    try:
        with sqlite3.connect(str(db_path), timeout=1.0) as conn:
            cols = _table_columns(conn, SNAPSHOT_TABLE)
            if not cols:
                logger.warning("[TONOSAMA RANKING MA] table columns empty db=%s table=%s", db_path, SNAPSHOT_TABLE)
                return pd.DataFrame()

            sym_col = next((c for c in _SYMBOL_COL_CANDIDATES if c in cols), None)
            time_col = next((c for c in _TIME_COL_CANDIDATES if c in cols), None)
            price_col = next((c for c in _PRICE_COL_CANDIDATES if c in cols), None)
            if sym_col is None or time_col is None or price_col is None:
                logger.warning(
                    "[TONOSAMA RANKING MA] required columns missing db=%s table=%s sym=%s time=%s price=%s columns=%s",
                    db_path,
                    SNAPSHOT_TABLE,
                    sym_col,
                    time_col,
                    price_col,
                    sorted(cols),
                )
                return pd.DataFrame()

            q = (
                f"SELECT * FROM {_quote_ident(SNAPSHOT_TABLE)} "
                f"WHERE CAST({_quote_ident(sym_col)} AS TEXT)=? "
                f"ORDER BY {_quote_ident(time_col)} DESC "
                f"LIMIT ?"
            )
            df = pd.read_sql_query(q, conn, params=(sym, int(max(RANKING_MA_LOOKBACK_ROWS, 10))))
        norm = _normalize_snapshot_df(df, symbol=sym)
        if not norm.empty:
            logger.info("[TONOSAMA RANKING MA] loaded from shared ranking db path=%s table=%s symbol=%s rows=%s", db_path, SNAPSHOT_TABLE, sym, len(norm))
            return norm
    except Exception:
        logger.debug("[TONOSAMA RANKING MA] db load failed path=%s table=%s symbol=%s", db_path, SNAPSHOT_TABLE, sym, exc_info=True)
    return pd.DataFrame()


def _load_symbol_snapshot(symbol: str) -> pd.DataFrame:
    sym = _normalize_symbol(symbol)
    for df in _candidate_global_snapshot_frames():
        norm = _normalize_snapshot_df(df, symbol=sym)
        if not norm.empty:
            logger.info("[TONOSAMA RANKING MA] loaded from global_data symbol=%s rows=%s", sym, len(norm))
            return norm.tail(int(max(RANKING_MA_LOOKBACK_ROWS, 10)))
    return _load_snapshot_from_db(sym)


def calc_ranking_snapshot_ma(symbol: str) -> dict[str, Any]:
    sym = _normalize_symbol(symbol)
    result: dict[str, Any] = {
        "ok": False,
        "symbol": sym,
        "source": "ranking_snapshot_1min",
        "table": SNAPSHOT_TABLE,
        "rows": 0,
        "latest_dt": None,
        "age_min": None,
        "ma3": 0.0,
        "ma5": 0.0,
        "ma3_prev": 0.0,
        "ma5_prev": 0.0,
        "ma3_slope": 0.0,
        "ma5_slope": 0.0,
        "ma3_slope_pct": 0.0,
        "ma5_slope_pct": 0.0,
        "reason": "unknown",
    }
    if not RANKING_MA_GUARD_ENABLED:
        result["ok"] = True
        result["reason"] = "disabled"
        return result
    try:
        df = _load_symbol_snapshot(sym)
        if df is None or df.empty:
            result["reason"] = "ranking_snapshot_empty"
            return result
        x = df.sort_values("datetime").tail(int(max(RANKING_MA_LOOKBACK_ROWS, 10))).copy()
        x["ma3"] = x["price"].rolling(3, min_periods=2).mean()
        x["ma5"] = x["price"].rolling(5, min_periods=2).mean()
        x["ma3_prev"] = x["ma3"].shift(1)
        x["ma5_prev"] = x["ma5"].shift(1)
        latest = x.tail(1).iloc[0]
        latest_dt = pd.to_datetime(latest["datetime"], errors="coerce")
        age_min = None
        if pd.notna(latest_dt):
            age_min = (pd.Timestamp(dt.datetime.now()) - latest_dt).total_seconds() / 60.0
        ma3 = float(latest.get("ma3") or 0.0)
        ma5 = float(latest.get("ma5") or 0.0)
        ma3_prev = float(latest.get("ma3_prev") or 0.0)
        ma5_prev = float(latest.get("ma5_prev") or 0.0)
        ma3_slope = ma3 - ma3_prev if ma3_prev else 0.0
        ma5_slope = ma5 - ma5_prev if ma5_prev else 0.0
        ma3_slope_pct = (ma3_slope / ma3_prev * 100.0) if ma3_prev else 0.0
        ma5_slope_pct = (ma5_slope / ma5_prev * 100.0) if ma5_prev else 0.0
        result.update({
            "ok": True,
            "rows": int(len(x)),
            "latest_dt": str(latest_dt) if pd.notna(latest_dt) else None,
            "age_min": round(float(age_min), 3) if age_min is not None else None,
            "ma3": ma3,
            "ma5": ma5,
            "ma3_prev": ma3_prev,
            "ma5_prev": ma5_prev,
            "ma3_slope": ma3_slope,
            "ma5_slope": ma5_slope,
            "ma3_slope_pct": ma3_slope_pct,
            "ma5_slope_pct": ma5_slope_pct,
            "reason": "ok",
        })
        if age_min is not None and age_min > RANKING_MA_MAX_AGE_MIN:
            result["ok"] = False
            result["reason"] = "ranking_snapshot_stale"
        logger.info(
            "[TONOSAMA RANKING MA] symbol=%s ok=%s table=%s rows=%s latest=%s age_min=%s ma3=%.4f ma5=%.4f ma3_slope=%.6f ma5_slope=%.6f reason=%s",
            sym,
            result["ok"],
            SNAPSHOT_TABLE,
            result["rows"],
            result["latest_dt"],
            result["age_min"],
            result["ma3"],
            result["ma5"],
            result["ma3_slope"],
            result["ma5_slope"],
            result["reason"],
        )
        return result
    except Exception:
        logger.exception("[TONOSAMA RANKING MA] calc failed symbol=%s", sym)
        result["reason"] = "exception"
        return result


def reject_reason_for_side(symbol: str, side: str) -> tuple[str | None, dict[str, Any]]:
    info = calc_ranking_snapshot_ma(symbol)
    side_u = str(side or "").upper()
    if not info.get("ok"):
        if RANKING_MA_FAIL_OPEN_IF_UNKNOWN:
            return None, info
        return "ranking_snapshot_ma_unknown_guard", info
    ma3_slope = float(info.get("ma3_slope") or 0.0)
    ma5_slope = float(info.get("ma5_slope") or 0.0)
    if side_u == "BUY" and (ma3_slope < 0 or ma5_slope < 0):
        return "buy_ranking_ma3_ma5_down_guard", info
    if side_u == "SELL" and (ma3_slope > 0 or ma5_slope > 0):
        return "sell_ranking_ma3_ma5_up_guard", info
    return None, info


__all__ = ["calc_ranking_snapshot_ma", "reject_reason_for_side"]
