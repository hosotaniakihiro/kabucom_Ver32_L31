# ============================================================
# File   : trading/ranking/active_symbols/ranking_source.py
# Version: Ver1.0-ACTIVE-SYMBOLS-RANKING-SOURCE
# ============================================================
from __future__ import annotations
import logging, datetime as dt
from typing import Any, Dict, List, Optional, Set
import pandas as pd
from global_state import global_data
from .config import TICK_COLUMNS, VALUE_COLUMNS, VOLUME_COLUMNS, VOLUME_SPEED_TOP_N, PRICE_COLUMNS
from .normalize import dedupe_keep_order, first_existing_col, normalize_symbol, safe_numeric_series, today_date_str, to_float

logger = logging.getLogger(__name__)


def get_latest_ranking_dict() -> Dict[Any, pd.DataFrame]:
    latest = getattr(global_data, "latest_ranking", None)
    if isinstance(latest, dict):
        return latest
    return {}


def normalize_ranking_df(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    if "symbol" not in out.columns:
        for c in ("code", "銘柄コード", "Symbol"):
            if c in out.columns:
                out["symbol"] = out[c]
                break
    if "symbol" not in out.columns:
        return pd.DataFrame()
    out["symbol"] = out["symbol"].map(normalize_symbol)
    out = out[out["symbol"].notna()].copy()
    if "datetime" not in out.columns:
        for c in ("snapshot_time", "inserted_at", "received_at", "created_at"):
            if c in out.columns:
                out["datetime"] = out[c]
                break
    if "date" not in out.columns and "datetime" in out.columns:
        try:
            out["date"] = pd.to_datetime(out["datetime"], errors="coerce").dt.strftime("%Y-%m-%d")
        except Exception:
            pass
    if "current_price" not in out.columns:
        c = first_existing_col(out, PRICE_COLUMNS)
        if c:
            out["current_price"] = out[c]
    if "trading_volume" not in out.columns:
        c = first_existing_col(out, VOLUME_COLUMNS)
        if c:
            out["trading_volume"] = out[c]
    if "trading_value" not in out.columns:
        c = first_existing_col(out, VALUE_COLUMNS)
        if c:
            out["trading_value"] = out[c]
    if "tick_count" not in out.columns:
        c = first_existing_col(out, TICK_COLUMNS)
        if c:
            out["tick_count"] = out[c]
    if "volume_speed" not in out.columns:
        for c in ("volume_delta_1m", "出来高増加", "volume_change"):
            if c in out.columns:
                out["volume_speed"] = out[c]
                break
    out["current_price"] = safe_numeric_series(out, "current_price")
    out["trading_volume"] = safe_numeric_series(out, "trading_volume")
    out["trading_value"] = safe_numeric_series(out, "trading_value")
    out["tick_count"] = safe_numeric_series(out, "tick_count")
    out["volume_speed"] = safe_numeric_series(out, "volume_speed")
    return out


def merged_latest_ranking_df(*, today_only: bool = True, now: Optional[dt.datetime] = None) -> pd.DataFrame:
    frames: List[pd.DataFrame] = []
    today = today_date_str(now)
    for _, df in get_latest_ranking_dict().items():
        try:
            ndf = normalize_ranking_df(df)
            if ndf.empty:
                continue
            if today_only and "date" in ndf.columns:
                ndf = ndf[ndf["date"].astype(str) == today].copy()
            if not ndf.empty:
                frames.append(ndf)
        except Exception:
            logger.debug("[ACTIVE] ranking df normalize failed", exc_info=True)
    if not frames:
        return pd.DataFrame()
    merged = pd.concat(frames, ignore_index=True)
    if merged.empty or "symbol" not in merged.columns:
        return pd.DataFrame()
    agg = {"current_price": "max", "trading_volume": "max", "trading_value": "max", "tick_count": "max", "volume_speed": "max"}
    try:
        merged = merged.groupby("symbol", as_index=False).agg(agg).reset_index(drop=True)
    except Exception:
        merged = merged.drop_duplicates(subset=["symbol"], keep="first")
    return merged


def today_ranking_symbols(now: Optional[dt.datetime] = None) -> List[str]:
    df = merged_latest_ranking_df(today_only=True, now=now)
    if df.empty or "symbol" not in df.columns:
        return []
    return dedupe_keep_order(df["symbol"].tolist())


def today_ranking_available(now: Optional[dt.datetime] = None) -> bool:
    return len(today_ranking_symbols(now=now)) > 0


def update_last_seen_from_ranking(now: dt.datetime) -> None:
    if not hasattr(global_data, "symbol_last_seen"):
        global_data.symbol_last_seen = {}
    for df in get_latest_ranking_dict().values():
        if df is None or df.empty or "symbol" not in df.columns:
            continue
        for sym in df["symbol"].dropna().astype(str):
            ns = normalize_symbol(sym)
            if ns:
                global_data.symbol_last_seen[ns] = now


def build_liquidity_map() -> Dict[str, Dict[str, float]]:
    out: Dict[str, Dict[str, float]] = {}
    df = merged_latest_ranking_df(today_only=True)
    if df.empty:
        return out
    for _, row in df.iterrows():
        sym = normalize_symbol(row.get("symbol"))
        if not sym:
            continue
        out[sym] = {
            "current_price": to_float(row.get("current_price"), 0.0),
            "trading_volume": to_float(row.get("trading_volume"), 0.0),
            "trading_value": to_float(row.get("trading_value"), 0.0),
            "tick_count": to_float(row.get("tick_count"), 0.0),
            "volume_speed": to_float(row.get("volume_speed"), 0.0),
        }
    return out


def extract_volume_speed_symbols() -> Set[str]:
    symbols: Set[str] = set()
    for df in get_latest_ranking_dict().values():
        if df is None or df.empty:
            continue
        ndf = normalize_ranking_df(df)
        if ndf.empty or "volume_speed" not in ndf.columns or "symbol" not in ndf.columns:
            continue
        try:
            df_sorted = ndf.sort_values("volume_speed", ascending=False)
        except Exception:
            continue
        for sym in df_sorted["symbol"].head(VOLUME_SPEED_TOP_N).astype(str):
            ns = normalize_symbol(sym)
            if ns:
                symbols.add(ns)
    return symbols
