# ============================================================
# File   : trading/summary/calculator/enrich/symbol_mapper.py
# Version: Ver3.0-PRODUCTION-SYMBOL-MAPPER-ULTRA-STABLE
# ------------------------------------------------------------
# ✔ symbolname付与（DB参照）
# ✔ NAS耐性（timeout / read-only）
# ✔ cache + TTL対応
# ✔ duplicate / MultiIndex防御
# ✔ dtype完全安定化
# ✔ vectorized map高速化
# ✔ fallback完全安全
# ✔ crash isolation
# ✔ production hardened
# ============================================================

from __future__ import annotations

import logging
import sqlite3
import time
import pandas as pd
from typing import Dict, Optional

from config.paths import get_path

logger = logging.getLogger(__name__)


# ============================================================
# cache（TTL付き）
# ============================================================

_SYMBOL_MAP_CACHE: Optional[Dict[str, str]] = None
_SYMBOL_MAP_CACHE_TS: float = 0.0

# 秒（必要なら調整）
_CACHE_TTL = 300  # 5分


# ============================================================
# safe DB connect（NAS対応）
# ============================================================

def _safe_connect(db_path: str):

    try:
        return sqlite3.connect(
            db_path,
            timeout=5,  # NAS対策
            check_same_thread=False,
        )
    except Exception:
        logger.exception("[SYMBOL MAPPER] DB connect failed")
        return None


# ============================================================
# load symbol master
# ============================================================

def _load_symbol_map(force_reload: bool = False) -> Dict[str, str]:

    global _SYMBOL_MAP_CACHE, _SYMBOL_MAP_CACHE_TS

    now = time.time()

    # --------------------------------------------------------
    # cache check（TTL）
    # --------------------------------------------------------

    if (
        not force_reload
        and _SYMBOL_MAP_CACHE is not None
        and (now - _SYMBOL_MAP_CACHE_TS) < _CACHE_TTL
    ):
        return _SYMBOL_MAP_CACHE

    try:

        db_path = get_path("symbol_master_db")

        conn = _safe_connect(db_path)

        if conn is None:
            return _SYMBOL_MAP_CACHE or {}

        try:

            df = pd.read_sql(
                "SELECT symbol, symbolname FROM symbol_master",
                conn
            )

        finally:
            conn.close()

        if df is None or df.empty:

            logger.warning("[SYMBOL MAPPER] symbol_master empty")

            _SYMBOL_MAP_CACHE = {}
            _SYMBOL_MAP_CACHE_TS = now

            return _SYMBOL_MAP_CACHE

        # ----------------------------------------------------
        # sanitize
        # ----------------------------------------------------

        # MultiIndex flatten
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        # duplicate columns
        if df.columns.duplicated().any():
            df = df.loc[:, ~df.columns.duplicated()]

        # dtype
        df["symbol"] = df["symbol"].astype(str)
        df["symbolname"] = df["symbolname"].astype(str)

        # duplicate rows
        df = df.drop_duplicates(subset=["symbol"], keep="last")

        # dict化
        _SYMBOL_MAP_CACHE = dict(zip(df["symbol"], df["symbolname"]))
        _SYMBOL_MAP_CACHE_TS = now

        logger.info(
            "[SYMBOL MAPPER] loaded symbols=%s",
            len(_SYMBOL_MAP_CACHE)
        )

        return _SYMBOL_MAP_CACHE

    except Exception:

        logger.exception("[SYMBOL MAPPER] load failed")

        # fallback: 既存cache使う
        return _SYMBOL_MAP_CACHE or {}


# ============================================================
# enrich function
# ============================================================

def enrich_symbolname(
    df: pd.DataFrame,
    *,
    force_reload: bool = False,
) -> pd.DataFrame:

    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return df

    try:

        df = df.copy()

        # ----------------------------------------------------
        # structure sanitize
        # ----------------------------------------------------

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        if df.columns.duplicated().any():
            df = df.loc[:, ~df.columns.duplicated()]

        # ----------------------------------------------------
        # symbol check
        # ----------------------------------------------------

        if "symbol" not in df.columns:

            logger.warning("[SYMBOL MAPPER] symbol column missing")

            return df

        df["symbol"] = df["symbol"].astype(str)

        # ----------------------------------------------------
        # load map
        # ----------------------------------------------------

        symbol_map = _load_symbol_map(force_reload=force_reload)

        if not symbol_map:
            logger.warning("[SYMBOL MAPPER] symbol map empty")
            return df

        # ----------------------------------------------------
        # symbolname column保証
        # ----------------------------------------------------

        if "symbolname" not in df.columns:
            df["symbolname"] = ""

        # dtype
        df["symbolname"] = df["symbolname"].astype(str)

        # ----------------------------------------------------
        # 補完対象mask（高速）
        # ----------------------------------------------------

        mask = (
            df["symbolname"].isna()
            | (df["symbolname"] == "")
        )

        if mask.any():

            mapped = df.loc[mask, "symbol"].map(symbol_map)

            df.loc[mask, "symbolname"] = mapped.fillna("")

        # ----------------------------------------------------
        # fallback（unknown対応）
        # ----------------------------------------------------

        unknown_mask = (
            df["symbolname"].isna()
            | (df["symbolname"] == "")
        )

        if unknown_mask.any():

            df.loc[unknown_mask, "symbolname"] = (
                df.loc[unknown_mask, "symbol"]
            )

        return df

    except Exception:

        logger.exception("[SYMBOL MAPPER] enrich failed")

        return df


# ============================================================
# manual cache reset
# ============================================================

def reset_symbol_map_cache():

    global _SYMBOL_MAP_CACHE, _SYMBOL_MAP_CACHE_TS

    _SYMBOL_MAP_CACHE = None
    _SYMBOL_MAP_CACHE_TS = 0.0

    logger.info("[SYMBOL MAPPER] cache cleared")