# ============================================================
# File   : trading/summary/push/runner.py
# Version: Ver2.2-PUSH-SUMMARY-RUNNER-DIRECT-OHLC-FALLBACK
#          -PUBLISH-COMPLETED-MERGED-SUMMARY
#          -GLOBAL-CONTEXT-BRIDGE
#          -SUMMARY-LATEST-RESCUE
#          -DISPLAY-SAFE
#          -SOURCE-PUSH-STRICT
#          -DISPLAY-DIAG
#          -PUBLISH-VERIFY-SOURCE-PUSH
#          -TECHNICAL-READY-FALLBACK-DISPLAY
#          -DIRECT-OHLC-FALLBACK-WHEN-INCREMENTAL-EMPTY
# ------------------------------------------------------------
# 目的:
#   - scheduler_jobs.summary.push_summary から呼ばれる実行本体
#   - global_data 等から push_df を安全に解決
#   - incremental.pipeline.process_single_interval へ橋渡し
#   - summary_latest_df を優先して返す
#   - completed summary を global_data へ source="push" として publish する
#   - display=True のとき表示関数を安全に呼ぶ
#
# 主修正 Ver2.1:
#   - get_merged_summary / set_merged_summary は source="push" を明示
#   - publish後に get_merged_summary(tf, source="push") で検証
#   - display直前に rows/symbols/dt/score/slope/rsi/macd を診断ログ出力
#   - technical_ready=0 でも score があれば暫定TOP10表示を許可
#   - 3min/5min の履歴不足をログで明確化
#   - push_df drop_duplicates を弱め、OHLC生成用tickを潰しにくくする
# ============================================================

from __future__ import annotations

import datetime as dt
import inspect
import logging
import time
from typing import Any, Optional

import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# optional imports
# ============================================================

try:
    from core.global_context.context import global_data  # type: ignore
except Exception:
    try:
        from global_state import global_data  # type: ignore
    except Exception:
        global_data = None

try:
    from trading.summary.engine.incremental.pipeline import process_single_interval
except Exception:
    process_single_interval = None

_DISPLAY_CANDIDATES: list[tuple[str, str]] = [
    ("scheduler_jobs.summary.display", "display_push_summary"),
    ("scheduler_jobs.summary.display", "print_push_summary"),
    ("scheduler_jobs.summary_jobs", "display_push_summary"),
    ("scheduler_jobs.summary_jobs", "print_push_summary"),
    ("trading.summary.display", "display_push_summary"),
    ("trading.summary.display", "print_push_summary"),
]


# ============================================================
# generic helpers
# ============================================================

def _as_interval(value: int | str) -> int:
    try:
        return int(str(value).strip().replace("min", "").replace("m", ""))
    except Exception:
        logger.warning("[push.runner] invalid interval=%r -> fallback to 1", value)
        return 1


def _ensure_df(x: Any, name: str = "df") -> pd.DataFrame:
    try:
        if x is None:
            return pd.DataFrame()
        if isinstance(x, pd.DataFrame):
            return x.copy()
        if isinstance(x, pd.Series):
            return x.to_frame().T.reset_index(drop=True)
        if isinstance(x, dict):
            for key in (
                "df",
                "push_df",
                "data",
                "result_df",
                "summary_df",
                "summary_latest_df",
                "latest_df",
                "publish_df",
            ):
                v = x.get(key)
                if isinstance(v, pd.DataFrame):
                    return v.copy()
                if isinstance(v, pd.Series):
                    return v.to_frame().T.reset_index(drop=True)
        return pd.DataFrame()
    except Exception:
        logger.exception("[push.runner] _ensure_df failed name=%s", name)
        return pd.DataFrame()


def _ensure_datetime(df: pd.DataFrame) -> pd.DataFrame:
    out = _ensure_df(df, "datetime")
    if out.empty:
        return out

    for c in ("datetime", "dt", "timestamp", "end_time", "snapshot_time", "received_at", "time"):
        if c in out.columns:
            out[c] = pd.to_datetime(out[c], errors="coerce")
            try:
                out[c] = out[c].dt.tz_localize(None)
            except Exception:
                pass

    if "datetime" not in out.columns:
        for c in ("dt", "timestamp", "end_time", "snapshot_time", "received_at", "time"):
            if c in out.columns:
                out["datetime"] = pd.to_datetime(out[c], errors="coerce")
                try:
                    out["datetime"] = out["datetime"].dt.tz_localize(None)
                except Exception:
                    pass
                break

    return out


def _safe_symbols(df: Any) -> int:
    try:
        if isinstance(df, pd.DataFrame) and "symbol" in df.columns:
            return int(df["symbol"].astype(str).nunique())
    except Exception:
        pass
    return 0


def _safe_latest_dt(df: Any):
    try:
        if isinstance(df, pd.DataFrame) and not df.empty:
            for c in ("datetime", "dt", "timestamp", "end_time", "snapshot_time", "received_at", "time"):
                if c in df.columns:
                    s = pd.to_datetime(df[c], errors="coerce").dropna()
                    if not s.empty:
                        x = s.max()
                        try:
                            x = x.tz_localize(None)
                        except Exception:
                            pass
                        return x
    except Exception:
        pass
    return None


def _safe_min_dt(df: Any):
    try:
        if isinstance(df, pd.DataFrame) and not df.empty:
            for c in ("datetime", "dt", "timestamp", "end_time", "snapshot_time", "received_at", "time"):
                if c in df.columns:
                    s = pd.to_datetime(df[c], errors="coerce").dropna()
                    if not s.empty:
                        x = s.min()
                        try:
                            x = x.tz_localize(None)
                        except Exception:
                            pass
                        return x
    except Exception:
        pass
    return None


def _nonnull_count(df: pd.DataFrame, col: str) -> int:
    try:
        if isinstance(df, pd.DataFrame) and col in df.columns:
            return int(pd.to_numeric(df[col], errors="coerce").notna().sum())
    except Exception:
        pass
    return 0


def _nonzero_count(df: pd.DataFrame, col: str) -> int:
    try:
        if isinstance(df, pd.DataFrame) and col in df.columns:
            s = pd.to_numeric(df[col], errors="coerce").fillna(0)
            return int((s != 0).sum())
    except Exception:
        pass
    return 0


def _profile_df(tag: str, df: pd.DataFrame, *, interval: Optional[int] = None) -> None:
    """
    表示されない原因を追うための統一診断ログ。
    """
    try:
        if df is None or df.empty:
            logger.warning("[PUSH SUMMARY DIAG] tag=%s interval=%s empty=True", tag, interval)
            return

        dt_min = _safe_min_dt(df)
        dt_max = _safe_latest_dt(df)

        logger.info(
            "[PUSH SUMMARY DIAG] tag=%s interval=%s rows=%s symbols=%s "
            "dt_min=%s dt_max=%s "
            "score_nonnull=%s score_nonzero=%s "
            "final_nonnull=%s display_nonnull=%s "
            "buy_nonnull=%s sell_nonnull=%s "
            "slope_nonnull=%s slope_nonzero=%s "
            "score_slope_nonnull=%s score_slope_nonzero=%s "
            "mtf_nonnull=%s mtf_nonzero=%s "
            "rsi_nonnull=%s macd_nonnull=%s signal_nonnull=%s "
            "close_nonnull=%s",
            tag,
            interval,
            len(df),
            _safe_symbols(df),
            dt_min,
            dt_max,
            _nonnull_count(df, "score"),
            _nonzero_count(df, "score"),
            _nonnull_count(df, "final_score"),
            _nonnull_count(df, "display_score"),
            _nonnull_count(df, "score_buy"),
            _nonnull_count(df, "score_sell"),
            _nonnull_count(df, "slope"),
            _nonzero_count(df, "slope"),
            _nonnull_count(df, "score_slope"),
            _nonzero_count(df, "score_slope"),
            _nonnull_count(df, "mtf"),
            _nonzero_count(df, "mtf"),
            _nonnull_count(df, "rsi"),
            _nonnull_count(df, "macd"),
            _nonnull_count(df, "signal"),
            _nonnull_count(df, "close"),
        )
    except Exception:
        logger.exception("[push.runner] _profile_df failed tag=%s interval=%s", tag, interval)


def _log_df_state(tag: str, df: pd.DataFrame, *, interval: Optional[int] = None) -> None:
    try:
        if df is None or df.empty:
            logger.warning("[push.runner][%s] empty interval=%s", tag, interval)
            return

        _profile_df(tag, df, interval=interval)

        logger.info(
            "[push.runner][%s] rows=%s symbols=%s latest_dt=%s cols=%s",
            tag,
            len(df),
            _safe_symbols(df),
            _safe_latest_dt(df),
            list(df.columns),
        )

        show_cols = [
            c for c in [
                "symbol", "symbolname", "score", "score_buy", "score_sell",
                "final_score", "display_score", "slope", "score_slope",
                "mtf", "score_mtf", "mtf_score", "rsi", "macd", "signal",
                "open", "high", "low", "close", "datetime"
            ] if c in df.columns
        ]
        if show_cols:
            logger.info(
                "[push.runner][%s] preview\n%s",
                tag,
                df[show_cols].head(10).to_string(index=False),
            )
    except Exception:
        logger.exception("[push.runner] _log_df_state failed tag=%s", tag)


def _latest_only_from_df(df: pd.DataFrame) -> pd.DataFrame:
    out = _ensure_datetime(df)
    if out.empty:
        return out

    dt_col = "datetime" if "datetime" in out.columns else None
    if dt_col is None or "symbol" not in out.columns:
        return out.reset_index(drop=True)

    out["symbol"] = out["symbol"].astype(str)
    out = out.dropna(subset=["symbol", dt_col]).copy()
    if out.empty:
        return out

    out = out.sort_values(["symbol", dt_col], kind="stable")
    out = out.groupby("symbol", as_index=False).tail(1)
    return out.reset_index(drop=True)


def _call_with_supported_kwargs(fn, *args, **kwargs):
    sig = inspect.signature(fn)
    allowed = set(sig.parameters.keys())
    filtered = {k: v for k, v in kwargs.items() if k in allowed}
    return fn(*args, **filtered)


def _coerce_numeric_if_exists(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    try:
        out = df.copy()
        for col in cols:
            if col in out.columns:
                out[col] = pd.to_numeric(out[col], errors="coerce")
        return out
    except Exception:
        logger.exception("[push.runner] _coerce_numeric_if_exists failed")
        return df


def _normalize_symbol(df: pd.DataFrame) -> pd.DataFrame:
    try:
        out = df.copy()
        if "symbol" in out.columns:
            out["symbol"] = out["symbol"].astype(str).str.strip()
            out = out[out["symbol"] != ""].copy()
        return out
    except Exception:
        logger.exception("[push.runner] _normalize_symbol failed")
        return df


def _prepare_publish_df(df: pd.DataFrame) -> pd.DataFrame:
    """
    GlobalContext 側 completed 判定に通りやすい形へ最低限整形する。
    """
    out = _ensure_datetime(df)
    if out.empty:
        return out

    out = _normalize_symbol(out)
    out = _coerce_numeric_if_exists(
        out,
        [
            "score", "score_total", "final_score", "display_score",
            "score_buy", "score_sell",
            "slope", "score_slope", "mtf", "score_mtf", "mtf_score",
            "rsi", "macd", "signal",
            "open", "high", "low", "close",
        ],
    )

    if "score" not in out.columns:
        # score が無ければ completed summary 扱いしにくい
        return pd.DataFrame()

    if "display_score" not in out.columns:
        out["display_score"] = pd.to_numeric(out["score"], errors="coerce")

    if "final_score" not in out.columns:
        out["final_score"] = pd.to_numeric(out["score"], errors="coerce")

    if "score_buy" not in out.columns:
        out["score_buy"] = pd.to_numeric(out["score"], errors="coerce")

    if "score_sell" not in out.columns:
        out["score_sell"] = 0.0

    if "symbolname" not in out.columns and "symbol" in out.columns:
        out["symbolname"] = ""

    out = out.reset_index(drop=True)
    return out


def _is_publishable_summary(df: pd.DataFrame) -> bool:
    try:
        if df is None or df.empty:
            return False
        required = {"symbol", "score"}
        if not required.issubset(df.columns):
            return False
        s = df["symbol"].fillna("").astype(str).str.strip()
        if s.eq("").all():
            return False
        score = pd.to_numeric(df["score"], errors="coerce")
        return int(score.notna().sum()) > 0
    except Exception:
        return False


def _has_displayable_score(df: pd.DataFrame) -> bool:
    try:
        if df is None or df.empty:
            return False
        for col in ("display_score", "final_score", "score", "score_buy", "score_sell"):
            if col in df.columns:
                s = pd.to_numeric(df[col], errors="coerce")
                if int(s.notna().sum()) > 0:
                    return True
        return False
    except Exception:
        return False


# ============================================================
# push df resolution
# ============================================================

def _get_push_df_from_global_data() -> pd.DataFrame:
    if global_data is None:
        return pd.DataFrame()

    for getter_name in (
        "get_push_df",
        "get_current_push_df",
        "get_push_dataframe",
    ):
        try:
            getter = getattr(global_data, getter_name, None)
            if callable(getter):
                df = _ensure_df(getter(), getter_name)
                if not df.empty:
                    logger.info("[push.runner] resolved push df via global_data.%s()", getter_name)
                    return df
        except Exception:
            logger.exception("[push.runner] getter failed name=%s", getter_name)

    for attr_name in (
        "push_df",
        "current_push_df",
        "push_dataframe",
        "stream_df",
    ):
        try:
            df = _ensure_df(getattr(global_data, attr_name, None), attr_name)
            if not df.empty:
                logger.info("[push.runner] resolved push df via global_data.%s", attr_name)
                return df
        except Exception:
            logger.exception("[push.runner] attr failed name=%s", attr_name)

    return pd.DataFrame()


def _filter_push_df_for_interval(
    df_push: pd.DataFrame,
    interval: int,
    now: Optional[dt.datetime],
) -> pd.DataFrame:
    out = _ensure_datetime(df_push)
    if out.empty:
        return out

    if "symbol" in out.columns:
        out["symbol"] = out["symbol"].astype(str)

    if "datetime" not in out.columns:
        logger.warning("[push.runner] push_df has no datetime column interval=%s cols=%s", interval, list(out.columns))
        return out

    out = out.dropna(subset=["datetime"]).copy()
    if out.empty:
        return out

    out = out.sort_values(["datetime"], kind="stable")

    latest_dt = pd.to_datetime(now) if now is not None else pd.to_datetime(out["datetime"], errors="coerce").max()
    try:
        latest_dt = latest_dt.tz_localize(None)
    except Exception:
        pass

    if pd.notna(latest_dt):
        # 3m/5m はテクニカル指標のために長めに残す。
        # 1m: 240分、3m/5m: 390分
        minutes = 390 if interval in (3, 5) else 240
        start_dt = latest_dt - pd.Timedelta(minutes=minutes)
        before = len(out)
        out = out[out["datetime"] >= start_dt].copy()
        logger.info(
            "[push.runner] filter window interval=%s latest_dt=%s start_dt=%s before=%s after=%s",
            interval,
            latest_dt,
            start_dt,
            before,
            len(out),
        )

    # 注意:
    # OHLC生成前のtickデータを symbol+datetime で強く重複削除すると、
    # 同一秒・同一分内の価格変化を潰す可能性がある。
    # 完全同一行だけ削除する。
    before_dup = len(out)
    try:
        out = out.drop_duplicates(keep="last")
    except Exception:
        pass
    after_dup = len(out)

    out = out.reset_index(drop=True)

    if before_dup != after_dup:
        logger.info(
            "[push.runner] duplicate exact-row drop interval=%s before=%s after=%s delta=%s",
            interval,
            before_dup,
            after_dup,
            before_dup - after_dup,
        )

    _log_df_state(f"push-filtered-{interval}m", out, interval=interval)
    return out




# ============================================================
# direct PUSH OHLC fallback
# ============================================================

def _first_existing_col(df: pd.DataFrame, names: list[str]) -> Optional[str]:
    try:
        for name in names:
            if name in df.columns:
                return name
    except Exception:
        pass
    return None


def _normalize_push_ticks_for_direct_ohlc(df: pd.DataFrame) -> pd.DataFrame:
    """PUSH tick / stream_data の列揺れを吸収して直接OHLC救済用tickへ正規化する。"""
    out = _ensure_df(df, "direct_ohlc_source")
    if out.empty:
        return out

    if "symbol" not in out.columns:
        c = _first_existing_col(out, ["Symbol", "symbol_code", "Code", "code"])
        if c:
            out["symbol"] = out[c]
    if "symbol" not in out.columns:
        logger.warning("[push.runner][DIRECT OHLC] missing symbol columns=%s", list(out.columns))
        return pd.DataFrame()
    out["symbol"] = out["symbol"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
    out = out[out["symbol"] != ""].copy()

    out = _ensure_datetime(out)
    if "datetime" not in out.columns:
        c = _first_existing_col(out, ["current_price_time", "CurrentPriceTime", "received_at", "timestamp", "dt", "time", "end_time"])
        if c:
            out["datetime"] = pd.to_datetime(out[c], errors="coerce")
    if "datetime" not in out.columns:
        logger.warning("[push.runner][DIRECT OHLC] missing datetime columns=%s", list(out.columns))
        return pd.DataFrame()
    out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
    try:
        out["datetime"] = out["datetime"].dt.tz_localize(None)
    except Exception:
        pass
    out = out.dropna(subset=["datetime"]).copy()

    price_col = _first_existing_col(out, ["close", "close_price", "current_price", "CurrentPrice", "price", "Price", "last_price", "LastPrice", "Close", "ClosePrice"])
    if not price_col:
        logger.warning("[push.runner][DIRECT OHLC] missing price column columns=%s", list(out.columns))
        return pd.DataFrame()
    out["close"] = pd.to_numeric(out[price_col], errors="coerce")
    out = out.dropna(subset=["close"]).copy()
    if out.empty:
        logger.warning("[push.runner][DIRECT OHLC] all price values are NaN price_col=%s", price_col)
        return out

    for target, names in {"open": ["open", "open_price", "Open", "OpenPrice"], "high": ["high", "high_price", "High", "HighPrice"], "low": ["low", "low_price", "Low", "LowPrice"]}.items():
        c = _first_existing_col(out, names)
        if c:
            out[target] = pd.to_numeric(out[c], errors="coerce").combine_first(out["close"])
        else:
            out[target] = out["close"]

    vol_col = _first_existing_col(out, ["volume", "Volume", "trading_volume", "TradingVolume", "cum_volume", "CumVolume", "last_cum_volume"])
    out["volume"] = pd.to_numeric(out[vol_col], errors="coerce").fillna(0.0) if vol_col else 0.0

    if "symbolname" not in out.columns:
        c = _first_existing_col(out, ["SymbolName", "symbol_name", "name", "Name"])
        out["symbolname"] = out[c] if c else out["symbol"]
    out["symbolname"] = out["symbolname"].fillna(out["symbol"]).astype(str)

    out = out.sort_values(["symbol", "datetime"], kind="stable").reset_index(drop=True)
    logger.info("[push.runner][DIRECT OHLC] normalized ticks rows=%s symbols=%s dt_min=%s dt_max=%s price_col=%s", len(out), _safe_symbols(out), _safe_min_dt(out), _safe_latest_dt(out), price_col)
    return out


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


def _enrich_direct_ohlc_summary(bars: pd.DataFrame, interval: int) -> pd.DataFrame:
    out = _ensure_datetime(bars)
    if out.empty:
        return out
    out = out.sort_values(["symbol", "datetime"], kind="stable").reset_index(drop=True)
    parts = []
    for _sym, one in out.groupby("symbol", sort=False):
        one = one.copy().sort_values("datetime", kind="stable")
        close = pd.to_numeric(one["close"], errors="coerce")
        prev_close = close.shift(1)
        pct = (close - prev_close) / prev_close.replace(0, pd.NA)
        open_s = pd.to_numeric(one["open"], errors="coerce")
        intrabar = (close - open_s) / open_s.replace(0, pd.NA)
        rng = (pd.to_numeric(one["high"], errors="coerce") - pd.to_numeric(one["low"], errors="coerce")) / close.replace(0, pd.NA)
        one["rsi"] = _calc_rsi_direct(close)
        ema12 = close.ewm(span=12, adjust=False, min_periods=1).mean()
        ema26 = close.ewm(span=26, adjust=False, min_periods=1).mean()
        one["macd"] = (ema12 - ema26).fillna(0.0)
        one["signal"] = one["macd"].ewm(span=9, adjust=False, min_periods=1).mean().fillna(0.0)
        one["hist"] = (one["macd"] - one["signal"]).fillna(0.0)
        one["slope"] = pct.combine_first(intrabar).fillna(0.0)
        one["slope_atr_scaled"] = one["slope"].fillna(0.0)
        one["mtf"] = 0.0
        one["score_slope"] = pd.to_numeric(one["slope"], errors="coerce").fillna(0.0) * 100.0
        one["score_mtf"] = 0.0
        base = pct.combine_first(intrabar).combine_first(rng).fillna(0.0) * 100.0
        tick_bonus = pd.to_numeric(one["tick_count"], errors="coerce").fillna(0).clip(lower=0) * 0.0001 if "tick_count" in one.columns else pd.Series([0.0001] * len(one), index=one.index)
        base = base.where(base.abs() > 0, tick_bonus)
        one["score"] = base.fillna(0.0001)
        one["score_total"] = one["score"] + one["score_slope"].fillna(0.0) + one["score_mtf"].fillna(0.0)
        one["final_score"] = one["score_total"]
        one["display_score"] = one["score_total"]
        one["score_buy"] = one["score_total"].clip(lower=0)
        one["score_sell"] = (-one["score_total"]).clip(lower=0)
        one["technical_ready"] = True
        one["symbol_hist_len"] = range(1, len(one) + 1)
        one["source"] = "push_stream_direct_ohlc_fallback"
        one["interval"] = int(interval)
        parts.append(one)
    out = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    _profile_df("direct-ohlc-enriched", out, interval=interval)
    return out


def _build_direct_ohlc_summary_from_push(df_push: pd.DataFrame, *, interval: int, now: Optional[dt.datetime] = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    ticks = _normalize_push_ticks_for_direct_ohlc(df_push)
    if ticks.empty:
        logger.warning("[push.runner][DIRECT OHLC] no normalized ticks interval=%s", interval)
        return pd.DataFrame(), pd.DataFrame()
    now_ts = pd.Timestamp(now) if now is not None else pd.to_datetime(ticks["datetime"], errors="coerce").max()
    try:
        now_ts = now_ts.tz_localize(None)
    except Exception:
        pass
    if pd.notna(now_ts):
        before = len(ticks)
        ticks = ticks[ticks["datetime"] <= (now_ts + pd.Timedelta(seconds=30))].copy()
        logger.info("[push.runner][DIRECT OHLC] future clamp interval=%s now=%s before=%s after=%s", interval, now_ts, before, len(ticks))
    if ticks.empty:
        return pd.DataFrame(), pd.DataFrame()
    freq = f"{int(interval)}min"
    ticks["_slot"] = ticks["datetime"].dt.floor(freq)

    def _last_nonempty(s: pd.Series):
        try:
            x = s.dropna()
            return x.iloc[-1] if not x.empty else ""
        except Exception:
            return ""

    bars = ticks.groupby(["symbol", "_slot"], as_index=False).agg(
        symbolname=("symbolname", _last_nonempty), open=("close", "first"), high=("close", "max"), low=("close", "min"), close=("close", "last"),
        volume=("volume", "max"), tick_count=("close", "count"), first_tick_at=("datetime", "min"), last_tick_at=("datetime", "max"),
    ).rename(columns={"_slot": "datetime"})
    try:
        ohlc2 = ticks.groupby(["symbol", "_slot"], as_index=False).agg(high2=("high", "max"), low2=("low", "min")).rename(columns={"_slot": "datetime"})
        bars = bars.merge(ohlc2, on=["symbol", "datetime"], how="left")
        bars["high"] = pd.concat([bars["high"], bars["high2"]], axis=1).max(axis=1)
        bars["low"] = pd.concat([bars["low"], bars["low2"]], axis=1).min(axis=1)
        bars = bars.drop(columns=["high2", "low2"], errors="ignore")
    except Exception:
        logger.debug("[push.runner][DIRECT OHLC] high/low correction skipped", exc_info=True)
    for c in ["open", "high", "low", "close", "volume"]:
        bars[c] = pd.to_numeric(bars[c], errors="coerce")
    bars = bars.dropna(subset=["symbol", "datetime", "close"]).copy()
    bars = _enrich_direct_ohlc_summary(bars, interval=interval)
    if bars.empty:
        return pd.DataFrame(), pd.DataFrame()
    expected_slot = pd.Timestamp(now_ts).floor(freq) if pd.notna(now_ts) else pd.to_datetime(bars["datetime"], errors="coerce").max()
    past = bars[pd.to_datetime(bars["datetime"], errors="coerce") <= expected_slot].copy()
    if past.empty:
        past = bars.copy()
    latest_slot = pd.to_datetime(past["datetime"], errors="coerce").max()
    latest = past[pd.to_datetime(past["datetime"], errors="coerce") == latest_slot].copy()
    logger.warning("[push.runner][DIRECT OHLC] built fallback interval=%s hist_rows=%s latest_rows=%s symbols=%s latest_slot=%s expected_slot=%s", interval, len(bars), len(latest), _safe_symbols(latest), latest_slot, expected_slot)
    _log_df_state("direct-ohlc-history", bars, interval=interval)
    _log_df_state("direct-ohlc-latest", latest, interval=interval)
    return bars.reset_index(drop=True), latest.reset_index(drop=True)


def _empty_result(interval: int, *, fallback_summary_df: Optional[pd.DataFrame] = None, fallback_latest_df: Optional[pd.DataFrame] = None) -> dict:
    return {"interval": int(interval), "summary_df": fallback_summary_df if isinstance(fallback_summary_df, pd.DataFrame) else pd.DataFrame(), "summary_latest_df": fallback_latest_df if isinstance(fallback_latest_df, pd.DataFrame) else pd.DataFrame(), "published": False}

# ============================================================
# global context publish / verify helpers
# ============================================================

def _get_merged_source_push(interval: int) -> pd.DataFrame:
    if global_data is None:
        return pd.DataFrame()

    # 優先: get_merged_summary(tf, source="push")
    try:
        getter = getattr(global_data, "get_merged_summary", None)
        if callable(getter):
            try:
                return _ensure_df(getter(interval, source="push"), "get_merged_summary_source_push")
            except TypeError:
                # 古い実装互換。ただし source 未指定になるので warning を出す
                logger.warning(
                    "[push.runner] global_data.get_merged_summary does not accept source=push tf=%s",
                    interval,
                )
                return _ensure_df(getter(interval), "get_merged_summary_no_source")
    except Exception:
        logger.exception("[push.runner] get_merged_summary verify failed tf=%s", interval)

    # 互換: get_push_merged_summary(tf)
    try:
        getter2 = getattr(global_data, "get_push_merged_summary", None)
        if callable(getter2):
            return _ensure_df(getter2(interval), "get_push_merged_summary")
    except Exception:
        logger.exception("[push.runner] get_push_merged_summary verify failed tf=%s", interval)

    return pd.DataFrame()


def _publish_to_global_context(interval: int, publish_df: pd.DataFrame) -> bool:
    if global_data is None:
        logger.warning("[push.runner] publish skipped interval=%s reason=global_data unavailable", interval)
        return False

    if publish_df is None or publish_df.empty:
        logger.warning("[push.runner] publish skipped interval=%s reason=empty df", interval)
        return False

    if not _is_publishable_summary(publish_df):
        logger.warning(
            "[push.runner] publish skipped interval=%s reason=not publishable cols=%s rows=%s",
            interval,
            list(publish_df.columns),
            len(publish_df),
        )
        return False

    _profile_df("publish-candidate", publish_df, interval=interval)

    ok = False

    try:
        setter = getattr(global_data, "set_push_summary", None)
        if callable(setter):
            setter(int(interval), publish_df.copy())
            logger.info(
                "[push.runner] publish cache done via set_push_summary tf=%s rows=%s",
                interval,
                len(publish_df),
            )
            ok = True
    except Exception:
        logger.exception("[push.runner] set_push_summary failed tf=%s", interval)

    try:
        setter = getattr(global_data, "set_push_merged_summary", None)
        if callable(setter):
            setter(interval, publish_df.copy())
            logger.info(
                "[push.runner] publish merged done via set_push_merged_summary tf=%s rows=%s",
                interval,
                len(publish_df),
            )
            ok = True
        else:
            setter2 = getattr(global_data, "set_merged_summary", None)
            if callable(setter2):
                try:
                    setter2(interval, publish_df.copy(), source="push")
                    logger.info(
                        "[push.runner] publish merged done via set_merged_summary tf=%s source=push rows=%s",
                        interval,
                        len(publish_df),
                    )
                except TypeError:
                    setter2(interval, publish_df.copy())
                    logger.warning(
                        "[push.runner] publish merged done via set_merged_summary WITHOUT source tf=%s rows=%s",
                        interval,
                        len(publish_df),
                    )
                ok = True
    except Exception:
        logger.exception("[push.runner] set merged summary failed tf=%s", interval)

    # Ver2.1: source="push" で実際に取り出せるか検証
    try:
        check_df = _get_merged_source_push(interval)
        logger.info(
            "[push.runner] publish verify source=push tf=%s rows=%s symbols=%s latest_dt=%s",
            interval,
            len(check_df),
            _safe_symbols(check_df),
            _safe_latest_dt(check_df),
        )
        _log_df_state(f"published-check-source-push-{interval}m", check_df, interval=interval)
    except Exception:
        logger.exception("[push.runner] publish verify failed tf=%s", interval)

    return ok


# ============================================================
# display
# ============================================================

def _resolve_display_callable():
    import importlib

    for module_name, func_name in _DISPLAY_CANDIDATES:
        try:
            mod = importlib.import_module(module_name)
            fn = getattr(mod, func_name, None)
            if callable(fn):
                logger.info("[push.runner] resolved display callable %s.%s", module_name, func_name)
                return fn
        except Exception:
            continue
    return None


def _safe_display(df: pd.DataFrame, interval: int, now: Optional[dt.datetime] = None) -> None:
    if df is None or df.empty:
        logger.warning("[push.runner] display skipped interval=%s reason=empty", interval)
        return

    _profile_df("display-input", df, interval=interval)

    if not _has_displayable_score(df):
        logger.warning(
            "[push.runner] display skipped interval=%s reason=no displayable score rows=%s cols=%s",
            interval,
            len(df),
            list(df.columns),
        )
        return

    fn = _resolve_display_callable()
    if not callable(fn):
        logger.warning("[push.runner] display callable not found interval=%s", interval)
        return

    try:
        _call_with_supported_kwargs(
            fn,
            df,
            interval=interval,
            now=now,
            title=f"PUSH SUMMARY {interval}m",
            source="push",
        )
        logger.info(
            "[push.runner] display done interval=%s rows=%s symbols=%s latest_dt=%s",
            interval,
            len(df),
            _safe_symbols(df),
            _safe_latest_dt(df),
        )
    except TypeError:
        try:
            # 古い表示関数互換
            fn(df, interval=interval)
            logger.info(
                "[push.runner] display done interval=%s rows=%s mode=legacy interval-only",
                interval,
                len(df),
            )
        except Exception:
            logger.exception("[push.runner] display failed interval=%s legacy retry", interval)
    except Exception:
        logger.exception("[push.runner] display failed interval=%s", interval)


# ============================================================
# main
# ============================================================

def run_push_summary_job(
    interval: int | str = 1,
    display: bool = True,
    now: Optional[dt.datetime] = None,
    *,
    push_df: Optional[pd.DataFrame] = None,
    **kwargs,
) -> dict:
    """
    PUSH由来サマリー実行本体

    戻り値:
      {
        "interval": int,
        "summary_df": pd.DataFrame,
        "summary_latest_df": pd.DataFrame,
        "published": bool,
      }
    """
    interval_n = _as_interval(interval)
    t0 = time.perf_counter()

    logger.info(
        "[push.runner] start interval=%s display=%s now=%s extra_keys=%s process_single_interval_available=%s",
        interval_n,
        display,
        now,
        sorted(list(kwargs.keys())),
        process_single_interval is not None,
    )

    if process_single_interval is None:
        logger.error("[push.runner] process_single_interval import unavailable -> try direct OHLC fallback")
        base_for_fallback = _ensure_df(push_df, "push_df_arg_fallback")
        if base_for_fallback.empty:
            base_for_fallback = _get_push_df_from_global_data()
        hist_fb, latest_fb = _build_direct_ohlc_summary_from_push(base_for_fallback, interval=interval_n, now=now)
        if not latest_fb.empty:
            publish_fb = _prepare_publish_df(latest_fb)
            published_fb = _publish_to_global_context(interval_n, publish_fb)
            if display:
                _safe_display(publish_fb, interval=interval_n, now=now)
            return {"interval": interval_n, "summary_df": hist_fb, "summary_latest_df": latest_fb, "published": bool(published_fb)}
        return _empty_result(interval_n)

    # --------------------------------------------------------
    # resolve push df
    # --------------------------------------------------------
    base_push_df = _ensure_df(push_df, "push_df_arg")
    if base_push_df.empty:
        base_push_df = _get_push_df_from_global_data()

    base_push_df = _ensure_datetime(base_push_df)

    if base_push_df.empty:
        logger.warning(
            "[push.runner] no push df interval=%s now=%s -> return empty",
            interval_n,
            now,
        )
        return {
            "interval": interval_n,
            "summary_df": pd.DataFrame(),
            "summary_latest_df": pd.DataFrame(),
            "published": False,
        }

    _log_df_state("push-base", base_push_df, interval=interval_n)

    work_push_df = _filter_push_df_for_interval(base_push_df, interval=interval_n, now=now)
    if work_push_df.empty:
        logger.warning(
            "[push.runner] filtered push df empty interval=%s now=%s",
            interval_n,
            now,
        )
        return {
            "interval": interval_n,
            "summary_df": pd.DataFrame(),
            "summary_latest_df": pd.DataFrame(),
            "published": False,
        }

    # 履歴不足の早期警告
    try:
        if "symbol" in work_push_df.columns and "datetime" in work_push_df.columns:
            tmp = work_push_df[["symbol", "datetime"]].copy()
            tmp["datetime"] = pd.to_datetime(tmp["datetime"], errors="coerce")
            tmp = tmp.dropna(subset=["symbol", "datetime"])
            # 分単位で何本あるかを見る
            tmp["minute"] = tmp["datetime"].dt.floor("min")
            per_symbol_minutes = tmp.groupby("symbol")["minute"].nunique()
            if not per_symbol_minutes.empty:
                logger.info(
                    "[push.runner] history profile interval=%s symbols=%s min_minutes=%s median_minutes=%.1f max_minutes=%s",
                    interval_n,
                    len(per_symbol_minutes),
                    int(per_symbol_minutes.min()),
                    float(per_symbol_minutes.median()),
                    int(per_symbol_minutes.max()),
                )
                if interval_n in (3, 5) and int(per_symbol_minutes.max()) < 30:
                    logger.warning(
                        "[push.runner] history may be insufficient interval=%s max_minutes=%s "
                        "=> 3m/5m technicals/top10 may be weak",
                        interval_n,
                        int(per_symbol_minutes.max()),
                    )
    except Exception:
        logger.exception("[push.runner] history profile failed interval=%s", interval_n)

    # --------------------------------------------------------
    # incremental pipeline
    # --------------------------------------------------------
    try:
        result = _call_with_supported_kwargs(process_single_interval, work_push_df, interval=interval_n, now=now)
    except Exception:
        logger.exception("[push.runner] process_single_interval failed interval=%s -> try direct OHLC fallback", interval_n)
        hist_fb, latest_fb = _build_direct_ohlc_summary_from_push(work_push_df, interval=interval_n, now=now)
        if latest_fb.empty:
            return _empty_result(interval_n)
        result = {"summary_df": hist_fb, "summary_latest_df": latest_fb, "_fallback_reason": "process_single_interval_exception"}

    if not isinstance(result, dict):
        logger.warning("[push.runner] process_single_interval returned non-dict interval=%s type=%s -> try direct OHLC fallback", interval_n, type(result).__name__)
        hist_fb, latest_fb = _build_direct_ohlc_summary_from_push(work_push_df, interval=interval_n, now=now)
        if latest_fb.empty:
            return _empty_result(interval_n)
        result = {"summary_df": hist_fb, "summary_latest_df": latest_fb, "_fallback_reason": "process_single_interval_non_dict"}

    summary_df = _ensure_df(result.get("summary_df"), "summary_df")
    summary_latest_df = _ensure_df(result.get("summary_latest_df"), "summary_latest_df")

    if summary_latest_df.empty and not summary_df.empty:
        summary_latest_df = _latest_only_from_df(summary_df)
        logger.warning("[push.runner] summary_latest_df rescued from summary_df interval=%s src_rows=%s out_rows=%s", interval_n, len(summary_df), len(summary_latest_df))

    if summary_df.empty and summary_latest_df.empty and not work_push_df.empty:
        logger.warning("[push.runner] incremental result empty interval=%s push_rows=%s -> direct OHLC fallback", interval_n, len(work_push_df))
        summary_df, summary_latest_df = _build_direct_ohlc_summary_from_push(work_push_df, interval=interval_n, now=now)

    summary_df = _ensure_datetime(summary_df)
    summary_latest_df = _ensure_datetime(summary_latest_df)

    _log_df_state(f"summary-{interval_n}m", summary_df, interval=interval_n)
    _log_df_state(f"summary-latest-{interval_n}m", summary_latest_df, interval=interval_n)

    # --------------------------------------------------------
    # publish
    # --------------------------------------------------------
    publish_df = _prepare_publish_df(summary_latest_df)
    if publish_df.empty or not _is_publishable_summary(publish_df):
        logger.warning(
            "[push.runner] latest publish candidate incomplete interval=%s -> fallback to summary_df latest",
            interval_n,
        )
        publish_df = _prepare_publish_df(_latest_only_from_df(summary_df))

    if publish_df.empty:
        logger.warning(
            "[push.runner] publish_df empty interval=%s summary_rows=%s latest_rows=%s",
            interval_n,
            len(summary_df),
            len(summary_latest_df),
        )

    published = _publish_to_global_context(interval_n, publish_df)

    logger.info(
        "[push.runner] publish result interval=%s published=%s rows=%s symbols=%s latest_dt=%s",
        interval_n,
        published,
        len(publish_df) if isinstance(publish_df, pd.DataFrame) else 0,
        _safe_symbols(publish_df) if isinstance(publish_df, pd.DataFrame) else 0,
        _safe_latest_dt(publish_df) if isinstance(publish_df, pd.DataFrame) else None,
    )

    # --------------------------------------------------------
    # display
    # --------------------------------------------------------
    if display:
        # 原則 publish_df を表示。
        # publish_df が空の場合のみ summary_latest_df / global source=push から救済。
        display_df = publish_df if isinstance(publish_df, pd.DataFrame) and not publish_df.empty else pd.DataFrame()

        if display_df.empty and isinstance(summary_latest_df, pd.DataFrame) and not summary_latest_df.empty:
            logger.warning("[push.runner] display_df rescued from summary_latest_df interval=%s", interval_n)
            display_df = _prepare_publish_df(summary_latest_df)

        if display_df.empty:
            from_global = _get_merged_source_push(interval_n)
            if not from_global.empty:
                logger.warning("[push.runner] display_df rescued from global source=push interval=%s", interval_n)
                display_df = _prepare_publish_df(from_global)

        try:
            _safe_display(display_df, interval=interval_n, now=now)
        except Exception:
            logger.exception("[push.runner] safe display wrapper failed interval=%s", interval_n)

    out = {
        "interval": interval_n,
        "summary_df": summary_df if isinstance(summary_df, pd.DataFrame) else pd.DataFrame(),
        "summary_latest_df": summary_latest_df if isinstance(summary_latest_df, pd.DataFrame) else pd.DataFrame(),
        "published": bool(published),
    }

    logger.info(
        "[push.runner] finished interval=%s summary_rows=%s latest_rows=%s latest_dt=%s published=%s elapsed=%.3fs",
        interval_n,
        len(out["summary_df"]),
        len(out["summary_latest_df"]),
        _safe_latest_dt(out["summary_latest_df"]),
        out["published"],
        time.perf_counter() - t0,
    )
    return out


__all__ = [
    "run_push_summary_job",
]