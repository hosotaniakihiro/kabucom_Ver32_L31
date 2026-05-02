# ============================================================
# File   : trading/ranking/summary/symbol_metadata.py
# Version: Ver1.0-PRODUCTION-RANKING-SUMMARY-SYMBOL-METADATA
# ------------------------------------------------------------
# ranking summary 用 symbol / symbolname 補完ユーティリティ
# ranking_summary_engine.py から安全に切り出すためのモジュール
# ============================================================

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any, Dict

import pandas as pd

logger = logging.getLogger(__name__)

SYMBOL_FLAGS_DB = r"\\192.168.0.22\AutoStockBuyAndSell\Basic\symbol_flags.db"

# ============================================================
# global_data 互換解決
# ============================================================

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
# basic helpers
# ============================================================

def _safe_str(v: Any) -> str:
    try:
        if v is None:
            return ""
        s = str(v).strip()
        if s.lower() in {"nan", "none", "nat"}:
            return ""
        return s
    except Exception:
        return ""


def _normalize_symbol(v: Any) -> str:
    s = _safe_str(v)
    if not s:
        return ""
    if "." in s:
        s = s.split(".", 1)[0].strip()
    return s


def _normalize_symbolname(v: Any) -> str:
    try:
        if v is None:
            return ""
        if pd.isna(v):
            return ""
        s = str(v).strip()
        if not s:
            return ""
        if s.lower() in {"nan", "none", "nat"}:
            return ""
        if s == "0":
            return ""
        return s
    except Exception:
        return ""


def _last_non_empty(series: pd.Series) -> str:
    try:
        if series is None or series.empty:
            return ""
        vals = series.tolist()
        for v in reversed(vals):
            s = _normalize_symbolname(v)
            if s:
                return s
        return ""
    except Exception:
        return ""


# ============================================================
# symbolname map loaders
# ============================================================

def _load_symbolname_map_from_global() -> Dict[str, str]:
    out: Dict[str, str] = {}

    try:
        candidates = [
            getattr(global_data, "symbol_name_map", None),
            getattr(global_data, "symbolname_map", None),
            getattr(global_data, "symbol_names", None),
            getattr(global_data, "symbols_master", None),
        ]

        for item in candidates:
            if isinstance(item, dict):
                for k, v in item.items():
                    sym = _normalize_symbol(k)
                    name = _normalize_symbolname(v)
                    if sym and name:
                        out[sym] = name

            elif isinstance(item, pd.DataFrame) and not item.empty:
                cols = {str(c).lower(): c for c in item.columns}
                sym_col = cols.get("symbol")
                name_col = cols.get("symbolname") or cols.get("name") or cols.get("symbol_name")
                if sym_col and name_col:
                    tmp = item[[sym_col, name_col]].copy()
                    for _, row in tmp.iterrows():
                        sym = _normalize_symbol(row.get(sym_col))
                        name = _normalize_symbolname(row.get(name_col))
                        if sym and name:
                            out[sym] = name

    except Exception:
        logger.exception("[RANKING SUMMARY] load symbolname map from global failed")

    return out


def _load_symbolname_map_from_db(db_path: str = SYMBOL_FLAGS_DB) -> Dict[str, str]:
    out: Dict[str, str] = {}

    try:
        p = Path(db_path)
        if not p.exists():
            logger.warning("[RANKING SUMMARY] symbol_flags db not found: %s", db_path)
            return out

        con = sqlite3.connect(str(p))
        try:
            df = pd.read_sql_query(
                """
                SELECT symbol, symbolname
                FROM symbol_flags
                WHERE symbol IS NOT NULL
                """,
                con,
            )
        finally:
            con.close()

        if df.empty:
            return out

        for _, row in df.iterrows():
            sym = _normalize_symbol(row.get("symbol"))
            name = _normalize_symbolname(row.get("symbolname"))
            if sym and name:
                out[sym] = name

        logger.info("[RANKING SUMMARY] loaded symbolname map from db rows=%d", len(out))
        return out

    except Exception:
        logger.exception("[RANKING SUMMARY] load symbolname map from db failed")
        return out


# ============================================================
# public helpers
# ============================================================

def _ensure_symbolname(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df.copy()

    out = df.copy()

    if "symbol" not in out.columns:
        logger.warning("[RANKING SUMMARY] symbol column missing while ensuring symbolname")
        out["symbolname"] = ""
        return out

    out["symbol"] = out["symbol"].map(_normalize_symbol)

    if "symbolname" not in out.columns:
        out["symbolname"] = ""
    else:
        out["symbolname"] = out["symbolname"].map(_normalize_symbolname)

    missing_mask = out["symbolname"].eq("")
    missing_before = int(missing_mask.sum())

    if missing_before <= 0:
        return out

    symbol_map: Dict[str, str] = {}

    try:
        symbol_map.update(_load_symbolname_map_from_db())
    except Exception:
        logger.exception("[RANKING SUMMARY] db symbolname merge failed")

    try:
        symbol_map.update(_load_symbolname_map_from_global())
    except Exception:
        logger.exception("[RANKING SUMMARY] global symbolname merge failed")

    if symbol_map:
        try:
            out.loc[missing_mask, "symbolname"] = (
                out.loc[missing_mask, "symbol"]
                .map(lambda x: symbol_map.get(_normalize_symbol(x), ""))
                .fillna("")
                .astype(str)
                .str.strip()
            )
        except Exception:
            logger.exception("[RANKING SUMMARY] symbolname fill from map failed")

    out["symbolname"] = out["symbolname"].map(_normalize_symbolname)

    unresolved = int(out["symbolname"].eq("").sum())
    logger.info(
        "[RANKING SUMMARY] symbolname ensured total=%d missing_before=%d unresolved=%d",
        len(out),
        missing_before,
        unresolved,
    )

    return out


def _display_symbolname(row: pd.Series) -> str:
    name = _normalize_symbolname(row.get("symbolname"))
    if name:
        return name
    return _normalize_symbol(row.get("symbol"))


__all__ = [
    "SYMBOL_FLAGS_DB",
    "_safe_str",
    "_normalize_symbol",
    "_normalize_symbolname",
    "_last_non_empty",
    "_load_symbolname_map_from_global",
    "_load_symbolname_map_from_db",
    "_ensure_symbolname",
    "_display_symbolname",
]