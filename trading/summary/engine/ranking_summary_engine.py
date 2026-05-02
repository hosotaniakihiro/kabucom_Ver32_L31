# ============================================================
# File   : trading/summary/engine/ranking_summary_engine.py
# Ver    : PRODUCTION-STABLE-RANKING-SUMMARY-WITH-RANKING-MA-FINAL-FIXED
# ------------------------------------------------------------
# ✔ build_ranking_summary 公開
# ✔ global_data.ranking_snapshot_1min 優先
# ✔ ranking DB fallback
# ✔ ranking_ma_1min join
# ✔ ma5 / ma25 / ma75 summary互換補完
# ✔ market filter
# ✔ symbolname / price / close / datetime 安全補完
# ✔ ranking_ma datetime列自動解決
# ✔ duplicate __future__ import 修正
# ✔ conn受け渡し修正
# ✔ production hardened
# ============================================================

from __future__ import annotations

import logging
from typing import Optional, Iterable

import pandas as pd
from sqlalchemy import text

logger = logging.getLogger(__name__)

try:
    from global_state import global_data  # type: ignore
except Exception:
    try:
        from core.global_context import global_data  # type: ignore
    except Exception:
        class _FallbackGlobalData:
            pass
        global_data = _FallbackGlobalData()


# ============================================================
# helpers
# ============================================================

def _safe_df(obj) -> pd.DataFrame:
    if isinstance(obj, pd.DataFrame):
        try:
            return obj.copy()
        except Exception:
            return obj
    try:
        return pd.DataFrame(obj).copy()
    except Exception:
        return pd.DataFrame()


def _safe_text(v) -> str:
    try:
        if v is None:
            return ""
        s = str(v).strip()
        if s.lower() in {"nan", "none", "nat", "<na>"}:
            return ""
        return s
    except Exception:
        return ""


def _safe_symbol(v) -> str:
    s = _safe_text(v)
    return s.replace(".0", "")


def _to_dt_series(s: pd.Series) -> pd.Series:
    try:
        out = pd.to_datetime(s, errors="coerce")
        try:
            out = out.dt.tz_localize(None)
        except Exception:
            pass
        return out
    except Exception:
        return pd.to_datetime(pd.Series([None] * len(s), index=s.index), errors="coerce")


def _normalize_market_text(v: object) -> str:
    txt = _safe_text(v).upper()
    mapping = {
        "TS": "スタンダード",
        "TG": "グロース",
        "TP": "プライム",
        "STD": "スタンダード",
        "GRT": "グロース",
        "PRM": "プライム",
        "STANDARD": "スタンダード",
        "GROWTH": "グロース",
        "PRIME": "プライム",
        "東証S": "スタンダード",
        "東証G": "グロース",
        "東証P": "プライム",
    }
    return mapping.get(txt, _safe_text(v))


def _safe_getattr(obj, name: str, default=None):
    try:
        return getattr(obj, name, default)
    except Exception:
        return default


def _ensure_summary_compatible_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = _safe_df(df)
    if out.empty:
        return out

    try:
        if "symbol" in out.columns:
            out["symbol"] = out["symbol"].map(_safe_symbol)
        else:
            out["symbol"] = ""

        if "symbolname" not in out.columns:
            out["symbolname"] = out["symbol"]
        else:
            out["symbolname"] = out["symbolname"].fillna("").astype(str)
            miss = out["symbolname"].eq("")
            out.loc[miss, "symbolname"] = out.loc[miss, "symbol"]

        dt_col = None
        for c in ("datetime", "snapshot_time", "time", "end_time", "start_time"):
            if c in out.columns:
                dt_col = c
                break

        if dt_col is None:
            if "date" in out.columns and "time" in out.columns:
                out["datetime"] = _to_dt_series(
                    out["date"].astype(str).str.strip() + " " + out["time"].astype(str).str.strip()
                )
            else:
                out["datetime"] = pd.NaT
        else:
            out["datetime"] = _to_dt_series(out[dt_col])

        if "close" not in out.columns:
            for c in ("current_price", "price", "close_price"):
                if c in out.columns:
                    out["close"] = pd.to_numeric(out[c], errors="coerce")
                    break
        if "close" not in out.columns:
            out["close"] = pd.NA

        if "close_price" not in out.columns:
            out["close_price"] = pd.to_numeric(out["close"], errors="coerce")

        if "price" not in out.columns:
            out["price"] = pd.to_numeric(out["close"], errors="coerce")

        for c in ("open", "high", "low"):
            if c not in out.columns:
                out[c] = pd.to_numeric(out["close"], errors="coerce")

        for src, dst in (
            ("open", "open_price"),
            ("high", "high_price"),
            ("low", "low_price"),
        ):
            if dst not in out.columns:
                out[dst] = pd.to_numeric(out[src], errors="coerce")

        if "volume" not in out.columns:
            for c in ("trading_volume", "vol"):
                if c in out.columns:
                    out["volume"] = pd.to_numeric(out[c], errors="coerce")
                    break
        if "volume" not in out.columns:
            out["volume"] = 0.0

        if "score" not in out.columns:
            out["score"] = 0.0
        else:
            out["score"] = pd.to_numeric(out["score"], errors="coerce").fillna(0.0)

        if "score_buy" not in out.columns:
            out["score_buy"] = pd.to_numeric(out["score"], errors="coerce").fillna(0.0)
        else:
            out["score_buy"] = pd.to_numeric(out["score_buy"], errors="coerce").fillna(0.0)

        if "score_sell" not in out.columns:
            out["score_sell"] = 0.0
        else:
            out["score_sell"] = pd.to_numeric(out["score_sell"], errors="coerce").fillna(0.0)

        for c in ("slope", "mtf", "score_slope", "score_mtf", "rsi", "macd", "signal", "hist"):
            if c not in out.columns:
                out[c] = pd.NA

        if "market_type_norm" not in out.columns:
            if "market" in out.columns:
                out["market_type_norm"] = out["market"].map(_normalize_market_text)
            elif "market_type" in out.columns:
                out["market_type_norm"] = out["market_type"].map(_normalize_market_text)
            else:
                out["market_type_norm"] = ""

        if "market_type" not in out.columns:
            out["market_type"] = out["market_type_norm"]

        return out
    except Exception:
        logger.exception("[RANKING SUMMARY] summary compatible normalize failed")
        return out


# ============================================================
# source loaders
# ============================================================

def _load_from_global_data() -> pd.DataFrame:
    candidates = []

    for attr in (
        "ranking_snapshot_1min",
        "latest_ranking_raw",
        "ranking_snapshot",
    ):
        try:
            candidates.append((attr, _safe_getattr(global_data, attr, None)))
        except Exception:
            pass

    for attr_name, src in candidates:
        df = _safe_df(src)
        if not df.empty:
            logger.info(
                "[RANKING SUMMARY] global_data source hit name=%s rows=%d",
                attr_name,
                len(df),
            )
            return df

    return pd.DataFrame()


def _load_from_ranking_db() -> pd.DataFrame:
    try:
        from database.session import get_ranking_engine
        engine = get_ranking_engine()
        if engine is None:
            logger.warning("[RANKING SUMMARY] ranking engine unavailable")
            return pd.DataFrame()

        sql = text("""
            SELECT *
            FROM ranking_snapshot_1min
        """)

        with engine.begin() as conn:
            df = pd.read_sql(sql, conn)

        return _safe_df(df)
    except Exception:
        logger.exception("[RANKING SUMMARY] ranking DB load failed")
        return pd.DataFrame()


# ============================================================
# ranking_ma helpers
# ============================================================

def _get_table_columns(conn, table_name: str) -> set[str]:
    try:
        rows = conn.execute(text(f"PRAGMA table_info({table_name})")).fetchall()
        return {str(r[1]) for r in rows if len(r) > 1}
    except Exception:
        logger.exception("[RANKING SUMMARY] failed to inspect columns: %s", table_name)
        return set()


def _resolve_ranking_ma_dt_expr(columns: set[str]) -> tuple[str | None, str | None]:
    if "datetime" in columns:
        return "datetime", "datetime"

    if "end_time" in columns:
        return "end_time", "end_time"

    if "start_time" in columns:
        return "start_time", "start_time"

    if "snapshot_time" in columns:
        return "snapshot_time", "snapshot_time"

    if "date" in columns and "time" in columns:
        expr = "date || ' ' || time"
        return expr, expr

    return None, None


def _load_latest_ranking_ma(conn, symbols: Iterable[str]) -> pd.DataFrame:
    symbols = [str(s).strip() for s in (symbols or []) if str(s).strip()]
    if not symbols:
        return pd.DataFrame(columns=["symbol", "datetime", "close", "ma5", "ma25", "ma75"])

    table_name = "ranking_ma_1min"
    columns = _get_table_columns(conn, table_name)
    if not columns:
        logger.warning("[RANKING SUMMARY] ranking_ma columns not found")
        return pd.DataFrame(columns=["symbol", "datetime", "close", "ma5", "ma25", "ma75"])

    dt_key_expr, dt_select_expr = _resolve_ranking_ma_dt_expr(columns)
    if not dt_key_expr or not dt_select_expr:
        logger.warning(
            "[RANKING SUMMARY] ranking_ma usable datetime column not found: cols=%s",
            sorted(columns),
        )
        return pd.DataFrame(columns=["symbol", "datetime", "close", "ma5", "ma25", "ma75"])

    value_cols = []
    for c in ["close", "ma5", "ma25", "ma75"]:
        if c in columns:
            value_cols.append(f"t.{c}")
        else:
            value_cols.append(f"NULL AS {c}")

    placeholders = ", ".join([f":s{i}" for i in range(len(symbols))])
    params = {f"s{i}": s for i, s in enumerate(symbols)}

    sql = f"""
        WITH latest AS (
            SELECT
                symbol,
                MAX({dt_key_expr}) AS max_dt
            FROM {table_name}
            WHERE symbol IN ({placeholders})
            GROUP BY symbol
        )
        SELECT
            t.symbol,
            {dt_select_expr} AS datetime,
            {", ".join(value_cols)}
        FROM {table_name} t
        INNER JOIN latest l
            ON t.symbol = l.symbol
           AND {dt_key_expr} = l.max_dt
    """

    try:
        df = pd.read_sql(text(sql), conn, params=params)
        df = _safe_df(df)

        if df.empty:
            logger.warning("[RANKING SUMMARY] ranking_ma empty")
            return pd.DataFrame(columns=["symbol", "datetime", "close", "ma5", "ma25", "ma75"])

        if "datetime" in df.columns:
            df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")

        for c in ("close", "ma5", "ma25", "ma75"):
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce")

        return df

    except Exception:
        logger.exception("[RANKING SUMMARY] ranking_ma load failed")
        return pd.DataFrame(columns=["symbol", "datetime", "close", "ma5", "ma25", "ma75"])


# ============================================================
# filters / enrich
# ============================================================

def _apply_market_filter(df: pd.DataFrame) -> pd.DataFrame:
    out = _safe_df(df)
    if out.empty:
        return out

    try:
        before = len(out)

        if "market_type_norm" in out.columns:
            keep = out["market_type_norm"].isin(["プライム", "スタンダード", "グロース", "ALL", ""])
            out = out[keep].copy()

        logger.info(
            "[RANKING SUMMARY] market filter before=%d after=%d",
            before,
            len(out),
        )
        return out.reset_index(drop=True)
    except Exception:
        logger.exception("[RANKING SUMMARY] market filter failed")
        return out


def _attach_ranking_ma(conn, df: pd.DataFrame) -> pd.DataFrame:
    out = _safe_df(df)
    if out.empty or "symbol" not in out.columns:
        return out

    try:
        ma_df = _load_latest_ranking_ma(conn, out["symbol"].astype(str).tolist())
        if ma_df.empty:
            for c in ("ma5", "ma25", "ma75"):
                if c not in out.columns:
                    out[c] = pd.NA
            logger.warning("[RANKING SUMMARY] ranking_ma empty")
            return out

        keep_cols = [c for c in ["symbol", "ma5", "ma25", "ma75"] if c in ma_df.columns]
        ma_df = ma_df[keep_cols].drop_duplicates(subset=["symbol"], keep="last")

        before_cols = set(out.columns)
        out = out.merge(ma_df, on="symbol", how="left", suffixes=("", "_majoin"))

        for c in ("ma5", "ma25", "ma75"):
            alt = f"{c}_majoin"
            if alt in out.columns:
                if c in before_cols:
                    base = pd.to_numeric(out[c], errors="coerce")
                    addv = pd.to_numeric(out[alt], errors="coerce")
                    out[c] = base.where(base.notna(), addv)
                else:
                    out[c] = pd.to_numeric(out[alt], errors="coerce")
                out.drop(columns=[alt], inplace=True, errors="ignore")

        logger.info(
            "[RANKING SUMMARY] ranking_ma joined rows=%d ma5_nonnull=%d ma25_nonnull=%d ma75_nonnull=%d",
            len(out),
            int(pd.to_numeric(out["ma5"], errors="coerce").notna().sum()) if "ma5" in out.columns else 0,
            int(pd.to_numeric(out["ma25"], errors="coerce").notna().sum()) if "ma25" in out.columns else 0,
            int(pd.to_numeric(out["ma75"], errors="coerce").notna().sum()) if "ma75" in out.columns else 0,
        )
        return out
    except Exception:
        logger.exception("[RANKING SUMMARY] ranking_ma attach failed")
        return out


# ============================================================
# main
# ============================================================

def build_ranking_summary(interval: int = 1) -> pd.DataFrame:
    logger.info("▶ ranking summary start interval=%s", interval)

    try:
        df = _load_from_global_data()
        src_name = "global_data.ranking_snapshot_1min"

        if df.empty:
            df = _load_from_ranking_db()
            src_name = "ranking_db.ranking_snapshot_1min"

        if df.empty:
            logger.warning("[RANKING SUMMARY] snapshot empty")
            return pd.DataFrame()

        logger.info(
            "[RANKING SUMMARY] loaded source=%s rows=%d symbols=%d",
            src_name,
            len(df),
            int(df["symbol"].astype(str).nunique()) if "symbol" in df.columns else 0,
        )

        df = _ensure_summary_compatible_columns(df)

        try:
            logger.info(
                "[RANKING SUMMARY] columns=%s market_head=%s",
                list(df.columns),
                df["market"].head(10).tolist() if "market" in df.columns else [],
            )
            logger.info(
                "[RANKING SUMMARY] market_type_head=%s",
                df["market_type"].head(10).tolist() if "market_type" in df.columns else [],
            )
            logger.info(
                "[RANKING SUMMARY] market_type_norm_head=%s",
                df["market_type_norm"].head(10).tolist() if "market_type_norm" in df.columns else [],
            )
        except Exception:
            logger.debug("[RANKING SUMMARY] column profile log failed", exc_info=True)

        df = _apply_market_filter(df)

        try:
            from database.session import get_ranking_engine
            engine = get_ranking_engine()
        except Exception:
            engine = None

        if engine is not None:
            try:
                with engine.begin() as conn:
                    df = _attach_ranking_ma(conn, df)
            except Exception:
                logger.exception("[RANKING SUMMARY] attach ranking_ma with engine failed")
        else:
            logger.warning("[RANKING SUMMARY] ranking engine unavailable for ranking_ma attach")
            for c in ("ma5", "ma25", "ma75"):
                if c not in df.columns:
                    df[c] = pd.NA

        if "datetime" in df.columns:
            s = _to_dt_series(df["datetime"])
            if s.notna().any():
                latest = s.max()
                df = df.loc[s == latest].copy().reset_index(drop=True)

        if "source" not in df.columns:
            df["source"] = f"ranking_summary_{int(interval)}min"
        else:
            df["source"] = df["source"].fillna(f"ranking_summary_{int(interval)}min").astype(str)

        logger.info(
            "[RANKING SUMMARY] done interval=%s rows=%d symbols=%d latest_dt=%s ma75_nonnull=%d",
            interval,
            len(df),
            int(df["symbol"].astype(str).nunique()) if not df.empty and "symbol" in df.columns else 0,
            str(df["datetime"].max()) if not df.empty and "datetime" in df.columns else None,
            int(pd.to_numeric(df["ma75"], errors="coerce").notna().sum()) if "ma75" in df.columns else 0,
        )
        return df

    except Exception:
        logger.exception("[RANKING SUMMARY] build failed")
        return pd.DataFrame()


def run_ranking_summary(interval: int = 1, **kwargs) -> pd.DataFrame:
    return build_ranking_summary(interval=interval)


def job_ranking_summary(interval: int = 1, **kwargs) -> pd.DataFrame:
    return build_ranking_summary(interval=interval)


def run(interval: int = 1, **kwargs) -> pd.DataFrame:
    return build_ranking_summary(interval=interval)


__all__ = [
    "build_ranking_summary",
    "run_ranking_summary",
    "job_ranking_summary",
    "run",
]