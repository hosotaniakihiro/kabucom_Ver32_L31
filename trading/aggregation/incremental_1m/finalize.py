"""
============================================================
finalize.py
Incremental1MEngine Finalize Logic
Ver29.2-PRODUCTION-STABLE-LAZY-SCORING-IMPORT
------------------------------------------------------------
✔ 1分バー確定処理
✔ history cache 統合
✔ indicator計算
✔ scoring計算
✔ summary保存
✔ HTF生成トリガー
✔ NaN / inf 防御
✔ volume安全化
✔ datetime安全化
✔ history cache 安全化
✔ HTF lazy import（循環import防止）
✔ scoring_main lazy import（循環import防止）
✔ 本番安定版
============================================================
"""

from __future__ import annotations

import logging
import math

import pandas as pd

from trading.summary.incremental_indicators import add_all_indicators
from trading.summary.persistence.summary_saver_bulk import bulk_upsert_summary

from trading.aggregation.unconfirmed_store import (
    delete as delete_unconfirmed,
)

from core.state.last_state_manager import last_state

logger = logging.getLogger(__name__)


# ============================================================
# Lazy import helper
# ============================================================

def _get_scoring_main():
    """
    循環 import 回避のため、scoring_main は関数内で遅延 import する。
    """
    from trading.scoring.core.scoring_core import scoring_main
    return scoring_main


# ============================================================
# SAFE NUMBER
# ============================================================

def _safe_float(v):
    try:
        if v is None:
            return 0.0

        f = float(v)

        if math.isnan(f) or math.isinf(f):
            return 0.0

        return f

    except Exception:
        return 0.0


# ============================================================
# INTERNAL SANITIZE
# ============================================================

def _sanitize(df):
    if df is None:
        return pd.DataFrame()

    if not isinstance(df, pd.DataFrame):
        return pd.DataFrame()

    if df.empty:
        return pd.DataFrame()

    df = df.copy()

    try:
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
    except Exception:
        logger.debug("[1M] multiindex flatten failed", exc_info=True)

    try:
        if df.columns.duplicated().any():
            df = df.loc[:, ~df.columns.duplicated()]
    except Exception:
        logger.debug("[1M] duplicate column cleanup failed", exc_info=True)

    if "symbol" in df.columns:
        try:
            df["symbol"] = (
                df["symbol"]
                .astype(str)
                .str.strip()
                .str.replace(r"\.0$", "", regex=True)
            )
        except Exception:
            logger.debug("[1M] symbol normalize failed", exc_info=True)

    return df


# ============================================================
# FINALIZE BAR
# ============================================================

def finalize_bar(engine, symbol: str, bar: dict):
    """
    1分バー確定処理
    """
    try:
        dt_bar = bar.get("minute")
        if dt_bar is None:
            return

        try:
            dt_bar = pd.to_datetime(dt_bar, errors="coerce")
        except Exception:
            return

        if pd.isna(dt_bar):
            return

        # --------------------------------------------------
        # SAFE NUMBERS
        # --------------------------------------------------
        open_raw = _safe_float(bar.get("open_price"))
        high_raw = _safe_float(bar.get("high_price"))
        low_raw = _safe_float(bar.get("low_price"))
        close_raw = _safe_float(bar.get("close_price"))
        volume_raw = _safe_float(bar.get("volume"))

        # --------------------------------------------------
        # DataFrame作成
        # --------------------------------------------------
        df_new = pd.DataFrame([{
            "symbol": str(symbol),
            "datetime": dt_bar,
            "open_price": open_raw,
            "high_price": high_raw,
            "low_price": low_raw,
            "close_price": close_raw,
            "volume": volume_raw,
            "source": "push",
        }])

        df_new = _sanitize(df_new)
        if df_new.empty:
            return

        # --------------------------------------------------
        # history取得
        # --------------------------------------------------
        try:
            df_prev = engine.history_cache.get(symbol)
        except Exception:
            logger.exception("[1M] history cache get failed")
            df_prev = None

        if df_prev is None:
            df_prev = pd.DataFrame()

        df_prev = _sanitize(df_prev)

        # --------------------------------------------------
        # concat safe
        # --------------------------------------------------
        try:
            df_all = pd.concat(
                [df_prev, df_new],
                ignore_index=True,
                copy=False,
            )
        except Exception:
            df_all = df_new.copy()

        df_all = _sanitize(df_all)
        if df_all.empty:
            return

        # --------------------------------------------------
        # datetime guard
        # --------------------------------------------------
        if "datetime" not in df_all.columns:
            if "end_time" in df_all.columns:
                df_all["datetime"] = pd.to_datetime(
                    df_all["end_time"],
                    errors="coerce",
                )
            elif "start_time" in df_all.columns:
                df_all["datetime"] = pd.to_datetime(
                    df_all["start_time"],
                    errors="coerce",
                )

        if "datetime" not in df_all.columns:
            return

        df_all["datetime"] = pd.to_datetime(
            df_all["datetime"],
            errors="coerce",
        )
        df_all = df_all.dropna(subset=["datetime"])

        if df_all.empty:
            return

        # --------------------------------------------------
        # OHLC alias normalize
        # --------------------------------------------------
        mapping = {
            "close_price": "close",
            "open_price": "open",
            "high_price": "high",
            "low_price": "low",
        }

        for src, dst in mapping.items():
            if src in df_all.columns and dst not in df_all.columns:
                df_all[dst] = df_all[src]

        # --------------------------------------------------
        # sort + dedupe
        # --------------------------------------------------
        if "symbol" not in df_all.columns:
            df_all["symbol"] = str(symbol)

        df_all = (
            df_all
            .drop_duplicates(
                subset=["symbol", "datetime"],
                keep="last",
            )
            .sort_values("datetime", kind="mergesort")
            .reset_index(drop=True)
        )

        # --------------------------------------------------
        # indicator
        # --------------------------------------------------
        try:
            df_all = add_all_indicators(df_all, interval=1)
        except TypeError:
            try:
                df_all = add_all_indicators(df_all)
            except Exception:
                logger.exception("[1M] indicator calculation failed")
        except Exception:
            logger.exception("[1M] indicator calculation failed")

        df_all = _sanitize(df_all)
        if df_all.empty:
            return

        # --------------------------------------------------
        # scoring
        # --------------------------------------------------
        try:
            scoring_main = _get_scoring_main()
            df_all = scoring_main(
                df_all,
                interval=1,
            )
        except Exception:
            logger.exception("[1M] scoring failed")

        df_all = _sanitize(df_all)
        if df_all.empty:
            return

        # --------------------------------------------------
        # 最新バー
        # --------------------------------------------------
        df_tail = df_all.tail(1).copy()
        if df_tail.empty:
            return

        # --------------------------------------------------
        # datetime normalize
        # --------------------------------------------------
        if "datetime" not in df_tail.columns:
            return

        df_tail["datetime"] = pd.to_datetime(
            df_tail["datetime"],
            errors="coerce",
        )
        df_tail = df_tail.dropna(subset=["datetime"])

        if df_tail.empty:
            return

        # --------------------------------------------------
        # time fields
        # --------------------------------------------------
        df_tail["date"] = df_tail["datetime"].dt.date
        df_tail["time"] = df_tail["datetime"].dt.time
        df_tail["start_time"] = df_tail["time"]

        df_tail["end_time"] = (
            df_tail["datetime"] + pd.Timedelta(minutes=1)
        ).dt.time

        df_tail["time_range"] = (
            df_tail["start_time"].astype(str)
            + "-"
            + df_tail["end_time"].astype(str)
        )

        # --------------------------------------------------
        # summary保存
        # --------------------------------------------------
        try:
            bulk_upsert_summary(
                df_tail,
                interval=1,
            )
        except Exception:
            logger.exception("[1M] summary upsert failed")

        # --------------------------------------------------
        # history cache 更新
        # --------------------------------------------------
        try:
            engine.history_cache.append(
                symbol,
                df_tail,
            )
        except Exception:
            logger.exception("[1M] history cache append failed")

        # --------------------------------------------------
        # HTF生成
        # --------------------------------------------------
        try:
            from trading.aggregation.higher_tf.htf_aggregator import (
                get_htf_engine,
            )

            htf_engine = get_htf_engine()
            row_dict = df_tail.iloc[0].to_dict()

            htf_engine.on_1m_confirmed(
                symbol,
                dt_bar,
                row_dict,
            )

        except Exception:
            logger.exception("[1M] higher TF generation failed")

        # --------------------------------------------------
        # unconfirmed削除
        # --------------------------------------------------
        try:
            delete_unconfirmed(
                symbol,
                dt_bar,
            )
        except Exception:
            logger.exception("[1M] unconfirmed delete failed")

        # --------------------------------------------------
        # state更新
        # --------------------------------------------------
        try:
            last_state.update_1m(dt_bar)
        except Exception:
            logger.exception("[1M] last_state update failed")

        logger.info(
            "[1M CONFIRMED] %s %s",
            symbol,
            dt_bar,
        )

    except Exception:
        logger.exception("[1M] finalize_bar crashed")