# ============================================================
# File   : trading/yahoo/normalize/yahoo_normalizer_resolver.py
# Version: Ver1.0-PRODUCTION-YAHOO-NORMALIZER-RESOLVER
# ------------------------------------------------------------
# ✔ complement_scheduler から Yahoo正規化責務を分離
# ✔ tidy形式最優先
# ✔ resolver/fallback両対応
# ✔ MultiIndex / yfinance形式 / wide-long 両対応
# ✔ production hardened
# ============================================================

from __future__ import annotations

import logging
from typing import Callable

import pandas as pd

logger = logging.getLogger(__name__)


def _safe_to_frame(df) -> pd.DataFrame:
    try:
        if df is None:
            return pd.DataFrame()
        if isinstance(df, pd.DataFrame):
            return df.copy()
        return pd.DataFrame(df)
    except Exception:
        logger.exception("[YAHOO NORMALIZER] to_frame failed")
        return pd.DataFrame()


def _flatten_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df.copy()

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
        logger.exception("[YAHOO NORMALIZER] flatten columns failed")

    cols = []
    for c in out.columns:
        s = str(c).strip()
        s = s.replace(" ", "_").replace("-", "_").replace("/", "_")
        s = s.replace("(", "").replace(")", "").replace(".", "_")
        s = s.lower()
        cols.append(s)
    out.columns = cols

    try:
        out = out.loc[:, ~out.columns.duplicated()]
    except Exception:
        pass

    return out


def _extract_datetime_column(out: pd.DataFrame) -> pd.DataFrame:
    if out is None or out.empty:
        return pd.DataFrame()

    df = out.copy()

    try:
        if isinstance(df.index, pd.DatetimeIndex):
            df["datetime"] = pd.to_datetime(df.index, errors="coerce")
            df = df.reset_index(drop=True)
            return df
    except Exception:
        pass

    candidates = [
        "datetime", "timestamp", "date", "time", "index",
        "datetime_utc", "datetime_jst",
    ]

    for c in candidates:
        if c in df.columns:
            try:
                ts = pd.to_datetime(df[c], errors="coerce")
                if ts.notna().any():
                    df["datetime"] = ts
                    return df
            except Exception:
                continue

    for c in list(df.columns):
        cl = str(c).lower()
        if "time" in cl or "date" in cl:
            try:
                ts = pd.to_datetime(df[c], errors="coerce")
                if ts.notna().any():
                    df["datetime"] = ts
                    return df
            except Exception:
                continue

    return df


def _coerce_symbol_series(s: pd.Series) -> pd.Series:
    out = s.astype(str).str.strip()
    out = out.str.replace(r"\.t$", "", regex=True)
    out = out.str.replace(r"\.0$", "", regex=True)
    out = out.replace({"nan": pd.NA, "none": pd.NA, "": pd.NA})
    return out


def _extract_symbol_column(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    out = df.copy()

    for c in ["symbol", "ticker", "code", "stock_code", "security_code"]:
        if c in out.columns:
            out["symbol"] = _coerce_symbol_series(out[c])
            return out

    prefixed = [c for c in out.columns if "__" in c]
    if prefixed:
        metric_prefixes = ("open__", "high__", "low__", "close__", "adj_close__", "volume__")
        hit = [c for c in prefixed if c.startswith(metric_prefixes)]
        if hit:
            long_rows = []
            dt_col = "datetime" if "datetime" in out.columns else None
            base = out.copy()

            for col in hit:
                try:
                    metric, ticker = col.split("__", 1)
                except ValueError:
                    continue

                tmp = pd.DataFrame()
                if dt_col and dt_col in base.columns:
                    tmp["datetime"] = base[dt_col]
                else:
                    continue

                tmp["symbol"] = ticker
                tmp[metric] = base[col]
                long_rows.append(tmp)

            if long_rows:
                merged = None
                for part in long_rows:
                    key_cols = ["datetime", "symbol"]
                    value_cols = [c for c in part.columns if c not in key_cols]
                    if not value_cols:
                        continue
                    if merged is None:
                        merged = part
                    else:
                        merged = merged.merge(part, on=key_cols, how="outer")

                if merged is not None and not merged.empty:
                    merged["symbol"] = _coerce_symbol_series(merged["symbol"])
                    return merged

    return out


def _rename_yahoo_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    out = df.copy()

    rename_map = {
        "open_price": "open",
        "high_price": "high",
        "low_price": "low",
        "close_price": "close",
        "adj_close": "adj_close",
        "adjusted_close": "adj_close",
        "adjclose": "adj_close",
        "trading_volume": "volume",
        "current_price": "close",
        "last_price": "close",
        "price": "price",
    }

    for src, dst in list(rename_map.items()):
        if src in out.columns and dst not in out.columns:
            out[dst] = out[src]

    patterns = {
        "open": ["open"],
        "high": ["high"],
        "low": ["low"],
        "close": ["close"],
        "volume": ["volume", "vol"],
    }

    for dst, keys in patterns.items():
        if dst in out.columns:
            continue
        for c in list(out.columns):
            cl = str(c).lower()
            if cl == dst:
                out[dst] = out[c]
                break
            if any(k in cl for k in keys):
                if dst == "close" and "adj" in cl:
                    continue
                out[dst] = out[c]
                break

    if "close" not in out.columns and "adj_close" in out.columns:
        out["close"] = out["adj_close"]

    if "price" not in out.columns and "close" in out.columns:
        out["price"] = out["close"]

    return out


def _normalize_numeric_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    out = df.copy()

    for col in ["open", "high", "low", "close", "volume", "price", "adj_close"]:
        if col not in out.columns:
            continue
        try:
            s = out[col]
            if isinstance(s, pd.DataFrame):
                s = s.iloc[:, 0]
            out[col] = pd.to_numeric(s, errors="coerce")
        except Exception:
            out[col] = pd.to_numeric(out[col], errors="coerce")

    return out


def _finalize_normalized_yahoo_df(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    out = df.copy()

    if "datetime" not in out.columns:
        logger.warning("[YAHOO NORMALIZER FALLBACK] missing datetime")
        return pd.DataFrame()

    out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
    out = out.dropna(subset=["datetime"])
    if out.empty:
        logger.warning("[YAHOO NORMALIZER FALLBACK] all datetime invalid")
        return pd.DataFrame()

    try:
        if getattr(out["datetime"].dt, "tz", None) is not None:
            out["datetime"] = out["datetime"].dt.tz_convert(None)
    except Exception:
        try:
            out["datetime"] = out["datetime"].dt.tz_localize(None)
        except Exception:
            pass

    if "symbol" not in out.columns:
        logger.warning("[YAHOO NORMALIZER FALLBACK] missing symbol")
        return pd.DataFrame()

    out["symbol"] = _coerce_symbol_series(out["symbol"])
    out = out.dropna(subset=["symbol"])
    if out.empty:
        logger.warning("[YAHOO NORMALIZER FALLBACK] all symbol invalid")
        return pd.DataFrame()

    required_ohlc = ["open", "high", "low", "close"]
    missing = [c for c in required_ohlc if c not in out.columns]
    if missing:
        logger.warning(
            "[YAHOO NORMALIZER FALLBACK] missing OHLC columns missing=%s cols=%s",
            missing,
            list(out.columns),
        )
        return pd.DataFrame()

    out = _normalize_numeric_columns(out)
    out = out.dropna(subset=["open", "high", "low", "close"])
    if out.empty:
        logger.warning("[YAHOO NORMALIZER FALLBACK] OHLC all invalid")
        return pd.DataFrame()

    if "volume" not in out.columns:
        out["volume"] = pd.NA

    out["price"] = pd.to_numeric(out["close"], errors="coerce")

    if "symbolname" not in out.columns:
        out["symbolname"] = ""

    out["symbolname"] = out["symbolname"].fillna("").astype(str)
    out["source"] = "yahoo_pipeline"

    out = out.drop_duplicates(subset=["symbol", "datetime"], keep="last")
    out = out.sort_values(["symbol", "datetime"]).reset_index(drop=True)

    keep_cols = [c for c in [
        "symbol", "symbolname", "datetime",
        "open", "high", "low", "close", "volume", "price", "source",
    ] if c in out.columns]

    out = out[keep_cols].copy()

    logger.info(
        "[YAHOO NORMALIZER FALLBACK] normalized rows=%d symbols=%d range=%s -> %s cols=%s",
        len(out),
        out["symbol"].nunique() if "symbol" in out.columns and not out.empty else 0,
        out["datetime"].min() if "datetime" in out.columns and not out.empty else None,
        out["datetime"].max() if "datetime" in out.columns and not out.empty else None,
        list(out.columns),
    )

    return out


def _fallback_normalize_yahoo_df(df: pd.DataFrame) -> pd.DataFrame:
    try:
        out = _safe_to_frame(df)
        if out.empty:
            return pd.DataFrame()

        out = _flatten_columns(out)
        if out.empty:
            return pd.DataFrame()

        out = _extract_datetime_column(out)
        if out.empty or "datetime" not in out.columns:
            logger.warning("[YAHOO NORMALIZER FALLBACK] datetime extraction failed")
            return pd.DataFrame()

        out = _extract_symbol_column(out)
        if out.empty:
            return pd.DataFrame()

        out = _flatten_columns(out)
        out = _rename_yahoo_columns(out)
        out = _finalize_normalized_yahoo_df(out)
        return out

    except Exception:
        logger.exception("[YAHOO NORMALIZER FALLBACK] fatal error")
        return pd.DataFrame()


def _resolve_normalize_yahoo_df() -> Callable[[pd.DataFrame], pd.DataFrame]:
    candidates = [
        ("trading.yahoo.normalize.yahoo_dataframe_normalizer", "normalize_yahoo_df"),
        ("trading.yahoo.yahoo_dataframe_normalizer", "normalize_yahoo_df"),
        ("trading.yahoo.normalize.yahoo_normalizer", "normalize_yahoo_df"),
        ("trading.yahoo.yahoo_normalizer", "normalize_yahoo_df"),
    ]

    for module_name, func_name in candidates:
        try:
            module = __import__(module_name, fromlist=[func_name])
            func = getattr(module, func_name, None)
            if callable(func):
                logger.info(
                    "[YAHOO COMPLEMENT] normalizer resolved from %s.%s",
                    module_name,
                    func_name,
                )
                return func
        except Exception:
            continue

    logger.warning("[YAHOO COMPLEMENT] normalize_yahoo_df resolver failed -> using fallback")
    return _fallback_normalize_yahoo_df


def normalize_yahoo_df(df: pd.DataFrame) -> pd.DataFrame:
    """
    最優先:
      loader.py が返す tidy 形式
      symbol,time,open_price,high_price,low_price,close_price,volume
    をそのまま安全変換する。
    それ以外だけ resolver/fallback に回す。
    """
    try:
        if isinstance(df, pd.DataFrame) and not df.empty:
            cols = set(df.columns)
            tidy_cols = {
                "symbol", "time", "open_price", "high_price", "low_price", "close_price",
            }

            if tidy_cols.issubset(cols):
                out = df.copy()

                out["symbol"] = (
                    out["symbol"].astype(str).str.strip()
                    .str.replace(r"\.0$", "", regex=True)
                    .str.replace(r"\.T$", "", regex=True)
                )
                out["datetime"] = pd.to_datetime(out["time"], errors="coerce")

                rename_map = {
                    "open_price": "open",
                    "high_price": "high",
                    "low_price": "low",
                    "close_price": "close",
                }
                for src, dst in rename_map.items():
                    out[dst] = pd.to_numeric(out[src], errors="coerce")

                if "volume" in out.columns:
                    out["volume"] = pd.to_numeric(out["volume"], errors="coerce")
                else:
                    out["volume"] = pd.NA

                if "symbolname" not in out.columns:
                    out["symbolname"] = ""

                out["symbolname"] = out["symbolname"].fillna("").astype(str)
                out["price"] = pd.to_numeric(out["close"], errors="coerce")
                out["source"] = "yahoo_pipeline"

                out = out.dropna(subset=["symbol", "datetime", "open", "high", "low", "close"])
                if out.empty:
                    logger.warning("[YAHOO COMPLEMENT] tidy normalized result empty after OHLC validation")
                    return pd.DataFrame()

                zero_mask = (
                    out[["open", "high", "low", "close"]].fillna(0).eq(0).all(axis=1)
                    & out["volume"].fillna(0).eq(0)
                )
                if int(zero_mask.sum()) > 0:
                    logger.warning(
                        "[YAHOO COMPLEMENT] tidy rows dropped by zero OHLCV guard=%d/%d",
                        int(zero_mask.sum()),
                        len(out),
                    )
                    out = out.loc[~zero_mask].copy()

                out = out.drop_duplicates(subset=["symbol", "datetime"], keep="last")
                out = out.sort_values(["symbol", "datetime"]).reset_index(drop=True)

                if out.empty:
                    logger.warning("[YAHOO COMPLEMENT] tidy normalized result empty after dedupe")
                    return pd.DataFrame()

                try:
                    logger.info(
                        "[YAHOO COMPLEMENT] tidy normalized rows=%d symbols=%d range=%s -> %s",
                        len(out),
                        out["symbol"].nunique(),
                        out["datetime"].min(),
                        out["datetime"].max(),
                    )
                except Exception:
                    pass

                keep_cols = [c for c in [
                    "symbol", "symbolname", "datetime",
                    "open", "high", "low", "close", "volume", "price", "source",
                ] if c in out.columns]
                return out[keep_cols].copy()

        fn = _resolve_normalize_yahoo_df()
        out = fn(df)

        if out is None or not isinstance(out, pd.DataFrame) or out.empty:
            logger.warning("[YAHOO COMPLEMENT] normalized result empty")
            return pd.DataFrame()

        must_cols = {"symbol", "datetime", "open", "high", "low", "close"}
        if not must_cols.issubset(set(out.columns)):
            logger.warning(
                "[YAHOO COMPLEMENT] normalized result missing columns missing=%s cols=%s",
                sorted(list(must_cols - set(out.columns))),
                list(out.columns),
            )
            return pd.DataFrame()

        for c in ["open", "high", "low", "close", "volume"]:
            if c in out.columns:
                out[c] = pd.to_numeric(out[c], errors="coerce")

        bad_mask = out[["open", "high", "low", "close"]].isna().all(axis=1)
        if bad_mask.any():
            logger.warning(
                "[YAHOO COMPLEMENT] dropping rows with all-NaN OHLC count=%d",
                int(bad_mask.sum()),
            )
            out = out.loc[~bad_mask].copy()

        if out.empty:
            return pd.DataFrame()

        out = out.drop_duplicates(subset=["symbol", "datetime"], keep="last")
        out = out.sort_values(["symbol", "datetime"]).reset_index(drop=True)

        return out

    except Exception:
        logger.exception("[YAHOO COMPLEMENT] normalize wrapper failed")
        return pd.DataFrame()


__all__ = ["normalize_yahoo_df"]