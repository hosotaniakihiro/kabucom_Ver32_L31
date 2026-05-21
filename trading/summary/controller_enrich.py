# ============================================================
# File   : trading/summary/controller_enrich.py
# Version: Ver02-SUMMARY-LATEST-RANKING-RANK-FALLBACK-FIX
# ------------------------------------------------------------
# Purpose:
#   - summary_controller の latest DF に、表示/保存前の補強列を付与する
#   - ranking_score / ranking_type / rank / change_rate / turnover を補完
#   - daily MTF を表示/保存前にも merge する
#
# Ver02:
#   - ranking_snapshot_1min に rank_position 列があるが中身が空のケースで、
#     rank 列を rank_position へ補完してから build_ranking_aggregate() に渡す。
#   - これにより [RANKING_AGG] no rows after normalize を防ぐ。
# ============================================================

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd

from global_state import global_data

logger = logging.getLogger(__name__)


# ------------------------------------------------------------
# small helpers
# ------------------------------------------------------------

def _find_col(df: pd.DataFrame, names: Iterable[str]) -> Optional[str]:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return None
    lower = {str(c).lower(): c for c in df.columns}
    for n in names:
        if n in df.columns:
            return n
        hit = lower.get(str(n).lower())
        if hit is not None:
            return hit
    return None


def _find_best_numeric_col(df: pd.DataFrame, names: Iterable[str]) -> Optional[str]:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return None
    best = None
    best_nonnull = -1
    for n in names:
        c = _find_col(df, (n,))
        if c is None:
            continue
        try:
            nonnull = int(pd.to_numeric(df[c], errors="coerce").notna().sum())
        except Exception:
            nonnull = 0
        if nonnull > best_nonnull:
            best = c
            best_nonnull = nonnull
    return best


def _symbol_series(s: pd.Series) -> pd.Series:
    return (
        s.astype(str)
        .str.strip()
        .str.replace(r"\.0$", "", regex=True)
        .str.replace(r"\.T$", "", regex=True)
    )


def _num(s, default=0.0) -> pd.Series:
    try:
        if isinstance(s, pd.Series):
            return pd.to_numeric(s, errors="coerce").fillna(default)
        return pd.Series(dtype="float64")
    except Exception:
        return pd.Series(dtype="float64")


def _fill_missing_or_zero(base: pd.Series, src: pd.Series) -> pd.Series:
    try:
        b = pd.to_numeric(base, errors="coerce")
        s = pd.to_numeric(src, errors="coerce")
        mask = b.isna() | b.fillna(0).eq(0)
        return b.where(~mask, s)
    except Exception:
        try:
            return base.combine_first(src)
        except Exception:
            return base


def _fill_missing_text(base: pd.Series, src: pd.Series) -> pd.Series:
    try:
        b = base.astype("object")
        s = src.astype("object")
        mask = b.isna() | b.astype(str).str.strip().isin(["", "nan", "None", "<NA>", "-"])
        return b.where(~mask, s)
    except Exception:
        try:
            return base.combine_first(src)
        except Exception:
            return base


# ------------------------------------------------------------
# daily MTF
# ------------------------------------------------------------

def attach_daily_mtf_for_display(df: pd.DataFrame, *, interval: int, context: str) -> pd.DataFrame:
    out = df.copy() if isinstance(df, pd.DataFrame) else pd.DataFrame()
    if out.empty:
        return out

    try:
        from trading.summary.mtf.daily_runtime_patch import merge_daily_mtf_for_ai

        before_nonzero = int((_num(out.get("score_mtf", pd.Series(index=out.index))) != 0).sum()) if "score_mtf" in out.columns else 0
        out = merge_daily_mtf_for_ai(out, source=f"SUMMARY_CONTROLLER_{context}_{interval}m")

        if isinstance(out, pd.DataFrame) and not out.empty:
            score_mtf = _num(out.get("score_mtf", pd.Series(index=out.index)), 0.0)
            if "mtf_alignment" not in out.columns:
                out["mtf_alignment"] = score_mtf
            else:
                out["mtf_alignment"] = _fill_missing_or_zero(out["mtf_alignment"], score_mtf)

            if "mtf_score" not in out.columns:
                out["mtf_score"] = score_mtf
            else:
                out["mtf_score"] = _fill_missing_or_zero(out["mtf_score"], score_mtf)

        after_nonzero = int((_num(out.get("score_mtf", pd.Series(index=out.index))) != 0).sum()) if "score_mtf" in out.columns else 0
        logger.info(
            "[SUMMARY ENRICH][DAILY_MTF] interval=%s context=%s rows=%s score_mtf_nonzero %s->%s",
            interval,
            context,
            len(out),
            before_nonzero,
            after_nonzero,
        )
        return out

    except Exception:
        logger.exception("[SUMMARY ENRICH][DAILY_MTF] failed interval=%s context=%s", interval, context)
        return out


# ------------------------------------------------------------
# ranking source
# ------------------------------------------------------------

def _get_ranking_df_from_global() -> pd.DataFrame:
    try:
        snapshot = getattr(global_data, "latest_ranking_snapshot", None)
        if isinstance(snapshot, list) and snapshot:
            return pd.DataFrame(snapshot)

        for name in ("latest_ranking_raw", "latest_ranking_df", "ranking_raw_df"):
            df = getattr(global_data, name, None)
            if isinstance(df, pd.DataFrame) and not df.empty:
                return df.copy()
    except Exception:
        logger.debug("[SUMMARY ENRICH][RANKING] global ranking read failed", exc_info=True)
    return pd.DataFrame()


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
    return cur.fetchone() is not None


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    cur = conn.execute(f"PRAGMA table_info(\"{table}\")")
    return [str(r[1]) for r in cur.fetchall()]


def _read_ranking_df_from_db(limit: int = 20000) -> pd.DataFrame:
    try:
        from ats.ats_ranking.db_path import get_usable_ranking_db_path

        path = get_usable_ranking_db_path(force_refresh=False, allow_fallback=False, prefer_today_even_if_empty=True)
        if not path or not Path(path).exists():
            return pd.DataFrame()

        with sqlite3.connect(str(path), timeout=2.0) as conn:
            conn.execute("PRAGMA busy_timeout=2000;")
            table = "ranking_snapshot_1min"
            if not _table_exists(conn, table):
                return pd.DataFrame()

            cols = _table_columns(conn, table)
            if not cols:
                return pd.DataFrame()

            time_col = None
            for c in ("datetime", "snapshot_time", "time", "created_at", "updated_at"):
                if c in cols:
                    time_col = c
                    break

            if time_col:
                sql = f'SELECT * FROM "{table}" ORDER BY "{time_col}" DESC LIMIT ?'
            else:
                sql = f'SELECT * FROM "{table}" ORDER BY rowid DESC LIMIT ?'

            df = pd.read_sql_query(sql, conn, params=(int(limit),))
            logger.info(
                "[SUMMARY ENRICH][RANKING] db source path=%s table=%s rows=%s time_col=%s",
                path,
                table,
                len(df),
                time_col,
            )
            return df

    except Exception:
        logger.exception("[SUMMARY ENRICH][RANKING] db read failed")
        return pd.DataFrame()


def _get_ranking_source_df() -> pd.DataFrame:
    df = _get_ranking_df_from_global()
    if isinstance(df, pd.DataFrame) and not df.empty:
        logger.info("[SUMMARY ENRICH][RANKING] source=global rows=%s", len(df))
        return df
    return _read_ranking_df_from_db()


# ------------------------------------------------------------
# ranking normalize / merge
# ------------------------------------------------------------

def _build_ranking_enrich_df(ranking_df: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(ranking_df, pd.DataFrame) or ranking_df.empty:
        return pd.DataFrame()

    try:
        symbol_col = _find_col(ranking_df, ("symbol", "code", "stock_code", "Symbol"))
        type_col = _find_col(ranking_df, ("rank_type", "ranking_type", "type", "category", "ranking_name"))
        rank_col = _find_best_numeric_col(ranking_df, ("rank_position", "ranking_position", "rank", "position", "順位", "No"))
        if symbol_col is None:
            return pd.DataFrame()

        work = ranking_df.copy()
        work["symbol"] = _symbol_series(work[symbol_col])
        work = work[work["symbol"].astype(str).str.strip().ne("")].copy()
        if work.empty:
            return pd.DataFrame()

        if type_col is None:
            work["ranking_type"] = ""
        else:
            work["ranking_type"] = work[type_col].fillna("").astype(str).str.strip()

        if rank_col is None:
            work["rank"] = pd.NA
        else:
            work["rank"] = pd.to_numeric(work[rank_col], errors="coerce")

        # build_ranking_aggregate は rank_position を優先して見るため、
        # 既存の rank_position が空なら必ず rank で上書き補完する。
        if "rank_position" not in work.columns:
            work["rank_position"] = work["rank"]
        else:
            rp = pd.to_numeric(work["rank_position"], errors="coerce")
            rk = pd.to_numeric(work["rank"], errors="coerce")
            work["rank_position"] = rp.where(rp.notna(), rk)

        change_col = _find_best_numeric_col(work, ("change_rate", "chg", "change_percentage", "change_pct", "騰落率", "rate"))
        turnover_col = _find_best_numeric_col(work, ("turnover", "trading_value", "売買代金", "value", "Value"))
        volume_col = _find_best_numeric_col(work, ("trading_volume", "volume", "売買高", "出来高", "TradingVolume"))
        market_col = _find_col(work, ("market", "market_type", "exchange", "Exchange", "exchange_name"))

        work["change_rate"] = pd.to_numeric(work[change_col], errors="coerce") if change_col else pd.NA
        work["turnover"] = pd.to_numeric(work[turnover_col], errors="coerce") if turnover_col else pd.NA
        work["trading_volume"] = pd.to_numeric(work[volume_col], errors="coerce") if volume_col else pd.NA
        work["market"] = work[market_col].fillna("").astype(str) if market_col else ""

        try:
            from trading.ranking.ranking_aggregate_builder import build_ranking_aggregate
            agg = build_ranking_aggregate(work)
        except Exception:
            logger.exception("[SUMMARY ENRICH][RANKING] aggregate failed")
            agg = pd.DataFrame()

        latest = (
            work.sort_values(["symbol", "rank"], ascending=[True, True], kind="mergesort")
            .drop_duplicates("symbol", keep="first")
            .copy()
        )

        keep = latest[["symbol", "ranking_type", "rank", "change_rate", "turnover", "trading_volume", "market"]].copy()
        keep["ranking"] = keep["ranking_type"]
        keep["rank_no"] = keep["rank"]
        keep["chg"] = keep["change_rate"]
        keep["turn"] = keep["turnover"]

        if isinstance(agg, pd.DataFrame) and not agg.empty and "symbol" in agg.columns:
            agg = agg.copy()
            agg["symbol"] = _symbol_series(agg["symbol"])
            agg_cols = [c for c in ["symbol", "ranking_score_total", "best_rank", "avg_rank", "rank_types_count"] if c in agg.columns]
            keep = keep.merge(agg[agg_cols], on="symbol", how="left")
        else:
            keep["ranking_score_total"] = 0.0

        keep["ranking_score_total"] = pd.to_numeric(keep.get("ranking_score_total", 0.0), errors="coerce").fillna(0.0)
        keep["ranking_score"] = keep["ranking_score_total"]

        logger.info(
            "[SUMMARY ENRICH][RANKING] enrich built rows=%s score_nonzero=%s rank_nonnull=%s type_nonempty=%s",
            len(keep),
            int((keep["ranking_score"] != 0).sum()) if "ranking_score" in keep.columns else 0,
            int(pd.to_numeric(keep.get("rank", pd.Series(dtype="float64")), errors="coerce").notna().sum()) if "rank" in keep.columns else 0,
            int(keep.get("ranking_type", pd.Series(dtype="object")).astype(str).str.strip().ne("").sum()) if "ranking_type" in keep.columns else 0,
        )
        return keep.drop_duplicates("symbol", keep="first")

    except Exception:
        logger.exception("[SUMMARY ENRICH][RANKING] build enrich df failed")
        return pd.DataFrame()


def attach_ranking_for_display(df: pd.DataFrame, *, interval: int, context: str) -> pd.DataFrame:
    out = df.copy() if isinstance(df, pd.DataFrame) else pd.DataFrame()
    if out.empty or "symbol" not in out.columns:
        return out

    try:
        out["symbol"] = _symbol_series(out["symbol"])
        ranking_df = _get_ranking_source_df()
        enrich = _build_ranking_enrich_df(ranking_df)
        if enrich.empty:
            logger.warning("[SUMMARY ENRICH][RANKING] skipped interval=%s context=%s reason=no_enrich", interval, context)
            return out

        before_cols = set(out.columns)
        before_score_nonzero = int((_num(out.get("ranking_score", pd.Series(index=out.index))) != 0).sum()) if "ranking_score" in out.columns else 0

        merged = out.merge(enrich, on="symbol", how="left", suffixes=("", "__rank_src"))

        for c in ("ranking", "ranking_type", "market"):
            src_c = f"{c}__rank_src"
            if src_c in merged.columns:
                if c in before_cols:
                    merged[c] = _fill_missing_text(merged[c], merged[src_c])
                else:
                    merged[c] = merged[src_c]

        for c in (
            "rank", "rank_no", "change_rate", "chg", "turnover", "turn",
            "trading_volume", "ranking_score", "ranking_score_total",
        ):
            src_c = f"{c}__rank_src"
            if src_c in merged.columns:
                if c in before_cols:
                    merged[c] = _fill_missing_or_zero(merged[c], merged[src_c])
                else:
                    merged[c] = merged[src_c]

        drop_cols = [c for c in merged.columns if c.endswith("__rank_src")]
        merged = merged.drop(columns=drop_cols, errors="ignore")

        hit = int(pd.to_numeric(merged.get("rank", pd.Series(index=merged.index)), errors="coerce").notna().sum()) if "rank" in merged.columns else 0
        after_score_nonzero = int((_num(merged.get("ranking_score", pd.Series(index=merged.index))) != 0).sum()) if "ranking_score" in merged.columns else 0
        logger.info(
            "[SUMMARY ENRICH][RANKING] interval=%s context=%s rows=%s hit=%s ranking_score_nonzero %s->%s",
            interval,
            context,
            len(merged),
            hit,
            before_score_nonzero,
            after_score_nonzero,
        )
        return merged

    except Exception:
        logger.exception("[SUMMARY ENRICH][RANKING] failed interval=%s context=%s", interval, context)
        return out


# ------------------------------------------------------------
# public
# ------------------------------------------------------------

def enrich_summary_latest(df: pd.DataFrame, *, interval: int, context: str = "latest") -> pd.DataFrame:
    out = df.copy() if isinstance(df, pd.DataFrame) else pd.DataFrame()
    if out.empty:
        return out

    out = attach_daily_mtf_for_display(out, interval=interval, context=context)
    out = attach_ranking_for_display(out, interval=interval, context=context)
    return out


__all__ = [
    "enrich_summary_latest",
    "attach_daily_mtf_for_display",
    "attach_ranking_for_display",
]
