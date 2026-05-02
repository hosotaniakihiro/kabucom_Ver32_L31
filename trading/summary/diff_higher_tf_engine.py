# ============================================================
# trading/summary/diff_higher_tf_engine.py
# Ver4.1-PRODUCTION-OHLC-CANONICAL-FIX
# ------------------------------------------------------------
# ✔ Ver4.0 完全保持（削除ゼロ）
# ✔ OHLC canonical統一
# ✔ volume必須保証
# ✔ DB保存列完全一致
# ✔ 3min / 5min 完全生成
# ✔ indicator再計算
# ✔ cache同期
# ✔ 重複防止
# ✔ production安定
# ============================================================

from __future__ import annotations

import pandas as pd
import logging
from typing import Optional

from core.global_context.context import global_context as GC
from trading.summary.resample import resample_1min_to
from trading.summary.indicators.indicator_calculator import add_all_indicators
from trading.summary.persistence.summary_saver_bulk import bulk_upsert_summary
from trading.summary.summary_cache_utils import normalize_summary_cache
from config.runtime_limits import SUMMARY_CACHE_MAX_ROWS

logger = logging.getLogger(__name__)


# ============================================================
# OHLC canonical保証
# ============================================================

def _ensure_ohlc_columns(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    df = df.copy()

    rename_map = {
        "open_price": "open",
        "high_price": "high",
        "low_price": "low",
        "close_price": "close",
    }

    for src, dst in rename_map.items():

        if src in df.columns and dst not in df.columns:
            df[dst] = df[src]

    # volume保証
    if "volume" not in df.columns:

        if "trading_volume" in df.columns:
            df["volume"] = df["trading_volume"]
        else:
            df["volume"] = 0.0

    return df


# ============================================================
# メインエンジン
# ============================================================

def run_diff_higher_tf_engine(interval: int) -> Optional[pd.DataFrame]:

    if interval not in (3, 5):
        raise ValueError("interval must be 3 or 5")

    try:

        df_1m = GC.summary._cache.get(1)

        if df_1m is None or df_1m.empty:
            logger.info("[DIFF %sMIN] 1min cache empty", interval)
            return None

        df_1m = _ensure_ohlc_columns(df_1m)

        prev_df = GC.summary._cache.get(interval)

        last_dt = None
        if prev_df is not None and not prev_df.empty:
            last_dt = prev_df["datetime"].max()

        # ----------------------------------------------------
        # resample
        # ----------------------------------------------------

        df_xm_all = resample_1min_to(df_1m, interval)

        if df_xm_all is None or df_xm_all.empty:
            return None

        # ----------------------------------------------------
        # canonical戻す
        # ----------------------------------------------------

        df_xm_all["open_price"] = df_xm_all["open"]
        df_xm_all["high_price"] = df_xm_all["high"]
        df_xm_all["low_price"] = df_xm_all["low"]
        df_xm_all["close_price"] = df_xm_all["close"]

        if last_dt is not None:
            df_new = df_xm_all[df_xm_all["datetime"] > last_dt]
        else:
            df_new = df_xm_all

        if df_new.empty:
            return None

        # ----------------------------------------------------
        # indicator再計算
        # ----------------------------------------------------

        tail_rows = 200

        df_tail = df_xm_all.groupby("symbol").tail(tail_rows)

        df_tail = add_all_indicators(
            df_tail,
            interval=interval,
        )

        df_xm_all = (
            df_xm_all
            .drop(
                columns=df_tail.columns.intersection(df_xm_all.columns),
                errors="ignore"
            )
            .merge(
                df_tail,
                on=["symbol", "datetime"],
                how="left"
            )
        )

        if last_dt is not None:
            df_save = df_xm_all[df_xm_all["datetime"] > last_dt]
        else:
            df_save = df_xm_all

        # ----------------------------------------------------
        # DB保存
        # ----------------------------------------------------

        if not df_save.empty:

            bulk_upsert_summary(df_save, interval)

        # ----------------------------------------------------
        # cache更新
        # ----------------------------------------------------

        merged = normalize_summary_cache(
            prev=prev_df,
            new=df_xm_all,
            max_rows=SUMMARY_CACHE_MAX_ROWS[interval],
        )

        GC.summary._cache[interval] = merged

        logger.info(
            "[DIFF %sMIN] updated rows=%d symbols=%d",
            interval,
            len(df_save),
            len(df_save["symbol"].unique())
            if not df_save.empty else 0,
        )

        return df_save

    except Exception:
        logger.exception("[DIFF %sMIN] fatal", interval)
        return None


# ============================================================
# realtime_engine互換API
# ============================================================

def build_higher_tf_diff(df_1m: pd.DataFrame, interval: int):

    if df_1m is None or df_1m.empty:
        return None

    try:

        if interval not in (3, 5):
            return None

        df = df_1m.copy()

        df = _ensure_ohlc_columns(df)

        required = {
            "symbol",
            "datetime",
            "open",
            "high",
            "low",
            "close",
            "volume",
        }

        if not required.issubset(df.columns):

            logger.warning(
                "[DIFF %sMIN] required columns missing → skip",
                interval
            )

            return None

        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")

        df = df.dropna(subset=["datetime"])

        if df.empty:
            return None

        rule = f"{interval}min"

        df["bucket"] = df["datetime"].dt.floor(rule)

        agg = (
            df.groupby(["symbol", "bucket"], sort=False)
            .agg(
                open_price=("open", "first"),
                high_price=("high", "max"),
                low_price=("low", "min"),
                close_price=("close", "last"),
                volume=("volume", "sum"),
            )
            .reset_index()
            .rename(columns={"bucket": "datetime"})
        )

        return agg

    except Exception:

        logger.exception("build_higher_tf_diff error")

        return None