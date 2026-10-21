# ============================================================
# File   : trading/push/subscription_manager/ranking_selector.py
# Version: V2.1-PUSH-RANKING-SELECTOR-FRESHNESS-LIQUIDITY-SAFE
# ------------------------------------------------------------
# Purpose:
#   kabu Station PUSH登録用のランキング由来銘柄を最大100銘柄へ整理する。
#
# V2.1 Fix:
#   - freshness を DB内最新時刻基準ではなく、現在時刻基準でも判定する。
#     09:39時点で最新snapshot=09:09でも fresh 扱いになる問題を防ぐ。
#   - liquidity filter が0件になったとき、完全 unfiltered へ戻さない。
#     段階的に条件を緩めるが、最低限の価格/出来高/売買代金ガードは残す。
#   - それでも0件なら、PUSH登録候補は空にして低流動性銘柄を監視対象へ戻さない。
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
from nas_root import nas_path, nas_root

logger = logging.getLogger(__name__)

DEFAULT_NAS_ROOT = nas_root()
TABLE_CANDIDATES = ("ranking_snapshot_1min", "ranking_raw_1min", "ranking")

GAINER_TOP_N = 50
GAINER_BY_VALUE_N = 35
LOSER_TOP_N = 50
LOSER_BY_VALUE_N = 25
VALUE_TOP_N = 50
VALUE_UP_RATE_N = 30
VALUE_DOWN_RATE_N = 20
GROWTH_TOP_N = 50
GROWTH_BY_VALUE_N = 15
STANDARD_TOP_N = 50
STANDARD_BY_VALUE_N = 15
TICK_TOP_N = 50
DEFAULT_MAX_SYMBOLS = 100
DEFAULT_REGISTER_LIMIT = 50

DEFAULT_FRESH_MINUTES = float(os.environ.get("PUSH_RANKING_SELECTOR_FRESH_MINUTES", "5"))
MAX_DB_STALE_MINUTES = float(os.environ.get("PUSH_RANKING_SELECTOR_MAX_DB_STALE_MINUTES", "12"))
MIN_PRICE = float(os.environ.get("PUSH_RANKING_SELECTOR_MIN_PRICE", "300"))
MIN_TRADING_VALUE = float(os.environ.get("PUSH_RANKING_SELECTOR_MIN_TRADING_VALUE", "30000000"))
MIN_VOLUME = float(os.environ.get("PUSH_RANKING_SELECTOR_MIN_VOLUME", "10000"))
MIN_TICK_COUNT = float(os.environ.get("PUSH_RANKING_SELECTOR_MIN_TICK_COUNT", "0"))
SQLITE_TIMEOUT_SEC = float(os.environ.get("PUSH_RANKING_SELECTOR_SQLITE_TIMEOUT_SEC", "5"))

# liquidity が厳しすぎる時の段階緩和。完全unfilteredには戻さない。
RELAXED_LIQUIDITY_STEPS: tuple[tuple[float, float, float, float, str], ...] = (
    (MIN_PRICE, MIN_TRADING_VALUE, MIN_VOLUME, MIN_TICK_COUNT, "strict_config"),
    (MIN_PRICE, max(MIN_TRADING_VALUE * 0.50, 10_000_000.0), max(MIN_VOLUME * 0.70, 7_000.0), MIN_TICK_COUNT, "relaxed_50pct_value"),
    (MIN_PRICE, max(MIN_TRADING_VALUE * 0.25, 5_000_000.0), max(MIN_VOLUME * 0.50, 5_000.0), 0.0, "relaxed_25pct_value"),
)


def _today_yyyymmdd() -> str:
    return dt.datetime.now().strftime("%Y%m%d")


def _normalize_yyyymmdd(value: Any = None) -> str:
    if value is None:
        return _today_yyyymmdd()
    if isinstance(value, dt.datetime):
        return value.strftime("%Y%m%d")
    if isinstance(value, dt.date):
        return value.strftime("%Y%m%d")
    s = str(value).strip().replace("-", "").replace("/", "")
    return s[:8] if len(s) >= 8 and s[:8].isdigit() else _today_yyyymmdd()


def _get_nas_root() -> str:
    for key in ("NAS_ROOT", "AUTOSTOCK_NAS_ROOT", "KABU_NAS_ROOT"):
        v = os.environ.get(key, "").strip()
        if v:
            return v.rstrip("\\/")
    return DEFAULT_NAS_ROOT


def _default_ranking_db_path(yyyymmdd: Any = None) -> str:
    d = _normalize_yyyymmdd(yyyymmdd)
    # 2026-09-07: ranking DB はローカルへ移した。ここを NAS 固定にすると
    # この経路だけ複製 (最大で複製間隔ぶん古い) を読むことになる。
    try:
        from trading.ranking import ranking_db_location

        return str(ranking_db_location.ranking_db_path(_as_date(d)))
    except Exception:
        pass
    return str(Path(_get_nas_root()) / "raw_data" / "kabu_station" / "ranking" / f"ranking{d}.db")


def _as_date(yyyymmdd: str):
    import datetime as _dt

    try:
        return _dt.datetime.strptime(str(yyyymmdd), "%Y%m%d").date()
    except Exception:
        return None


def _resolve_ranking_db_path(db_path: Optional[str | os.PathLike] = None, yyyymmdd: Any = None) -> Optional[str]:
    if db_path:
        p = str(db_path)
        if os.path.exists(p):
            return p
        logger.warning("[PUSH RANKING SELECTOR] explicit db_path not found path=%s", p)
        return p
    try:
        from ats.ats_ranking import get_usable_ranking_db_path  # type: ignore
        p = get_usable_ranking_db_path(force_refresh=False)
        if p and os.path.exists(str(p)):
            return str(p)
    except Exception:
        pass
    return _default_ranking_db_path(yyyymmdd)


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=SQLITE_TIMEOUT_SEC)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
    except Exception:
        pass
    return conn


def _list_tables(conn: sqlite3.Connection) -> list[str]:
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        return [str(r[0]) for r in rows]
    except Exception:
        return []


def _read_table(conn: sqlite3.Connection, table_name: str) -> pd.DataFrame:
    try:
        return pd.read_sql_query(f'SELECT * FROM "{table_name}"', conn)
    except Exception:
        logger.debug("[PUSH RANKING SELECTOR] read table failed table=%s", table_name, exc_info=True)
        return pd.DataFrame()


def _normalize_symbol(value: Any) -> str:
    if value is None:
        return ""
    s = str(value).strip()
    if not s:
        return ""
    if "." in s and s.upper().endswith(".T"):
        s = s.rsplit(".", 1)[0]
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]
    return s.strip()


def _dedupe_keep_order(items: Iterable[Any]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        s = _normalize_symbol(item)
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _first_col(df: pd.DataFrame, candidates: Iterable[str]) -> Optional[str]:
    if df is None or df.empty:
        return None
    cols = list(df.columns)
    lower_map = {str(c).lower(): c for c in cols}
    for c in candidates:
        if c in cols:
            return c
        lc = str(c).lower()
        if lc in lower_map:
            return lower_map[lc]
    return None


def _to_num(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce")


def _safe_str_series(s: pd.Series) -> pd.Series:
    return s.astype(str).fillna("").str.strip()


def _parse_dt_series(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, errors="coerce")


def _normalize_df(df: pd.DataFrame, *, table_name: str = "") -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    symbol_col = _first_col(out, ("symbol", "Symbol", "code", "Code", "銘柄コード", "銘柄"))
    if not symbol_col:
        logger.warning("[PUSH RANKING SELECTOR] symbol column not found table=%s columns=%s", table_name, list(out.columns))
        return pd.DataFrame()
    out["symbol"] = out[symbol_col].map(_normalize_symbol)
    out = out[out["symbol"].astype(str).str.len() > 0].copy()

    name_col = _first_col(out, ("symbolname", "symbol_name", "name", "Name", "銘柄名", "銘柄名称"))
    out["symbolname"] = _safe_str_series(out[name_col]) if name_col else ""
    category_col = _first_col(out, ("category", "ranking_category", "ranking_type", "rank_type", "type", "Type", "ranking_name", "ランキング種別", "ランキング名", "種別", "table_name"))
    out["category"] = _safe_str_series(out[category_col]) if category_col else ""
    market_col = _first_col(out, ("market", "Market", "exchange", "Exchange", "exchange_division", "division", "market_name", "市場", "市場区分", "市場名"))
    out["market"] = _safe_str_series(out[market_col]) if market_col else ""
    rank_col = _first_col(out, ("rank", "rank_no", "ranking", "順位"))
    out["rank_no"] = _to_num(out[rank_col]) if rank_col else pd.NA
    price_col = _first_col(out, ("current_price", "price", "Price", "close", "close_price", "現在値", "株価"))
    out["current_price"] = _to_num(out[price_col]) if price_col else pd.NA
    rate_col = _first_col(out, ("change_rate", "change_percentage", "change_ratio", "rate", "change_percent", "change_pct", "price_change_rate", "rise_rate", "fall_rate", "騰落率", "上昇率", "下落率", "値上がり率", "値下がり率"))
    out["change_rate"] = _to_num(out[rate_col]) if rate_col else pd.NA
    value_col = _first_col(out, ("trading_value", "turnover", "amount", "売買代金", "売買金額", "代金"))
    out["trading_value"] = _to_num(out[value_col]) if value_col else pd.NA
    volume_col = _first_col(out, ("volume", "trading_volume", "出来高", "売買高"))
    out["volume"] = _to_num(out[volume_col]) if volume_col else pd.NA
    tick_col = _first_col(out, ("tick_count", "ticks", "tick", "TickCount", "TICK回数", "ティック回数", "約定回数"))
    out["tick_count"] = _to_num(out[tick_col]) if tick_col else pd.NA
    time_col = _first_col(out, ("snapshot_time", "datetime", "created_at", "updated_at", "inserted_at", "time", "取得時刻", "時刻"))
    out["snapshot_time"] = _parse_dt_series(out[time_col]) if time_col else pd.NaT

    try:
        cat = out["category"].astype(str)
        is_down_cat = cat.str.contains("値下がり|下落|loser|fall|down", case=False, regex=True, na=False)
        mask = is_down_cat & out["change_rate"].notna() & (out["change_rate"] > 0)
        out.loc[mask, "change_rate"] = -out.loc[mask, "change_rate"].abs()
    except Exception:
        pass
    return out


def _align_all_na_dtypes_for_concat(frames: list) -> list:
    """concat 前に all-NA 列の dtype を揃える。

    trading/push/push_stream/dataframe._align_all_na_dtypes() の考え方を
    複数フレーム向けに一般化したもの。

    - object dtype の全 NA 列を float64 へ明示する (実測でこれだけで警告が消える)
    - int64/bool 列は NaN を入れられないので触らない
    - float64 は pandas が将来この連結で採用する型そのもの
    値は全て NA のままなので、結果の値と列集合は変わらない。
    """
    out = []
    for f in frames:
        try:
            cast = {}
            for c in f.columns:
                col = f[c]
                # object dtype の全 NA 列だけを float64 へ。
                # int64/bool 列を astype しようとすると NaN が入らず例外になるので触らない。
                if col.dtype == object and col.isna().all():
                    cast[c] = "float64"
            out.append(f.astype(cast) if cast else f)
        except Exception:
            logger.debug("[PUSH RANKING SELECTOR] all-NA dtype align failed for one frame", exc_info=True)
            out.append(f)
    return out


def _load_ranking_df_core(*, db_path: Optional[str | os.PathLike] = None, yyyymmdd: Any = None) -> pd.DataFrame:
    path = _resolve_ranking_db_path(db_path=db_path, yyyymmdd=yyyymmdd)
    if not path:
        logger.warning("[PUSH RANKING SELECTOR] ranking db path unresolved")
        return pd.DataFrame()
    if not os.path.exists(str(path)):
        logger.warning("[PUSH RANKING SELECTOR] ranking db not found path=%s", path)
        return pd.DataFrame()
    conn: Optional[sqlite3.Connection] = None
    try:
        conn = _connect(str(path))
        tables = _list_tables(conn)
        frames: list[pd.DataFrame] = []
        for table in TABLE_CANDIDATES:
            if table not in tables:
                continue
            df = _read_table(conn, table)
            if df.empty:
                continue
            ndf = _normalize_df(df, table_name=table)
            if ndf.empty:
                continue
            ndf["_source_table"] = table
            frames.append(ndf)
            logger.info("[PUSH RANKING SELECTOR] loaded table=%s rows=%d path=%s", table, len(ndf), path)
        # 全 NA のフレームを含むと
        #   FutureWarning: The behavior of DataFrame concatenation with empty or all-NA entries
        # になる。使えるデータが無いフレームだけ除く (情報は落ちない)。
        frames = [f for f in frames if isinstance(f, pd.DataFrame) and not f.empty and not f.isna().all().all()]
        if not frames:
            logger.warning("[PUSH RANKING SELECTOR] no usable ranking tables path=%s tables=%s", path, tables)
            return pd.DataFrame()
        # ranking テーブルごとに来る列が違うため、あるフレームでだけ
        # 全 NA になる列ができる。そのまま concat すると dtype 判定が
        # 将来変わるため、先に dtype を明示しておく。
        frames = _align_all_na_dtypes_for_concat(frames)
        all_df = pd.concat(frames, ignore_index=True, sort=False)
        all_df = all_df[all_df["symbol"].astype(str).str.len() > 0].copy()
        logger.info("[PUSH RANKING SELECTOR] loaded total rows=%d symbols=%d path=%s", len(all_df), all_df["symbol"].nunique(), path)
        return all_df
    except sqlite3.OperationalError:
        logger.exception("[PUSH RANKING SELECTOR] ranking db operational error path=%s", path)
        return pd.DataFrame()
    except Exception:
        logger.exception("[PUSH RANKING SELECTOR] ranking db load failed path=%s", path)
        return pd.DataFrame()
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass


def _apply_freshness(df: pd.DataFrame, fresh_minutes: float = DEFAULT_FRESH_MINUTES) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    if "snapshot_time" not in df.columns or df["snapshot_time"].notna().sum() <= 0:
        return df
    latest = df["snapshot_time"].max()
    if pd.isna(latest):
        return df
    now = pd.Timestamp(dt.datetime.now())
    latest_age_min = (now - latest).total_seconds() / 60.0
    if latest_age_min > MAX_DB_STALE_MINUTES:
        logger.warning(
            "[PUSH RANKING SELECTOR] ranking db stale -> return empty latest=%s now=%s age_min=%.1f max_age=%.1f before=%d",
            latest, now, latest_age_min, MAX_DB_STALE_MINUTES, len(df),
        )
        return df.iloc[0:0].copy()
    cutoff = max(latest - pd.Timedelta(minutes=max(0.0, float(fresh_minutes))), now - pd.Timedelta(minutes=max(0.0, float(fresh_minutes))))
    out = df[(df["snapshot_time"].isna()) | (df["snapshot_time"] >= cutoff)].copy()
    logger.info("[PUSH RANKING SELECTOR] freshness filter latest=%s now=%s cutoff=%s before=%d after=%d age_min=%.1f", latest, now, cutoff, len(df), len(out), latest_age_min)
    return out


def _apply_min_liquidity_core(df: pd.DataFrame, *, min_price: float = MIN_PRICE, min_trading_value: float = MIN_TRADING_VALUE, min_volume: float = MIN_VOLUME, min_tick_count: float = MIN_TICK_COUNT, strict: bool = False) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    before = len(out)
    def apply_cond(col: str, min_value: float) -> None:
        nonlocal out
        if min_value <= 0:
            return
        if col not in out.columns or out[col].notna().sum() <= 0:
            if strict:
                out = out.iloc[0:0].copy()
            return
        out = out[(out[col].isna()) | (out[col] >= float(min_value))].copy()
    apply_cond("current_price", min_price)
    apply_cond("trading_value", min_trading_value)
    apply_cond("volume", min_volume)
    apply_cond("tick_count", min_tick_count)
    logger.info("[PUSH RANKING SELECTOR] liquidity filter before=%d after=%d min_price=%.1f min_value=%.1f min_volume=%.1f min_tick=%.1f strict=%s", before, len(out), min_price, min_trading_value, min_volume, min_tick_count, strict)
    return out


# ============================================================
# 流動性フィルタが全件 0 件になったときの段階緩和
#   旧 core/startup/liquidity_empty_fallback_patch.py (V1)
# ------------------------------------------------------------
# 代金列の単位違いや未補完で after=0 になり、
#   [PUSH RANKING SELECTOR] liquidity filter before=2753 after=0
# のまま候補が消えるのを防ぐ。完全な unfiltered には落とさず、
# turnover_5m -> turnover_1m -> price+volume -> price のみ、と段階的に緩める。
#
# 旧パッチは ENABLE_LIQ_EMPTY_FALLBACK_PATCH が真のときだけ install される
# opt-in (既定 OFF) だった。そのスコープを _liq_empty_fallback_enabled() で再現する。
# ============================================================
_LIQ_RELAX_STEPS = (
    ("turnover_5m", 5_000_000.0, None, None),
    ("turnover_1m", 1_000_000.0, None, 0.0),
    ("price_volume_only", 0.0, None, 0.0),
    ("price_only", 0.0, 0.0, 0.0),
)


def _liq_empty_fallback_enabled() -> bool:
    """旧 sitecustomize._install_liq_empty_fallback_only_if_enabled() の判定を再現する。

    ENABLE_LIQ_EMPTY_FALLBACK_PATCH が真のときだけ install されていた (既定 OFF)。
    無効時は sitecustomize が DISABLE_LIQ_EMPTY_FALLBACK_PATCH=1 を setdefault する。
    """
    try:
        if str(os.environ.get("DISABLE_LIQ_EMPTY_FALLBACK_PATCH", "")).strip() == "1":
            return False
        v = os.environ.get("ENABLE_LIQ_EMPTY_FALLBACK_PATCH")
        if v is None or str(v).strip() == "":
            return False
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return False


def _apply_min_liquidity(
    df: pd.DataFrame,
    *,
    min_price: float = None,
    min_trading_value: float = None,
    min_volume: float = None,
    min_tick_count: float = None,
    strict: bool = False,
) -> pd.DataFrame:
    # 既定値はモジュール定数を「呼び出し時に」読む。
    # (旧パッチが getattr(rs, "MIN_PRICE", ...) で解決していた挙動に合わせる)
    if min_price is None:
        min_price = float(MIN_PRICE)
    if min_trading_value is None:
        min_trading_value = float(MIN_TRADING_VALUE)
    if min_volume is None:
        min_volume = float(MIN_VOLUME)
    if min_tick_count is None:
        min_tick_count = float(MIN_TICK_COUNT)

    before = 0 if df is None else len(df)
    out = _apply_min_liquidity_core(
        df,
        min_price=min_price,
        min_trading_value=min_trading_value,
        min_volume=min_volume,
        min_tick_count=min_tick_count,
        strict=strict,
    )
    if not _liq_empty_fallback_enabled():
        return out
    if out is not None and not out.empty:
        return out
    if df is None or getattr(df, "empty", True):
        return pd.DataFrame()

    for label, tv, vol, tick in _LIQ_RELAX_STEPS:
        vol = min_volume if vol is None else vol
        tick = min_tick_count if tick is None else tick
        try:
            cand = _apply_min_liquidity_core(
                df,
                min_price=min_price,
                min_trading_value=tv,
                min_volume=vol,
                min_tick_count=tick,
                strict=False,
            )
        except Exception:
            logger.debug("[LIQ EMPTY FALLBACK] selector relax failed step=%s", label, exc_info=True)
            continue
        if cand is not None and not cand.empty:
            logger.warning(
                "[LIQ EMPTY FALLBACK] PUSH selector liquidity relaxed step=%s before=%d after=%d original_min_value=%.1f min_price=%.1f min_volume=%.1f",
                label, before, len(cand), float(min_trading_value), float(min_price), float(vol),
            )
            return cand

    logger.warning("[LIQ EMPTY FALLBACK] PUSH selector liquidity still empty before=%d -> keep original empty", before)
    return out


def _apply_liquidity_safe(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    for min_price, min_value, min_volume, min_tick, label in RELAXED_LIQUIDITY_STEPS:
        out = _apply_min_liquidity(df, min_price=min_price, min_trading_value=min_value, min_volume=min_volume, min_tick_count=min_tick, strict=False)
        if not out.empty:
            logger.info("[PUSH RANKING SELECTOR] liquidity accepted step=%s rows=%d", label, len(out))
            return out
        logger.warning("[PUSH RANKING SELECTOR] liquidity step empty step=%s", label)
    logger.warning("[PUSH RANKING SELECTOR] liquidity filter empty -> no unfiltered fallback; return empty")
    return df.iloc[0:0].copy()


def _latest_per_symbol(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    if "snapshot_time" in out.columns and out["snapshot_time"].notna().sum() > 0:
        out = out.sort_values(["symbol", "snapshot_time"], ascending=[True, False], kind="mergesort")
        out = out.drop_duplicates(subset=["symbol"], keep="first")
    else:
        out = out.drop_duplicates(subset=["symbol"], keep="first")
    return out.reset_index(drop=True)


def _category_contains(df: pd.DataFrame, patterns: str) -> pd.Series:
    if df is None or df.empty or "category" not in df.columns:
        return pd.Series([False] * (0 if df is None else len(df)), index=None if df is None else df.index)
    return df["category"].astype(str).str.contains(patterns, case=False, regex=True, na=False)


def _market_contains(df: pd.DataFrame, patterns: str) -> pd.Series:
    if df is None or df.empty or "market" not in df.columns:
        return pd.Series([False] * (0 if df is None else len(df)), index=None if df is None else df.index)
    return df["market"].astype(str).str.contains(patterns, case=False, regex=True, na=False)


def _is_gainer_category(df: pd.DataFrame) -> pd.Series:
    return _category_contains(df, "値上がり|上昇|gainer|rise|up")


def _is_loser_category(df: pd.DataFrame) -> pd.Series:
    return _category_contains(df, "値下がり|下落|loser|fall|down")


def _is_value_category(df: pd.DataFrame) -> pd.Series:
    return _category_contains(df, "売買代金|売買金額|代金|turnover|value|amount")


def _is_tick_category(df: pd.DataFrame) -> pd.Series:
    return _category_contains(df, "TICK|ティック|約定回数|tick")


def _is_growth_market(df: pd.DataFrame) -> pd.Series:
    return _market_contains(df, "グロース|growth|TG")


def _is_standard_market(df: pd.DataFrame) -> pd.Series:
    return _market_contains(df, "スタンダード|standard|TS")


def _sort_by_trading_value(df: pd.DataFrame, ascending: bool = False) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    if "trading_value" in out.columns and out["trading_value"].notna().sum() > 0:
        return out.sort_values("trading_value", ascending=ascending, kind="mergesort").copy()
    if "volume" in out.columns and out["volume"].notna().sum() > 0:
        return out.sort_values("volume", ascending=ascending, kind="mergesort").copy()
    return out


def _sort_by_change_rate_up(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    if "change_rate" in out.columns and out["change_rate"].notna().sum() > 0:
        return out.sort_values("change_rate", ascending=False, kind="mergesort").copy()
    if "rank_no" in out.columns and out["rank_no"].notna().sum() > 0:
        return out.sort_values("rank_no", ascending=True, kind="mergesort").copy()
    return out


def _sort_by_change_rate_down(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    if "change_rate" in out.columns and out["change_rate"].notna().sum() > 0:
        return out.sort_values("change_rate", ascending=True, kind="mergesort").copy()
    if "rank_no" in out.columns and out["rank_no"].notna().sum() > 0:
        return out.sort_values("rank_no", ascending=True, kind="mergesort").copy()
    return out


def _sort_by_tick_count(df: pd.DataFrame, ascending: bool = False) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    if "tick_count" in out.columns and out["tick_count"].notna().sum() > 0:
        return out.sort_values("tick_count", ascending=ascending, kind="mergesort").copy()
    if "rank_no" in out.columns and out["rank_no"].notna().sum() > 0:
        return out.sort_values("rank_no", ascending=True, kind="mergesort").copy()
    return out


def _symbols_from_df(df: pd.DataFrame, label: str, limit: int) -> list[str]:
    if df is None or df.empty:
        logger.info("[PUSH RANKING SELECTOR] bucket empty label=%s", label)
        return []
    out = _dedupe_keep_order(df["symbol"].tolist())[: int(limit)]
    logger.info("[PUSH RANKING SELECTOR] bucket label=%s rows=%d symbols=%d head=%s", label, len(df), len(out), out[:20])
    return out


def _append_unique_until(merged: list[str], symbols: Sequence[Any], *, max_symbols: int) -> list[str]:
    seen = set(_dedupe_keep_order(merged))
    out = _dedupe_keep_order(merged)
    for s in symbols:
        sym = _normalize_symbol(s)
        if not sym or sym in seen:
            continue
        seen.add(sym)
        out.append(sym)
        if max_symbols and len(out) >= int(max_symbols):
            break
    return out


def _select_all_market_gainers_by_value(df: pd.DataFrame) -> list[str]:
    if df.empty:
        return []
    sub = df[_is_gainer_category(df)].copy()
    if sub.empty and "change_rate" in df.columns:
        sub = df[df["change_rate"] > 0].copy()
    sub = _latest_per_symbol(sub)
    top50 = _sort_by_change_rate_up(sub).head(GAINER_TOP_N)
    selected = _sort_by_trading_value(top50).head(GAINER_BY_VALUE_N)
    return _symbols_from_df(selected, "all_gainers_top50_by_value35", GAINER_BY_VALUE_N)


def _select_all_market_losers_by_value(df: pd.DataFrame) -> list[str]:
    if df.empty:
        return []
    sub = df[_is_loser_category(df)].copy()
    if sub.empty and "change_rate" in df.columns:
        sub = df[df["change_rate"] < 0].copy()
    sub = _latest_per_symbol(sub)
    top50 = _sort_by_change_rate_down(sub).head(LOSER_TOP_N)
    selected = _sort_by_trading_value(top50).head(LOSER_BY_VALUE_N)
    return _symbols_from_df(selected, "all_losers_top50_by_value25", LOSER_BY_VALUE_N)


def _select_value_top_up_rate(df: pd.DataFrame) -> list[str]:
    if df.empty:
        return []
    sub = df[_is_value_category(df)].copy()
    if sub.empty:
        sub = df.copy()
    sub = _latest_per_symbol(sub)
    value_top50 = _sort_by_trading_value(sub).head(VALUE_TOP_N)
    if "change_rate" in value_top50.columns and value_top50["change_rate"].notna().sum() > 0:
        value_top50 = value_top50[value_top50["change_rate"] > 0].copy()
    selected = _sort_by_change_rate_up(value_top50).head(VALUE_UP_RATE_N)
    return _symbols_from_df(selected, "value_top50_up_rate30", VALUE_UP_RATE_N)


def _select_value_top_down_rate(df: pd.DataFrame) -> list[str]:
    if df.empty:
        return []
    sub = df[_is_value_category(df)].copy()
    if sub.empty:
        sub = df.copy()
    sub = _latest_per_symbol(sub)
    value_top50 = _sort_by_trading_value(sub).head(VALUE_TOP_N)
    if "change_rate" in value_top50.columns and value_top50["change_rate"].notna().sum() > 0:
        value_top50 = value_top50[value_top50["change_rate"] < 0].copy()
    selected = _sort_by_change_rate_down(value_top50).head(VALUE_DOWN_RATE_N)
    return _symbols_from_df(selected, "value_top50_down_rate20", VALUE_DOWN_RATE_N)


def _select_growth_market_gainers_by_value(df: pd.DataFrame) -> list[str]:
    if df.empty:
        return []
    market_mask = _is_growth_market(df)
    gain_mask = _is_gainer_category(df)
    sub = df[market_mask & gain_mask].copy()
    if sub.empty and "change_rate" in df.columns:
        sub = df[market_mask & (df["change_rate"] > 0)].copy()
    sub = _latest_per_symbol(sub)
    top50 = _sort_by_change_rate_up(sub).head(GROWTH_TOP_N)
    selected = _sort_by_trading_value(top50).head(GROWTH_BY_VALUE_N)
    return _symbols_from_df(selected, "growth_gainers_top50_by_value15", GROWTH_BY_VALUE_N)


def _select_standard_market_gainers_by_value(df: pd.DataFrame) -> list[str]:
    if df.empty:
        return []
    market_mask = _is_standard_market(df)
    gain_mask = _is_gainer_category(df)
    sub = df[market_mask & gain_mask].copy()
    if sub.empty and "change_rate" in df.columns:
        sub = df[market_mask & (df["change_rate"] > 0)].copy()
    sub = _latest_per_symbol(sub)
    top50 = _sort_by_change_rate_up(sub).head(STANDARD_TOP_N)
    selected = _sort_by_trading_value(top50).head(STANDARD_BY_VALUE_N)
    return _symbols_from_df(selected, "standard_gainers_top50_by_value15", STANDARD_BY_VALUE_N)


def _select_tick_top_fill(df: pd.DataFrame) -> list[str]:
    if df.empty:
        return []
    sub = df[_is_tick_category(df)].copy()
    if sub.empty:
        sub = df.copy()
    sub = _latest_per_symbol(sub)
    selected = _sort_by_tick_count(sub).head(TICK_TOP_N)
    return _symbols_from_df(selected, "tick_top50_fill", TICK_TOP_N)


def split_push_ranking_symbols_for_rotation(symbols: Sequence[Any], *, register_limit: int = DEFAULT_REGISTER_LIMIT) -> Tuple[list[str], list[str]]:
    uniq = _dedupe_keep_order(symbols)
    limit = int(register_limit or DEFAULT_REGISTER_LIMIT)
    if limit <= 0:
        limit = DEFAULT_REGISTER_LIMIT
    rotation_a = uniq[:limit]
    rotation_b = uniq[limit : limit * 2]
    logger.info("[PUSH RANKING SELECTOR] split rotation total=%d A=%d B=%d limit=%d", len(uniq), len(rotation_a), len(rotation_b), limit)
    return rotation_a, rotation_b


def pick_push_ranking_symbols_for_rotation(symbols: Sequence[Any], *, rotation: str = "A", register_limit: int = DEFAULT_REGISTER_LIMIT) -> list[str]:
    a, b = split_push_ranking_symbols_for_rotation(symbols, register_limit=register_limit)
    r = str(rotation or "A").upper().strip()
    selected = b if r in ("B", "ROTATION_B", "1", "SECOND", "NEXT") else a
    logger.info("[PUSH RANKING SELECTOR] pick rotation=%s selected=%d head=%s", r, len(selected), selected[:20])
    return selected


def build_push_ranking_symbols(*, max_symbols: int = DEFAULT_MAX_SYMBOLS, db_path: Optional[str | os.PathLike] = None, yyyymmdd: Any = None, fresh_minutes: float = DEFAULT_FRESH_MINUTES, apply_liquidity: bool = True) -> list[str]:
    try:
        limit = int(max_symbols or DEFAULT_MAX_SYMBOLS)
        if limit <= 0:
            limit = DEFAULT_MAX_SYMBOLS
        df = _load_ranking_df(db_path=db_path, yyyymmdd=yyyymmdd)
        if df.empty:
            logger.warning("[PUSH RANKING SELECTOR] no ranking df")
            return []
        df = _apply_freshness(df, fresh_minutes=fresh_minutes)
        if df.empty:
            logger.warning("[PUSH RANKING SELECTOR] no ranking df after freshness filter")
            return []
        if apply_liquidity:
            df = _apply_liquidity_safe(df)
            if df.empty:
                logger.warning("[PUSH RANKING SELECTOR] no ranking df after liquidity filter")
                return []
        merged: list[str] = []
        buckets = [
            ("all_gainers_top50_by_value35", _select_all_market_gainers_by_value),
            ("all_losers_top50_by_value25", _select_all_market_losers_by_value),
            ("value_top50_up_rate30", _select_value_top_up_rate),
            ("value_top50_down_rate20", _select_value_top_down_rate),
            ("growth_gainers_top50_by_value15", _select_growth_market_gainers_by_value),
            ("standard_gainers_top50_by_value15", _select_standard_market_gainers_by_value),
            ("tick_top50_fill", _select_tick_top_fill),
        ]
        for label, fn in buckets:
            if len(merged) >= limit:
                break
            symbols = fn(df)
            before = len(merged)
            merged = _append_unique_until(merged, symbols, max_symbols=limit)
            logger.info("[PUSH RANKING SELECTOR] merge bucket=%s added_raw=%d before=%d after=%d", label, len(symbols), before, len(merged))
        merged = _dedupe_keep_order(merged)[:limit]
        logger.info("[PUSH RANKING SELECTOR] final symbols=%d max=%d head=%s", len(merged), limit, merged[:30])
        if len(merged) < min(limit, DEFAULT_MAX_SYMBOLS):
            logger.warning("[PUSH RANKING SELECTOR] final symbols less than target count=%d target=%d", len(merged), min(limit, DEFAULT_MAX_SYMBOLS))
        return merged
    except Exception:
        logger.exception("[PUSH RANKING SELECTOR] build failed")
        return []


__all__ = [
    "build_push_ranking_symbols",
    "split_push_ranking_symbols_for_rotation",
    "pick_push_ranking_symbols_for_rotation",
]


# ============================================================
# NAS/SMB sqlite I/O ガード (旧 core/startup/nas_sqlite_io_guard_patch.py)
# ============================================================

def _load_ranking_df(*, db_path=None, yyyymmdd=None) -> pd.DataFrame:
    from utils import nas_sqlite_io_guard as _guard

    path = None
    try:
        path = db_path
        if path is None:
            path = _resolve_ranking_db_path(db_path=None, yyyymmdd=yyyymmdd)
        if path and _guard.is_bad(path):
            logger.warning("[NAS SQLITE IO GUARD] skip ranking_selector bad path=%s", path)
            return pd.DataFrame()
        return _load_ranking_df_core(db_path=db_path, yyyymmdd=yyyymmdd)
    except Exception as e:
        if path and _guard.handle_error(path, e):
            return pd.DataFrame()
        raise
