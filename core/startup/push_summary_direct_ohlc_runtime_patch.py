# ============================================================
# File   : core/startup/push_summary_direct_ohlc_runtime_patch.py
# Version: V1-PRODUCTION-PUSH-DIRECT-OHLC-ROBUST-COLUMN-PATCH
# ------------------------------------------------------------
# 目的:
#   PUSH rows は存在するのに summary が0件になる問題の最終防衛。
#
# 背景:
#   push_summary_engine には direct OHLC fallback があるが、
#   実PUSH DataFrameの列名揺れにより close/current_price を作れないと、
#   push_rows>0 でも direct fallback が0件で終わる。
#
# 対応:
#   - trading.summary.engine.push_summary_engine の
#     _normalize_push_source_df / _build_direct_ohlc_from_push_source を起動時patch。
#   - CurrentPrice/current_price/現在値/BidPrice/AskPrice/売気配/買気配などから価格を復元。
#   - volume が無い場合でも 0 としてOHLC作成を継続。
#   - close が無い場合は bid/ask mid、片側気配、または open/high/low から復元。
#   - 生成結果に最低限の score / rsi / macd / technical_ready を付け、
#     display/entry 側が空扱いしにくい形にする。
# ============================================================
from __future__ import annotations

import logging
import re
from typing import Iterable, Optional

import pandas as pd

logger = logging.getLogger(__name__)

_PATCHED = False
_ORIGINALS: dict[str, object] = {}


def _safe_copy_df(value) -> pd.DataFrame:
    if isinstance(value, pd.DataFrame):
        return value.copy()
    try:
        return pd.DataFrame(value).copy()
    except Exception:
        return pd.DataFrame()


def _key(s: str) -> str:
    return re.sub(r"[^0-9a-zA-Z一-龥ぁ-んァ-ヶー_]", "", str(s)).lower()


def _find_col(df: pd.DataFrame, names: Iterable[str]) -> Optional[str]:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return None
    direct = {str(c): c for c in df.columns}
    lowered = {str(c).lower(): c for c in df.columns}
    keyed = {_key(str(c)): c for c in df.columns}
    for name in names:
        s = str(name)
        if s in direct:
            return direct[s]
        if s.lower() in lowered:
            return lowered[s.lower()]
        k = _key(s)
        if k in keyed:
            return keyed[k]
    return None


def _num(df: pd.DataFrame, names: Iterable[str]) -> pd.Series:
    out = pd.Series([pd.NA] * len(df), index=df.index, dtype="object")
    for name in names:
        c = _find_col(df, [name])
        if c is None:
            continue
        try:
            s = pd.to_numeric(df[c], errors="coerce")
            out = out.combine_first(s)
        except Exception:
            continue
    return pd.to_numeric(out, errors="coerce")


def _str_series(df: pd.DataFrame, names: Iterable[str], default: str = "") -> pd.Series:
    c = _find_col(df, names)
    if c is None:
        return pd.Series([default] * len(df), index=df.index, dtype="object")
    try:
        return df[c].astype(str)
    except Exception:
        return pd.Series([default] * len(df), index=df.index, dtype="object")


def _dt_series(df: pd.DataFrame, names: Iterable[str]) -> pd.Series:
    out = pd.Series([pd.NaT] * len(df), index=df.index, dtype="datetime64[ns]")
    for name in names:
        c = _find_col(df, [name])
        if c is None:
            continue
        try:
            s = pd.to_datetime(df[c], errors="coerce")
            try:
                s = s.dt.tz_localize(None)
            except Exception:
                pass
            out = out.combine_first(s)
        except Exception:
            continue
    return pd.to_datetime(out, errors="coerce")


def _patched_normalize_push_source_df(df: pd.DataFrame) -> pd.DataFrame:
    raw = _safe_copy_df(df)
    if raw.empty:
        return raw

    out = raw.copy()

    # symbol
    if "symbol" not in out.columns:
        out["symbol"] = _str_series(
            out,
            [
                "symbol", "Symbol", "SYMBOL", "code", "Code", "銘柄コード",
                "symbol_code", "SymbolCode", "IssueCode", "issue_code",
            ],
        )
    out["symbol"] = (
        out["symbol"].astype(str)
        .str.strip()
        .str.replace(r"\.0$", "", regex=True)
    )
    out = out[out["symbol"].ne("") & out["symbol"].ne("nan")].copy()
    if out.empty:
        logger.warning("[PUSH DIRECT OHLC PATCH] normalize empty after symbol filter raw_cols=%s", list(raw.columns))
        return out

    # datetime
    out["datetime"] = _dt_series(
        out,
        [
            "datetime", "Datetime", "date_time", "timestamp", "Timestamp",
            "CurrentPriceTime", "current_price_time", "currentpricetime",
            "PriceTime", "price_time", "ReceivedAt", "received_at",
            "inserted_at", "created_at", "time", "Time", "時刻", "現在値時刻",
        ],
    )
    if out["datetime"].isna().all():
        # 最終防衛: PUSHが来ているなら現在時刻でバー化する。
        out["datetime"] = pd.Timestamp.now().floor("s")
        logger.warning("[PUSH DIRECT OHLC PATCH] datetime all NaT -> filled now rows=%d", len(out))

    # price candidates
    close = _num(
        out,
        [
            "close", "close_price", "Close", "ClosePrice", "price", "Price",
            "current_price", "CurrentPrice", "currentprice", "現在値", "現値",
            "last_price", "LastPrice", "lastprice", "約定値", "ExecutionPrice",
            "price_current", "current", "last",
        ],
    )
    bid = _num(
        out,
        [
            "bid", "bid_price", "BidPrice", "BestBid", "best_bid", "Buy1Price",
            "buy1_price", "買気配", "買気配値", "最良買気配", "Bid1Price",
        ],
    )
    ask = _num(
        out,
        [
            "ask", "ask_price", "AskPrice", "BestAsk", "best_ask", "Sell1Price",
            "sell1_price", "売気配", "売気配値", "最良売気配", "Ask1Price",
        ],
    )
    mid = None
    try:
        mid = (bid + ask) / 2.0
        mid = mid.where((bid > 0) & (ask > 0))
    except Exception:
        mid = pd.Series([pd.NA] * len(out), index=out.index)

    close = close.combine_first(mid).combine_first(bid).combine_first(ask)

    open_ = _num(out, ["open", "open_price", "Open", "OpenPrice", "始値"])
    high = _num(out, ["high", "high_price", "High", "HighPrice", "高値"])
    low = _num(out, ["low", "low_price", "Low", "LowPrice", "安値"])

    close = close.combine_first(open_).combine_first(high).combine_first(low)
    open_ = open_.combine_first(close)
    high = high.combine_first(close)
    low = low.combine_first(close)

    out["close"] = pd.to_numeric(close, errors="coerce")
    out["open"] = pd.to_numeric(open_, errors="coerce")
    out["high"] = pd.to_numeric(high, errors="coerce")
    out["low"] = pd.to_numeric(low, errors="coerce")

    out["close_price"] = out["close"]
    out["open_price"] = out["open"]
    out["high_price"] = out["high"]
    out["low_price"] = out["low"]
    out["price"] = out["close"]
    out["current_price"] = out["close"]

    vol = _num(
        out,
        [
            "volume", "Volume", "trading_volume", "TradingVolume", "出来高",
            "CumVolume", "cum_volume", "last_cum_volume", "CurrentVolume",
            "current_volume", "売買数量", "TradingVolumeToday",
        ],
    )
    out["volume"] = pd.to_numeric(vol, errors="coerce").fillna(0.0)
    out["trading_volume"] = out["volume"]

    if "symbolname" not in out.columns:
        out["symbolname"] = _str_series(
            out,
            ["symbolname", "SymbolName", "symbol_name", "name", "Name", "銘柄名", "IssueName"],
            default="",
        )
    out["symbolname"] = out["symbolname"].replace({"": pd.NA, "nan": pd.NA}).fillna(out["symbol"])

    out["source"] = out.get("source", "push_stream")

    before = len(out)
    out = out.dropna(subset=["datetime", "symbol", "close"]).copy()
    out = out[pd.to_numeric(out["close"], errors="coerce") > 0].copy()

    latest = out["datetime"].max() if not out.empty else None
    logger.warning(
        "[PUSH DIRECT OHLC PATCH] normalized raw_rows=%d out_rows=%d dropped=%d cols=%d symbols=%d latest=%s price_nonnull=%d sample_cols=%s",
        len(raw),
        len(out),
        before - len(out),
        len(out.columns),
        int(out["symbol"].nunique()) if not out.empty and "symbol" in out.columns else 0,
        latest,
        int(out["close"].notna().sum()) if "close" in out.columns else 0,
        list(raw.columns)[:20],
    )
    return out.reset_index(drop=True)


def _calc_rsi_direct(close: pd.Series, period: int = 14) -> pd.Series:
    try:
        delta = close.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=1).mean()
        avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=1).mean()
        rs = avg_gain / avg_loss.replace(0, pd.NA)
        return pd.to_numeric(100 - (100 / (1 + rs)), errors="coerce").fillna(50.0)
    except Exception:
        return pd.Series([50.0] * len(close), index=close.index)


def _patched_build_direct_ohlc_from_push_source(push_df: pd.DataFrame, *, interval: int) -> pd.DataFrame:
    ticks = _patched_normalize_push_source_df(push_df)
    if ticks.empty:
        logger.warning("[PUSH DIRECT OHLC PATCH] direct OHLC skipped normalized empty interval=%s", interval)
        return pd.DataFrame()

    try:
        ticks["datetime"] = pd.to_datetime(ticks["datetime"], errors="coerce")
        ticks["_slot"] = ticks["datetime"].dt.floor(f"{int(interval)}min")
        ticks = ticks.sort_values(["symbol", "datetime"], kind="stable")
    except Exception:
        logger.exception("[PUSH DIRECT OHLC PATCH] slot build failed interval=%s", interval)
        return pd.DataFrame()

    def _last_nonempty(s: pd.Series):
        x = s.dropna()
        return x.iloc[-1] if not x.empty else ""

    try:
        bars = (
            ticks.groupby(["symbol", "_slot"], as_index=False)
            .agg(
                symbolname=("symbolname", _last_nonempty),
                open=("close", "first"),
                high=("close", "max"),
                low=("close", "min"),
                close=("close", "last"),
                volume=("volume", "max"),
                tick_count=("close", "count"),
                first_tick_at=("datetime", "min"),
                last_tick_at=("datetime", "max"),
            )
            .rename(columns={"_slot": "datetime"})
        )
    except Exception:
        logger.exception("[PUSH DIRECT OHLC PATCH] groupby OHLC failed interval=%s", interval)
        return pd.DataFrame()

    if bars.empty:
        return pd.DataFrame()

    for c in ("open", "high", "low", "close", "volume"):
        bars[c] = pd.to_numeric(bars[c], errors="coerce")
    bars = bars.dropna(subset=["symbol", "datetime", "close"]).copy()
    bars = bars[bars["close"] > 0].copy()
    if bars.empty:
        return pd.DataFrame()

    parts: list[pd.DataFrame] = []
    for _sym, one in bars.groupby("symbol", sort=False):
        one = one.copy().sort_values("datetime", kind="stable")
        close = pd.to_numeric(one["close"], errors="coerce")
        prev_close = close.shift(1)
        pct = (close - prev_close) / prev_close.replace(0, pd.NA)
        intrabar = (close - pd.to_numeric(one["open"], errors="coerce")) / pd.to_numeric(one["open"], errors="coerce").replace(0, pd.NA)

        one["rsi"] = _calc_rsi_direct(close)
        ema12 = close.ewm(span=12, adjust=False, min_periods=1).mean()
        ema26 = close.ewm(span=26, adjust=False, min_periods=1).mean()
        one["macd"] = (ema12 - ema26).fillna(0.0)
        one["signal"] = one["macd"].ewm(span=9, adjust=False, min_periods=1).mean().fillna(0.0)
        one["hist"] = (one["macd"] - one["signal"]).fillna(0.0)
        one["slope"] = pct.combine_first(intrabar).fillna(0.0)
        one["slope_atr_scaled"] = one["slope"]
        one["mtf"] = 0.0
        one["score_slope"] = one["slope"] * 100.0
        one["score_mtf"] = 0.0
        tick_bonus = pd.to_numeric(one.get("tick_count", 1), errors="coerce").fillna(1).clip(lower=1) * 0.0001
        base = (one["score_slope"].abs() + tick_bonus).fillna(0.0001)
        signed = base.where(one["score_slope"] >= 0, -base)
        one["score"] = signed
        one["score_total"] = signed
        one["final_score"] = signed
        one["display_score"] = signed
        one["score_buy"] = signed.clip(lower=0)
        one["score_sell"] = (-signed).clip(lower=0)
        one["technical_ready"] = True
        one["display_ready"] = True
        one["symbol_hist_len"] = range(1, len(one) + 1)
        one["source"] = "push_stream_direct_ohlc_runtime_patch"
        one["interval"] = int(interval)
        one["open_price"] = one["open"]
        one["high_price"] = one["high"]
        one["low_price"] = one["low"]
        one["close_price"] = one["close"]
        one["current_price"] = one["close"]
        one["price"] = one["close"]
        parts.append(one)

    out = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    logger.warning(
        "[PUSH DIRECT OHLC PATCH] built interval=%s rows=%d symbols=%d latest=%s",
        interval,
        len(out),
        int(out["symbol"].nunique()) if not out.empty and "symbol" in out.columns else 0,
        out["datetime"].max() if not out.empty and "datetime" in out.columns else None,
    )
    return out.reset_index(drop=True)


def install() -> bool:
    global _PATCHED
    if _PATCHED:
        return True

    try:
        import trading.summary.engine.push_summary_engine as pse
    except Exception:
        logger.exception("[PUSH DIRECT OHLC PATCH] import push_summary_engine failed")
        return False

    try:
        old_norm = getattr(pse, "_normalize_push_source_df", None)
        old_direct = getattr(pse, "_build_direct_ohlc_from_push_source", None)
        _ORIGINALS["_normalize_push_source_df"] = old_norm
        _ORIGINALS["_build_direct_ohlc_from_push_source"] = old_direct

        pse._normalize_push_source_df = _patched_normalize_push_source_df
        pse._build_direct_ohlc_from_push_source = _patched_build_direct_ohlc_from_push_source

        _PATCHED = True
        logger.warning("[PUSH DIRECT OHLC PATCH] installed robust column normalization + direct OHLC fallback")
        return True
    except Exception:
        logger.exception("[PUSH DIRECT OHLC PATCH] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[PUSH DIRECT OHLC PATCH] auto install failed")
