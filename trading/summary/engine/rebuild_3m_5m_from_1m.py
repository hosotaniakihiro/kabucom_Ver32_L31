# ============================================================
# File   : trading/summary/engine/rebuild_3m_5m_from_1m.py
# Ver30-PRODUCTION-REBUILD-3M-5M-FROM-1M
# ------------------------------------------------------------
# ✔ 1min を正本として 3min / 5min を派生生成
# ✔ 00分基点の 3分足 / 5分足
# ✔ 前営業日・当日をまたぐ multi-day input 対応
# ✔ 指標再計算 / scoring 再計算フックあり
# ✔ DB UPSERT + global_data cache 更新
# ✔ 防御的実装
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
from typing import Optional

import pandas as pd

from global_state import global_data

logger = logging.getLogger(__name__)


def _ensure_datetime(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    out = df.copy()

    if "datetime" in out.columns:
        out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
    else:
        date_col = "date" if "date" in out.columns else None
        time_col = None
        if "end_time" in out.columns:
            time_col = "end_time"
        elif "time" in out.columns:
            time_col = "time"

        if date_col and time_col:
            out["datetime"] = pd.to_datetime(
                out[date_col].astype(str) + " " + out[time_col].astype(str),
                errors="coerce",
            )
        else:
            out["datetime"] = pd.NaT

    out = out.dropna(subset=["datetime"]).reset_index(drop=True)
    return out


def _normalize_1m_columns(df_1m: pd.DataFrame) -> pd.DataFrame:
    if df_1m is None or df_1m.empty:
        return pd.DataFrame()

    df = _ensure_datetime(df_1m)

    rename_map = {
        "open": "open_price",
        "high": "high_price",
        "low": "low_price",
        "close": "close_price",
    }
    for src, dst in rename_map.items():
        if src in df.columns and dst not in df.columns:
            df[dst] = df[src]

    if "symbol" not in df.columns:
        raise ValueError("df_1m must contain symbol column")

    for col in ["open_price", "high_price", "low_price", "close_price", "volume"]:
        if col not in df.columns:
            df[col] = 0.0

    if "symbolname" not in df.columns:
        df["symbolname"] = ""

    return df.sort_values(["symbol", "datetime"]).reset_index(drop=True)


def _resample_from_1m(df_1m: pd.DataFrame, tf_min: int) -> pd.DataFrame:
    """
    1min → 3min / 5min
    00分基点で right-closed/right-label のバーを作る
    """
    if df_1m is None or df_1m.empty:
        return pd.DataFrame()

    if tf_min not in (3, 5):
        raise ValueError("tf_min must be 3 or 5")

    df = _normalize_1m_columns(df_1m).copy()
    df = df.set_index("datetime")

    agg_map = {
        "open_price": "first",
        "high_price": "max",
        "low_price": "min",
        "close_price": "last",
        "volume": "sum",
    }

    keep_last_cols = [
        c for c in [
            "symbolname",
            "score_buy",
            "score_sell",
            "slope",
            "slope_atr_scaled",
            "mtf_alignment",
            "rsi",
            "macd",
            "ma5",
            "ma25",
            "ma75",
        ] if c in df.columns
    ]

    full_agg = dict(agg_map)
    for c in keep_last_cols:
        full_agg[c] = "last"

    df_tf = (
        df.groupby("symbol", group_keys=False)
        .resample(
            f"{tf_min}min",
            label="right",
            closed="right",
            origin="start_day",
        )
        .agg(full_agg)
        .reset_index()
    )

    df_tf = df_tf.dropna(subset=["open_price", "high_price", "low_price", "close_price"])
    if df_tf.empty:
        return df_tf

    df_tf["date"] = df_tf["datetime"].dt.date
    df_tf["time"] = df_tf["datetime"].dt.time
    df_tf["end_time"] = df_tf["datetime"].dt.time
    df_tf["time_range"] = f"{tf_min}min"
    df_tf["open"] = df_tf["open_price"]
    df_tf["high"] = df_tf["high_price"]
    df_tf["low"] = df_tf["low_price"]
    df_tf["close"] = df_tf["close_price"]
    df_tf["interval"] = tf_min
    df_tf["source"] = "resampled_from_1min"

    return df_tf.sort_values(["symbol", "datetime"]).reset_index(drop=True)


def _apply_indicator_pipeline(df: pd.DataFrame, interval: int) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    out = df.copy()

    # 既存プロジェクト側の計算器に出来るだけ合わせる
    try:
        from trading.summary.indicator_calculator import calculate_indicators  # type: ignore

        out = calculate_indicators(out, interval=interval)
        return out
    except Exception:
        logger.info("[rebuild_%sm] calculate_indicators unavailable -> fallback", interval)

    try:
        from trading.summary.calculator.indicator_pipeline import apply_indicators  # type: ignore

        out = apply_indicators(out, interval=interval)
        return out
    except Exception:
        logger.info("[rebuild_%sm] apply_indicators unavailable -> fallback", interval)

    return out


def _apply_scoring_pipeline(df: pd.DataFrame, interval: int) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    out = df.copy()

    try:
        from trading.summary.engine.processors.scoring import apply_scoring  # type: ignore

        out = apply_scoring(out, interval=interval)
        return out
    except Exception:
        logger.info("[rebuild_%sm] apply_scoring unavailable -> fallback", interval)

    try:
        from trading.summary.scoring_core import calculate_scores  # type: ignore

        out = calculate_scores(out, interval=interval)
        return out
    except Exception:
        logger.info("[rebuild_%sm] calculate_scores unavailable -> fallback", interval)

    return out


def _upsert_summary_df(df: pd.DataFrame, interval: int) -> None:
    if df is None or df.empty:
        return

    try:
        from trading.summary.persistence.summary_saver_bulk import bulk_upsert_summary  # type: ignore

        bulk_upsert_summary(interval=interval, df=df)
        return
    except Exception:
        logger.info("[rebuild_%sm] bulk_upsert_summary unavailable -> fallback", interval)

    try:
        from trading.summary.summary_updater import upsert_all_summaries  # type: ignore

        key = f"{interval}min"
        upsert_all_summaries({key: df}, update_cache=False)
        return
    except Exception:
        logger.exception("❌ upsert failed interval=%s", interval)
        raise


def _update_global_cache(interval: int, df: pd.DataFrame) -> None:
    if df is None or df.empty:
        return

    try:
        if hasattr(global_data, "set_merged_summary"):
            global_data.set_merged_summary(interval, df)
        else:
            merged = getattr(global_data, "merged_summary", None)
            if merged is None:
                merged = {}
                setattr(global_data, "merged_summary", merged)
            merged[interval] = df
    except Exception:
        logger.exception("failed merged_summary interval=%s", interval)

    try:
        multi = getattr(global_data, "multi_summary", None)
        if multi is None:
            multi = {}
            setattr(global_data, "multi_summary", multi)
        multi[interval] = df
    except Exception:
        logger.exception("failed multi_summary interval=%s", interval)

    try:
        latest_map = getattr(global_data, "latest_summary_by_interval", None)
        if latest_map is None:
            latest_map = {}
            setattr(global_data, "latest_summary_by_interval", latest_map)
        latest_map[interval] = df
    except Exception:
        logger.exception("failed latest_summary_by_interval interval=%s", interval)

    try:
        if interval == 3:
            setattr(global_data, "latest_summary_3m", df)
        elif interval == 5:
            setattr(global_data, "latest_summary_5m", df)
    except Exception:
        logger.exception("failed latest_summary_%sm", interval)


def rebuild_3m_5m_from_1m(
    df_1m: pd.DataFrame,
    *,
    rebuild_3m: bool = True,
    rebuild_5m: bool = True,
    persist: bool = True,
    update_cache: bool = True,
) -> tuple[Optional[pd.DataFrame], Optional[pd.DataFrame]]:
    """
    完成済み 1min DF から 3min / 5min を再構築する
    """
    if df_1m is None or df_1m.empty:
        logger.warning("⚠ rebuild_3m_5m_from_1m: empty 1min")
        return None, None

    df_1m = _normalize_1m_columns(df_1m)

    out_3m: Optional[pd.DataFrame] = None
    out_5m: Optional[pd.DataFrame] = None

    if rebuild_3m:
        try:
            out_3m = _resample_from_1m(df_1m, 3)
            out_3m = _apply_indicator_pipeline(out_3m, 3)
            out_3m = _apply_scoring_pipeline(out_3m, 3)
            if persist and out_3m is not None and not out_3m.empty:
                _upsert_summary_df(out_3m, 3)
            if update_cache and out_3m is not None and not out_3m.empty:
                _update_global_cache(3, out_3m)
            logger.info("✅ rebuild 3min rows=%d", 0 if out_3m is None else len(out_3m))
        except Exception:
            logger.exception("❌ rebuild 3min failed")

    if rebuild_5m:
        try:
            out_5m = _resample_from_1m(df_1m, 5)
            out_5m = _apply_indicator_pipeline(out_5m, 5)
            out_5m = _apply_scoring_pipeline(out_5m, 5)
            if persist and out_5m is not None and not out_5m.empty:
                _upsert_summary_df(out_5m, 5)
            if update_cache and out_5m is not None and not out_5m.empty:
                _update_global_cache(5, out_5m)
            logger.info("✅ rebuild 5min rows=%d", 0 if out_5m is None else len(out_5m))
        except Exception:
            logger.exception("❌ rebuild 5min failed")

    return out_3m, out_5m