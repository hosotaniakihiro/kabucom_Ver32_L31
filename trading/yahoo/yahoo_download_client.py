# ============================================================
# File   : trading/yahoo/yahoo_download_client.py
# Version: Ver2.0-PRODUCTION-YAHOO-DOWNLOAD-CLIENT-HARD-TIMEOUT
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
# ✔ 1銘柄ハングでも全体停止しない
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import time
import threading
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
    s = str(symbol or "").strip().upper()
    return s


def _normalize_yahoo_symbol(symbol: str) -> str:
    s = _normalize_symbol_token(symbol)
    if not s:
        return s

    if s.endswith(".T"):
        return s

    if s.isdigit():
        return f"{s}.T"

    return s


def _extract_symbol_only_columns(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """
    yfinance の MultiIndex columns から対象 symbol の列だけを厳密抽出する。
    """
    if df is None or df.empty:
        return pd.DataFrame()

    out = df.copy()
    target = _normalize_symbol_token(symbol)
    target_alt = _normalize_symbol_token(_normalize_yahoo_symbol(symbol))

    try:
        if not isinstance(out.columns, pd.MultiIndex):
            return out

        keep_cols = []

        for col in out.columns:
            try:
                if not isinstance(col, tuple) or len(col) < 2:
                    continue

                sym = _normalize_symbol_token(col[1])

                if sym == target or sym == target_alt:
                    keep_cols.append(col)
            except Exception:
                continue

        if not keep_cols:
            logger.warning(
                "[YAHOO DEBUG] %s no matching MultiIndex columns found raw_cols=%s",
                symbol,
                list(out.columns),
            )
            return pd.DataFrame()

        out = out.loc[:, keep_cols].copy()
        out.columns = [str(c[0]).strip() for c in keep_cols]

        return out

    except Exception:
        logger.exception("[YAHOO DEBUG] %s symbol-only extract failed", symbol)
        return pd.DataFrame()


# ============================================================
# dataframe guards
# ============================================================

def _ensure_series(value):
    if isinstance(value, pd.DataFrame):
        if value.shape[1] <= 0:
            return pd.Series(dtype="float64")
        return value.iloc[:, 0]
    return value


def _drop_duplicate_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    out = df.copy()

    try:
        out = out.loc[:, ~out.columns.duplicated(keep="last")]
    except Exception:
        logger.exception("[YAHOO DEBUG] duplicate columns drop failed")

    return out


def _drop_duplicate_index(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    out = df.copy()

    try:
        out = out[~out.index.duplicated(keep="last")]
    except Exception:
        logger.exception("[YAHOO DEBUG] duplicate index drop failed")

    return out


def _replace_inf(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    out = df.copy()

    try:
        out = out.replace([np.inf, -np.inf], np.nan)
    except Exception:
        logger.exception("[YAHOO DEBUG] replace inf failed")

    return out


# ============================================================
# column normalize
# ============================================================

def _flatten_columns(df: pd.DataFrame) -> pd.DataFrame:
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
    out = _drop_duplicate_columns(out)
    return out


# ============================================================
# index normalize
# ============================================================

def _normalize_index(df: pd.DataFrame) -> pd.DataFrame:
    out = _drop_duplicate_index(df)
    out = out.reset_index()

    rename_map = {}
    for c in out.columns:
        cl = str(c).strip().lower()
        if cl == "datetime":
            rename_map[c] = "time"
        elif cl == "date":
            rename_map[c] = "time"
        elif cl == "index":
            rename_map[c] = "time"

    if rename_map:
        out = out.rename(columns=rename_map)

    out = _drop_duplicate_columns(out)
    return out


# ============================================================
# datetime normalize
# ============================================================

def _normalize_datetime(df: pd.DataFrame) -> pd.DataFrame:
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
        return pd.DataFrame()

    out["time"] = pd.to_datetime(_ensure_series(out["time"]), errors="coerce")
    out = out.dropna(subset=["time"])

    if out.empty:
        return out

    try:
        if getattr(out["time"].dt, "tz", None) is not None:
            out["time"] = out["time"].dt.tz_convert("Asia/Tokyo").dt.tz_localize(None)
        else:
            out["time"] = (
                pd.to_datetime(out["time"], errors="coerce")
                .dt.tz_localize("UTC")
                .dt.tz_convert("Asia/Tokyo")
                .dt.tz_localize(None)
            )
    except Exception:
        logger.exception("[YAHOO DEBUG] datetime JST normalize failed")

    out = out.dropna(subset=["time"])

    try:
        out = out.sort_values("time")
        out = out.drop_duplicates(subset=["time"], keep="last")
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
        return pd.DataFrame()

    if "volume" not in out.columns:
        out["volume"] = pd.NA

    for col in ["open_price", "high_price", "low_price", "close_price", "volume"]:
        s = _ensure_series(out[col])
        out[col] = pd.to_numeric(s, errors="coerce")

    out = _replace_inf(out)
    out = out.dropna(subset=["open_price", "high_price", "low_price", "close_price"])

    if out.empty:
        logger.warning("[YAHOO DEBUG] empty after OHLC numeric validation")
        return pd.DataFrame()

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
        logger.warning(
            "[YAHOO DEBUG] dropping zero OHLCV rows=%d/%d",
            zero_count,
            len(out),
        )
        out = out.loc[~zero_mask].copy()

    return out


# ============================================================
# volume safety
# ============================================================

def _fix_volume(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()

    if "volume" not in out.columns:
        return out

    try:
        out["volume"] = pd.to_numeric(_ensure_series(out["volume"]), errors="coerce")
        out.loc[out["volume"] < 0, "volume"] = 0.0
    except Exception:
        out["volume"] = pd.to_numeric(_ensure_series(out["volume"]), errors="coerce")

    return out


# ============================================================
# delay filter
# ============================================================

def _apply_delay_filter(df: pd.DataFrame) -> pd.DataFrame:
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
        store["df"] = df
        store["error"] = None
    except Exception as e:
        store["df"] = pd.DataFrame()
        store["error"] = e


def _download_raw_once(
    symbol: str,
    interval: str = "1m",
) -> pd.DataFrame:
    yahoo_symbol = _normalize_yahoo_symbol(symbol)

    store: dict = {
        "df": pd.DataFrame(),
        "error": None,
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
        return pd.DataFrame()

    err = store.get("error")
    if err is not None:
        raise err

    df = store.get("df")
    if df is None:
        return pd.DataFrame()

    if not isinstance(df, pd.DataFrame):
        try:
            df = pd.DataFrame(df)
        except Exception:
            return pd.DataFrame()

    """try:
        logger.info(
            "[YAHOO DEBUG] yf.download done symbol=%s rows=%s elapsed=%.3fs",
            symbol,
            0 if df is None else len(df),
            max(time.time() - started, 0.0),
        )
    except Exception:
        pass"""

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
    Yahoo 1min download safe wrapper
    """

    if not symbol:
        return pd.DataFrame()

    raw_df = pd.DataFrame()
    last_error = None

    for attempt in range(1, YAHOO_MAX_RETRIES + 1):
        attempt_started = time.time()

        try:
            raw_df = _download_raw_once(symbol=symbol, interval=interval)

            if raw_df is None or raw_df.empty:
                logger.debug(
                    "[YAHOO DEBUG] %s empty dataframe attempt=%d/%d elapsed=%.3fs",
                    symbol,
                    attempt,
                    YAHOO_MAX_RETRIES,
                    max(time.time() - attempt_started, 0.0),
                )
            last_error = None
            break

        except ValueError as e:
            last_error = e
            if "No objects to concatenate" in str(e):
                logger.warning(
                    "[YAHOO DEBUG] %s no objects to concatenate attempt=%d/%d",
                    symbol,
                    attempt,
                    YAHOO_MAX_RETRIES,
                )
                return pd.DataFrame()

            logger.exception(
                "[YAHOO DEBUG] download value error %s attempt=%d/%d",
                symbol,
                attempt,
                YAHOO_MAX_RETRIES,
            )

        except RuntimeError as e:
            last_error = e
            logger.exception(
                "[YAHOO DEBUG] runtime download failed %s attempt=%d/%d",
                symbol,
                attempt,
                YAHOO_MAX_RETRIES,
            )

        except Exception as e:
            last_error = e
            logger.exception(
                "[YAHOO DEBUG] download failed %s attempt=%d/%d",
                symbol,
                attempt,
                YAHOO_MAX_RETRIES,
            )

        time.sleep(YAHOO_RETRY_SLEEP_SEC)

    if last_error is not None and (raw_df is None or raw_df.empty):
        return pd.DataFrame()

    if raw_df is None or raw_df.empty:
        return pd.DataFrame()

    try:
        out = raw_df.copy()
        out = _drop_duplicate_columns(out)
        out = _drop_duplicate_index(out)

        out = _extract_symbol_only_columns(out, symbol)
        if out.empty:
            logger.warning("[YAHOO DEBUG] %s symbol-only extraction empty", symbol)
            return pd.DataFrame()

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
            return pd.DataFrame()

        out = _fix_volume(out)
        out = _replace_inf(out)
        out = _apply_delay_filter(out)

        if out.empty:
            logger.debug("[YAHOO DEBUG] %s empty after delay filter", symbol)
            return out

        out = out.sort_values("time").drop_duplicates(subset=["time"], keep="last").copy()

        out["datetime"] = out["time"]
        out["symbol"] = str(symbol).replace(".T", "")

        return out

    except Exception:
        logger.exception("[YAHOO DEBUG] normalization failed %s", symbol)
        return pd.DataFrame()