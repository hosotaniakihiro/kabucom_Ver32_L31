# ============================================================
# File   : trading/summary/pipeline/fetch_pipeline.py
# Version: Ver2.1-PRODUCTION-HARDENED-FETCH-PIPELINE-SOURCE-PUSH-FIX
# ------------------------------------------------------------
# ✔ summary取得パイプライン
# ✔ global_data summary取得
# ✔ DataFrame安全化
# ✔ NaN / inf 防御
# ✔ symbolname保証
# ✔ interval対応
# ✔ logger
# ✔ dtype安全化
# ✔ datetime安全化
# ✔ production hardened
# ✔ 本番安定版
# ✔ NEW: get_merged_summary を source="push" 固定
# ✔ NEW: source 対応前の旧 global_state にも後方互換
# ============================================================

from __future__ import annotations

import logging
import numpy as np
import pandas as pd

from global_state import global_data

logger = logging.getLogger(__name__)


# ============================================================
# DataFrame safety
# ============================================================

def _safe_df(df) -> pd.DataFrame:

    try:

        if df is None:
            return pd.DataFrame()

        if not isinstance(df, pd.DataFrame):
            return pd.DataFrame()

        if df.empty:
            return pd.DataFrame()

        df = df.copy()

        # inf → NaN
        try:
            df.replace([np.inf, -np.inf], np.nan, inplace=True)
        except Exception:
            pass

        # index normalize
        try:
            df = df.reset_index(drop=True)
        except Exception:
            pass

        return df

    except Exception:

        logger.exception("[fetch_pipeline] safe_df failed")

        return pd.DataFrame()


# ============================================================
# symbolname guarantee
# ============================================================

def _ensure_symbolname(df: pd.DataFrame) -> pd.DataFrame:

    try:

        if df.empty:
            return df

        if "symbol" not in df.columns:
            return df

        name_candidates = [
            "symbolname",
            "name",
            "symbol_name",
            "symbolname_jp",
        ]

        name_col = None

        for c in name_candidates:

            if c in df.columns:
                name_col = c
                break

        if name_col is None:

            df["symbolname"] = df["symbol"]

        else:

            df["symbolname"] = df[name_col]

        df["symbolname"] = (
            df["symbolname"]
            .fillna(df["symbol"])
            .astype(str)
        )

        # 空文字防御
        try:

            df.loc[
                df["symbolname"].str.strip() == "",
                "symbolname"
            ] = df["symbol"]

        except Exception:
            pass

        return df

    except Exception:

        logger.exception("[fetch_pipeline] symbolname guarantee failed")

        try:
            df["symbolname"] = df["symbol"]
        except Exception:
            pass

        return df


# ============================================================
# symbol normalize
# ============================================================

def _normalize_symbol(df: pd.DataFrame) -> pd.DataFrame:

    try:

        if "symbol" not in df.columns:
            return df

        df["symbol"] = df["symbol"].astype(str).str.strip()

        return df

    except Exception:

        logger.exception("[fetch_pipeline] symbol normalize failed")

        return df


# ============================================================
# datetime normalize
# ============================================================

def _normalize_datetime(df: pd.DataFrame) -> pd.DataFrame:

    try:

        if "datetime" not in df.columns:
            return df

        df["datetime"] = pd.to_datetime(
            df["datetime"],
            errors="coerce"
        )

        return df

    except Exception:

        logger.exception("[fetch_pipeline] datetime normalize failed")

        return df


# ============================================================
# final sanitize
# ============================================================

def _final_sanitize(df: pd.DataFrame) -> pd.DataFrame:

    try:

        if df.empty:
            return df

        # inf
        try:
            df.replace([np.inf, -np.inf], np.nan, inplace=True)
        except Exception:
            pass

        # object列のみ fill
        try:

            obj_cols = df.select_dtypes(include="object").columns

            for c in obj_cols:

                df[c] = df[c].fillna("")

        except Exception:
            pass

        return df

    except Exception:

        logger.exception("[fetch_pipeline] final sanitize failed")

        return df


# ============================================================
# fetch helper
# ============================================================

def _get_push_merged_summary(interval: int) -> pd.DataFrame:

    try:
        getter = getattr(global_data, "get_merged_summary", None)

        if callable(getter):
            try:
                df = getter(interval, source="push")
            except TypeError:
                df = getter(interval)

            df = _safe_df(df)
            if not df.empty:
                return df

    except Exception:
        logger.exception(
            "[fetch_pipeline] get_merged_summary(source=push) failed interval=%s",
            interval,
        )

    try:
        getter = getattr(global_data, "get_push_merged_summary", None)

        if callable(getter):
            df = getter(interval)
            df = _safe_df(df)
            if not df.empty:
                return df

    except Exception:
        logger.exception(
            "[fetch_pipeline] get_push_merged_summary fallback failed interval=%s",
            interval,
        )

    try:
        df = getattr(global_data, f"merged_summary_{int(interval)}", None)
        df = _safe_df(df)
        if not df.empty:
            logger.warning(
                "[fetch_pipeline] legacy merged_summary_%s fallback used",
                interval,
            )
            return df
    except Exception:
        logger.exception(
            "[fetch_pipeline] legacy merged_summary_%s fallback failed",
            interval,
        )

    return pd.DataFrame()


# ============================================================
# main fetch pipeline
# ============================================================

def run_fetch_pipeline(interval: int) -> pd.DataFrame:

    try:

        # ----------------------------------------------------
        # fetch from global_data (PUSH fixed)
        # ----------------------------------------------------

        df = _get_push_merged_summary(int(interval))

        if df.empty:

            logger.warning(
                "[fetch_pipeline] summary empty interval=%s",
                interval
            )

            return pd.DataFrame()

        # ----------------------------------------------------
        # normalize
        # ----------------------------------------------------

        df = _normalize_symbol(df)

        df = _normalize_datetime(df)

        # ----------------------------------------------------
        # symbolname guarantee
        # ----------------------------------------------------

        df = _ensure_symbolname(df)

        # ----------------------------------------------------
        # final sanitize
        # ----------------------------------------------------

        df = _final_sanitize(df)

        # ----------------------------------------------------
        # log
        # ----------------------------------------------------

        logger.info(
            "[fetch_pipeline] interval=%s rows=%s cols=%s",
            interval,
            len(df),
            len(df.columns)
        )

        return df.reset_index(drop=True)

    except Exception:

        logger.exception(
            "[fetch_pipeline] failed interval=%s",
            interval
        )

        return pd.DataFrame()