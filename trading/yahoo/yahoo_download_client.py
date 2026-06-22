# ============================================================
# File   : trading/yahoo/yahoo_download_client.py
# Version: Ver2.1-PRODUCTION-YAHOO-DOWNLOAD-CLIENT-KEYERROR-SAFE
# ------------------------------------------------------------
# ✔ safe_download
# ✔ Yahoo 1m 安定取得
# ✔ MultiIndex完全防御
# ✔ 対象シンボル列のみ厳密抽出
# ✔ timezone完全吸収
# ✔ column normalize
# ✔ price normalize
# ✔ NaN / inf 防御
# ✔ volume safety
# ✔ 20分遅延フィルタ
# ✔ yfinance crash guard
# ✔ yfinance shared._DFS KeyError 防御
# ✔ No objects to concatenate 防御
# ✔ dataframe構造保証
# ✔ production safe
# ✔ zero-padding ban for missing OHLC
# ✔ strict OHLC validation
# ✔ duplicate index 防御
# ✔ duplicate columns 防御
# ✔ serial retry safe
# ✔ ticker= 明示で単一銘柄固定
# ✔ yfinance call timeout
# ✔ 1銘柄ハング/失敗でも全体停止しない
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from typing import Optional

import numpy as np
import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

# ============================================================
# config
# ============================================================

YAHOO_DELAY_MIN = 20
YAHOO_MAX_RETRIES = 2
YAHOO_RETRY_SLEEP_SEC = 0.8

# 1回の yf.download 許容秒数
YF_DOWNLOAD_TIMEOUT_SEC = 15.0


# ============================================================
# symbol helpers
# ============================================================

def _normalize_symbol_token(symbol: str) -> str:
    return str(symbol or "").strip().upper()


def _normalize_yahoo_symbol(symbol: str) -> str:
    s = _normalize_symbol_token(symbol)
    if not s:
        return s
    if s.endswith(".T"):
        return s
    if s.isdigit():
        return f"{s}.T"
    return s


def _plain_symbol(symbol: str) -> str:
    return str(symbol or "").strip().upper().replace(".T", "")


# ============================================================
# dataframe guards
# ============================================================

def _ensure_series(value):
    if isinstance(value, pd.DataFrame):
        if value.shape[1] <= 0:
            return pd.Series(dtype="float64")
        return value.iloc[:, 0]
    return value


def _empty_df() -> pd.DataFrame:
    return pd.DataFrame()


def _drop_duplicate_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return _empty_df()
    out = df.copy()
    try:
        out = out.loc[:, ~out.columns.duplicated(keep="last")]
    except Exception:
        logger.exception("[YAHOO DEBUG] duplicate columns drop failed")
    return out


def _drop_duplicate_index(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return _empty_df()
    out = df.copy()
    try:
        out = out[~out.index.duplicated(keep="last")]
    except Exception:
        logger.exception("[YAHOO DEBUG] duplicate index drop failed")
    return out


def _replace_inf(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return _empty_df()
    out = df.copy()
    try:
        out = out.replace([np.inf, -np.inf], np.nan)
    except Exception:
        logger.exception("[YAHOO DEBUG] replace inf failed")
    return out


# ============================================================
# yfinance MultiIndex helpers
# ============================================================

def _extract_symbol_only_columns(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """
    yfinance の MultiIndex columns から対象 symbol の列だけを厳密抽出する。
    単一tickerの通常DataFrameならそのまま返す。
    """
    if df is None or df.empty:
        return _empty_df()

    out = df.copy()
    target = _normalize_symbol_token(symbol)
    target_alt = _normalize_symbol_token(_normalize_yahoo_symbol(symbol))

    try:
        if not isinstance(out.columns, pd.MultiIndex):
            return out

        keep_cols = []
        for col in out.columns:
            try:
                if not isinstance(col, tuple):
                    continue
                tokens = [_normalize_symbol_token(x) for x in col if x is not None]
                if target in tokens or target_alt in tokens:
                    keep_cols.append(col)
            except Exception:
                continue

        # yf.download(tickers="9399.T") では MultiIndex の第2階層が ticker ではない場合がある。
        # 単一銘柄で ticker 階層が見つからない場合は、価格列だけを残す方向にフォールバックする。
        if not keep_cols:
            price_names = {"open", "high", "low", "close", "adj close", "volume"}
            fallback_cols = []
            for col in out.columns:
                try:
                    if isinstance(col, tuple) and str(col[0]).strip().lower() in price_names:
                        fallback_cols.append(col)
                except Exception:
                    continue
            if fallback_cols:
                keep_cols = fallback_cols
            else:
                logger.warning(
                    "[YAHOO DEBUG] %s no matching MultiIndex columns found raw_cols=%s",
                    symbol,
                    list(out.columns),
                )
                return _empty_df()

        out = out.loc[:, keep_cols].copy()
        new_cols = []
        for c in keep_cols:
            try:
                new_cols.append(str(c[0]).strip())
            except Exception:
                new_cols.append(str(c).strip())
        out.columns = new_cols
        return out

    except Exception:
        logger.exception("[YAHOO DEBUG] %s symbol-only extract failed", symbol)
        return _empty_df()


# ============================================================
# column/index normalize
# ============================================================

def _flatten_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return _empty_df()
    out = df.copy()
    try:
        if isinstance(out.columns, pd.MultiIndex):
            flattened = []
            for col in out.columns:
                parts = []
                for x in col:
                    if x is None:
                        continue
                    s = str(x).strip()
                    if not s or s.lower() == "nan":
                        continue
                    parts.append(s)
                flattened.append("__".join(parts))
            out.columns = flattened
    except Exception:
        logger.exception("[YAHOO DEBUG] flatten columns failed")
    return out


def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return _empty_df()
    out = _flatten_columns(df)
    new_cols = []
    for c in out.columns:
        s = str(c).strip().lower()
        s = s.replace(" ", "_")
        s = s.replace("-", "_")
        s = s.replace("/", "_")
        s = s.replace("(", "")
        s = s.replace(")", "")
        s = s.replace(".", "_")
        new_cols.append(s)
    out.columns = new_cols
    return _drop_duplicate_columns(out)


def _normalize_index(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return _empty_df()
    out = _drop_duplicate_index(df)
    out = out.reset_index()

    rename_map = {}
    for c in out.columns:
        cl = str(c).strip().lower()
        if cl in {"datetime", "date", "index"}:
            rename_map[c] = "time"
    if rename_map:
        out = out.rename(columns=rename_map)

    return _drop_duplicate_columns(out)


def _normalize_datetime(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return _empty_df()

    out = df.copy()
    if "time" not in out.columns:
        for c in list(out.columns):
            cl = str(c).lower()
            if "time" in cl or "date" in cl:
                ts = pd.to_datetime(_ensure_series(out[c]), errors="coerce")
                if ts.notna().any():
                    out["time"] = ts
                    break

    if "time" not in out.columns:
        return _empty_df()

    out["time"] = pd.to_datetime(_ensure_series(out["time"]), errors="coerce")
    out = out.dropna(subset=["time"])
    if out.empty:
        return out

    try:
        if getattr(out["time"].dt, "tz", None) is not None:
            out["time"] = out["time"].dt.tz_convert("Asia/Tokyo").dt.tz_localize(None)
        else:
            # yfinanceのtimestampは多くの場合UTC相当。失敗時は元のnaive値を残す。
            try:
                out["time"] = (
                    pd.to_datetime(out["time"], errors="coerce")
                    .dt.tz_localize("UTC")
                    .dt.tz_convert("Asia/Tokyo")
                    .dt.tz_localize(None)
                )
            except Exception:
                out["time"] = pd.to_datetime(_ensure_series(out["time"]), errors="coerce")
    except Exception:
        logger.exception("[YAHOO DEBUG] datetime JST normalize failed")

    out = out.dropna(subset=["time"])
    try:
        out = out.sort_values("time").drop_duplicates(subset=["time"], keep="last")
    except Exception:
        logger.exception("[YAHOO DEBUG] datetime sort/dedup failed")
    return out


# ============================================================
# price normalize
# ============================================================

def _pick_first_matching_column(df: pd.DataFrame, patterns: list[str]) -> Optional[str]:
    cols = list(df.columns)
    for p in patterns:
        if p in cols:
            return p
    for c in cols:
        cl = str(c).lower()
        for p in patterns:
            if p == "close" and "adj" in cl:
                continue
            if p in cl:
                return c
    return None


def _normalize_prices(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return _empty_df()

    out = df.copy()
    mapping = {
        "open_price": ["open_price", "open"],
        "high_price": ["high_price", "high"],
        "low_price": ["low_price", "low"],
        "close_price": ["close_price", "close", "adj_close", "adjclose"],
        "volume": ["volume", "trading_volume", "vol"],
    }

    for dst, patterns in mapping.items():
        if dst in out.columns:
            continue
        src = _pick_first_matching_column(out, patterns)
        if src is not None:
            out[dst] = _ensure_series(out[src])

    required_ohlc = ["open_price", "high_price", "low_price", "close_price"]
    missing = [c for c in required_ohlc if c not in out.columns]
    if missing:
        logger.warning(
            "[YAHOO DEBUG] missing OHLC columns missing=%s cols=%s",
            missing,
            list(out.columns),
        )
        return _empty_df()

    if "volume" not in out.columns:
        out["volume"] = pd.NA

    for col in ["open_price", "high_price", "low_price", "close_price", "volume"]:
        out[col] = pd.to_numeric(_ensure_series(out[col]), errors="coerce")

    out = _replace_inf(out)
    out = out.dropna(subset=["open_price", "high_price", "low_price", "close_price"])
    if out.empty:
        logger.warning("[YAHOO DEBUG] empty after OHLC numeric validation")
        return _empty_df()

    zero_mask = (
        out[["open_price", "high_price", "low_price", "close_price"]]
        .fillna(0)
        .eq(0)
        .all(axis=1)
    )
    if "volume" in out.columns:
        zero_mask = zero_mask & out["volume"].fillna(0).eq(0)

    zero_count = int(zero_mask.sum())
    if zero_count > 0:
        logger.warning("[YAHOO DEBUG] dropping zero OHLCV rows=%d/%d", zero_count, len(out))
        out = out.loc[~zero_mask].copy()

    # downstream互換用 alias
    out["open"] = out["open_price"]
    out["high"] = out["high_price"]
    out["low"] = out["low_price"]
    out["close"] = out["close_price"]
    return out


# ============================================================
# volume/delay safety
# ============================================================

def _fix_volume(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return _empty_df()
    out = df.copy()
    if "volume" not in out.columns:
        return out
    try:
        out["volume"] = pd.to_numeric(_ensure_series(out["volume"]), errors="coerce")
        out.loc[out["volume"] < 0, "volume"] = 0.0
    except Exception:
        out["volume"] = pd.to_numeric(_ensure_series(out["volume"]), errors="coerce")
    return out


def _apply_delay_filter(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return _empty_df()
    cutoff = dt.datetime.now().replace(second=0, microsecond=0) - dt.timedelta(minutes=YAHOO_DELAY_MIN)
    return df[df["time"] <= cutoff].copy()


# ============================================================
# yfinance call with timeout
# ============================================================

def _yf_download_worker(store: dict, yahoo_symbol: str, interval: str) -> None:
    try:
        df = yf.download(
            tickers=yahoo_symbol,
            interval=interval,
            period="1d",
            progress=False,
            auto_adjust=False,
            threads=False,
            group_by="column",
        )
        if df is None:
            store["df"] = _empty_df()
        elif isinstance(df, pd.DataFrame):
            store["df"] = df
        else:
            try:
                store["df"] = pd.DataFrame(df)
            except Exception:
                store["df"] = _empty_df()
        store["error"] = None
    except KeyError as e:
        # yfinance内部 shared._DFS[ticker] 欠落。銘柄単位の一時失敗なので全体停止させない。
        store["df"] = _empty_df()
        store["error"] = None
        store["soft_error"] = f"KeyError: {e}"
    except ValueError as e:
        # No objects to concatenate 等。空DFで安全にスキップ。
        store["df"] = _empty_df()
        store["error"] = None
        store["soft_error"] = f"ValueError: {e}"
    except Exception as e:
        # その他も銘柄単位では空DFにする。例外スタック連発でメイン処理を止めない。
        store["df"] = _empty_df()
        store["error"] = None
        store["soft_error"] = f"{type(e).__name__}: {e}"


def _download_raw_once(symbol: str, interval: str = "1m") -> pd.DataFrame:
    yahoo_symbol = _normalize_yahoo_symbol(symbol)
    if not yahoo_symbol:
        return _empty_df()

    store: dict = {
        "df": _empty_df(),
        "error": None,
        "soft_error": None,
    }

    th = threading.Thread(
        target=_yf_download_worker,
        args=(store, yahoo_symbol, interval),
        name=f"YFDownload-{yahoo_symbol}",
        daemon=True,
    )

    started = time.time()
    th.start()
    th.join(timeout=YF_DOWNLOAD_TIMEOUT_SEC)

    if th.is_alive():
        logger.warning(
            "[YAHOO DEBUG] yf.download timeout symbol=%s yahoo_symbol=%s timeout=%.1fs",
            symbol,
            yahoo_symbol,
            YF_DOWNLOAD_TIMEOUT_SEC,
        )
        return _empty_df()

    soft_error = store.get("soft_error")
    if soft_error:
        logger.warning(
            "[YAHOO DEBUG] yf.download soft failed symbol=%s yahoo_symbol=%s interval=%s err=%s elapsed=%.3fs",
            symbol,
            yahoo_symbol,
            interval,
            soft_error,
            max(time.time() - started, 0.0),
        )
        return _empty_df()

    err = store.get("error")
    if err is not None:
        logger.warning(
            "[YAHOO DEBUG] yf.download failed symbol=%s yahoo_symbol=%s interval=%s err=%s",
            symbol,
            yahoo_symbol,
            interval,
            err,
        )
        return _empty_df()

    df = store.get("df")
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return _empty_df()

    return df.copy()


# ============================================================
# safe download
# ============================================================

def safe_download(
    symbol: str,
    start_dt: dt.datetime,
    end_dt: dt.datetime,
    interval: str = "1m",
) -> pd.DataFrame:
    """
    Yahoo 1min download safe wrapper.
    1銘柄の yfinance 失敗/KeyError/empty/timeout は空DataFrameとして返し、全体処理は止めない。
    """
    if not symbol:
        return _empty_df()

    raw_df = _empty_df()

    for attempt in range(1, YAHOO_MAX_RETRIES + 1):
        attempt_started = time.time()
        raw_df = _download_raw_once(symbol=symbol, interval=interval)

        if raw_df is not None and not raw_df.empty:
            break

        logger.debug(
            "[YAHOO DEBUG] %s empty dataframe attempt=%d/%d elapsed=%.3fs",
            symbol,
            attempt,
            YAHOO_MAX_RETRIES,
            max(time.time() - attempt_started, 0.0),
        )
        if attempt < YAHOO_MAX_RETRIES:
            time.sleep(YAHOO_RETRY_SLEEP_SEC)

    if raw_df is None or raw_df.empty:
        return _empty_df()

    try:
        out = raw_df.copy()
        out = _drop_duplicate_columns(out)
        out = _drop_duplicate_index(out)

        out = _extract_symbol_only_columns(out, symbol)
        if out.empty:
            logger.warning("[YAHOO DEBUG] %s symbol-only extraction empty", symbol)
            return _empty_df()

        out = _drop_duplicate_columns(out)
        out = _drop_duplicate_index(out)
        out = _normalize_columns(out)
        out = _normalize_index(out)
        out = _normalize_datetime(out)
        if out.empty:
            return out

        if start_dt is not None:
            out = out[out["time"] >= start_dt - dt.timedelta(days=2)]
        if end_dt is not None:
            out = out[out["time"] <= end_dt]
        if out.empty:
            return out

        out = _normalize_prices(out)
        if out.empty:
            logger.warning("[YAHOO DEBUG] %s normalize_prices empty", symbol)
            return _empty_df()

        out = _fix_volume(out)
        out = _replace_inf(out)
        out = _apply_delay_filter(out)
        if out.empty:
            logger.debug("[YAHOO DEBUG] %s empty after delay filter", symbol)
            return out

        out = out.sort_values("time").drop_duplicates(subset=["time"], keep="last").copy()
        out["datetime"] = out["time"]
        out["symbol"] = _plain_symbol(symbol)
        return out

    except Exception:
        logger.exception("[YAHOO DEBUG] normalization failed %s", symbol)
        return _empty_df()
