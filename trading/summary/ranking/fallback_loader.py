# ============================================================
# File   : trading/summary/ranking/fallback_loader.py
# Ver    : PRODUCTION-STABLE-RANKING-FALLBACK-LOADER-V1.0
#          -RANKING-ONLY
#          -NO-PUSH-FALLBACK
# ------------------------------------------------------------
# ✔ RANKING由来 fallback 専用
# ✔ PUSH系は一切参照しない
# ✔ global_data / ranking cache / DB から RANKING系のみ復元
# ✔ filter_ranking_like_rows で push 混入を除外
# ✔ future row clamp 前提の素直な dataframe を返す
# ============================================================

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any, Optional

import pandas as pd

logger = logging.getLogger(__name__)

try:
    from global_state import global_data
except Exception:
    try:
        from core.global_context.context import global_data  # type: ignore
    except Exception:
        global_data = None


# ============================================================
# basic helpers
# ============================================================

def _safe_attr(obj: Any, name: str, default=None):
    try:
        return getattr(obj, name, default)
    except Exception:
        return default


def _safe_copy_df(value: Any) -> pd.DataFrame:
    if isinstance(value, pd.DataFrame):
        return value.copy()

    if isinstance(value, tuple) and len(value) >= 1 and isinstance(value[0], pd.DataFrame):
        return value[0].copy()

    if isinstance(value, dict):
        for key in (
            "result_df",
            "merged_df",
            "df",
            "summary_df",
            "output_df",
            "display_df",
            "latest_df",
            "latest_summary_df",
        ):
            v = value.get(key)
            if isinstance(v, pd.DataFrame):
                return v.copy()

    try:
        return pd.DataFrame(value).copy()
    except Exception:
        return pd.DataFrame()


def _ensure_datetime(df: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return df

    out = df.copy()

    if "datetime" in out.columns:
        out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
    elif "end_time" in out.columns:
        out["datetime"] = pd.to_datetime(out["end_time"], errors="coerce")
    elif "snapshot_time" in out.columns:
        out["datetime"] = pd.to_datetime(out["snapshot_time"], errors="coerce")
    elif "CurrentPriceTime" in out.columns:
        out["datetime"] = pd.to_datetime(out["CurrentPriceTime"], errors="coerce")
    elif "current_price_time" in out.columns:
        out["datetime"] = pd.to_datetime(out["current_price_time"], errors="coerce")
    elif "received_at" in out.columns:
        out["datetime"] = pd.to_datetime(out["received_at"], errors="coerce")
    else:
        out["datetime"] = pd.NaT

    try:
        out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce").dt.tz_localize(None)
    except Exception:
        pass

    return out


def _ensure_symbol(df: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return df

    out = df.copy()

    if "symbol" not in out.columns:
        for c in ("Symbol", "symbol_code", "Code", "code"):
            if c in out.columns:
                out["symbol"] = out[c]
                break

    if "symbol" not in out.columns:
        out["symbol"] = ""

    out["symbol"] = out["symbol"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
    out = out[out["symbol"] != ""].copy()
    return out


def _safe_symbol_count(df: pd.DataFrame) -> int:
    if not isinstance(df, pd.DataFrame) or df.empty or "symbol" not in df.columns:
        return 0
    try:
        return int(df["symbol"].astype(str).nunique())
    except Exception:
        return 0


def _safe_latest_dt(df: pd.DataFrame):
    if not isinstance(df, pd.DataFrame) or df.empty:
        return None
    if "datetime" not in df.columns:
        return None
    try:
        s = pd.to_datetime(df["datetime"], errors="coerce")
        return s.max() if s.notna().any() else None
    except Exception:
        return None


def _normalize_df(df: pd.DataFrame) -> pd.DataFrame:
    out = _safe_copy_df(df)
    if out.empty:
        return out

    out = _ensure_symbol(out)
    out = _ensure_datetime(out)

    if "source" not in out.columns:
        out["source"] = "ranking_fallback"

    out = out.dropna(subset=["symbol"], how="any")
    if "datetime" in out.columns:
        out = out.dropna(subset=["datetime"], how="all")

    return out.reset_index(drop=True)


def _log_df_state(label: str, interval: int, df: pd.DataFrame) -> None:
    logger.info(
        "[ranking.fallback] %s interval=%s rows=%s symbols=%s latest_dt=%s cols=%s",
        label,
        interval,
        len(df) if isinstance(df, pd.DataFrame) else 0,
        _safe_symbol_count(df),
        _safe_latest_dt(df),
        list(df.columns)[:20] if isinstance(df, pd.DataFrame) else [],
    )


# ============================================================
# source filter
# ============================================================

def filter_ranking_like_rows(df: pd.DataFrame) -> pd.DataFrame:
    """
    RANKING系に見える行だけ残す。
    push / stream 系の混入を除外する。
    """
    df = _normalize_df(df)
    if df.empty:
        return df

    try:
        out = df.copy()

        if "source" not in out.columns:
            out["source"] = "ranking_fallback"

        src = out["source"].astype(str).str.lower()

        ranking_like = (
            src.str.contains("ranking", na=False)
            | src.str.contains("rank", na=False)
        )

        # source が弱くても、rankingっぽい列があれば残す
        explicit_rank_cols = pd.Series(False, index=out.index)

        for c in ("rank_type", "rank_type_id", "rank_position", "best_rank", "best_rank_value"):
            if c in out.columns:
                explicit_rank_cols = explicit_rank_cols | out[c].notna()

        # pushらしい行は落とす
        push_like = pd.Series(False, index=out.index)
        if "source" in out.columns:
            push_like = push_like | src.str.contains("push|stream|incremental|recovery", regex=True, na=False)

        # 明示的な rank 列があるもの、または source が ranking 系のものを残す
        keep_mask = (ranking_like | explicit_rank_cols) & (~push_like)

        # もし keep が全滅するなら、pushっぽくないものだけ残して過剰除外を防ぐ
        if int(keep_mask.sum()) == 0:
            keep_mask = ~push_like

        before = len(out)
        out = out.loc[keep_mask].copy()
        removed = before - len(out)

        logger.info(
            "[ranking.fallback] filter_ranking_like_rows before=%s after=%s removed=%s",
            before,
            len(out),
            removed,
        )
        return out.reset_index(drop=True)

    except Exception:
        logger.exception("[ranking.fallback] filter_ranking_like_rows failed")
        return df


# ============================================================
# global_data fallback
# ============================================================

def _get_from_global_data(candidates: list[str]) -> Any:
    if global_data is None:
        return None

    for name in candidates:
        try:
            if hasattr(global_data, name):
                value = getattr(global_data, name)
                if value is not None:
                    logger.info("[ranking.fallback] global_data hit key=%s type=%s", name, type(value).__name__)
                    return value
        except Exception:
            pass
    return None


def _fallback_from_global_data(interval: int) -> pd.DataFrame:
    tf = int(interval)

    df = _get_from_global_data([
        f"ranking_summary_{tf}min",
        f"ranking_summary_{tf}",
        f"latest_ranking_summary_{tf}min",
        f"latest_ranking_summary_{tf}",
    ])

    if (not isinstance(df, pd.DataFrame)) or df.empty:
        getter = _safe_attr(global_data, "get_ranking_summary", None)
        if callable(getter):
            try:
                df = getter(tf)
                logger.info("[ranking.fallback] get_ranking_summary(tf=%s) used", tf)
            except Exception:
                logger.exception("[ranking.fallback] get_ranking_summary(tf=%s) failed", tf)

    if (not isinstance(df, pd.DataFrame)) or df.empty:
        getter = _safe_attr(global_data, "get_latest_ranking_summary", None)
        if callable(getter):
            try:
                df = getter(tf)
                logger.info("[ranking.fallback] get_latest_ranking_summary(tf=%s) used", tf)
            except Exception:
                logger.exception("[ranking.fallback] get_latest_ranking_summary(tf=%s) failed", tf)

    out = _normalize_df(_safe_copy_df(df))
    out = filter_ranking_like_rows(out)
    _log_df_state("after_global_data", tf, out)
    return out


# ============================================================
# DB fallback
# ============================================================

def _candidate_ranking_db_paths(now=None) -> list[Path]:
    paths: list[Path] = []

    try:
        p = _safe_attr(global_data, "ranking_db_path", None)
        if p:
            paths.append(Path(str(p)))
    except Exception:
        pass

    try:
        base = Path(r"\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\ranking")
        if now is not None:
            ymd = pd.Timestamp(now).strftime("%Y%m%d")
            paths.append(base / f"ranking{ymd}.db")
    except Exception:
        pass

    uniq: list[Path] = []
    seen = set()
    for p in paths:
        s = str(p)
        if s not in seen:
            seen.add(s)
            uniq.append(p)
    return uniq


def _read_table_if_exists(con, table: str) -> pd.DataFrame:
    try:
        return pd.read_sql_query(f"SELECT * FROM {table}", con)
    except Exception:
        return pd.DataFrame()


def _read_ranking_table_from_db(db_path: Path, interval: int) -> pd.DataFrame:
    if not db_path.exists():
        return pd.DataFrame()

    table_candidates = [
        f"ranking_summary_{interval}min",
        f"ranking_snapshot_{interval}min",
        "ranking_snapshot_1min" if int(interval) == 1 else "",
    ]
    table_candidates = [t for t in table_candidates if t]

    try:
        con = sqlite3.connect(str(db_path))
        try:
            tables = pd.read_sql_query(
                "SELECT name FROM sqlite_master WHERE type='table'",
                con,
            )
            table_names = set(tables["name"].astype(str).tolist()) if not tables.empty else set()

            for table in table_candidates:
                if table not in table_names:
                    continue

                df = _read_table_if_exists(con, table)
                if isinstance(df, pd.DataFrame) and not df.empty:
                    logger.info(
                        "[ranking.fallback] db hit path=%s table=%s rows=%s",
                        db_path,
                        table,
                        len(df),
                    )
                    return df
        finally:
            con.close()

    except Exception:
        logger.exception("[ranking.fallback] db open failed path=%s", db_path)

    return pd.DataFrame()


def _fallback_from_db(interval: int, now=None) -> pd.DataFrame:
    for db_path in _candidate_ranking_db_paths(now=now):
        df = _read_ranking_table_from_db(db_path, interval)
        df = _normalize_df(df)
        df = filter_ranking_like_rows(df)
        if not df.empty:
            _log_df_state(f"after_db path={db_path}", interval, df)
            return df

    return pd.DataFrame()


# ============================================================
# public fallback
# ============================================================

def fallback_ranking_summary_df(interval: int, now=None) -> pd.DataFrame:
    """
    RANKING専用 fallback。
    優先順位:
      1) global_data の ranking_summary / latest_ranking_summary
      2) ranking DB の ranking_summary_{interval}min / ranking_snapshot_{interval}min
    PUSH系は一切参照しない。
    """
    interval = int(interval)
    logger.info("[ranking.fallback] fallback_ranking_summary_df start interval=%s now=%s", interval, now)

    df = _fallback_from_global_data(interval)
    if not df.empty:
        logger.info("[ranking.fallback] fallback resolved from global_data interval=%s rows=%s", interval, len(df))
        return df.reset_index(drop=True)

    df = _fallback_from_db(interval, now=now)
    if not df.empty:
        logger.info("[ranking.fallback] fallback resolved from db interval=%s rows=%s", interval, len(df))
        return df.reset_index(drop=True)

    logger.warning("[ranking.fallback] fallback unresolved interval=%s -> empty", interval)
    return pd.DataFrame()


__all__ = [
    "fallback_ranking_summary_df",
    "filter_ranking_like_rows",
]