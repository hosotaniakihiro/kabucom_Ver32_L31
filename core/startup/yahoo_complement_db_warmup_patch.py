from __future__ import annotations

import datetime as dt
import logging
import os
import threading
from pathlib import Path
from typing import Any, Optional

import pandas as pd

logger = logging.getLogger(__name__)
_DONE = False
_INSTALL_LOCK = threading.Lock()


def _env_bool(name: str, default: bool = False) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
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


def _safe_ts(v: Any) -> Optional[pd.Timestamp]:
    try:
        ts = pd.to_datetime(v, errors="coerce")
        if pd.isna(ts):
            return None
        ts = pd.Timestamp(ts)
        try:
            ts = ts.tz_localize(None)
        except Exception:
            try:
                ts = ts.tz_convert(None)
            except Exception:
                pass
        return ts
    except Exception:
        return None


def _safe_date_list(date_yyyymmdd: Optional[str], *, now: Optional[dt.datetime], lookback_days: int) -> list[str]:
    base: dt.datetime
    try:
        if date_yyyymmdd:
            base = dt.datetime.strptime(str(date_yyyymmdd), "%Y%m%d")
        else:
            base = now or dt.datetime.now()
    except Exception:
        base = now or dt.datetime.now()

    days = max(0, min(int(lookback_days), 14))
    out: list[str] = []
    for i in range(days + 1):
        d = (base - dt.timedelta(days=i)).strftime("%Y%m%d")
        if d not in out:
            out.append(d)
    return out


def _normalize_for_concat(df: pd.DataFrame) -> pd.DataFrame:
    try:
        if df is None or df.empty:
            return pd.DataFrame()
        out = df.copy()
        if "datetime" not in out.columns:
            for c in ("timestamp", "dt", "date_time", "created_at"):
                if c in out.columns:
                    out["datetime"] = out[c]
                    break
        if "symbol" not in out.columns:
            for c in ("code", "Symbol", "銘柄コード"):
                if c in out.columns:
                    out["symbol"] = out[c]
                    break
        if "close" not in out.columns:
            for c in ("close_price", "current_price", "price", "終値"):
                if c in out.columns:
                    out["close"] = out[c]
                    break
        if "open" not in out.columns:
            for c in ("open_price", "始値"):
                if c in out.columns:
                    out["open"] = out[c]
                    break
        if "high" not in out.columns:
            for c in ("high_price", "高値"):
                if c in out.columns:
                    out["high"] = out[c]
                    break
        if "low" not in out.columns:
            for c in ("low_price", "安値"):
                if c in out.columns:
                    out["low"] = out[c]
                    break
        if "volume" not in out.columns:
            for c in ("vol", "出来高"):
                if c in out.columns:
                    out["volume"] = out[c]
                    break
        if "symbol" in out.columns:
            out["symbol"] = out["symbol"].astype(str).str.strip()
        if "datetime" in out.columns:
            out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
            try:
                out["datetime"] = out["datetime"].dt.tz_localize(None)
            except Exception:
                pass
        return out.dropna(subset=["symbol", "datetime"], how="any") if {"symbol", "datetime"}.issubset(out.columns) else pd.DataFrame()
    except Exception:
        logger.debug("[YAHOO DB WARMUP] normalize failed", exc_info=True)
        return pd.DataFrame()


def _query_one_db(db_path: str, *, symbols: list[str], start_ts: pd.Timestamp, end_ts: pd.Timestamp) -> pd.DataFrame:
    if not db_path or not Path(db_path).exists() or not symbols:
        return pd.DataFrame()
    try:
        from trading.yahoo.pipeline.complement.db import connect_sqlite, table_exists
        from trading.yahoo.pipeline.complement.constants import summary_table_for_interval

        table = summary_table_for_interval(1)
        chunks: list[pd.DataFrame] = []
        with connect_sqlite(db_path, timeout=3.0) as con:
            if not table_exists(con, table):
                return pd.DataFrame()
            for i in range(0, len(symbols), 300):
                part = symbols[i : i + 300]
                ph = ",".join(["?"] * len(part))
                sql = (
                    f"SELECT * FROM {table} "
                    f"WHERE symbol IN ({ph}) AND datetime >= ? AND datetime <= ? "
                    f"ORDER BY symbol, datetime"
                )
                params = list(part) + [str(start_ts), str(end_ts)]
                try:
                    chunk = pd.read_sql_query(sql, con, params=params)
                    if chunk is not None and not chunk.empty:
                        chunks.append(chunk)
                except Exception:
                    logger.debug("[YAHOO DB WARMUP] read_sql failed db=%s", db_path, exc_info=True)
        return pd.concat(chunks, ignore_index=True, sort=False) if chunks else pd.DataFrame()
    except Exception:
        logger.debug("[YAHOO DB WARMUP] query db failed path=%s", db_path, exc_info=True)
        return pd.DataFrame()


def _fetch_db_warmup(raw: pd.DataFrame, *, base_dir: Optional[str], summary_db_path: Optional[str], date_yyyymmdd: Optional[str], now: Optional[dt.datetime]) -> pd.DataFrame:
    try:
        norm = _normalize_for_concat(raw)
        if norm.empty or "symbol" not in norm.columns or "datetime" not in norm.columns:
            return pd.DataFrame()
        symbols = sorted([s for s in norm["symbol"].dropna().astype(str).unique().tolist() if s])
        if not symbols:
            return pd.DataFrame()

        min_ts = _safe_ts(norm["datetime"].min())
        max_ts = _safe_ts(norm["datetime"].max())
        if min_ts is None or max_ts is None:
            return pd.DataFrame()

        min_bars = max(20, _env_int("YAHOO_COMPLEMENT_DB_WARMUP_MIN_BARS", 75))
        lookback_days = max(1, _env_int("YAHOO_COMPLEMENT_DB_WARMUP_LOOKBACK_DAYS", 7))
        # 1分足換算で十分な余裕を持って前日DBまで拾う。
        start_ts = min_ts - pd.Timedelta(days=lookback_days)
        end_ts = max_ts

        from trading.yahoo.pipeline.complement.db import get_summary_db_path

        paths: list[str] = []
        if summary_db_path:
            paths.append(str(summary_db_path))
        for d in _safe_date_list(date_yyyymmdd, now=now, lookback_days=lookback_days):
            p = get_summary_db_path(date_yyyymmdd=d, base_dir=base_dir)
            if p not in paths:
                paths.append(p)

        frames = [_query_one_db(p, symbols=symbols, start_ts=start_ts, end_ts=end_ts) for p in paths]
        frames = [f for f in frames if isinstance(f, pd.DataFrame) and not f.empty]
        if not frames:
            logger.info(
                "[YAHOO DB WARMUP] no db warmup rows symbols=%s raw_min=%s raw_max=%s paths=%s",
                len(symbols), min_ts, max_ts, len(paths),
            )
            return pd.DataFrame()

        warm = _normalize_for_concat(pd.concat(frames, ignore_index=True, sort=False))
        if warm.empty:
            return pd.DataFrame()

        # symbolごとに直近N本だけ残す。raw側と結合するので過剰読込を抑える。
        warm = warm.sort_values(["symbol", "datetime"], kind="stable")
        warm = warm.groupby("symbol", group_keys=False).tail(min_bars).reset_index(drop=True)
        logger.warning(
            "[YAHOO DB WARMUP] loaded rows=%s symbols=%s min=%s max=%s bars=%s paths=%s",
            len(warm), warm["symbol"].nunique(), warm["datetime"].min(), warm["datetime"].max(), min_bars, len(paths),
        )
        return warm
    except Exception:
        logger.exception("[YAHOO DB WARMUP] fetch failed")
        return pd.DataFrame()


def _merge_db_warmup(df_yahoo: pd.DataFrame, *, base_dir: Optional[str], summary_db_path: Optional[str], date_yyyymmdd: Optional[str], now: Optional[dt.datetime]) -> pd.DataFrame:
    try:
        if not _env_bool("YAHOO_COMPLEMENT_DB_WARMUP_ENABLED", True):
            return df_yahoo
        raw = _normalize_for_concat(df_yahoo)
        if raw.empty:
            return df_yahoo
        warm = _fetch_db_warmup(raw, base_dir=base_dir, summary_db_path=summary_db_path, date_yyyymmdd=date_yyyymmdd, now=now)
        if warm.empty:
            return df_yahoo
        merged = pd.concat([warm, raw], ignore_index=True, sort=False)
        merged["symbol"] = merged["symbol"].astype(str).str.strip()
        merged["datetime"] = pd.to_datetime(merged["datetime"], errors="coerce")
        merged = merged.dropna(subset=["symbol", "datetime"])
        # rawを後勝ちにして、DB由来の古い行で今回取得行を潰さない。
        merged = merged.drop_duplicates(subset=["symbol", "datetime"], keep="last")
        merged = merged.sort_values(["symbol", "datetime"], kind="stable").reset_index(drop=True)
        logger.warning(
            "[YAHOO DB WARMUP] merged raw_rows=%s warm_rows=%s merged_rows=%s symbols=%s min=%s max=%s",
            len(raw), len(warm), len(merged), merged["symbol"].nunique(), merged["datetime"].min(), merged["datetime"].max(),
        )
        return merged
    except Exception:
        logger.exception("[YAHOO DB WARMUP] merge failed")
        return df_yahoo


def _patch_once() -> bool:
    try:
        if not _env_bool("YAHOO_COMPLEMENT_DB_WARMUP_ENABLED", True):
            logger.warning("[YAHOO DB WARMUP] disabled by env")
            return False
        import trading.yahoo.pipeline.complement.runner as runner

        cur = getattr(runner, "run_yahoo_complement_pipeline", None)
        if not callable(cur):
            return False
        if getattr(cur, "_yahoo_db_warmup_v1", False):
            return True
        orig_pipeline = getattr(cur, "_original", cur)
        orig_once = getattr(getattr(runner, "run_yahoo_complement_once", None), "_original", getattr(runner, "run_yahoo_complement_once", None))

        def patched_pipeline(df_yahoo: pd.DataFrame, *args, **kwargs):
            merged = _merge_db_warmup(
                df_yahoo,
                base_dir=kwargs.get("base_dir"),
                summary_db_path=kwargs.get("summary_db_path"),
                date_yyyymmdd=kwargs.get("date_yyyymmdd"),
                now=kwargs.get("now"),
            )
            return orig_pipeline(merged, *args, **kwargs)

        patched_pipeline._yahoo_db_warmup_v1 = True  # type: ignore[attr-defined]
        patched_pipeline._original = orig_pipeline  # type: ignore[attr-defined]
        runner.run_yahoo_complement_pipeline = patched_pipeline

        if callable(orig_once):
            def patched_once(df_yahoo: pd.DataFrame, *args, **kwargs):
                merged = _merge_db_warmup(
                    df_yahoo,
                    base_dir=kwargs.get("base_dir"),
                    summary_db_path=kwargs.get("summary_db_path"),
                    date_yyyymmdd=kwargs.get("date_yyyymmdd"),
                    now=None,
                )
                return orig_once(merged, *args, **kwargs)

            patched_once._yahoo_db_warmup_v1 = True  # type: ignore[attr-defined]
            patched_once._original = orig_once  # type: ignore[attr-defined]
            runner.run_yahoo_complement_once = patched_once

        logger.warning("[YAHOO DB WARMUP] patched runner complement pipeline v1")
        return True
    except Exception:
        logger.exception("[YAHOO DB WARMUP] patch failed")
        return False


def install() -> bool:
    global _DONE
    with _INSTALL_LOCK:
        if _DONE:
            return _patch_once()
        ok = _patch_once()
        _DONE = True
        logger.warning("[YAHOO DB WARMUP] installed v1 ok=%s", ok)
        return bool(ok)


try:
    install()
except Exception:
    logger.exception("[YAHOO DB WARMUP] auto install failed")

__all__ = ["install"]
