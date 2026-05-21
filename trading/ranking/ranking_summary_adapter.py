# ============================================================
# File   : trading/ranking/ranking_summary_adapter.py
# Version: Ver1.1-ITER-TRADE-DATES-COMPAT
# ------------------------------------------------------------
# ✔ iter_trade_dates(max_days=...) 非対応環境でも動くよう互換化
# ✔ ランキング由来エントリーの TECH SCORE 生成で落ちないようにする
# ✔ 取得対象営業日が不足する場合は平日フォールバックで補完
# ============================================================
from __future__ import annotations

import datetime as dt
import logging
import inspect
import pandas as pd
import numpy as np

from sqlalchemy import func

from database.session import Session_ranking
from database.models import RankingSnapshot1Min

from trading.yahoo.loader import load_yahoo_1min_range
from trading.summary.calculator import iter_trade_dates

logger = logging.getLogger(__name__)


# ============================================================
# 内部ユーティリティ
# ============================================================

def _normalize_datetime(x) -> dt.datetime | None:
    """
    tz-aware / string / datetime を安全に tz-naive JST datetime に正規化
    """
    t = pd.to_datetime(x, errors="coerce")
    if pd.isna(t):
        return None
    if t.tzinfo is not None:
        t = t.tz_convert("Asia/Tokyo").tz_localize(None)
    return t.to_pydatetime()


def _calc_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """
    RSI（Wilder方式）
    """
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi


def _fallback_trade_dates(end_date: dt.date, max_trade_days: int) -> list[dt.date]:
    """
    iter_trade_dates が新旧どちらの引数にも合わない場合の安全フォールバック。
    厳密な祝日判定ではないが、ランキングエントリー停止より安全。
    """
    out: list[dt.date] = []
    d = end_date - dt.timedelta(days=1)
    guard = 0
    while len(out) < max_trade_days and guard < max_trade_days * 4 + 30:
        if d.weekday() < 5:
            out.append(d)
        d -= dt.timedelta(days=1)
        guard += 1
    return out


def _iter_trade_dates_compat(end_date: dt.date, max_trade_days: int) -> list[dt.date]:
    """
    trading.summary.calculator.iter_trade_dates のシグネチャ差異を吸収する。

    既存ログのエラー:
      TypeError: iter_trade_dates() got an unexpected keyword argument 'max_days'

    対策:
      1) max_days 対応版なら keyword で呼ぶ
      2) 非対応なら positional を試す
      3) それも駄目なら平日フォールバック
    """
    try:
        sig = inspect.signature(iter_trade_dates)
        if "max_days" in sig.parameters:
            return list(iter_trade_dates(end_date, max_days=max_trade_days))
    except Exception:
        pass

    try:
        return list(iter_trade_dates(end_date, max_trade_days))
    except TypeError:
        pass
    except Exception:
        logger.warning("[RANKING_ADAPTER] iter_trade_dates positional failed", exc_info=True)

    try:
        return list(iter_trade_dates(end_date))[:max_trade_days]
    except TypeError:
        pass
    except Exception:
        logger.warning("[RANKING_ADAPTER] iter_trade_dates single-arg failed", exc_info=True)

    fallback = _fallback_trade_dates(end_date, max_trade_days)
    logger.warning(
        "[RANKING_ADAPTER] iter_trade_dates incompatible -> weekday fallback dates=%s",
        [d.strftime("%Y-%m-%d") for d in fallback[:10]],
    )
    return fallback


# ============================================================
# メイン API
# ============================================================

def build_ranking_like_summary_1min(
    *,
    symbols: list[str],
    end_time: dt.datetime,
    bars: int = 80,
    max_trade_days: int = 30,
) -> pd.DataFrame:
    """
    ranking_snapshot_1min（当日＋過去営業日）を
    summary_1min 完全互換 DataFrame に変換する
    """

    if not symbols:
        return pd.DataFrame()

    symbols = [str(s) for s in symbols]

    # --------------------------------------------------------
    # 当日 + 過去営業日を必ず含める
    # --------------------------------------------------------
    trade_dates = [end_time.date()]
    for d in _iter_trade_dates_compat(end_time.date(), max_trade_days=max_trade_days):
        if d not in trade_dates:
            trade_dates.append(d)

    logger.info(
        "[RANKING_ADAPTER] build start symbols=%s end=%s bars=%s trade_dates_head=%s",
        len(symbols),
        end_time,
        bars,
        [d.strftime("%Y-%m-%d") for d in trade_dates[:5]],
    )

    session = Session_ranking()
    dfs: list[pd.DataFrame] = []

    try:
        for trade_date in trade_dates:
            rows = (
                session.query(
                    RankingSnapshot1Min.symbol,
                    RankingSnapshot1Min.symbolname,
                    RankingSnapshot1Min.snapshot_time,
                    RankingSnapshot1Min.current_price,
                    RankingSnapshot1Min.trading_volume,
                )
                .filter(RankingSnapshot1Min.symbol.in_(symbols))
                .filter(
                    func.date(RankingSnapshot1Min.snapshot_time)
                    == trade_date.strftime("%Y-%m-%d")
                )
                .order_by(RankingSnapshot1Min.snapshot_time.asc())
                .all()
            )

            if not rows:
                continue

            df = pd.DataFrame(
                rows,
                columns=[
                    "symbol",
                    "symbolname",
                    "snapshot_time",
                    "current_price",
                    "trading_volume",
                ],
            )

            if not df.empty:
                dfs.append(df)

            if sum(len(x) for x in dfs) >= bars * len(symbols):
                break

    finally:
        session.close()

    if not dfs:
        logger.warning("[RANKING_ADAPTER] snapshot data empty")
        return pd.DataFrame()

    # --------------------------------------------------------
    # 正規化（summary 互換）
    # --------------------------------------------------------
    df_all = (
        pd.concat(dfs, ignore_index=True)
        .rename(
            columns={
                "snapshot_time": "datetime",
                "current_price": "close_price",
                "trading_volume": "volume",
            }
        )
    )

    df_all["datetime"] = df_all["datetime"].apply(_normalize_datetime)
    df_all = df_all.dropna(subset=["datetime"])

    df_all = (
        df_all.sort_values("datetime")
        .groupby("symbol")
        .tail(bars)
        .reset_index(drop=True)
    )

    # --------------------------------------------------------
    # Yahoo 終値補完（完全 SAFE）
    # --------------------------------------------------------
    try:
        yahoo_df = load_yahoo_1min_range(
            symbols=symbols,
            start=df_all["datetime"].min(),
            end=end_time,
        )

        if yahoo_df is not None and not yahoo_df.empty:
            yahoo_df = yahoo_df.rename(columns={"close": "yahoo_close"})

            df_all = pd.merge(
                df_all,
                yahoo_df[["symbol", "datetime", "yahoo_close"]],
                on=["symbol", "datetime"],
                how="left",
            )

            mask = df_all["close_price"].isna()
            df_all.loc[mask, "close_price"] = df_all.loc[mask, "yahoo_close"]

            df_all = df_all.drop(columns=["yahoo_close"])

    except Exception:
        logger.exception("[RANKING_ADAPTER] Yahoo merge failed (ignored)")

    # --------------------------------------------------------
    # calculate_summary 互換カラム補完
    # --------------------------------------------------------
    df_all["price"] = df_all["close_price"]
    df_all["close"] = df_all["close_price"]
    df_all["open"] = df_all["close_price"]
    df_all["high"] = df_all["close_price"]
    df_all["low"] = df_all["close_price"]
    df_all["open_price"] = df_all["close_price"]
    df_all["high_price"] = df_all["close_price"]
    df_all["low_price"] = df_all["close_price"]
    df_all["turnover"] = pd.to_numeric(df_all["close_price"], errors="coerce").fillna(0.0) * pd.to_numeric(df_all["volume"], errors="coerce").fillna(0.0)

    # --------------------------------------------------------
    # Ranking 強度 MA / RSI（価格とは独立思想）
    # --------------------------------------------------------
    out_dfs: list[pd.DataFrame] = []

    for symbol, g in df_all.groupby("symbol"):
        g = g.sort_values("datetime").copy()

        g["ranking_ma5"] = g["close_price"].rolling(5).mean()
        g["ranking_ma25"] = g["close_price"].rolling(25).mean()
        g["ranking_ma75"] = g["close_price"].rolling(75).mean()
        g["ranking_rsi"] = _calc_rsi(g["close_price"], period=14)

        out_dfs.append(g)

    df_all = pd.concat(out_dfs, ignore_index=True)

    logger.info(
        "[RANKING_ADAPTER] build done rows=%s symbols=%s dt_min=%s dt_max=%s",
        len(df_all),
        df_all["symbol"].nunique() if "symbol" in df_all.columns else 0,
        df_all["datetime"].min() if "datetime" in df_all.columns else None,
        df_all["datetime"].max() if "datetime" in df_all.columns else None,
    )

    return df_all
