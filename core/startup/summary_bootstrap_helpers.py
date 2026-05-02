# ============================================================
# File   : core/startup/summary_bootstrap_helpers.py
# Ver    : PRODUCTION-STABLE-REV13.2-SUMMARY-BOOTSTRAP-HELPERS
#          -PERSIST-SNAPSHOT-KEEPN-FIX
# ------------------------------------------------------------
# 【概要】
#   summary_bootstrap 用の共通 helper 群
#
# 【主な機能】
#   - symbol / datetime 系 helper
#   - summary frame 正規化
#   - display列補完
#   - symbolname backfill
#   - market/universe filter
#   - startup persist policy
#
# 【今回の修正】
#   - 3min / 5min の persist snapshot keep_n を 1 -> 3 へ修正
#   - persist_summary_df_to_db で keep_n を明示上書き可能にした
#   - 履歴が十分あるのに保存直前で latest-only に潰しすぎる問題を緩和
# ============================================================

from __future__ import annotations

import logging
import os
import sqlite3
from typing import Dict, Iterable, List, Optional, Set

import pandas as pd

from global_state import global_data
from trading.summary.recovery.helpers import (
    ensure_dataframe,
    safe_get_series,
    normalize_datetime_columns,
)
from trading.summary.recovery.persistence import (
    finalize_for_upsert,
    upsert_summary_df,
)

logger = logging.getLogger(__name__)

_ALLOWED_MARKETS = {"プライム", "スタンダード", "グロース"}
_EXCLUDE_NAME_KEYWORDS = (
    "ETF", "ETN", "REIT", "指数", "連動", "レバ", "ダブル", "ベア",
    "インデックス", "上場投信", "投資口", "J-REIT", "REIT ETF",
)

_SYMBOLNAME_DB_CANDIDATES = [
    r"\\192.168.0.22\AutoStockBuyAndSell\Basic\symbol_flags.db",
    r"\\192.168.0.22\AutoStockBuyAndSell\basic\symbol_flags.db",
]

_STARTUP_PERSIST_ENABLED_BY_TF = {
    1: True,
    3: True,
    5: True,
}

_STARTUP_PERSIST_ENABLED_BY_STAGE = {
    "initial_preload": False,
    "recent_preload": False,
    "multi_day_preload": False,
    "finalize": True,
}

# ------------------------------------------------------------
# 修正前:
#   1:2, 3:1, 5:1
# 修正後:
#   3min / 5min は latest-only に潰しすぎないよう 3 本保持
# ------------------------------------------------------------
_STARTUP_PERSIST_LATEST_ROWS_PER_SYMBOL = {
    1: 2,
    3: 3,
    5: 3,
}


def safe_symbol_series(df: pd.DataFrame) -> Optional[pd.Series]:
    if df is None or df.empty:
        return None

    for col in ("symbol", "code", "ticker", "stock_code"):
        if col in df.columns:
            try:
                s = safe_get_series(df, col)
                if s is None:
                    continue
                s = s.astype(str).str.strip()
                s = s.replace({"": pd.NA, "nan": pd.NA, "None": pd.NA})
                return s
            except Exception:
                logger.debug("symbol series normalize failed col=%s", col, exc_info=True)
    return None


def safe_symbol_nunique(df: pd.DataFrame) -> int:
    try:
        s = safe_symbol_series(df)
        if s is None:
            return 0
        return int(s.dropna().astype(str).nunique())
    except Exception:
        return 0


def dedupe_keep_order(values: Iterable[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for x in values:
        s = str(x).strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def coerce_datetime_series(df: pd.DataFrame, *cols: str) -> Optional[pd.Series]:
    if df is None or df.empty:
        return None

    for col in cols:
        if col in df.columns:
            try:
                s0 = safe_get_series(df, col)
                if s0 is None:
                    continue
                s = pd.to_datetime(s0, errors="coerce")
                try:
                    if getattr(s.dt, "tz", None) is not None:
                        s = s.dt.tz_localize(None)
                except Exception:
                    pass
                if s.notna().any():
                    return s
            except Exception:
                logger.debug("datetime coerce failed col=%s", col, exc_info=True)
    return None


def normalize_market_text(s: object) -> str:
    txt = str(s or "").strip().upper()
    mapping = {
        "PRIME": "プライム",
        "STANDARD": "スタンダード",
        "GROWTH": "グロース",
        "P": "プライム",
        "S": "スタンダード",
        "G": "グロース",
        "東証P": "プライム",
        "東証S": "スタンダード",
        "東証G": "グロース",
        "TSE PRIME": "プライム",
        "TSE STANDARD": "スタンダード",
        "TSE GROWTH": "グロース",
    }
    return mapping.get(txt, str(s or "").strip())


def log_boot_df(stage: str, tf: Optional[int], df: pd.DataFrame) -> None:
    try:
        if not isinstance(df, pd.DataFrame):
            logger.info("[BOOT CHECK][%s] not_dataframe", stage)
            return

        latest = None
        dt_s = coerce_datetime_series(df, "datetime", "end_time", "start_time", "time")
        if dt_s is not None and dt_s.notna().any():
            latest = dt_s.max()

        score_nonzero = 0
        slope_nonzero = 0
        mtf_nonzero = 0
        score_mtf_nonzero = 0

        if "score" in df.columns:
            score_nonzero = int((pd.to_numeric(safe_get_series(df, "score"), errors="coerce").fillna(0) != 0).sum())
        if "slope" in df.columns:
            slope_nonzero = int((pd.to_numeric(safe_get_series(df, "slope"), errors="coerce").fillna(0) != 0).sum())
        if "mtf" in df.columns:
            mtf_nonzero = int((pd.to_numeric(safe_get_series(df, "mtf"), errors="coerce").fillna(0) != 0).sum())
        if "score_mtf" in df.columns:
            score_mtf_nonzero = int((pd.to_numeric(safe_get_series(df, "score_mtf"), errors="coerce").fillna(0) != 0).sum())

        logger.info(
            "[BOOT CHECK][%s%s] rows=%d symbols=%d cols=%d latest=%s "
            "has_symbolname=%s has_score=%s has_slope=%s has_mtf=%s has_score_mtf=%s "
            "score_nonzero=%d slope_nonzero=%d mtf_nonzero=%d score_mtf_nonzero=%d",
            f"{tf}min/" if tf is not None else "",
            stage,
            len(df),
            safe_symbol_nunique(df),
            len(df.columns),
            latest,
            "symbolname" in df.columns,
            "score" in df.columns,
            "slope" in df.columns,
            "mtf" in df.columns,
            "score_mtf" in df.columns,
            score_nonzero,
            slope_nonzero,
            mtf_nonzero,
            score_mtf_nonzero,
        )
    except Exception:
        logger.debug("boot df log failed stage=%s tf=%s", stage, tf, exc_info=True)


def normalize_summary_frame(df: pd.DataFrame, tf: int = 1) -> pd.DataFrame:
    out = ensure_dataframe(df)
    if out.empty:
        return out

    out = normalize_datetime_columns(out, interval=int(tf))

    try:
        if "symbolname" not in out.columns:
            out["symbolname"] = ""
        else:
            out["symbolname"] = safe_get_series(out, "symbolname").fillna("").astype(str)
    except Exception:
        logger.debug("symbolname normalize failed", exc_info=True)

    return out


def ensure_summary_display_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df

    df = df.copy()

    try:
        if "score" not in df.columns:
            if "score_total" in df.columns:
                df["score"] = pd.to_numeric(safe_get_series(df, "score_total"), errors="coerce").fillna(0)
            elif "score_buy" in df.columns:
                df["score"] = pd.to_numeric(safe_get_series(df, "score_buy"), errors="coerce").fillna(0)
            else:
                df["score"] = 0.0
        else:
            df["score"] = pd.to_numeric(safe_get_series(df, "score"), errors="coerce").fillna(0)

        if "slope" not in df.columns:
            if "score_slope" in df.columns:
                df["slope"] = pd.to_numeric(safe_get_series(df, "score_slope"), errors="coerce").fillna(0)
            elif "slope_atr_scaled" in df.columns:
                df["slope"] = pd.to_numeric(safe_get_series(df, "slope_atr_scaled"), errors="coerce").fillna(0)
            else:
                df["slope"] = 0.0
        else:
            df["slope"] = pd.to_numeric(safe_get_series(df, "slope"), errors="coerce").fillna(0)

        if "score_mtf" not in df.columns:
            if "mtf_score" in df.columns:
                df["score_mtf"] = pd.to_numeric(safe_get_series(df, "mtf_score"), errors="coerce").fillna(0)
            elif "mtf" in df.columns:
                df["score_mtf"] = pd.to_numeric(safe_get_series(df, "mtf"), errors="coerce").fillna(0)
            else:
                df["score_mtf"] = 0.0
        else:
            df["score_mtf"] = pd.to_numeric(safe_get_series(df, "score_mtf"), errors="coerce").fillna(0)

        if "mtf" not in df.columns:
            df["mtf"] = pd.to_numeric(safe_get_series(df, "score_mtf"), errors="coerce").fillna(0)
        else:
            mtf_s = pd.to_numeric(safe_get_series(df, "mtf"), errors="coerce")
            score_mtf_s = pd.to_numeric(safe_get_series(df, "score_mtf"), errors="coerce")
            try:
                df["mtf"] = mtf_s.where(mtf_s.notna() & (mtf_s != 0), score_mtf_s).fillna(0)
            except Exception:
                df["mtf"] = mtf_s.combine_first(score_mtf_s).fillna(0)

        for col in ("score_buy", "score_sell", "score_total", "score_slope", "score_mtf", "mtf_score"):
            if col not in df.columns:
                df[col] = 0.0
            else:
                df[col] = pd.to_numeric(safe_get_series(df, col), errors="coerce").fillna(0)

    except Exception:
        logger.exception("display column ensure failed")

    return df


def final_profile_log(df: pd.DataFrame, tf: int) -> None:
    try:
        if df is None or df.empty:
            logger.warning("⚠ [%smin] final profile skipped empty", tf)
            return

        zero_score = int((pd.to_numeric(safe_get_series(df, "score"), errors="coerce").fillna(0) == 0).sum()) if "score" in df.columns else -1
        zero_slope = int((pd.to_numeric(safe_get_series(df, "slope"), errors="coerce").fillna(0) == 0).sum()) if "slope" in df.columns else -1
        zero_mtf = int((pd.to_numeric(safe_get_series(df, "mtf"), errors="coerce").fillna(0) == 0).sum()) if "mtf" in df.columns else -1

        logger.info(
            "📊 [%smin] final profile rows=%d symbols=%d zero_score=%d zero_slope=%d zero_mtf=%d has_symbolname=%s",
            tf, len(df), safe_symbol_nunique(df), zero_score, zero_slope, zero_mtf, "symbolname" in df.columns
        )
    except Exception:
        logger.exception("final profile log failed tf=%s", tf)


def should_startup_persist(tf: int, stage: str) -> bool:
    tf_ok = bool(_STARTUP_PERSIST_ENABLED_BY_TF.get(int(tf), False))
    stage_ok = bool(_STARTUP_PERSIST_ENABLED_BY_STAGE.get(str(stage), False))
    ok = tf_ok and stage_ok
    logger.info("🧭 [%smin/%s] startup persist policy tf_ok=%s stage_ok=%s -> %s", tf, stage, tf_ok, stage_ok, ok)
    return ok


def limit_latest_rows_per_symbol(
    df: pd.DataFrame,
    tf: int,
    *,
    keep_n: Optional[int] = None,
) -> pd.DataFrame:
    out = normalize_summary_frame(df, tf=tf)
    if out.empty:
        return out

    if "symbol" not in out.columns or "datetime" not in out.columns:
        return out

    try:
        keep_n = max(int(keep_n if keep_n is not None else _STARTUP_PERSIST_LATEST_ROWS_PER_SYMBOL.get(int(tf), 1)), 1)
        out = (
            out.sort_values(["symbol", "datetime"])
            .groupby("symbol", group_keys=False)
            .tail(keep_n)
            .reset_index(drop=True)
        )
        logger.info("✂ [%smin] latest rows per symbol limited keep_n=%d rows=%d symbols=%d", tf, keep_n, len(out), safe_symbol_nunique(out))
        return out
    except Exception:
        logger.exception("latest rows per symbol limit failed tf=%s", tf)
        return out


def persist_summary_df_to_db(
    df: pd.DataFrame,
    tf: int,
    *,
    stage: str,
    allow_empty_skip: bool = True,
    keep_latest_rows_per_symbol: Optional[int] = None,
) -> int:
    if not should_startup_persist(tf, stage):
        logger.info("⏭ [%smin/%s] startup persist skipped by policy", tf, stage)
        return 0

    out = normalize_summary_frame(df, tf=tf)
    out = ensure_summary_display_columns(out)

    if out.empty and allow_empty_skip:
        logger.warning("⚠ [%smin/%s] persist skipped: empty", tf, stage)
        return 0

    try:
        out = finalize_for_upsert(out, int(tf))
        out = limit_latest_rows_per_symbol(out, tf, keep_n=keep_latest_rows_per_symbol)

        if out.empty:
            logger.warning("⚠ [%smin/%s] persist skipped after finalize/limit: empty", tf, stage)
            return 0

        rows = upsert_summary_df(out, int(tf))
        logger.info("💾 [%smin/%s] summary DB persisted rows=%d symbols=%d", tf, stage, rows, safe_symbol_nunique(out))
        return int(rows)
    except Exception:
        logger.exception("❌ [%smin/%s] summary DB persist failed", tf, stage)
        return 0


def load_symbol_map_from_db() -> Dict[str, str]:
    out: Dict[str, str] = {}
    sql = """
        SELECT symbol, symbolname
        FROM symbol_flags
        WHERE symbol IS NOT NULL
          AND symbolname IS NOT NULL
          AND TRIM(symbol) != ''
          AND TRIM(symbolname) != ''
    """

    for db_path in _SYMBOLNAME_DB_CANDIDATES:
        try:
            if not os.path.exists(db_path):
                continue
            with sqlite3.connect(db_path, timeout=15) as conn:
                df = pd.read_sql(sql, conn)

            if df.empty:
                continue

            for _, row in df.iterrows():
                key = str(row.get("symbol", "")).strip()
                val = str(row.get("symbolname", "")).strip()
                if key and val and val != key:
                    out[key] = val

            if out:
                logger.info("✅ symbol map loaded from db path=%s rows=%d", db_path, len(out))
                return out
        except Exception:
            logger.debug("symbol map db load failed path=%s", db_path, exc_info=True)

    return out


def backfill_symbolname(df: pd.DataFrame) -> pd.DataFrame:
    out = normalize_summary_frame(df)
    if out.empty:
        return out

    s = safe_symbol_series(out)
    if s is None:
        return out

    out["symbol"] = s.reindex(out.index)
    if "symbolname" not in out.columns:
        out["symbolname"] = ""
    else:
        out["symbolname"] = safe_get_series(out, "symbolname").fillna("").astype(str).str.strip()

    miss = out["symbolname"].eq("") | out["symbolname"].eq(out["symbol"])

    symbol_map = {}
    try:
        for attr in ("symbol_name_map", "symbolname_map", "symbol_master_map", "symbol_map", "symbol_names"):
            maybe = getattr(global_data, attr, None)
            if isinstance(maybe, dict) and maybe:
                for k, v in maybe.items():
                    key = str(k).strip()
                    val = str(v).strip() if v is not None else ""
                    if key and val and val != key:
                        symbol_map[key] = val
    except Exception:
        logger.debug("global symbol map load failed", exc_info=True)

    if not symbol_map:
        symbol_map = load_symbol_map_from_db()

    if symbol_map:
        out.loc[miss, "symbolname"] = (
            out.loc[miss, "symbol"].map(symbol_map).fillna(out.loc[miss, "symbolname"]).astype(str).str.strip()
        )

    miss2 = out["symbolname"].eq("")
    out.loc[miss2, "symbolname"] = out.loc[miss2, "symbol"]
    return out


def load_allowed_symbol_universe() -> Set[str]:
    try:
        from utils.market_filter import get_tradeable_symbols

        syms = get_tradeable_symbols()
        if syms:
            out = {str(x).strip() for x in syms if str(x).strip()}
            if out:
                logger.info("✅ allowed symbol universe loaded via utils.market_filter count=%d", len(out))
                return out
    except Exception:
        logger.debug("market_filter universe load failed", exc_info=True)

    db_candidates = [
        r"\\192.168.0.22\AutoStockBuyAndSell\Basic\symbol_flags.db",
        r"\\192.168.0.22\AutoStockBuyAndSell\basic\symbol_flags.db",
    ]

    sql = """
        SELECT symbol, market_type, symbolname
        FROM symbol_flags
        WHERE market_type IN ('プライム','スタンダード','グロース')
    """

    for db_path in db_candidates:
        try:
            with sqlite3.connect(db_path, timeout=15) as conn:
                df = pd.read_sql(sql, conn)

            if not df.empty and "symbol" in df.columns:
                if "market_type" in df.columns:
                    df["market_type"] = df["market_type"].astype(str).map(normalize_market_text)
                    df = df[df["market_type"].isin(_ALLOWED_MARKETS)]

                if "symbolname" in df.columns:
                    name_s = df["symbolname"].astype(str)
                    mask = ~name_s.str.contains("|".join(_EXCLUDE_NAME_KEYWORDS), case=False, na=False, regex=True)
                    df = df[mask]

                out = set(df["symbol"].astype(str).str.strip().tolist())
                if out:
                    logger.info("✅ allowed symbol universe loaded via symbol_flags.db count=%d", len(out))
                    return out
        except Exception:
            logger.debug("symbol_flags direct load failed path=%s", db_path, exc_info=True)

    for attr in ("watch_symbols", "active_symbols", "push_symbols", "monitor_symbols"):
        try:
            vals = getattr(global_data, attr, None)
            if vals:
                out = {str(x).strip() for x in vals if str(x).strip()}
                if out:
                    logger.info("✅ allowed symbol universe loaded via global_data.%s count=%d", attr, len(out))
                    return out
        except Exception:
            continue

    logger.warning("⚠ allowed symbol universe unresolved")
    return set()


def apply_market_filter_df(df: pd.DataFrame) -> pd.DataFrame:
    out = normalize_summary_frame(df)
    if out.empty:
        return out

    try:
        out = backfill_symbolname(out)
        s = safe_symbol_series(out)
        if s is None:
            logger.warning("⚠ market filter skipped: symbol column missing")
            return out

        out["symbol"] = s.reindex(out.index)
        before = len(out)

        allowed = load_allowed_symbol_universe()
        if allowed:
            out = out[out["symbol"].astype(str).isin(allowed)]

        for market_col in ("market_type", "market"):
            if market_col in out.columns:
                s2 = safe_get_series(out, market_col)
                if s2 is not None:
                    norm = s2.map(normalize_market_text)
                    keep = norm.isin(_ALLOWED_MARKETS)
                    if int(keep.sum()) > 0:
                        out = out[keep]
                    break

        if "symbolname" in out.columns:
            pat = "|".join(_EXCLUDE_NAME_KEYWORDS)
            mask = ~out["symbolname"].astype(str).str.contains(pat, case=False, na=False, regex=True)
            out = out[mask]

        after = len(out)
        if after != before:
            logger.info("🧹 market/universe filter applied rows: %d -> %d", before, after)

        return out.reset_index(drop=True)
    except Exception:
        logger.exception("market filter apply failed")
        return out


def filter_symbol_list(symbols: Iterable[str]) -> List[str]:
    base = dedupe_keep_order(symbols)
    if not base:
        return []

    allowed = load_allowed_symbol_universe()
    if not allowed:
        return base

    out = [s for s in base if s in allowed]
    logger.info("🧹 symbol list filtered by allowed universe: %d -> %d", len(base), len(out))
    return out


__all__ = [
    "safe_symbol_series",
    "safe_symbol_nunique",
    "dedupe_keep_order",
    "coerce_datetime_series",
    "normalize_market_text",
    "log_boot_df",
    "normalize_summary_frame",
    "ensure_summary_display_columns",
    "final_profile_log",
    "should_startup_persist",
    "limit_latest_rows_per_symbol",
    "persist_summary_df_to_db",
    "load_symbol_map_from_db",
    "backfill_symbolname",
    "load_allowed_symbol_universe",
    "apply_market_filter_df",
    "filter_symbol_list",
]