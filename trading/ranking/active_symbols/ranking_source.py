# ============================================================
# File   : trading/ranking/active_symbols/ranking_source.py
# Version: Ver1.2-ACTIVE-SYMBOLS-CATEGORY-QUOTA-DB-FALLBACK
# ------------------------------------------------------------
# 今日ランキングからの監視100銘柄候補をカテゴリ配分で作る。
#
# Ver1.2:
#   - push_receiver / summary_database は別プロセスのため、ranking_collector の
#     global_data.latest_ranking が共有されず today_ranking symbols=0 になる。
#   - global_data.latest_ranking が空の場合、rankingYYYYMMDD.db の
#     ranking_snapshot / ranking_snapshot_1min / ranking_raw から最新ランキングを読む。
# ============================================================
from __future__ import annotations

import datetime as dt
import logging
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import pandas as pd

from global_state import global_data
from .config import (
    ACTIVE_GAINERS_BY_VALUE_N,
    ACTIVE_GROWTH_GAINERS_N,
    ACTIVE_RANK_SLICE_N,
    ACTIVE_TICK_BY_GAINERS_N,
    ALL_MARKET_CODES,
    GROWTH_MARKET_CODES,
    MARKET_COLUMNS,
    RANK_COLUMNS,
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
from sqlite_cm import committing_connect

logger = logging.getLogger(__name__)
_DB_CACHE: dict[str, Any] = {"ts": 0.0, "data": {}}


def _rank_type_to_key(v: Any) -> str:
    s = str(v or "").strip()
    return s or "ranking"


def _read_latest_ranking_from_db(limit_per_type: int = 250) -> Dict[Any, pd.DataFrame]:
    """Load latest ranking frames from today's ranking DB when in-memory ranking is empty."""
    try:
        import time
        now_ts = time.time()
        cache_ttl = 10.0
        if isinstance(_DB_CACHE.get("data"), dict) and now_ts - float(_DB_CACHE.get("ts") or 0.0) < cache_ttl:
            data = _DB_CACHE.get("data") or {}
            if data:
                return data

        from database.paths.ranking_paths import get_ranking_db_path
        db_path = Path(get_ranking_db_path())
        if not db_path.exists():
            return {}

        tables: list[str] = []
        with committing_connect(str(db_path), timeout=3.0) as conn:
            conn.execute("PRAGMA query_only=ON")
            conn.execute("PRAGMA busy_timeout=3000")
            try:
                table_rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                names = {str(x[0]) for x in table_rows}
            except Exception:
                names = set()
            for t in ("ranking_snapshot", "ranking_snapshot_1min", "ranking_raw"):
                if t in names:
                    tables.append(t)
            if not tables:
                return {}

            frames: Dict[Any, pd.DataFrame] = {}
            for table in tables:
                try:
                    cols = [str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
                    if not cols or "symbol" not in cols:
                        continue
                    dt_col = "datetime" if "datetime" in cols else ("snapshot_time" if "snapshot_time" in cols else "inserted_at")
                    type_col = "ranking_type" if "ranking_type" in cols else ("rank_type" if "rank_type" in cols else "category")
                    price_col = "current_price" if "current_price" in cols else ("price" if "price" in cols else "0")
                    vol_col = "trading_volume" if "trading_volume" in cols else ("volume" if "volume" in cols else "0")
                    val_col = "trading_value" if "trading_value" in cols else ("turnover" if "turnover" in cols else "0")
                    tick_col = "tick_count" if "tick_count" in cols else "0"
                    change_col = "change_percentage" if "change_percentage" in cols else ("change_rate" if "change_rate" in cols else "0")
                    symbolname_col = "symbolname" if "symbolname" in cols else "''"
                    rank_col = "rank" if "rank" in cols else "0"
                    market_col = ("market" if "market" in cols
                                  else ("exchange" if "exchange" in cols else "''"))
                    where = f"date({dt_col}) = date('now','localtime')" if dt_col in cols else "1=1"
                    sql = f"""
                    SELECT symbol,
                           {symbolname_col} AS symbolname,
                           {dt_col} AS datetime,
                           {type_col} AS ranking_type,
                           {price_col} AS current_price,
                           {vol_col} AS trading_volume,
                           {val_col} AS trading_value,
                           {tick_col} AS tick_count,
                           {change_col} AS change_percentage,
                           {rank_col} AS rank,
                           {market_col} AS market
                    FROM {table}
                    WHERE {where}
                    ORDER BY {dt_col} DESC
                    LIMIT {int(limit_per_type) * 10}
                    """
                    df = pd.read_sql_query(sql, conn)
                    if df.empty:
                        continue
                    df["_table"] = table
                    df["ranking_type"] = df["ranking_type"].fillna("").astype(str)
                    for rtype, g in df.groupby("ranking_type", dropna=False):
                        key = _rank_type_to_key(rtype)
                        part = g.head(limit_per_type).copy()
                        if key in frames:
                            frames[key] = pd.concat([frames[key], part], ignore_index=True)
                        else:
                            frames[key] = part
                except Exception:
                    logger.debug("[ACTIVE DB FALLBACK] table read failed table=%s", table, exc_info=True)

        if frames:
            _DB_CACHE["ts"] = now_ts
            _DB_CACHE["data"] = frames
            logger.warning(
                "[ACTIVE DB FALLBACK] loaded ranking db frames=%s rows=%s db=%s keys=%s",
                len(frames),
                sum(len(x) for x in frames.values()),
                db_path,
                list(frames.keys())[:10],
            )
        return frames
    except Exception:
        logger.debug("[ACTIVE DB FALLBACK] load failed", exc_info=True)
        return {}


def get_latest_ranking_dict() -> Dict[Any, pd.DataFrame]:
    latest = getattr(global_data, "latest_ranking", None)
    if isinstance(latest, dict) and latest:
        return latest
    return _read_latest_ranking_from_db()


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

    # 2026-09-29: 設計の「各ランキング上位50位」と「グロース市場専用枠」を
    # 実装するため、順位と市場を落とさずに残す。
    if "rank" not in out.columns:
        c = first_existing_col(out, RANK_COLUMNS)
        if c:
            out["rank"] = out[c]
    out["rank"] = safe_numeric_series(out, "rank") if "rank" in out.columns else 0.0
    if "market" not in out.columns:
        c = first_existing_col(out, MARKET_COLUMNS)
        out["market"] = out[c].astype(str) if c else ""
    else:
        out["market"] = out["market"].astype(str)
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
    frames: Dict[str, List[pd.DataFrame]] = {"gainers": [], "losers": [], "value": [], "tick": [], "volume_speed": [], "other": []}
    raw = get_latest_ranking_dict()
    for key, df in raw.items():
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
    if raw and not any(frames.values()):
        logger.warning("[ACTIVE CATEGORY QUOTA] ranking frames all empty after normalize/filter raw_keys=%s today_only=%s", list(raw.keys())[:10], today_only)
    return frames


def _concat_kind(frames: Dict[str, List[pd.DataFrame]], kind: str) -> pd.DataFrame:
    xs = frames.get(kind, []) or []
    if not xs:
        return pd.DataFrame()
    try:
        return pd.concat(xs, ignore_index=True)
    except Exception:
        return pd.DataFrame()


def _top_rank(df: pd.DataFrame, n: int, *, metric_col: str, ascending: bool = False) -> pd.DataFrame:
    """そのランキングの上位 n 位だけに絞る (設計の「上位50位」)。

    rank 列が使えるならそれで切る。NULL/0 ばかりの場合は、そのランキングの
    並び順を決めている指標 (値上がり率なら change_percentage、売買代金なら
    trading_value、TICK なら tick_count) の上位 n 件で代用する。
    ranking_summary_* は rank が NULL になることがあるため代用経路が必要。
    """
    if df is None or df.empty or n <= 0:
        return df if df is not None else pd.DataFrame()
    out = df.copy()
    if "rank" in out.columns:
        r = pd.to_numeric(out["rank"], errors="coerce")
        usable = r.notna() & (r >= 1)
        if int(usable.sum()) >= max(1, int(0.5 * len(out))):
            # 半分以上で順位が取れるならそれを使う。取れない行は落とす。
            return out.loc[usable & (r <= float(n))].copy()
    if metric_col in out.columns:
        try:
            out = out.sort_values(metric_col, ascending=ascending)
        except Exception:
            pass
    return out.head(int(n)).copy()


def _by_market(df: pd.DataFrame, codes) -> pd.DataFrame:
    """market / exchange がこの集合のものだけ。市場列が無ければそのまま返す。"""
    if df is None or df.empty or "market" not in df.columns:
        return df if df is not None else pd.DataFrame()
    want = {str(x).strip().upper() for x in (codes or ())}
    if not want:
        return df
    m = df["market"].astype(str).str.strip().str.upper()
    out = df.loc[m.isin(want)].copy()
    return out


def _split_up_down(value: pd.DataFrame):
    """売買代金ランキングを上昇組 / 下落組に分ける。"""
    if value is None or value.empty or "change_percentage" not in value.columns:
        return pd.DataFrame(), pd.DataFrame()
    ch = pd.to_numeric(value["change_percentage"], errors="coerce").fillna(0.0)
    up = value.loc[ch >= 0].copy()
    down = value.loc[ch < 0].copy()
    if not down.empty:
        down["_abs_change"] = pd.to_numeric(
            down["change_percentage"], errors="coerce").abs().fillna(0.0)
    return up, down


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
    """設計どおりに監視母集団を組む (2026-09-29 修正)。

    全市場・値上がり率 上位50 -> 売買代金 上位35
    全市場・値下がり率 上位50 -> 売買代金 上位25
    全市場・売買代金   上位50 -> 上昇率 上位30 / 下落率 上位20
    全市場・TICK回数   上位50 -> 上昇率 上位30
    グロース・値上がり率 上位30
    -> 重複排除して最大 TARGET_ACTIVE_SYMBOLS 銘柄

    ACTIVE_RANK_SLICE_N=0 にすると上位スライスを掛けない従来動作に戻る。
    """
    frames = _ranking_frames_by_kind(today_only=True, now=now)
    gainers_all_raw = _concat_kind(frames, "gainers")
    losers_raw = _concat_kind(frames, "losers")
    value_raw = _concat_kind(frames, "value")
    tick_raw = _concat_kind(frames, "tick")
    if tick_raw.empty:
        tick_raw = _concat_kind(frames, "volume_speed")

    slice_n = int(ACTIVE_RANK_SLICE_N or 0)

    # --- 全市場分 ---------------------------------------------------
    # 市場列で ALL を選べるならそれを使う。使えない (列が無い / ALL が無い)
    # ときは全部をそのまま母集団にする。
    def _all_market(df: pd.DataFrame) -> pd.DataFrame:
        picked = _by_market(df, ALL_MARKET_CODES)
        return picked if not picked.empty else df

    gainers = _top_rank(_all_market(gainers_all_raw), slice_n,
                        metric_col="change_percentage", ascending=False)
    losers = _top_rank(_all_market(losers_raw), slice_n,
                       metric_col="change_percentage", ascending=True)
    value = _top_rank(_all_market(value_raw), slice_n,
                      metric_col="trading_value", ascending=False)
    tick = _top_rank(_all_market(tick_raw), slice_n,
                     metric_col="tick_count", ascending=False)

    v_up, v_down = _split_up_down(value)

    # --- グロース市場・値上がり率 -----------------------------------
    growth = _by_market(gainers_all_raw, GROWTH_MARKET_CODES)
    growth_pick = _pick_symbols(
        _top_rank(growth, ACTIVE_GROWTH_GAINERS_N,
                  metric_col="change_percentage", ascending=False),
        n=ACTIVE_GROWTH_GAINERS_N, sort_col="change_percentage", ascending=False)

    selected: List[str] = []
    # 1. 値上がり率上位50 の中から売買代金上位35
    _append_unique(selected, _pick_symbols(
        gainers, n=ACTIVE_GAINERS_BY_VALUE_N, sort_col="trading_value", ascending=False))
    # 2. 値下がり率上位50 の中から売買代金上位25
    _append_unique(selected, _pick_symbols(
        losers, n=ACTIVE_LOSERS_BY_VALUE_N, sort_col="trading_value", ascending=False))
    # 3. 売買代金上位50 の中から上昇率上位30
    _append_unique(selected, _pick_symbols(
        v_up, n=ACTIVE_VALUE_BY_GAINERS_N, sort_col="change_percentage", ascending=False))
    # 4. 売買代金上位50 の中から下落率上位20
    _append_unique(selected, _pick_symbols(
        v_down, n=ACTIVE_VALUE_BY_LOSERS_N, sort_col="_abs_change", ascending=False))
    # 5. TICK上位50 の中から上昇率上位30
    _append_unique(selected, _pick_symbols(
        tick, n=ACTIVE_TICK_BY_GAINERS_N, sort_col="change_percentage", ascending=False))
    # 6. グロース市場・値上がり率上位30
    _append_unique(selected, growth_pick, max_total=TARGET_ACTIVE_SYMBOLS)

    logger.warning(
        "[ACTIVE CATEGORY QUOTA] selected=%d slice_n=%d "
        "pool(all)=gainers %d->%d losers %d->%d value %d->%d tick %d->%d "
        "growth=%d quota=%d/%d/%d/%d/%d/%d min_price=%.1f max_price=%.1f head=%s",
        len(selected), slice_n,
        len(gainers_all_raw), len(gainers), len(losers_raw), len(losers),
        len(value_raw), len(value), len(tick_raw), len(tick),
        len(growth),
        ACTIVE_GAINERS_BY_VALUE_N, ACTIVE_LOSERS_BY_VALUE_N,
        ACTIVE_VALUE_BY_GAINERS_N, ACTIVE_VALUE_BY_LOSERS_N,
        ACTIVE_TICK_BY_GAINERS_N, ACTIVE_GROWTH_GAINERS_N,
        MIN_PRICE, MAX_PRICE, selected[:20],
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
    agg = {"current_price": "max", "trading_volume": "max", "trading_value": "max", "tick_count": "max", "volume_speed": "max", "change_percentage": "max"}
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
