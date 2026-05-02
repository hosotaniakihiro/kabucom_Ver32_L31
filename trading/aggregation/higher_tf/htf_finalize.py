"""
============================================================
htf_finalize.py
Higher Timeframe Finalize Logic
------------------------------------------------------------
✔ 3分 / 5分バー確定処理
✔ history取得
✔ indicator計算
✔ scoring計算
✔ summary保存
✔ merged_summary同期
✔ symbolname保証
✔ NaN / inf 防御
✔ dtype崩壊防止
✔ 本番安定版
✔ FIX: scoring_main の循環 import 回避（遅延 import）
============================================================
"""

from __future__ import annotations

import logging
import pandas as pd
import numpy as np

from trading.aggregation.higher_tf.htf_history_loader import load_history
from trading.summary.persistence.summary_saver_bulk import bulk_upsert_summary

from core.state.last_state_manager import last_state
from global_state import global_data
from trading.aggregation.higher_tf.htf_indicator_pipeline import run_pipeline

logger = logging.getLogger(__name__)


# ============================================================
# Lazy import helpers
# ============================================================

def _get_scoring_main():
    """
    循環 import 回避のため、scoring_main は関数内で遅延 import する。
    """
    from trading.scoring.core.scoring_core import scoring_main
    return scoring_main


# ============================================================
# Utility
# ============================================================

def _safe_float(v, default=0.0):
    try:
        f = float(v)
        if np.isnan(f) or np.isinf(f):
            return default
        return f
    except Exception:
        return default


def _sanitize_df(df: pd.DataFrame) -> pd.DataFrame:
    """
    DataFrame安全化
    """
    try:
        if df is None:
            return pd.DataFrame()

        if not isinstance(df, pd.DataFrame):
            df = pd.DataFrame(df)

        if df.empty:
            return df

        df = df.copy()
        df.replace([np.inf, -np.inf], np.nan, inplace=True)

        for col in df.columns:
            try:
                if hasattr(df[col], "dtype") and df[col].dtype.kind in {"f", "i"}:
                    df[col] = df[col].fillna(0.0)
                else:
                    df[col] = df[col].where(pd.notna(df[col]), None)
            except Exception:
                logger.debug("[HTF sanitize column failed] col=%s", col, exc_info=True)

        return df

    except Exception:
        logger.exception("[HTF sanitize failed]")
        return df if isinstance(df, pd.DataFrame) else pd.DataFrame()


# ============================================================
# FINALIZE
# ============================================================

def finalize_htf_bar(tf: int, symbol: str, bar: dict):
    """
    HTFバー確定処理
    """
    try:
        dt_bar = bar.get("minute")

        if dt_bar is None:
            return

        dt_bar = pd.to_datetime(dt_bar, errors="coerce")
        if pd.isna(dt_bar):
            return

        # --------------------------------------------------
        # history取得
        # --------------------------------------------------
        try:
            df_hist = load_history(tf, symbol)
            if df_hist is None:
                df_hist = pd.DataFrame()
        except Exception:
            logger.exception("[HTF history load failed]")
            df_hist = pd.DataFrame()

        # --------------------------------------------------
        # symbolname取得
        # --------------------------------------------------
        symbolname = None

        try:
            df_meta = global_data.get_merged_summary(1)
            if df_meta is not None and not df_meta.empty and "symbol" in df_meta.columns:
                meta = df_meta[df_meta["symbol"].astype(str) == str(symbol)].tail(1)
                if not meta.empty:
                    symbolname = meta.iloc[0].get("symbolname")
        except Exception:
            logger.exception("[HTF symbolname meta lookup failed]")

        if not symbolname:
            try:
                if df_hist is not None and not df_hist.empty and "symbolname" in df_hist.columns:
                    last_row = df_hist.tail(1)
                    symbolname = last_row.iloc[0].get("symbolname")
            except Exception:
                logger.debug("[HTF symbolname history fallback failed]", exc_info=True)

        if not symbolname:
            symbolname = symbol

        # --------------------------------------------------
        # 新バー生成
        # --------------------------------------------------
        df_new = pd.DataFrame([{
            "symbol": str(symbol),
            "symbolname": str(symbolname),
            "datetime": dt_bar,
            "open_price": _safe_float(bar.get("open_price")),
            "high_price": _safe_float(bar.get("high_price")),
            "low_price": _safe_float(bar.get("low_price")),
            "close_price": _safe_float(bar.get("close_price")),
            "volume": _safe_float(bar.get("volume")),
            "source": "push",
        }])

        df_new["datetime"] = pd.to_datetime(df_new["datetime"], errors="coerce")
        df_new = df_new.dropna(subset=["datetime"])
        if df_new.empty:
            return

        # --------------------------------------------------
        # time metadata
        # --------------------------------------------------
        df_new["date"] = df_new["datetime"].dt.date
        df_new["time"] = df_new["datetime"].dt.time
        df_new["start_time"] = df_new["time"]
        df_new["end_time"] = (df_new["datetime"] + pd.to_timedelta(tf, unit="m")).dt.time
        df_new["time_range"] = (
            df_new["start_time"].astype(str)
            + "-"
            + df_new["end_time"].astype(str)
        )

        # --------------------------------------------------
        # merge
        # --------------------------------------------------
        df_all = pd.concat([df_hist, df_new], ignore_index=True)

        if "datetime" not in df_all.columns:
            logger.warning("[HTF finalize] datetime missing after merge tf=%s symbol=%s", tf, symbol)
            return

        df_all["datetime"] = pd.to_datetime(df_all["datetime"], errors="coerce")
        df_all = df_all.dropna(subset=["datetime"])
        if df_all.empty:
            return

        df_all = df_all.sort_values("datetime").reset_index(drop=True)

        # --------------------------------------------------
        # indicator
        # --------------------------------------------------
        try:
            df_all = run_pipeline(df_all, tf)
        except Exception:
            logger.exception("[HTF indicator failed]")
            return

        if df_all is None or df_all.empty:
            return

        # --------------------------------------------------
        # scoring
        # --------------------------------------------------
        try:
            scoring_main = _get_scoring_main()
            df_all = scoring_main(
                df_all,
                interval=tf,
            )
        except Exception:
            logger.exception("[HTF scoring failed]")
            return

        if df_all is None or df_all.empty:
            return

        df_save = df_all.tail(1).copy()
        if df_save.empty:
            return

        # --------------------------------------------------
        # DataFrame安全化
        # --------------------------------------------------
        df_save = _sanitize_df(df_save)
        if df_save.empty:
            return

        # --------------------------------------------------
        # DB保存
        # --------------------------------------------------
        try:
            bulk_upsert_summary(
                df_save,
                interval=tf,
            )
        except Exception:
            logger.exception("[HTF save failed]")

        # --------------------------------------------------
        # merged_summary同期
        # --------------------------------------------------
        try:
            current = global_data.get_merged_summary(tf)

            if current is None or current.empty:
                df_updated = df_save.copy()
            else:
                current = current.copy()

                if "datetime" in current.columns:
                    current["datetime"] = pd.to_datetime(
                        current["datetime"],
                        errors="coerce"
                    )

                df_updated = pd.concat(
                    [current, df_save],
                    ignore_index=True
                )

            if "datetime" not in df_updated.columns or "symbol" not in df_updated.columns:
                logger.warning("[HTF merged sync skipped missing key columns tf=%s symbol=%s", tf, symbol)
            else:
                df_updated = (
                    df_updated
                    .sort_values(["symbol", "datetime"])
                    .drop_duplicates(
                        ["symbol", "datetime"],
                        keep="last"
                    )
                    .reset_index(drop=True)
                )

                global_data.set_merged_summary(
                    tf,
                    df_updated
                )

        except Exception:
            logger.exception("[HTF merged sync failed]")

        # --------------------------------------------------
        # state更新
        # --------------------------------------------------
        try:
            if int(tf) == 3:
                last_state.update_3m(dt_bar)
            else:
                last_state.update_5m(dt_bar)
        except Exception:
            logger.exception("[HTF last_state update failed]")

        logger.info(
            "[%sm CONFIRMED] %s %s",
            tf,
            symbol,
            dt_bar
        )

    except Exception:
        logger.exception("[HTF finalize fatal]")