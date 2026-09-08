# ============================================================
# File   : trading/ranking/summary/technicals_adapter.py
# Ver    : PRODUCTION-STABLE-REV1.0-RANKING-SUMMARY-TECHNICALS-ADAPTER
# ------------------------------------------------------------
# 【概要】
#   technical_from_ranking 呼び出し + fallback technical
# ============================================================

from __future__ import annotations

import datetime as dt
import inspect
import logging
import os
from typing import Iterable, Optional

import pandas as pd

logger = logging.getLogger(__name__)


try:
    from trading.ranking.summary.technical_from_ranking import (
        build_ranking_summary_technical as _external_build_ranking_summary_technical,
        get_latest_ranking_summary_rows as _external_get_latest_ranking_summary_rows,
    )
except Exception:
    _external_build_ranking_summary_technical = None
    _external_get_latest_ranking_summary_rows = None
    logger.warning(
        "[RANKING SUMMARY RUNNER] technical_from_ranking import failed -> fallback technical will be used",
        exc_info=True,
    )


def fallback_add_indicators(df: pd.DataFrame, *, interval: int) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    x = df.copy()
    x = x.sort_values(["symbol", "datetime"], kind="mergesort")

    try:
        x["close"] = pd.to_numeric(x["close"], errors="coerce")
    except Exception:
        pass

    frames: list[pd.DataFrame] = []

    for _, g in x.groupby("symbol", sort=False):
        gg = g.copy()

        try:
            close = pd.to_numeric(gg["close"], errors="coerce")
            delta = close.diff()
            gain = delta.clip(lower=0)
            loss = -delta.clip(upper=0)
            avg_gain = gain.rolling(14, min_periods=3).mean()
            avg_loss = loss.rolling(14, min_periods=3).mean()
            rs = avg_gain / avg_loss.replace(0, pd.NA)
            gg["rsi"] = 100 - (100 / (1 + rs))
        except Exception:
            gg["rsi"] = pd.NA

        try:
            ema12 = pd.to_numeric(gg["close"], errors="coerce").ewm(span=12, adjust=False).mean()
            ema26 = pd.to_numeric(gg["close"], errors="coerce").ewm(span=26, adjust=False).mean()
            macd = ema12 - ema26
            signal = macd.ewm(span=9, adjust=False).mean()
            gg["macd"] = macd
            gg["signal"] = signal
            gg["macd_hist"] = macd - signal
            gg["hist"] = gg["macd_hist"]
        except Exception:
            gg["macd"] = pd.NA
            gg["signal"] = pd.NA
            gg["macd_hist"] = pd.NA
            gg["hist"] = pd.NA

        try:
            gg["slope"] = pd.to_numeric(gg["close"], errors="coerce").pct_change(3) * 100.0
            gg["score_slope"] = gg["slope"]
        except Exception:
            gg["slope"] = pd.NA
            gg["score_slope"] = pd.NA

        frames.append(gg)

    if not frames:
        return x

    out = pd.concat(frames, ignore_index=True)
    out["interval"] = int(interval)

    return out


def call_external_technical(
    base_df: pd.DataFrame,
    *,
    interval: int,
    trade_date: Optional[str | int | dt.date | dt.datetime],
    lookback_minutes: int,
    symbols: Optional[Iterable[str]],
    ranking_db_path: Optional[str],
    yahoo_db_path: Optional[str],
    use_yahoo_fill: bool,
) -> pd.DataFrame:
    if not callable(_external_build_ranking_summary_technical):
        logger.warning(
            "[RANKING SUMMARY RUNNER] external technical unavailable -> fallback"
        )
        return fallback_add_indicators(base_df, interval=interval)

    fn = _external_build_ranking_summary_technical

    kwargs = {
        "interval": interval,
        "trade_date": trade_date,
        "lookback_minutes": lookback_minutes,
        "symbols": symbols,
        "ranking_db_path": ranking_db_path,
        "yahoo_db_path": yahoo_db_path,
        "use_yahoo_fill": use_yahoo_fill,
    }

    try:
        sig = inspect.signature(fn)
        params = sig.parameters
        accepts_var_kw = any(
            p.kind == inspect.Parameter.VAR_KEYWORD
            for p in params.values()
        )

        if accepts_var_kw:
            call_kwargs = kwargs
        else:
            call_kwargs = {k: v for k, v in kwargs.items() if k in params}

        try:
            return fn(base_df, **call_kwargs)
        except TypeError:
            logger.exception(
                "[RANKING SUMMARY RUNNER] technical build TypeError interval=%s -> retry df only",
                interval,
            )
            return fn(base_df)

    except Exception:
        logger.exception(
            "[RANKING SUMMARY RUNNER] external technical failed interval=%s -> fallback",
            interval,
        )
        return fallback_add_indicators(base_df, interval=interval)


# technical が既に付いているかの判定に使う代表列。
# build_ranking_summary_technical が作る TECHNICAL_COLUMNS の部分集合。
_LATEST_TECH_MARKERS = ("rsi", "macd", "ma75", "close")


def _technicals_already_attached(df: "pd.DataFrame") -> bool:
    """このフレームに technical 指標が既に付与されているか。

    runner.py は call_external_technical() の結果をそのまま
    get_latest_rows() へ渡す。その場合 external 経路の中で
    build_ranking_summary_technical() が同じ計算をもう一度行うため、
    1 サイクルにつき指標計算が 2 回走っていた (実測 96.5 秒)。
    """
    try:
        cols = set(map(str, df.columns))
        if not all(c in cols for c in _LATEST_TECH_MARKERS):
            return False
        # 列があるだけでなく、中身が入っていることまで確かめる
        # (空列だけ作られているケースで再計算を飛ばさないため)。
        for c in ("rsi", "macd"):
            s = pd.to_numeric(df[c], errors="coerce")
            if s.notna().any():
                return True
        return False
    except Exception:
        return False


def get_latest_rows(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    # ------------------------------------------------------------
    # technical 再計算の回避 (2026-09-03)
    # ------------------------------------------------------------
    # external 経路 (technical_from_ranking.get_latest_ranking_summary_rows) は
    # 内部で build_ranking_summary_technical() を呼ぶ。runner から渡される
    # フレームは既に technical 付与済みなので、そのまま通すと
    # 同じ指標計算が 1 サイクルに 2 回走る。
    # 実測: latest=96.472s -> 下の安価な抽出だけなら 0.1 秒未満。
    _force_external = str(
        os.environ.get("RANKING_SUMMARY_LATEST_FORCE_EXTERNAL", "")
    ).strip().lower() in {"1", "true", "yes", "on"}

    if not _force_external and _technicals_already_attached(df):
        try:
            x = df
            dtc = pd.to_datetime(x["datetime"], errors="coerce")
            m = dtc.notna()
            if not m.any():
                return pd.DataFrame()
            x = x.loc[m]
            dtc = dtc.loc[m]

            # external 経路 (technical_from_ranking.get_latest_ranking_summary_rows) は
            # **symbol ごとの最終行**を返す。最新 datetime の行だけを採ると、
            # 最新足に載っていない銘柄が落ちて Summary AI 候補が減ってしまう
            # (検証: 675 銘柄中 97 銘柄が欠落した)。同じ意味論に揃える。
            if "symbol" not in x.columns:
                return x.reset_index(drop=True).copy()

            order = dtc.sort_values(kind="mergesort").index
            latest = x.loc[order].groupby("symbol", sort=False).tail(1)

            latest = latest.reset_index(drop=True).copy()

            # external 経路は build を通す過程で時刻列を datetime64 へ正規化する。
            # 値は同じでも dtype が object のままだと下流の .dt アクセサが落ちるので、
            # 出力の型を external 経路に合わせる (3 列だけなので安価)。
            for _c in ("datetime", "start_time", "end_time"):
                if _c in latest.columns:
                    try:
                        latest[_c] = pd.to_datetime(latest[_c], errors="coerce")
                    except Exception:
                        pass

            logger.info(
                "[RANKING SUMMARY RUNNER] latest rows fast path rows=%s symbols=%s "
                "(technical 付与済みのため再計算しない)",
                len(latest),
                latest["symbol"].nunique() if "symbol" in latest.columns else -1,
            )
            return latest
        except Exception:
            logger.exception(
                "[RANKING SUMMARY RUNNER] latest rows fast path failed -> external/fallback へ"
            )

    if callable(_external_get_latest_ranking_summary_rows):
        try:
            latest = _external_get_latest_ranking_summary_rows(df)
            if isinstance(latest, pd.DataFrame) and not latest.empty:
                return latest.reset_index(drop=True)
        except Exception:
            logger.exception(
                "[RANKING SUMMARY RUNNER] external latest rows failed -> fallback latest per symbol"
            )

    try:
        x = df.copy()
        x["datetime"] = pd.to_datetime(x["datetime"], errors="coerce")
        x = x[x["datetime"].notna()].copy()

        if x.empty:
            return pd.DataFrame()

        latest_dt = x["datetime"].max()
        latest = x[x["datetime"] == latest_dt].copy()

        if latest["symbol"].nunique() < max(5, int(x["symbol"].nunique() * 0.1)):
            idx = x.sort_values("datetime").groupby("symbol", sort=False).tail(1).index
            latest = x.loc[idx].copy()

        return latest.reset_index(drop=True)

    except Exception:
        logger.exception(
            "[RANKING SUMMARY RUNNER] fallback latest rows failed"
        )
        return df.tail(100).copy().reset_index(drop=True)