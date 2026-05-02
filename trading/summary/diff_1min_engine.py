# ============================================================
# trading/summary/diff_1min_engine.py
# Ver3.2-PRODUCTION-ULTRA-STABLE
# ------------------------------------------------------------
# ✔ Ver3.0 完全互換（削除ゼロ）
# ✔ 1分足 差分専用エンジン
# ✔ push_ring_buffer 前提
# ✔ last_datetime 以降のみ処理
# ✔ confirmed barのみ生成
# ✔ indicator再計算は必要範囲のみ
# ✔ DB bulk upsert
# ✔ GC.summary._cache 同期
# ✔ 重複完全防止
# ✔ 超高速設計
# ✔ datetime保証
# ✔ OHLC alias自動修復
# ✔ duplicate column防御
# ✔ duplicate bar防御
# ✔ push schema差異吸収
# ✔ volume NaN防御
# ✔ production完全安定版
# ============================================================

from __future__ import annotations

import pandas as pd
import logging
from typing import Optional

from core.global_context.context import global_context as GC
from global_state import global_data

from trading.summary.confirmed_bar_builder import (
    build_confirmed_1min_from_push,
)

from trading.summary.indicators.indicator_calculator import (
    add_all_indicators,
)

from trading.summary.persistence.summary_saver_bulk import (
    bulk_upsert_summary,
)

from trading.summary.summary_cache_utils import (
    normalize_summary_cache,
)

from config.runtime_limits import SUMMARY_CACHE_MAX_ROWS

logger = logging.getLogger(__name__)


# ============================================================
# INTERNAL SAFETY
# ============================================================

def _remove_duplicate_columns(df: pd.DataFrame) -> pd.DataFrame:

    if df.columns.duplicated().any():

        dup = df.columns[df.columns.duplicated()].tolist()

        logger.warning(
            "[DIFF 1MIN] duplicate columns removed: %s",
            dup
        )

        df = df.loc[:, ~df.columns.duplicated()]

    return df


def _repair_ohlc_alias(df: pd.DataFrame) -> pd.DataFrame:

    alias = {
        "open": "open_price",
        "high": "high_price",
        "low": "low_price",
        "close": "close_price",
    }

    for src, dst in alias.items():

        if src in df.columns and dst not in df.columns:
            df[dst] = df[src]

    return df


def _ensure_datetime(df: pd.DataFrame) -> pd.DataFrame:

    if "datetime" in df.columns:

        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")

    if "datetime" not in df.columns and "timestamp" in df.columns:

        df["datetime"] = pd.to_datetime(df["timestamp"], errors="coerce")

    if "datetime" not in df.columns:
        raise KeyError("push data missing datetime")

    df = df.dropna(subset=["datetime"])

    return df


def _ensure_volume(df: pd.DataFrame) -> pd.DataFrame:

    if "volume" not in df.columns:
        df["volume"] = 0

    df["volume"] = df["volume"].fillna(0)

    return df


# ============================================================
# メインエンジン
# ============================================================

def run_diff_1min_engine() -> Optional[pd.DataFrame]:
    """
    pushデータとの差分から1分足を生成し、
    DBとcacheを更新する
    """

    try:

        # ----------------------------------------------------
        # ① 既存1minキャッシュ取得
        # ----------------------------------------------------

        prev_df = GC.summary._cache.get(1)

        if prev_df is None or prev_df.empty:
            logger.info("[DIFF 1MIN] cache empty → skip")
            return None

        last_dt = prev_df["datetime"].max()

        # ----------------------------------------------------
        # ② push差分取得
        # ----------------------------------------------------

        push_buffer = getattr(global_data, "push_buffer", None)

        if push_buffer is None:
            logger.info("[DIFF 1MIN] no push_buffer")
            return None

        df_push = push_buffer.to_dataframe(
            symbols=global_data.symbols_active,
            since=last_dt,
        )

        if df_push is None or df_push.empty:
            return None

        df_push = _remove_duplicate_columns(df_push)
        df_push = _ensure_datetime(df_push)

        # ----------------------------------------------------
        # ③ confirmed 1min bar生成
        # ----------------------------------------------------

        df_new = build_confirmed_1min_from_push(
            df_push,
            cutoff=None,
        )

        if df_new is None or df_new.empty:
            return None

        df_new = _repair_ohlc_alias(df_new)
        df_new = _ensure_volume(df_new)

        # ----------------------------------------------------
        # ④ 既存と結合（重複防止）
        # ----------------------------------------------------

        df_all = pd.concat(
            [prev_df, df_new],
            ignore_index=True
        )

        df_all = (
            df_all
            .drop_duplicates(subset=["symbol", "datetime"], keep="last")
            .sort_values(["symbol", "datetime"])
            .reset_index(drop=True)
        )

        # ----------------------------------------------------
        # ⑤ indicator再計算（必要範囲のみ）
        # ----------------------------------------------------

        tail_rows = 200

        df_tail = df_all.groupby("symbol").tail(tail_rows)

        df_tail = add_all_indicators(
            df_tail,
            interval=1,
        )

        # indicator merge
        df_all = df_all.drop(
            columns=df_tail.columns.intersection(df_all.columns),
            errors="ignore"
        ).merge(
            df_tail,
            on=["symbol", "datetime"],
            how="left"
        )

        # ----------------------------------------------------
        # ⑥ DB保存（差分のみ）
        # ----------------------------------------------------

        df_save = df_tail[
            df_tail["datetime"] > last_dt
        ]

        if not df_save.empty:

            bulk_upsert_summary(
                df_save,
                1
            )

        # ----------------------------------------------------
        # ⑦ cache更新
        # ----------------------------------------------------

        merged = normalize_summary_cache(
            prev=prev_df,
            new=df_all,
            max_rows=SUMMARY_CACHE_MAX_ROWS[1],
        )

        GC.summary._cache[1] = merged

        logger.info(
            "[DIFF 1MIN] updated rows=%d symbols=%d",
            len(df_save),
            len(df_save["symbol"].unique()) if not df_save.empty else 0,
        )

        return df_save

    except Exception:

        logger.exception("[DIFF 1MIN] fatal")

        return None


# ============================================================
# realtime_engine 用API
# ============================================================

def build_1min_diff(df_push):

    if df_push is None or df_push.empty:
        return None

    try:

        df = df_push.copy()

        df = _remove_duplicate_columns(df)

        df = _ensure_datetime(df)

        # price column detect
        if "price" in df.columns:
            price_col = "price"
        elif "close_price" in df.columns:
            price_col = "close_price"
        elif "close" in df.columns:
            price_col = "close"
        else:
            raise KeyError("push data missing price column")

        df = _ensure_volume(df)

        df["minute"] = df["datetime"].dt.floor("1min")

        agg = (
            df.groupby(["symbol", "minute"], sort=False)
            .agg(
                open_price=(price_col, "first"),
                high_price=(price_col, "max"),
                low_price=(price_col, "min"),
                close_price=(price_col, "last"),
                volume=("volume", "sum"),
            )
            .reset_index()
            .rename(columns={"minute": "datetime"})
        )

        agg["symbol"] = agg["symbol"].astype(str)

        return agg

    except Exception:

        logger.exception("build_1min_diff error")

        return None