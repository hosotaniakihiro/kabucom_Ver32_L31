# ============================================================
# File   : trading/summary/summary_analysis_logger.py
# Version: Ver22.0-PRODUCTION-SUMMARY-ANALYSIS-LOGGER-UNIFIED-FORMAT
# ------------------------------------------------------------
# ✔ display_score prioritized
# ✔ score_total/final_score fallback
# ✔ ETF/ETN/REIT/index inverse exclusion
# ✔ volume>0 filter
# ✔ JPX market filter if available
# ✔ market_type_norm prioritized
# ✔ TS/TP/TG normalization added
# ✔ safe logger for analysis view
# ✔ legacy compatibility keep
# ✔ production hardened
# ✔ symbolname backfill added
# ✔ global_data symbol map fallback
# ✔ symbol_flags.db fallback
# ✔ empty/duplicate symbolname safe handling
# ✔ display debug log enhanced
# ✔ RSIはNaN→0.0へ潰さない
# ✔ RSI未計算は "-" 表示
# ✔ RSI表示専用formatter追加
# ✔ technical_ready=False の銘柄は RSI を表示しない
# ✔ ready列候補を吸収
# ✔ NEW: name / symbolname を1本化して表示
# ✔ NEW: datetime は表示しない
# ✔ NEW: 価格系は小数第1位
# ✔ NEW: 指標系は小数第2位
# ============================================================

from __future__ import annotations

import logging
import os
import sqlite3
from typing import Iterable, Optional

import pandas as pd

logger = logging.getLogger(__name__)

_ALLOWED_MARKETS = {"プライム", "スタンダード", "グロース"}

_EXCLUDE_NAME_KEYWORDS = [
    "ETF",
    "ETN",
    "REIT",
    "指数",
    "連動",
    "レバ",
    "インバース",
    "ベア",
    "ダブル",
]

_MARKET_COL_CANDIDATES = [
    "market_type_norm",
    "market_type",
    "market",
    "market_name",
    "市場",
    "市場区分",
]

_SCORE_COL_CANDIDATES = [
    "display_score",
    "score_total",
    "final_score",
    "combined_score",
    "score",
    "absolute_score",
    "buy_score",
    "score_buy",
]

_CLOSE_COL_CANDIDATES = [
    "close",
    "close_price",
    "closevalue",
    "closeValue",
    "price",
    "現在値",
    "終値",
]

_VOLUME_COL_CANDIDATES = [
    "volume",
    "出来高",
    "volume_total",
]

_RSI_COL_CANDIDATES = [
    "rsi",
]

_MA75_COL_CANDIDATES = [
    "ma75",
]

_READY_COL_CANDIDATES = [
    "technical_ready",
    "is_technical_ready",
    "ready",
]

_SYMBOLNAME_COL_CANDIDATES = [
    "symbolname",
    "name",
    "銘柄名",
]

_SYMBOLNAME_DB_CANDIDATES = [
    r"\\192.168.0.22\AutoStockBuyAndSell\Basic\symbol_flags.db",
    r"\\192.168.0.22\AutoStockBuyAndSell\basic\symbol_flags.db",
]


# ============================================================
# helpers
# ============================================================

def _first_existing(columns: Iterable[str], candidates: list[str]) -> Optional[str]:
    colset = set(columns)
    for c in candidates:
        if c in colset:
            return c
    return None


def _series_numeric(df: pd.DataFrame, col: Optional[str], default: float = 0.0) -> pd.Series:
    if not col or col not in df.columns:
        return pd.Series(default, index=df.index, dtype="float64")
    return pd.to_numeric(df[col], errors="coerce").fillna(default)


def _series_numeric_keep_nan(df: pd.DataFrame, col: Optional[str]) -> pd.Series:
    if not col or col not in df.columns:
        return pd.Series([pd.NA] * len(df), index=df.index, dtype="object")
    return pd.to_numeric(df[col], errors="coerce")


def _series_text(df: pd.DataFrame, col: Optional[str], default: str = "") -> pd.Series:
    if not col or col not in df.columns:
        return pd.Series(default, index=df.index, dtype="object")
    return df[col].fillna(default).astype(str)


def _series_bool(df: pd.DataFrame, col: Optional[str], default: bool = False) -> pd.Series:
    if not col or col not in df.columns:
        return pd.Series(default, index=df.index, dtype="bool")

    s = df[col]

    if pd.api.types.is_bool_dtype(s):
        return s.fillna(default).astype(bool)

    txt = s.fillna("").astype(str).str.strip().str.lower()
    true_set = {"1", "true", "t", "yes", "y", "on"}
    false_set = {"0", "false", "f", "no", "n", "off", ""}

    out = pd.Series(default, index=df.index, dtype="bool")
    out.loc[txt.isin(true_set)] = True
    out.loc[txt.isin(false_set)] = False

    num = pd.to_numeric(s, errors="coerce")
    out.loc[num.notna()] = num.loc[num.notna()].astype(float) != 0.0

    return out


def _resolve_score_column(df: pd.DataFrame) -> Optional[str]:
    for c in _SCORE_COL_CANDIDATES:
        if c in df.columns:
            s = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
            if int((s != 0).sum()) > 0:
                return c

    for c in _SCORE_COL_CANDIDATES:
        if c in df.columns:
            return c
    return None


def _resolve_close_column(df: pd.DataFrame) -> Optional[str]:
    return _first_existing(df.columns, _CLOSE_COL_CANDIDATES)


def _resolve_volume_column(df: pd.DataFrame) -> Optional[str]:
    return _first_existing(df.columns, _VOLUME_COL_CANDIDATES)


def _resolve_rsi_column(df: pd.DataFrame) -> Optional[str]:
    return _first_existing(df.columns, _RSI_COL_CANDIDATES)


def _resolve_ma75_column(df: pd.DataFrame) -> Optional[str]:
    return _first_existing(df.columns, _MA75_COL_CANDIDATES)


def _resolve_ready_column(df: pd.DataFrame) -> Optional[str]:
    return _first_existing(df.columns, _READY_COL_CANDIDATES)


def _resolve_symbolname_column(df: pd.DataFrame) -> Optional[str]:
    return _first_existing(df.columns, _SYMBOLNAME_COL_CANDIDATES)


def _resolve_market_column(df: pd.DataFrame) -> Optional[str]:
    return _first_existing(df.columns, _MARKET_COL_CANDIDATES)


def _ensure_symbol(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    if "symbol" not in out.columns:
        out["symbol"] = ""
    out["symbol"] = out["symbol"].astype(str).str.strip()
    out = out[out["symbol"] != ""].copy()
    return out


def _normalize_market_text(x) -> str:
    s_raw = str(x or "").strip()
    s = s_raw.upper()

    mapping = {
        "PRIME": "プライム",
        "STANDARD": "スタンダード",
        "GROWTH": "グロース",
        "P": "プライム",
        "S": "スタンダード",
        "G": "グロース",
        "TP": "プライム",
        "TS": "スタンダード",
        "TG": "グロース",
        "東証P": "プライム",
        "東証S": "スタンダード",
        "東証G": "グロース",
        "TSE PRIME": "プライム",
        "TSE STANDARD": "スタンダード",
        "TSE GROWTH": "グロース",
    }
    return mapping.get(s, s_raw)


def _load_symbol_map_from_global_data() -> dict[str, str]:
    out: dict[str, str] = {}

    try:
        from global_state import global_data  # type: ignore
    except Exception:
        return out

    candidate_attrs = [
        "symbol_name_map",
        "symbolname_map",
        "symbol_master_map",
        "symbol_map",
        "symbol_names",
    ]

    for attr in candidate_attrs:
        try:
            maybe = getattr(global_data, attr, None)
            if isinstance(maybe, dict) and maybe:
                for k, v in maybe.items():
                    key = str(k).strip()
                    val = str(v).strip() if v is not None else ""
                    if key and val and val != key:
                        out[key] = val
        except Exception:
            logger.debug("[summary_analysis_logger] global_data attr load failed: %s", attr, exc_info=True)

    for method_name in ("get_symbol_name_map", "get_symbolname_map"):
        try:
            method = getattr(global_data, method_name, None)
            if callable(method):
                maybe = method()
                if isinstance(maybe, dict) and maybe:
                    for k, v in maybe.items():
                        key = str(k).strip()
                        val = str(v).strip() if v is not None else ""
                        if key and val and val != key:
                            out[key] = val
        except Exception:
            logger.debug("[summary_analysis_logger] global_data method load failed: %s", method_name, exc_info=True)

    return out


def _load_symbol_map_from_db() -> dict[str, str]:
    out: dict[str, str] = {}
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
                logger.info(
                    "[summary_analysis_logger] symbol map loaded from db path=%s rows=%d",
                    db_path,
                    len(out),
                )
                return out

        except Exception:
            logger.debug(
                "[summary_analysis_logger] symbol map db load failed path=%s",
                db_path,
                exc_info=True,
            )

    return out


def _backfill_symbolname(df: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return df.copy() if isinstance(df, pd.DataFrame) else pd.DataFrame()

    out = df.copy()

    if "symbol" not in out.columns:
        return out

    if "symbolname" not in out.columns:
        out["symbolname"] = ""

    if "name" not in out.columns:
        out["name"] = ""

    out["symbol"] = out["symbol"].astype(str).str.strip()
    out["symbolname"] = out["symbolname"].fillna("").astype(str).str.strip()
    out["name"] = out["name"].fillna("").astype(str).str.strip()

    miss_symbolname = out["symbolname"].eq("") | out["symbolname"].eq(out["symbol"])
    miss_name = out["name"].eq("") | out["name"].eq(out["symbol"])

    symbol_map = {}
    try:
        symbol_map.update(_load_symbol_map_from_global_data())
    except Exception:
        logger.debug("[summary_analysis_logger] global_data symbol map load failed", exc_info=True)

    if not symbol_map:
        try:
            symbol_map.update(_load_symbol_map_from_db())
        except Exception:
            logger.debug("[summary_analysis_logger] db symbol map load failed", exc_info=True)

    if symbol_map:
        out.loc[miss_symbolname, "symbolname"] = (
            out.loc[miss_symbolname, "symbol"]
            .map(symbol_map)
            .fillna(out.loc[miss_symbolname, "symbolname"])
            .astype(str)
            .str.strip()
        )
        out.loc[miss_name, "name"] = (
            out.loc[miss_name, "symbol"]
            .map(symbol_map)
            .fillna(out.loc[miss_name, "name"])
            .astype(str)
            .str.strip()
        )

    miss2 = out["symbolname"].eq("")
    out.loc[miss2, "symbolname"] = out.loc[miss2, "symbol"]

    miss3 = out["name"].eq("")
    out.loc[miss3, "name"] = out.loc[miss3, "symbolname"]

    logger.info(
        "[summary_analysis_logger] symbolname backfill total=%d mapped=%d fallback_to_symbol=%d",
        len(out),
        int((out["symbolname"] != out["symbol"]).sum()),
        int((out["symbolname"] == out["symbol"]).sum()),
    )
    return out


def _unify_display_name(df: pd.DataFrame) -> pd.Series:
    """
    表示名は symbolname 優先、空なら name、さらに空なら symbol
    """
    symbol_s = _series_text(df, "symbol")
    symbolname_s = _series_text(df, "symbolname").astype(str).str.strip()
    name_s = _series_text(df, "name").astype(str).str.strip()

    out = symbolname_s.copy()
    out = out.mask(out.eq(""), name_s)
    out = out.mask(out.eq(""), symbol_s)
    out = out.fillna("").astype(str).str.strip()
    out = out.mask(out.eq(""), symbol_s)
    return out


def _apply_market_filter(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    market_col = _resolve_market_column(out)
    if market_col is None:
        return out

    market_s = _series_text(out, market_col).map(_normalize_market_text)
    before = len(out)
    out = out[market_s.isin(_ALLOWED_MARKETS)].copy()
    after = len(out)

    logger.info(
        "[summary_analysis_logger] market filter applied before=%d after=%d col=%s unique_sample=%s",
        before,
        after,
        market_col,
        market_s.dropna().astype(str).unique()[:10].tolist(),
    )
    return out


def _apply_name_exclusion(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    if "_display_name" in out.columns:
        name_s = out["_display_name"].fillna("").astype(str)
    else:
        name_col = _resolve_symbolname_column(out)
        if name_col is None:
            return out
        name_s = _series_text(out, name_col)

    mask = pd.Series(True, index=out.index)
    for kw in _EXCLUDE_NAME_KEYWORDS:
        mask &= ~name_s.str.contains(kw, case=False, na=False)

    before = len(out)
    out = out[mask].copy()
    after = len(out)

    logger.info(
        "[summary_analysis_logger] name exclusion applied before=%d after=%d",
        before,
        after,
    )
    return out


def _apply_volume_filter(df: pd.DataFrame, min_volume: float) -> pd.DataFrame:
    out = df.copy()
    volume_col = _resolve_volume_column(out)
    if volume_col is None:
        return out

    vol = _series_numeric(out, volume_col)
    before = len(out)
    out = out[vol >= float(min_volume)].copy()
    after = len(out)

    logger.info(
        "[summary_analysis_logger] volume filter applied before=%d after=%d col=%s min_volume=%s",
        before,
        after,
        volume_col,
        min_volume,
    )
    return out


def _prepare_display_frame(
    df: pd.DataFrame,
    apply_market_filter: bool = True,
    apply_name_filter: bool = True,
    min_volume: float = 1.0,
) -> pd.DataFrame:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return df.copy() if isinstance(df, pd.DataFrame) else pd.DataFrame()

    out = df.copy()
    out = _ensure_symbol(out)
    out = _backfill_symbolname(out)

    out["_display_name"] = _unify_display_name(out)

    if apply_market_filter:
        out = _apply_market_filter(out)

    if apply_name_filter:
        out = _apply_name_exclusion(out)

    out = _apply_volume_filter(out, min_volume=min_volume)

    if out.empty:
        return out

    score_col = _resolve_score_column(out)
    close_col = _resolve_close_column(out)
    volume_col = _resolve_volume_column(out)
    ma75_col = _resolve_ma75_column(out)
    rsi_col = _resolve_rsi_column(out)
    ready_col = _resolve_ready_column(out)

    out["_display_score"] = _series_numeric(out, score_col)
    out["_display_close"] = _series_numeric(out, close_col)
    out["_display_volume"] = _series_numeric(out, volume_col)
    out["_display_ma75"] = _series_numeric(out, ma75_col)
    out["_display_rsi"] = _series_numeric_keep_nan(out, rsi_col)
    out["_technical_ready"] = _series_bool(out, ready_col, default=False)

    out.loc[~out["_technical_ready"], "_display_rsi"] = pd.NA

    out = out.sort_values(
        by=["_display_score", "_display_volume", "symbol"],
        ascending=[False, False, True],
        kind="mergesort",
    ).reset_index(drop=True)

    logger.info(
        "[summary_analysis_logger] prepared rows=%d score_col=%s close_col=%s volume_col=%s rsi_col=%s ready_col=%s rsi_nonnull=%d ready_rows=%d display_name_nonempty=%d",
        len(out),
        score_col,
        close_col,
        volume_col,
        rsi_col,
        ready_col,
        int(pd.to_numeric(out["_display_rsi"], errors="coerce").notna().sum()),
        int(out["_technical_ready"].fillna(False).astype(bool).sum()),
        int(out["_display_name"].astype(str).str.strip().ne("").sum()),
    )

    return out


def _format_symbol_name(symbol: str, name: str) -> str:
    symbol = str(symbol).strip()
    name = str(name).strip()
    if not name or name == symbol:
        return f"{symbol}"
    return f"{symbol}({name})"


def _fmt_price1(x) -> str:
    try:
        v = float(x)
        if pd.isna(v):
            return "-"
        return f"{v:.1f}"
    except Exception:
        return "-"


def _fmt_metric2(x) -> str:
    try:
        v = float(x)
        if pd.isna(v):
            return "-"
        return f"{v:.2f}"
    except Exception:
        return "-"


def _fmt_rsi(x) -> str:
    try:
        v = float(x)
        if pd.isna(v):
            return "-"
        return f"{v:.2f}"
    except Exception:
        return "-"


def _log_top_rows(df: pd.DataFrame, title: str, top_n: int = 10) -> None:
    logger.info("========== %s ==========", title)

    if not isinstance(df, pd.DataFrame) or df.empty:
        logger.info("[summary_analysis_logger] no rows")
        return

    top = df.head(int(top_n)).copy()

    for i, (_, row) in enumerate(top.iterrows(), start=1):
        symbol = str(row.get("symbol", "")).strip()
        name = str(row.get("_display_name", "")).strip()
        score = row.get("_display_score", pd.NA)
        close = row.get("_display_close", pd.NA)
        volume = row.get("_display_volume", pd.NA)
        ma75 = row.get("_display_ma75", pd.NA)
        rsi = row.get("_display_rsi", pd.NA)

        logger.info(
            "%2d. %s score=%s C=%s V=%s MA75=%s RSI=%s",
            i,
            _format_symbol_name(symbol, name),
            _fmt_metric2(score),
            _fmt_price1(close),
            _fmt_price1(volume),
            _fmt_price1(ma75),
            _fmt_rsi(rsi),
        )


# ============================================================
# public api
# ============================================================

def log_summary_analysis(
    df: pd.DataFrame,
    interval: Optional[int | str] = None,
    top_n: int = 10,
    min_volume: float = 1.0,
    apply_market_filter: bool = True,
    apply_name_filter: bool = True,
) -> pd.DataFrame:
    if not isinstance(df, pd.DataFrame):
        logger.warning("[summary_analysis_logger] input is not DataFrame")
        return pd.DataFrame()

    if df.empty:
        logger.info("[summary_analysis_logger] empty input")
        return df.copy()

    prepared = _prepare_display_frame(
        df=df,
        apply_market_filter=apply_market_filter,
        apply_name_filter=apply_name_filter,
        min_volume=min_volume,
    )

    title = "📊 SUMMARY RANKING"
    if interval is not None:
        title = f"📊 SUMMARY RANKING ({interval}min)" if isinstance(interval, int) else f"📊 SUMMARY RANKING ({interval})"

    _log_top_rows(prepared, title=title, top_n=top_n)
    return prepared


def print_summary_analysis(
    df: pd.DataFrame,
    interval: Optional[int | str] = None,
    top_n: int = 10,
    min_volume: float = 1.0,
) -> pd.DataFrame:
    return log_summary_analysis(
        df=df,
        interval=interval,
        top_n=top_n,
        min_volume=min_volume,
        apply_market_filter=True,
        apply_name_filter=True,
    )


def log_summary_ranking_analysis(
    df: pd.DataFrame,
    interval: Optional[int | str] = None,
    top_n: int = 10,
    min_volume: float = 1.0,
) -> pd.DataFrame:
    return log_summary_analysis(
        df=df,
        interval=interval,
        top_n=top_n,
        min_volume=min_volume,
        apply_market_filter=True,
        apply_name_filter=True,
    )


def verify_summary_vs_entry(
    summary_df: pd.DataFrame,
    entry_df: Optional[pd.DataFrame] = None,
    interval: Optional[int | str] = None,
    top_n: int = 10,
    min_volume: float = 1.0,
) -> pd.DataFrame:
    if not isinstance(summary_df, pd.DataFrame):
        logger.warning("[summary_analysis_logger] verify_summary_vs_entry: summary_df is not DataFrame")
        return pd.DataFrame()

    prepared = _prepare_display_frame(
        df=summary_df,
        apply_market_filter=True,
        apply_name_filter=True,
        min_volume=min_volume,
    )

    if prepared.empty:
        logger.info("[summary_analysis_logger] verify_summary_vs_entry: prepared summary empty")
        return prepared

    prepared = prepared.copy()

    entry_symbols = set()
    if isinstance(entry_df, pd.DataFrame) and not entry_df.empty and "symbol" in entry_df.columns:
        try:
            entry_symbols = set(entry_df["symbol"].astype(str).str.strip().tolist())
        except Exception:
            entry_symbols = set()

    prepared["in_entry_candidates"] = prepared["symbol"].astype(str).isin(entry_symbols)

    title = "📊 SUMMARY vs ENTRY VERIFY"
    if interval is not None:
        title = f"📊 SUMMARY vs ENTRY VERIFY ({interval}min)" if isinstance(interval, int) else f"📊 SUMMARY vs ENTRY VERIFY ({interval})"

    logger.info("========== %s ==========", title)

    top = prepared.head(int(top_n)).copy()
    for i, (_, row) in enumerate(top.iterrows(), start=1):
        symbol = str(row.get("symbol", "")).strip()
        name = str(row.get("_display_name", "")).strip()
        score = row.get("_display_score", pd.NA)
        close = row.get("_display_close", pd.NA)
        volume = row.get("_display_volume", pd.NA)
        ma75 = row.get("_display_ma75", pd.NA)
        rsi = row.get("_display_rsi", pd.NA)
        hit = bool(row.get("in_entry_candidates", False))

        logger.info(
            "%2d. %s score=%s C=%s V=%s MA75=%s RSI=%s entry_hit=%s",
            i,
            _format_symbol_name(symbol, name),
            _fmt_metric2(score),
            _fmt_price1(close),
            _fmt_price1(volume),
            _fmt_price1(ma75),
            _fmt_rsi(rsi),
            hit,
        )

    return prepared


def log_summary_ranking(
    df: pd.DataFrame,
    interval: Optional[int | str] = None,
    top_n: int = 10,
    min_volume: float = 1.0,
) -> pd.DataFrame:
    return log_summary_analysis(
        df=df,
        interval=interval,
        top_n=top_n,
        min_volume=min_volume,
        apply_market_filter=True,
        apply_name_filter=True,
    )


def print_summary_ranking(
    df: pd.DataFrame,
    interval: Optional[int | str] = None,
    top_n: int = 10,
    min_volume: float = 1.0,
) -> pd.DataFrame:
    return log_summary_analysis(
        df=df,
        interval=interval,
        top_n=top_n,
        min_volume=min_volume,
        apply_market_filter=True,
        apply_name_filter=True,
    )


def analyze_summary_ranking(
    df: pd.DataFrame,
    interval: Optional[int | str] = None,
    top_n: int = 10,
    min_volume: float = 1.0,
) -> pd.DataFrame:
    return log_summary_analysis(
        df=df,
        interval=interval,
        top_n=top_n,
        min_volume=min_volume,
        apply_market_filter=True,
        apply_name_filter=True,
    )
