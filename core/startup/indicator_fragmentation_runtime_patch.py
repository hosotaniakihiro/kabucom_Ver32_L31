# ============================================================
# File   : core/startup/indicator_fragmentation_runtime_patch.py
# Version: V2.1-INDICATOR-PREVDAY-WARMUP-MA12
# ------------------------------------------------------------
# 目的:
#   trading.summary.indicators.indicator_calculator で、昼休みなどに
#   3000銘柄超へ指標計算した際の PerformanceWarning を抑える。
#
#   さらに、寄り付き直後に当日分足だけでは ma25/ma75/RSI/MACD/ATR が
#   未成熟になる問題を避けるため、前営業日の summary DB から最後N本を
#   「計算用にだけ」一時連結する。
#
# V2.1:
#   - 1m/3m/5m summary 出力に ma12 / ma12_slope / MA12位置関係を追加。
#   - MA12は短期スキャル用の補助線。強制エントリー禁止ではなく、後段の
#     Summary-AI / Ranking / Tonosama がスコア補正に使える列として保存する。
#
# 方針:
#   - 前営業日行は __prevday_indicator_warmup=1 を付けてメモリ上で連結する。
#   - indicator_calculator の戻り値から warmup 行を必ず削除する。
#   - DB保存、Discord表示、ENTRY判定へ渡るのは当日入力行だけ。
#   - 現物/建玉/発注ロジックには触らない。
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import re
import sqlite3
import warnings
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Iterable

import pandas as pd

try:
    from pandas.errors import PerformanceWarning
except Exception:  # pragma: no cover
    PerformanceWarning = Warning  # type: ignore

logger = logging.getLogger(__name__)
VERSION = "V2.1-INDICATOR-PREVDAY-WARMUP-MA12"
_INSTALLED = False
_WARMUP_COL = "__prevday_indicator_warmup"
_TRUE = {"1", "true", "yes", "y", "on", "enable", "enabled"}


def _env_bool(name: str, default: bool) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in _TRUE
    except Exception:
        return bool(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


def _normalize_interval(interval: Any) -> str:
    try:
        s = str(interval or "1min").strip().lower()
        if s in {"1", "1m", "1min", "1minute"}:
            return "1min"
        if s in {"3", "3m", "3min", "3minute"}:
            return "3min"
        if s in {"5", "5m", "5min", "5minute"}:
            return "5min"
        return s
    except Exception:
        return "1min"


def _table_for_interval(interval: Any) -> str:
    s = _normalize_interval(interval)
    if s == "3min":
        return "stock_summary_3min"
    if s == "5min":
        return "stock_summary_5min"
    return "stock_summary_1min"


def _summary_base_dirs() -> list[Path]:
    vals = [
        os.getenv("SUMMARY_DB_DIR"),
        os.getenv("AUTO_STOCK_DB_DIR"),
        os.getenv("AUTO_STOCK_BUY_SELL_DIR"),
        os.getenv("AUTOSTOCK_DB_DIR"),
        r"\\192.168.0.22\AutoStockBuyAndSell",
    ]
    out: list[Path] = []
    seen: set[str] = set()
    for v in vals:
        if not v:
            continue
        try:
            p = Path(str(v))
            key = str(p).lower()
            if key not in seen:
                out.append(p)
                seen.add(key)
        except Exception:
            pass
    return out


def _target_date_yyyymmdd(df: pd.DataFrame) -> str:
    try:
        if "datetime" not in df.columns:
            return dt.date.today().strftime("%Y%m%d")
        s = pd.to_datetime(df["datetime"], errors="coerce").dropna()
        if s.empty:
            return dt.date.today().strftime("%Y%m%d")
        return s.max().date().strftime("%Y%m%d")
    except Exception:
        return dt.date.today().strftime("%Y%m%d")


def _find_prev_summary_db(target_yyyymmdd: str) -> str:
    try:
        explicit = os.getenv("PREVDAY_INDICATOR_WARMUP_DB_PATH", "").strip()
        if explicit and Path(explicit).exists():
            return explicit
    except Exception:
        pass

    best_date = ""
    best_path = ""
    pattern = re.compile(r"summary(\d{8})\.db$", re.IGNORECASE)
    for base in _summary_base_dirs():
        try:
            if not base.exists():
                continue
            for p in base.glob("summary*.db"):
                m = pattern.search(p.name)
                if not m:
                    continue
                ymd = m.group(1)
                if ymd < target_yyyymmdd and ymd > best_date:
                    best_date = ymd
                    best_path = str(p)
        except Exception:
            continue
    return best_path


def _symbol_col(df: pd.DataFrame) -> str | None:
    for c in ("symbol", "Symbol", "code", "Code", "stock_code", "銘柄コード"):
        if c in df.columns:
            return c
    return None


def _symbols_from_df(df: pd.DataFrame) -> tuple[str, ...]:
    try:
        c = _symbol_col(df)
        if not c:
            return tuple()
        syms = (
            df[c]
            .astype(str)
            .str.strip()
            .str.replace(r"\.0$", "", regex=True)
            .str.replace(r"\.T$", "", regex=True)
        )
        return tuple(sorted(x for x in syms.unique().tolist() if x and x.lower() != "nan"))
    except Exception:
        return tuple()


def _chunks(xs: Iterable[str], n: int) -> Iterable[list[str]]:
    buf: list[str] = []
    for x in xs:
        buf.append(x)
        if len(buf) >= n:
            yield buf
            buf = []
    if buf:
        yield buf


@lru_cache(maxsize=64)
def _load_prevday_warmup_cached(db_path: str, table: str, symbols: tuple[str, ...], rows_per_symbol: int) -> pd.DataFrame:
    if not db_path or not symbols:
        return pd.DataFrame()
    if rows_per_symbol <= 0:
        return pd.DataFrame()
    try:
        path = Path(db_path)
        if not path.exists():
            return pd.DataFrame()
    except Exception:
        return pd.DataFrame()

    frames: list[pd.DataFrame] = []
    try:
        with sqlite3.connect(str(db_path), timeout=2.0) as conn:
            conn.execute("PRAGMA busy_timeout=2000;")
            try:
                cols = [str(r[1]) for r in conn.execute(f'PRAGMA table_info("{table}")').fetchall()]
            except Exception:
                cols = []
            if not cols:
                return pd.DataFrame()
            sym_col = "symbol" if "symbol" in cols else ("Symbol" if "Symbol" in cols else None)
            dt_col = "datetime" if "datetime" in cols else ("time" if "time" in cols else None)
            if not sym_col or not dt_col:
                return pd.DataFrame()

            for part in _chunks(symbols, 300):
                placeholders = ",".join(["?"] * len(part))
                params: list[Any] = list(part) + [int(rows_per_symbol)]
                sql = f'''
                    SELECT * FROM (
                        SELECT *, ROW_NUMBER() OVER(PARTITION BY "{sym_col}" ORDER BY "{dt_col}" DESC) AS __rn
                        FROM "{table}"
                        WHERE CAST("{sym_col}" AS TEXT) IN ({placeholders})
                    )
                    WHERE __rn <= ?
                    ORDER BY "{sym_col}", "{dt_col}"
                '''
                try:
                    d = pd.read_sql_query(sql, conn, params=params)
                except Exception:
                    # Fallback for old SQLite without window functions.
                    limit = max(int(rows_per_symbol) * max(1, len(part)) * 3, int(rows_per_symbol))
                    sql2 = f'''
                        SELECT * FROM "{table}"
                        WHERE CAST("{sym_col}" AS TEXT) IN ({placeholders})
                        ORDER BY "{dt_col}" DESC
                        LIMIT ?
                    '''
                    d = pd.read_sql_query(sql2, conn, params=list(part) + [limit])
                    if not d.empty:
                        d[dt_col] = pd.to_datetime(d[dt_col], errors="coerce")
                        d = d.dropna(subset=[dt_col]).sort_values([sym_col, dt_col])
                        d = d.groupby(sym_col, as_index=False).tail(int(rows_per_symbol))
                if not d.empty:
                    frames.append(d)
    except Exception:
        logger.debug("[IND PREVDAY WARMUP] DB read failed db=%s table=%s", db_path, table, exc_info=True)
        return pd.DataFrame()

    if not frames:
        return pd.DataFrame()
    try:
        out = pd.concat(frames, ignore_index=True, sort=False)
        out = out.loc[:, ~pd.Index(out.columns).duplicated()].copy()
        if "__rn" in out.columns:
            out = out.drop(columns=["__rn"], errors="ignore")
        return out
    except Exception:
        return pd.DataFrame()


def _with_prevday_warmup(df: pd.DataFrame, *, interval: Any) -> pd.DataFrame:
    if not _env_bool("PREVDAY_INDICATOR_WARMUP_ENABLED", True):
        return df
    if not isinstance(df, pd.DataFrame) or df.empty:
        return df
    if _WARMUP_COL in df.columns:
        return df
    if "datetime" not in df.columns:
        return df
    symbols = _symbols_from_df(df)
    if not symbols:
        return df
    rows_per_symbol = max(0, _env_int("PREVDAY_INDICATOR_WARMUP_BARS", 120))
    if rows_per_symbol <= 0:
        return df
    target_ymd = _target_date_yyyymmdd(df)
    db_path = _find_prev_summary_db(target_ymd)
    if not db_path:
        return df
    table = _table_for_interval(interval)
    warm = _load_prevday_warmup_cached(db_path, table, symbols, rows_per_symbol)
    if warm.empty:
        return df
    try:
        cur = df.copy()
        cur[_WARMUP_COL] = False
        warm = warm.copy()
        warm[_WARMUP_COL] = True
        # Keep only warmup rows older than today's earliest input row per symbol/date.
        warm["datetime"] = pd.to_datetime(warm["datetime"], errors="coerce")
        cur["datetime"] = pd.to_datetime(cur["datetime"], errors="coerce")
        warm = warm.dropna(subset=["datetime"])
        cur = cur.dropna(subset=["datetime"])
        if warm.empty or cur.empty:
            return df
        out = pd.concat([warm, cur], ignore_index=True, sort=False)
        logger.warning(
            "[IND PREVDAY WARMUP] applied interval=%s db=%s table=%s symbols=%s warm_rows=%s current_rows=%s bars=%s save=0",
            _normalize_interval(interval),
            db_path,
            table,
            len(symbols),
            len(warm),
            len(cur),
            rows_per_symbol,
        )
        return out
    except Exception:
        logger.debug("[IND PREVDAY WARMUP] concat failed interval=%s", interval, exc_info=True)
        return df


def _drop_warmup_rows(out: Any) -> Any:
    try:
        if isinstance(out, pd.DataFrame) and _WARMUP_COL in out.columns:
            before = len(out)
            keep = ~out[_WARMUP_COL].fillna(False).astype(bool)
            out = out.loc[keep].copy()
            out = out.drop(columns=[_WARMUP_COL], errors="ignore")
            dropped = before - len(out)
            if dropped > 0:
                logger.warning("[IND PREVDAY WARMUP] dropped warmup rows after calculation dropped=%s output_rows=%s save=0", dropped, len(out))
            return out
    except Exception:
        logger.debug("[IND PREVDAY WARMUP] drop warmup rows failed", exc_info=True)
    return out


def _add_ma12_features(out: Any) -> Any:
    try:
        if not _env_bool("SUMMARY_MA12_FEATURES_ENABLED", True):
            return out
        if not isinstance(out, pd.DataFrame) or out.empty:
            return out
        sym_col = _symbol_col(out)
        if not sym_col:
            return out
        price_col = None
        for c in ("close", "close_price", "price", "current_price"):
            if c in out.columns:
                price_col = c
                break
        if price_col is None:
            return out

        x = out.copy()
        x["__ma12_order"] = range(len(x))
        if "datetime" in x.columns:
            x["__ma12_dt"] = pd.to_datetime(x["datetime"], errors="coerce")
            x = x.sort_values([sym_col, "__ma12_dt", "__ma12_order"], na_position="last")
        else:
            x = x.sort_values([sym_col, "__ma12_order"], na_position="last")

        price = pd.to_numeric(x[price_col], errors="coerce")
        ma12 = price.groupby(x[sym_col], sort=False).transform(lambda s: s.rolling(12, min_periods=1).mean())
        x["ma12"] = ma12
        x["ma12_slope"] = ma12.groupby(x[sym_col], sort=False).diff().fillna(0.0)
        denom = ma12.replace(0, pd.NA)
        x["ma12_slope_pct"] = (x["ma12_slope"] / denom).fillna(0.0)
        x["price_above_ma12"] = (price > ma12).astype(int)
        x["price_below_ma12"] = (price < ma12).astype(int)
        x["ma12_up"] = (x["ma12_slope"] > 0).astype(int)
        x["ma12_down"] = (x["ma12_slope"] < 0).astype(int)
        if "ma5" in x.columns:
            ma5 = pd.to_numeric(x["ma5"], errors="coerce")
            x["ma5_above_ma12"] = (ma5 > ma12).astype(int)
            x["ma5_below_ma12"] = (ma5 < ma12).astype(int)
            x["ma5_ma12_gap_pct"] = ((ma5 - ma12) / denom).fillna(0.0)
        if "ma25" in x.columns:
            ma25 = pd.to_numeric(x["ma25"], errors="coerce")
            x["ma12_above_ma25"] = (ma12 > ma25).astype(int)
            x["ma12_below_ma25"] = (ma12 < ma25).astype(int)
            x["ma12_ma25_gap_pct"] = ((ma12 - ma25) / ma25.replace(0, pd.NA)).fillna(0.0)
        x = x.sort_values("__ma12_order").drop(columns=["__ma12_order", "__ma12_dt"], errors="ignore")
        logger.warning("[MA12 SUMMARY] attached rows=%s symbols=%s cols=ma12,ma12_slope,ma12_slope_pct", len(x), x[sym_col].nunique())
        return x
    except Exception:
        logger.exception("[MA12 SUMMARY] attach failed")
        return out


def _wrap_indicator_func(fn: Callable[..., Any], *, name: str) -> Callable[..., Any]:
    if getattr(fn, "_indicator_fragmentation_runtime_patch", False):
        return fn

    def _wrapped(*args: Any, **kwargs: Any) -> Any:
        interval = kwargs.get("interval", "1min")
        args2 = args
        try:
            if args and isinstance(args[0], pd.DataFrame):
                first = _with_prevday_warmup(args[0], interval=interval)
                if first is not args[0]:
                    args2 = (first, *args[1:])
            elif isinstance(kwargs.get("df"), pd.DataFrame):
                kwargs = dict(kwargs)
                kwargs["df"] = _with_prevday_warmup(kwargs["df"], interval=interval)
        except Exception:
            logger.debug("[IND PREVDAY WARMUP] pre-wrap failed func=%s", name, exc_info=True)
            args2 = args

        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                category=PerformanceWarning,
                message=".*DataFrame is highly fragmented.*",
            )
            out = fn(*args2, **kwargs)
        out = _drop_warmup_rows(out)
        out = _add_ma12_features(out)
        try:
            if isinstance(out, pd.DataFrame) and not out.empty:
                return out.copy()
        except Exception:
            pass
        return out

    _wrapped.__name__ = getattr(fn, "__name__", name)
    _wrapped.__doc__ = getattr(fn, "__doc__", None)
    _wrapped._indicator_fragmentation_runtime_patch = True  # type: ignore[attr-defined]
    _wrapped._indicator_prevd_warmup_v2 = True  # type: ignore[attr-defined]
    _wrapped._indicator_ma12_features_v21 = True  # type: ignore[attr-defined]
    _wrapped._original = fn  # type: ignore[attr-defined]
    return _wrapped


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        os.environ.setdefault("PREVDAY_INDICATOR_WARMUP_ENABLED", "1")
        os.environ.setdefault("PREVDAY_INDICATOR_WARMUP_BARS", "120")
        os.environ.setdefault("SUMMARY_MA12_FEATURES_ENABLED", "1")
        import trading.summary.indicators.indicator_calculator as mod

        patched = []
        for name in ("add_all_indicators", "calculate_indicators", "add_indicators"):
            fn = getattr(mod, name, None)
            if callable(fn):
                setattr(mod, name, _wrap_indicator_func(fn, name=name))
                patched.append(name)

        warnings.filterwarnings(
            "ignore",
            category=PerformanceWarning,
            message=".*DataFrame is highly fragmented.*",
            module=r".*trading\.summary\.indicators\.indicator_calculator.*",
        )

        _INSTALLED = True
        logger.warning(
            "[IND FRAGMENTATION PATCH] installed version=%s patched=%s prevday_warmup=%s bars=%s ma12=%s save=0",
            VERSION,
            patched,
            _env_bool("PREVDAY_INDICATOR_WARMUP_ENABLED", True),
            _env_int("PREVDAY_INDICATOR_WARMUP_BARS", 120),
            _env_bool("SUMMARY_MA12_FEATURES_ENABLED", True),
        )
        return True
    except Exception:
        logger.exception("[IND FRAGMENTATION PATCH] install failed version=%s", VERSION)
        return False


try:
    install()
except Exception:
    logger.exception("[IND FRAGMENTATION PATCH] auto install failed")


__all__ = ["install"]
