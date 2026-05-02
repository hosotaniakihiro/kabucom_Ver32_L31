# ============================================================
# File   : trading/yahoo/correction/yahoo_correction_engine.py
# Version: Ver2.3-PRO-YAHOO-CORRECTION-L14-SAFE
#          -LAZY-SCORING-IMPORT-FIX
# ------------------------------------------------------------
# ✔ global_data.intraday_delta の Yahoo差分を使用
# ✔ 20分以内のバーは触らない
# ✔ OHLC不足時は close で最低限補完
# ✔ summary engine resolver 依存を除去
# ✔ missing-bar resolver 不要
# ✔ indicator 再計算
# ✔ scoring 再計算
# ✔ bulk_upsert_summary で summary DBへ保存
# ✔ scheduler安全
# ✔ L14実配置向け
# ✔ FIX: scoring_main の循環 import 回避（遅延 import）
# ============================================================

from __future__ import annotations

import datetime as dt
import logging

import pandas as pd

from global_state import global_data
from trading.summary.persistence.summary_saver_bulk import bulk_upsert_summary
from trading.summary.indicators.indicator_calculator import add_all_indicators

logger = logging.getLogger(__name__)

YAHOO_DELAY_MIN = 20
INTERVAL = 1


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
# Utility
# ============================================================

def _safe_df(df) -> pd.DataFrame:
    try:
        if df is None:
            return pd.DataFrame()

        if not isinstance(df, pd.DataFrame):
            df = pd.DataFrame(df)

        if df.empty:
            return pd.DataFrame()

        out = df.copy()

        try:
            out = out.loc[:, ~out.columns.duplicated()]
        except Exception:
            pass

        return out

    except Exception:
        logger.exception("[YAHOO CORRECTION] _safe_df failed")
        return pd.DataFrame()


def _normalize_delta(df_delta: pd.DataFrame) -> pd.DataFrame:
    df = _safe_df(df_delta)
    if df.empty:
        return df

    rename_map = {
        "time": "datetime",
        "open_price": "open",
        "high_price": "high",
        "low_price": "low",
        "close_price": "close",
    }

    for src, dst in rename_map.items():
        if src in df.columns and dst not in df.columns:
            df[dst] = df[src]

    required = {"symbol", "datetime", "close"}
    if not required.issubset(df.columns):
        logger.warning(
            "[YAHOO CORRECTION] missing required columns=%s",
            sorted(required - set(df.columns)),
        )
        return pd.DataFrame()

    df["symbol"] = (
        df["symbol"]
        .astype(str)
        .str.strip()
        .str.replace(r"\.0$", "", regex=True)
    )
    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")

    for col in ["open", "high", "low", "close", "volume"]:
        if col not in df.columns:
            if col == "volume":
                df[col] = 0.0
            else:
                df[col] = pd.NA

        df[col] = pd.to_numeric(df[col], errors="coerce")

    # OHLC 最低限補完
    for col in ["open", "high", "low"]:
        df[col] = df[col].fillna(df["close"])

    df["volume"] = df["volume"].fillna(0.0)

    df = df.dropna(subset=["symbol", "datetime", "close"])
    if df.empty:
        return pd.DataFrame()

    try:
        if getattr(df["datetime"].dt, "tz", None) is not None:
            try:
                df["datetime"] = df["datetime"].dt.tz_convert(None)
            except Exception:
                df["datetime"] = df["datetime"].dt.tz_localize(None)
    except Exception:
        pass

    # 20分以内は触らない
    cutoff = dt.datetime.now() - dt.timedelta(minutes=YAHOO_DELAY_MIN)
    df = df[df["datetime"] <= cutoff]

    if df.empty:
        return pd.DataFrame()

    df = (
        df.drop_duplicates(subset=["symbol", "datetime"], keep="last")
          .sort_values(["symbol", "datetime"])
          .reset_index(drop=True)
    )

    return df


def _prepare_summary_frame(df: pd.DataFrame) -> pd.DataFrame:
    work = df.copy()

    work["open_price"] = work["open"]
    work["high_price"] = work["high"]
    work["low_price"] = work["low"]
    work["close_price"] = work["close"]

    work["date"] = work["datetime"].dt.strftime("%Y-%m-%d")
    work["time_range"] = work["datetime"].dt.strftime("%H:%M")
    work["interval"] = INTERVAL

    if "source" not in work.columns:
        work["source"] = "yahoo_correction"

    if "symbolname" not in work.columns:
        work["symbolname"] = ""

    return work


def _latest_only_per_symbol(df: pd.DataFrame) -> pd.DataFrame:
    """
    correction側は monitor 差分前提。
    毎回巨大データを扱わず、受け取った差分内の重複だけ落とす。
    """
    try:
        if df is None or df.empty:
            return pd.DataFrame()

        work = df.copy()
        work = work.sort_values(["symbol", "datetime"])

        return (
            work.drop_duplicates(
                subset=["symbol", "datetime"],
                keep="last",
            )
            .reset_index(drop=True)
        )

    except Exception:
        logger.exception("[YAHOO CORRECTION] latest filter failed")
        return df if isinstance(df, pd.DataFrame) else pd.DataFrame()


def apply_yahoo_correction():
    """
    global_data.intraday_delta に載った Yahoo差分から
    stock_summary_1min を補完する
    """
    try:
        df_delta = getattr(global_data, "intraday_delta", None)
        df_delta = _normalize_delta(df_delta)

        if df_delta.empty:
            logger.debug("[YAHOO CORRECTION] delta empty")
            return

        df_summary = _prepare_summary_frame(df_delta)
        df_summary = _latest_only_per_symbol(df_summary)

        if df_summary.empty:
            logger.debug("[YAHOO CORRECTION] summary frame empty")
            return

        # indicator
        try:
            df_summary = add_all_indicators(df_summary, interval=INTERVAL)
        except TypeError:
            try:
                df_summary = add_all_indicators(df_summary)
            except Exception:
                logger.exception("[YAHOO CORRECTION] add_all_indicators failed")
        except Exception:
            logger.exception("[YAHOO CORRECTION] add_all_indicators failed")

        df_summary = _safe_df(df_summary)
        if df_summary.empty:
            return

        # scoring
        try:
            scoring_main = _get_scoring_main()
            df_summary = scoring_main(df_summary, interval=INTERVAL)
        except Exception:
            logger.exception("[YAHOO CORRECTION] scoring_main failed")

        df_summary = _safe_df(df_summary)
        if df_summary.empty:
            return

        # summary DB 保存
        bulk_upsert_summary(df_summary, interval=INTERVAL)

        try:
            setattr(global_data, "last_yahoo_correction_at", dt.datetime.now())
        except Exception:
            pass

        logger.info(
            "[YAHOO CORRECTION] inserted rows=%d symbols=%d range=%s→%s",
            len(df_summary),
            df_summary["symbol"].nunique() if "symbol" in df_summary.columns else 0,
            df_summary["datetime"].min() if "datetime" in df_summary.columns and not df_summary.empty else None,
            df_summary["datetime"].max() if "datetime" in df_summary.columns and not df_summary.empty else None,
        )

    except Exception:
        logger.exception("[YAHOO CORRECTION] failed")