# ============================================================
# File   : trading/ranking/ranking_summary_adapter.py
# Version: Ver1.2-YAHOO-MERGE-COLUMN-COMPAT
# ------------------------------------------------------------
# ✔ iter_trade_dates(max_days=...) 非対応環境でも動くよう互換化
# ✔ ランキング由来エントリーの TECH SCORE 生成で落ちないようにする
# ✔ 取得対象営業日が不足する場合は平日フォールバックで補完
# ✔ Yahoo補完DFの列名揺れ datetime/close を吸収
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


def _first_existing_col(df: pd.DataFrame, names: tuple[str, ...]) -> str | None:
    """
    大文字小文字の揺れも含めて最初に存在する列名を返す。
    """
    if df is None or df.empty:
        return None
    cols = list(df.columns)
    lower_map = {str(c).lower(): c for c in cols}
    for name in names:
        if name in df.columns:
            return name
        hit = lower_map.get(str(name).lower())
        if hit is not None:
            return hit
    return None


def _normalize_yahoo_df_for_merge(yahoo_df: pd.DataFrame) -> pd.DataFrame:
    """
    load_yahoo_1min_range の戻り列名揺れを吸収して、
    symbol / datetime / yahoo_close だけのDFに正規化する。

    想定外の列名でも ERROR にせず WARNING でスキップ可能にする。
    """
    if yahoo_df is None or yahoo_df.empty:
        return pd.DataFrame()

    y = yahoo_df.copy()

    symbol_col = _first_existing_col(
        y,
        (
            "symbol",
            "code",
            "stock_code",
            "Symbol",
            "銘柄コード",
        ),
    )
    dt_col = _first_existing_col(
        y,
        (
            "datetime",
            "Datetime",
            "timestamp",
            "Timestamp",
            "date_time",
            "DateTime",
            "time",
            "Time",
            "date",
            "Date",
        ),
    )
    close_col = _first_existing_col(
        y,
        (
            "yahoo_close",
            "close",
            "Close",
            "close_price",
            "current_price",
            "price",
            "終値",
        ),
    )

    # date + time 別列の場合
    if dt_col is None:
        date_col = _first_existing_col(y, ("date", "Date", "日付"))
        time_col = _first_existing_col(y, ("time", "Time", "時刻"))
        if date_col is not None and time_col is not None:
            y["__datetime__"] = y[date_col].astype(str) + " " + y[time_col].astype(str)
            dt_col = "__datetime__"

    missing = []
    if symbol_col is None:
        missing.append("symbol")
    if dt_col is None:
        missing.append("datetime")
    if close_col is None:
        missing.append("close")

    if missing:
        logger.warning(
            "[RANKING_ADAPTER] Yahoo merge skipped missing=%s columns=%s",
            missing,
            list(y.columns),
        )
        return pd.DataFrame()

    out = pd.DataFrame(
        {
            "symbol": y[symbol_col].astype(str).str.strip(),
            "datetime": y[dt_col].apply(_normalize_datetime),
            "yahoo_close": pd.to_numeric(y[close_col], errors="coerce"),
        }
    )
    out = out.dropna(subset=["datetime", "yahoo_close"])
    out = out[out["symbol"] != ""]

    if out.empty:
        logger.warning(
            "[RANKING_ADAPTER] Yahoo merge skipped after normalize empty columns=%s",
            list(yahoo_df.columns),
        )
        return pd.DataFrame()

    # 同一symbol/datetimeが複数ある場合は最後を採用
    out = out.sort_values(["symbol", "datetime"]).drop_duplicates(["symbol", "datetime"], keep="last")

    logger.info(
        "[RANKING_ADAPTER] Yahoo normalized rows=%s symbols=%s dt_min=%s dt_max=%s cols=%s",
        len(out),
        out["symbol"].nunique(),
        out["datetime"].min(),
        out["datetime"].max(),
        list(yahoo_df.columns),
    )
    return out


def _fallback_trade_dates(end_date: dt.date, max_trade_days: int) -> list[dt.date]:
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

        ymerge = _normalize_yahoo_df_for_merge(yahoo_df)
        if ymerge is not None and not ymerge.empty:
            before_missing = int(df_all["close_price"].isna().sum())

            df_all = pd.merge(
                df_all,
                ymerge[["symbol", "datetime", "yahoo_close"]],
                on=["symbol", "datetime"],
                how="left",
            )

            mask = df_all["close_price"].isna() & df_all["yahoo_close"].notna()
            filled = int(mask.sum())
            df_all.loc[mask, "close_price"] = df_all.loc[mask, "yahoo_close"]
            df_all = df_all.drop(columns=["yahoo_close"])

            logger.info(
                "[RANKING_ADAPTER] Yahoo merge ok yahoo_rows=%s missing_before=%s filled=%s",
                len(ymerge),
                before_missing,
                filled,
            )

    except Exception:
        # ランキングエントリー本体を止めないため、ERRORではなくWARNINGに落とす
        logger.warning("[RANKING_ADAPTER] Yahoo merge failed ignored", exc_info=True)

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
