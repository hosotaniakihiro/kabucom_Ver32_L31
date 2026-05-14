# ============================================================
# File   : trading/ranking/active_symbols/ranking_source.py
# Version: Ver1.1-ACTIVE-SYMBOLS-CATEGORY-QUOTA-SELECTION
# ------------------------------------------------------------
# 今日ランキングからの監視100銘柄候補をカテゴリ配分で作る。
#
# 配分:
#   1. 値上がり率ランキングで売買代金上位30銘柄
#   2. 値下がり率ランキングで売買代金上位10銘柄
#   3. 売買代金ランキングで上昇率上位30銘柄
#   4. 売買代金ランキングで下落率上位10銘柄
#   5. 残りはTICKデータ上位で100銘柄まで補充
#
# 価格:
#   監視銘柄は 200円以上 5000円以下を基本にする。
#   上限は ACTIVE_MAX_PRICE で変更可能。
# ============================================================
from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Dict, List, Optional, Set, Tuple

import pandas as pd

from global_state import global_data
from .config import (
    ACTIVE_GAINERS_BY_VALUE_N,
    ACTIVE_LOSERS_BY_VALUE_N,
    ACTIVE_TICK_SUPPLEMENT_N,
    ACTIVE_USE_CATEGORY_QUOTA_SELECTION,
    ACTIVE_VALUE_BY_GAINERS_N,
    ACTIVE_VALUE_BY_LOSERS_N,
    CHANGE_COLUMNS,
    MAX_PRICE,
    MIN_PRICE,
    PRICE_COLUMNS,
    RANK_TYPE_COLUMNS,
    TARGET_ACTIVE_SYMBOLS,
    TICK_COLUMNS,
    VALUE_COLUMNS,
    VOLUME_COLUMNS,
    VOLUME_SPEED_TOP_N,
)
from .normalize import (
    dedupe_keep_order,
    first_existing_col,
    normalize_symbol,
    safe_numeric_series,
    today_date_str,
    to_float,
)

logger = logging.getLogger(__name__)


def get_latest_ranking_dict() -> Dict[Any, pd.DataFrame]:
    latest = getattr(global_data, "latest_ranking", None)
    if isinstance(latest, dict):
        return latest
    return {}


def _rank_key_text(key: Any) -> str:
    try:
        if isinstance(key, tuple):
            return "_".join(str(x) for x in key)
        return str(key or "")
    except Exception:
        return ""


def _detect_rank_kind(text: str) -> str:
    s = str(text or "")
    if any(x in s for x in ("値上がり", "上昇率", "騰落率上位", "gainer", "Gainer", "rise", "up")):
        return "gainers"
    if any(x in s for x in ("値下がり", "下落率", "loser", "Loser", "fall", "down")):
        return "losers"
    if any(x in s for x in ("売買代金", "売買金額", "turnover", "value", "Value")):
        return "value"
    if any(x in s for x in ("TICK", "Tick", "tick", "ティック", "約定回数")):
        return "tick"
    if any(x in s for x in ("出来高急増", "出来高増加", "volume_speed")):
        return "volume_speed"
    return "other"


def normalize_ranking_df(df: pd.DataFrame, *, rank_key: Any = None) -> pd.DataFrame:
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

    if "ranking_type" not in out.columns:
        c = first_existing_col(out, RANK_TYPE_COLUMNS)
        if c:
            out["ranking_type"] = out[c]
        else:
            out["ranking_type"] = _rank_key_text(rank_key)

    rank_text = _rank_key_text(rank_key)
    try:
        if "ranking_type" in out.columns:
            first_type = str(out["ranking_type"].dropna().astype(str).head(1).iloc[0]) if len(out["ranking_type"].dropna()) else ""
            rank_text = f"{rank_text}_{first_type}"
    except Exception:
        pass
    out["rank_kind"] = _detect_rank_kind(rank_text)

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
    if "change_percentage" not in out.columns:
        c = first_existing_col(out, CHANGE_COLUMNS)
        if c:
            out["change_percentage"] = out[c]
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
    out["change_percentage"] = safe_numeric_series(out, "change_percentage")
    return out


def _today_filter(ndf: pd.DataFrame, *, today_only: bool, now: Optional[dt.datetime]) -> pd.DataFrame:
    if ndf.empty:
        return ndf
    if not today_only:
        return ndf
    today = today_date_str(now)
    if "date" in ndf.columns:
        return ndf[ndf["date"].astype(str) == today].copy()
    return ndf


def _price_band_filter(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or "current_price" not in df.columns:
        return df
    min_price = float(MIN_PRICE or 0.0)
    max_price = float(MAX_PRICE or 0.0)
    out = df.copy()
    if min_price > 0:
        out = out[(out["current_price"] <= 0) | (out["current_price"] >= min_price)].copy()
    if max_price > 0:
        out = out[(out["current_price"] <= 0) | (out["current_price"] <= max_price)].copy()
    return out


def _ranking_frames_by_kind(*, today_only: bool = True, now: Optional[dt.datetime] = None) -> Dict[str, List[pd.DataFrame]]:
    frames: Dict[str, List[pd.DataFrame]] = {
        "gainers": [],
        "losers": [],
        "value": [],
        "tick": [],
        "volume_speed": [],
        "other": [],
    }
    for key, df in get_latest_ranking_dict().items():
        try:
            ndf = normalize_ranking_df(df, rank_key=key)
            ndf = _today_filter(ndf, today_only=today_only, now=now)
            ndf = _price_band_filter(ndf)
            if ndf.empty:
                continue
            kind = str(ndf["rank_kind"].dropna().astype(str).head(1).iloc[0]) if "rank_kind" in ndf.columns else _detect_rank_kind(_rank_key_text(key))
            if kind not in frames:
                kind = "other"
            frames[kind].append(ndf)
        except Exception:
            logger.debug("[ACTIVE] ranking df normalize failed", exc_info=True)
    return frames


def _concat_kind(frames: Dict[str, List[pd.DataFrame]], kind: str) -> pd.DataFrame:
    xs = frames.get(kind, []) or []
    if not xs:
        return pd.DataFrame()
    try:
        return pd.concat(xs, ignore_index=True)
    except Exception:
        return pd.DataFrame()


def _pick_symbols(df: pd.DataFrame, *, n: int, sort_col: str, ascending: bool = False) -> List[str]:
    if df is None or df.empty or "symbol" not in df.columns:
        return []
    out = df.copy()
    if sort_col not in out.columns:
        out[sort_col] = 0.0
    try:
        out = out.sort_values(sort_col, ascending=ascending)
    except Exception:
        pass
    return dedupe_keep_order(out["symbol"].astype(str).head(max(0, int(n))).tolist())


def _append_unique(dst: List[str], src: List[str], *, max_total: int = TARGET_ACTIVE_SYMBOLS) -> None:
    seen = set(dst)
    for s in src:
        ns = normalize_symbol(s)
        if not ns or ns in seen:
            continue
        dst.append(ns)
        seen.add(ns)
        if len(dst) >= max_total:
            break


def category_quota_ranking_symbols(now: Optional[dt.datetime] = None) -> List[str]:
    """ユーザー指定のカテゴリ配分で今日ランキングから監視候補を作る。"""
    frames = _ranking_frames_by_kind(today_only=True, now=now)
    gainers = _concat_kind(frames, "gainers")
    losers = _concat_kind(frames, "losers")
    value = _concat_kind(frames, "value")
    tick = _concat_kind(frames, "tick")
    if tick.empty:
        tick = _concat_kind(frames, "volume_speed")

    selected: List[str] = []

    # 1. 値上がり率ランキングで売買代金上位30銘柄
    _append_unique(selected, _pick_symbols(gainers, n=ACTIVE_GAINERS_BY_VALUE_N, sort_col="trading_value", ascending=False))

    # 2. 値下がり率ランキングで売買代金上位10銘柄
    _append_unique(selected, _pick_symbols(losers, n=ACTIVE_LOSERS_BY_VALUE_N, sort_col="trading_value", ascending=False))

    # 3. 売買代金ランキングで上昇率上位30銘柄
    if not value.empty:
        v_up = value[value.get("change_percentage", 0) >= 0].copy() if "change_percentage" in value.columns else value.copy()
    else:
        v_up = pd.DataFrame()
    _append_unique(selected, _pick_symbols(v_up, n=ACTIVE_VALUE_BY_GAINERS_N, sort_col="change_percentage", ascending=False))

    # 4. 売買代金ランキングで下落率上位10銘柄
    if not value.empty:
        v_down = value[value.get("change_percentage", 0) < 0].copy() if "change_percentage" in value.columns else value.copy()
        try:
            v_down["_abs_change"] = v_down["change_percentage"].abs()
        except Exception:
            v_down["_abs_change"] = 0.0
    else:
        v_down = pd.DataFrame()
    _append_unique(selected, _pick_symbols(v_down, n=ACTIVE_VALUE_BY_LOSERS_N, sort_col="_abs_change", ascending=False))

    # 5. 残りはTICK上位で100まで補充
    tick_pick = _pick_symbols(tick, n=ACTIVE_TICK_SUPPLEMENT_N, sort_col="tick_count", ascending=False)
    _append_unique(selected, tick_pick, max_total=TARGET_ACTIVE_SYMBOLS)

    logger.warning(
        "[ACTIVE CATEGORY QUOTA] selected=%d gainers_by_value=%d losers_by_value=%d value_up=%d value_down=%d tick_supplement=%d min_price=%.1f max_price=%.1f head=%s",
        len(selected),
        ACTIVE_GAINERS_BY_VALUE_N,
        ACTIVE_LOSERS_BY_VALUE_N,
        ACTIVE_VALUE_BY_GAINERS_N,
        ACTIVE_VALUE_BY_LOSERS_N,
        ACTIVE_TICK_SUPPLEMENT_N,
        MIN_PRICE,
        MAX_PRICE,
        selected[:20],
    )
    return selected[:TARGET_ACTIVE_SYMBOLS]


def merged_latest_ranking_df(*, today_only: bool = True, now: Optional[dt.datetime] = None) -> pd.DataFrame:
    frames: List[pd.DataFrame] = []
    for key, df in get_latest_ranking_dict().items():
        try:
            ndf = normalize_ranking_df(df, rank_key=key)
            ndf = _today_filter(ndf, today_only=today_only, now=now)
            ndf = _price_band_filter(ndf)
            if not ndf.empty:
                frames.append(ndf)
        except Exception:
            logger.debug("[ACTIVE] ranking df normalize failed", exc_info=True)
    if not frames:
        return pd.DataFrame()
    merged = pd.concat(frames, ignore_index=True)
    if merged.empty or "symbol" not in merged.columns:
        return pd.DataFrame()
    agg = {
        "current_price": "max",
        "trading_volume": "max",
        "trading_value": "max",
        "tick_count": "max",
        "volume_speed": "max",
        "change_percentage": "max",
    }
    try:
        merged = merged.groupby("symbol", as_index=False).agg(agg).reset_index(drop=True)
    except Exception:
        merged = merged.drop_duplicates(subset=["symbol"], keep="first")
    return merged


def today_ranking_symbols(now: Optional[dt.datetime] = None) -> List[str]:
    if ACTIVE_USE_CATEGORY_QUOTA_SELECTION:
        symbols = category_quota_ranking_symbols(now=now)
        if symbols:
            return symbols
    df = merged_latest_ranking_df(today_only=True, now=now)
    if df.empty or "symbol" not in df.columns:
        return []
    return dedupe_keep_order(df["symbol"].tolist())


def today_ranking_available(now: Optional[dt.datetime] = None) -> bool:
    return len(today_ranking_symbols(now=now)) > 0


def update_last_seen_from_ranking(now: dt.datetime) -> None:
    if not hasattr(global_data, "symbol_last_seen"):
        global_data.symbol_last_seen = {}
    for key, df in get_latest_ranking_dict().items():
        if df is None or df.empty:
            continue
        ndf = normalize_ranking_df(df, rank_key=key)
        if ndf.empty or "symbol" not in ndf.columns:
            continue
        for sym in ndf["symbol"].dropna().astype(str):
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
            "change_percentage": to_float(row.get("change_percentage"), 0.0),
        }
    return out


def extract_volume_speed_symbols() -> Set[str]:
    symbols: Set[str] = set()
    for key, df in get_latest_ranking_dict().items():
        if df is None or df.empty:
            continue
        ndf = normalize_ranking_df(df, rank_key=key)
        if ndf.empty or "symbol" not in ndf.columns:
            continue
        sort_col = "volume_speed" if "volume_speed" in ndf.columns else "tick_count"
        try:
            df_sorted = ndf.sort_values(sort_col, ascending=False)
        except Exception:
            continue
        for sym in df_sorted["symbol"].head(VOLUME_SPEED_TOP_N).astype(str):
            ns = normalize_symbol(sym)
            if ns:
                symbols.add(ns)
    return symbols
