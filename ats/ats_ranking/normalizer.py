# ============================================================
# File   : ats/ats_ranking/normalizer.py
# Version: Ver1.0-ATS-RANKING-NORMALIZER
# ============================================================

from __future__ import annotations

import logging
import sqlite3
from typing import Dict, Iterable, List, Optional

import numpy as np
import pandas as pd

from global_state import global_data
from config.paths import get_path

from .constants import MARKET_CODE_TO_LABEL

logger = logging.getLogger(__name__)


def _normalize_symbol(x) -> str:
    try:
        s = str(x).strip()
    except Exception:
        return ""

    if not s:
        return ""

    if s.endswith(".0"):
        s = s[:-2].strip()

    return s


def _unique_keep_order(seq: Iterable[str]) -> List[str]:
    out: List[str] = []
    seen = set()

    for x in seq:
        s = _normalize_symbol(x)
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)

    return out


def _safe_numeric_series(series: pd.Series, default=0) -> pd.Series:
    return (
        pd.to_numeric(series, errors="coerce")
        .replace([np.inf, -np.inf], np.nan)
        .fillna(default)
    )


def _safe_symbol(df: pd.DataFrame) -> pd.DataFrame:
    if "symbol" not in df.columns:
        df["symbol"] = ""

    df["symbol"] = df["symbol"].map(_normalize_symbol)
    df = df[df["symbol"] != ""].copy()
    return df


def _normalize_market_type(x) -> str:
    if x is None:
        return ""

    s = str(x).strip()
    if not s:
        return ""

    if s in ("プライム", "スタンダード", "グロース"):
        return s

    return MARKET_CODE_TO_LABEL.get(s.upper(), s)


def _drop_duplicate_symbols(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty or "symbol" not in df.columns:
        return df

    for dt_col in ["snapshot_time", "datetime", "updated_at", "created_at"]:
        if dt_col in df.columns:
            try:
                tmp = df.copy()
                tmp["__dt__"] = pd.to_datetime(tmp[dt_col], errors="coerce")
                tmp = tmp.sort_values("__dt__", ascending=False, kind="mergesort")
                tmp = tmp.drop_duplicates(subset=["symbol"], keep="first")
                return tmp.drop(columns=["__dt__"], errors="ignore")
            except Exception:
                logger.exception("duplicate symbol drop with datetime-like failed: %s", dt_col)

    return df.drop_duplicates(subset=["symbol"], keep="last").copy()


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        ).fetchone()
        return bool(row)
    except Exception:
        return False


def _list_tables(conn: sqlite3.Connection) -> List[str]:
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        return [str(r[0]) for r in rows if r and len(r) > 0]
    except Exception:
        return []


def _safe_read_sql(conn: sqlite3.Connection, sql: str) -> pd.DataFrame:
    try:
        return pd.read_sql_query(sql, conn)
    except Exception:
        logger.exception("read_sql failed: %s", sql)
        return pd.DataFrame()


def _table_row_count(conn: sqlite3.Connection, table_name: str) -> int:
    try:
        if not _table_exists(conn, table_name):
            return 0
        row = conn.execute(f"SELECT COUNT(*) FROM [{table_name}]").fetchone()
        return int(row[0]) if row and len(row) > 0 and row[0] is not None else 0
    except Exception:
        logger.debug("table row count failed table=%s", table_name, exc_info=True)
        return 0


def _load_market_map(symbols: List[str]) -> Dict[str, str]:
    out: Dict[str, str] = {}

    if not symbols:
        return out

    try:
        flags_map = getattr(global_data, "symbol_flags", {})
        if isinstance(flags_map, dict):
            for s in symbols:
                flags = flags_map.get(str(s))
                if isinstance(flags, dict):
                    mt = _normalize_market_type(flags.get("market_type"))
                    if mt:
                        out[str(s)] = mt
    except Exception:
        logger.exception("global_data.symbol_flags market load failed")

    missing = [str(s) for s in symbols if str(s) not in out]
    if not missing:
        return out

    try:
        db_path = get_path("symbol_flags_db")
    except Exception:
        db_path = r"\\192.168.0.22\AutoStockBuyAndSell\Basic\symbol_flags.db"

    try:
        placeholders = ",".join("?" for _ in missing)
        sql = f"""
            SELECT symbol, market_type
            FROM symbol_flags
            WHERE symbol IN ({placeholders})
        """
        with sqlite3.connect(str(db_path)) as conn:
            rows = conn.execute(sql, missing).fetchall()

        for sym, market_type in rows:
            sym = _normalize_symbol(sym)
            mt = _normalize_market_type(market_type)
            if sym and mt:
                out[sym] = mt

    except Exception:
        logger.exception("symbol_flags.db market load failed")

    return out


def _coalesce_prefer_nonempty(
    df: pd.DataFrame,
    candidates: List[str],
    out_col: str,
    *,
    numeric: bool = False,
    prefer_positive: bool = False,
    default=None,
) -> pd.DataFrame:
    x = df.copy()

    if numeric:
        if default is None:
            default = 0.0
        out = pd.Series([np.nan] * len(x), index=x.index, dtype="float64")

        for c in candidates:
            if c not in x.columns:
                continue

            s = pd.to_numeric(x[c], errors="coerce").replace([np.inf, -np.inf], np.nan)

            if prefer_positive:
                take_mask = out.isna() | (out <= 0)
                cand_mask = s.notna() & (s > 0)
            else:
                take_mask = out.isna()
                cand_mask = s.notna()

            mask = take_mask & cand_mask
            if mask.any():
                out.loc[mask] = s.loc[mask]

        x[out_col] = out.fillna(default)
        return x

    if default is None:
        default = ""

    out = pd.Series([None] * len(x), index=x.index, dtype="object")

    for c in candidates:
        if c not in x.columns:
            continue

        s = x[c]

        try:
            s2 = s.astype("object")
        except Exception:
            s2 = s

        def _is_nonempty(v) -> bool:
            if v is None:
                return False
            try:
                if pd.isna(v):
                    return False
            except Exception:
                pass
            if isinstance(v, str):
                return v.strip() != ""
            return True

        cand_mask = s2.map(_is_nonempty)
        out_mask = out.map(lambda v: v is None)
        mask = out_mask & cand_mask
        if mask.any():
            out.loc[mask] = s2.loc[mask]

    x[out_col] = out.map(lambda v: default if v is None else v)
    return x


def _infer_common_columns(df: pd.DataFrame) -> pd.DataFrame:
    x = df.copy()

    x = _coalesce_prefer_nonempty(x, ["symbol", "Symbol", "銘柄コード", "code", "Code"], "symbol")
    x = _coalesce_prefer_nonempty(x, ["symbolname", "SymbolName", "銘柄名", "name"], "symbolname")
    x = _coalesce_prefer_nonempty(x, ["rank_type", "RankType", "type", "ranking_type"], "rank_type")
    x = _coalesce_prefer_nonempty(x, ["market", "Market", "market_type", "市場"], "market")
    x = _coalesce_prefer_nonempty(
        x, ["rank_position", "RankPosition", "rank", "順位"],
        "rank_position", numeric=True, prefer_positive=True, default=np.nan
    )
    x = _coalesce_prefer_nonempty(
        x, ["current_price", "price", "close", "現在値"],
        "current_price", numeric=True, prefer_positive=True, default=0
    )
    x = _coalesce_prefer_nonempty(
        x, ["trading_volume", "volume", "売買高"],
        "trading_volume", numeric=True, prefer_positive=True, default=0
    )
    x = _coalesce_prefer_nonempty(
        x, ["volume_speed", "volume_spike", "出来高急増"],
        "volume_speed", numeric=True, prefer_positive=True, default=0
    )
    x = _coalesce_prefer_nonempty(x, ["rank_strength", "score_strength"], "rank_strength", numeric=True, default=0)
    x = _coalesce_prefer_nonempty(x, ["rank_persistence", "persistence"], "rank_persistence", numeric=True, default=0)
    x = _coalesce_prefer_nonempty(x, ["rank_delta", "delta_rank"], "rank_delta", numeric=True, default=0)
    x = _coalesce_prefer_nonempty(x, ["price_delta_1m", "price_change_1m"], "price_delta_1m", numeric=True, default=0)
    x = _coalesce_prefer_nonempty(x, ["volume_delta_1m", "volume_change_1m"], "volume_delta_1m", numeric=True, default=0)
    x = _coalesce_prefer_nonempty(x, ["snapshot_time", "datetime", "updated_at", "created_at"], "snapshot_time")
    x = _coalesce_prefer_nonempty(x, ["source", "Source"], "source")

    return x


def _standardize_snapshot_df(df: pd.DataFrame, source_name: str) -> Optional[pd.DataFrame]:
    if df is None or df.empty:
        return None

    try:
        x = _infer_common_columns(df)
        x = _safe_symbol(x)
        if x.empty:
            return None

        if "rank_type" not in x.columns:
            x["rank_type"] = ""
        if "market" not in x.columns:
            x["market"] = ""
        if "rank_position" not in x.columns:
            x["rank_position"] = np.nan
        if "symbolname" not in x.columns:
            x["symbolname"] = ""
        if "snapshot_time" not in x.columns:
            x["snapshot_time"] = pd.NaT
        if "source" not in x.columns:
            x["source"] = source_name

        for col in [
            "rank_position",
            "current_price",
            "trading_volume",
            "volume_speed",
            "rank_strength",
            "rank_persistence",
            "rank_delta",
            "price_delta_1m",
            "volume_delta_1m",
        ]:
            if col not in x.columns:
                x[col] = 0
            default_val = np.nan if col == "rank_position" else 0
            x[col] = _safe_numeric_series(x[col], default=default_val)

        x["rank_type"] = x["rank_type"].astype(str).str.strip()
        x["market"] = x["market"].map(_normalize_market_type)
        x["snapshot_time"] = pd.to_datetime(x["snapshot_time"], errors="coerce")

        try:
            tmp = x.copy()
            tmp["__dt__"] = pd.to_datetime(tmp["snapshot_time"], errors="coerce")
            tmp = tmp.sort_values("__dt__", ascending=False, kind="mergesort")
            tmp = tmp.drop_duplicates(subset=["symbol", "rank_type"], keep="first")
            x = tmp.drop(columns=["__dt__"], errors="ignore")
        except Exception:
            logger.exception("drop duplicate symbol/rank_type latest failed")

        x["market_type"] = x["market"].astype(str)
        missing_market = x["market_type"].astype(str).str.strip() == ""
        if missing_market.any():
            market_map = _load_market_map(x.loc[missing_market, "symbol"].astype(str).tolist())
            if market_map:
                x.loc[missing_market, "market_type"] = (
                    x.loc[missing_market, "symbol"].astype(str).map(
                        lambda s: market_map.get(str(s), "")
                    )
                )

        x["market_type"] = x["market_type"].map(_normalize_market_type)

        logger.info(
            "[ATS RANKING SOURCE] source=%s rows=%d symbols=%d rank_type_nonempty=%d market_nonempty=%d price>0=%d vol>0=%d vspd>0=%d",
            source_name,
            len(x),
            x["symbol"].nunique(),
            int((x["rank_type"].astype(str).str.strip() != "").sum()),
            int((x["market_type"].astype(str).str.strip() != "").sum()),
            int((x["current_price"] > 0).sum()),
            int((x["trading_volume"] > 0).sum()),
            int((x["volume_speed"] > 0).sum()),
        )

        return x

    except Exception:
        logger.exception("standardize snapshot df failed: %s", source_name)
        return None


def _standardize_summary_fallback(df: pd.DataFrame) -> Optional[pd.DataFrame]:
    if df is None or df.empty:
        return None

    try:
        x = df.copy()
        x = _safe_symbol(x)
        if x.empty:
            return None

        x = _drop_duplicate_symbols(x)

        for c in ["close", "prev_close", "turnover", "volume"]:
            if c not in x.columns:
                x[c] = 0
            x[c] = _safe_numeric_series(x[c], default=0)

        if "volume_ma5" not in x.columns:
            x["volume_ma5"] = np.nan
        else:
            x["volume_ma5"] = _safe_numeric_series(x["volume_ma5"], default=np.nan)

        base = x["prev_close"].replace(0, np.nan)
        x["pct_change"] = (
            ((x["close"] - x["prev_close"]) / base)
            .replace([np.inf, -np.inf], np.nan)
            .fillna(0)
            * 100
        )

        x["capital_score"] = x["pct_change"] * np.log1p(x["turnover"].clip(lower=0))
        denom = x["volume_ma5"].replace(0, np.nan)
        x["volume_spike"] = (
            (x["volume"] / denom)
            .replace([np.inf, -np.inf], np.nan)
            .fillna(1)
        )
        x["total_score"] = x["capital_score"].fillna(0) * x["volume_spike"].fillna(1)

        market_map = _load_market_map(x["symbol"].astype(str).tolist())
        x["market_type"] = x["symbol"].astype(str).map(lambda s: market_map.get(str(s), ""))

        x["rank_type"] = ""
        x["rank_position"] = np.nan
        x["trading_volume"] = x["volume"]
        x["volume_speed"] = x["volume_spike"]
        x["current_price"] = x["close"]
        x["snapshot_time"] = pd.Timestamp.now()
        x["source"] = "summary_cache_1min"
        x["symbolname"] = x.get("symbolname", "")
        x["market"] = x.get("market_type", "")

        logger.warning("[ATS RANKING] fallback to summary_cache['1min'] rows=%d", len(x))
        return x

    except Exception:
        logger.exception("standardize summary fallback failed")
        return None