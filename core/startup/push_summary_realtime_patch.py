# ============================================================
# File   : core/startup/push_summary_realtime_patch.py
# Version: REV1-PUSH-SUMMARY-REALTIME-DB-FALLBACK
# ------------------------------------------------------------
# PURPOSE
#   PUSH DB は増えているのに global_data 側の push_df が空/古い場合、
#   push_summary_engine が古い merged summary だけを再利用して stale になる。
#
# FIX
#   1) push_summary_engine の PUSH入力に pushYYYYMMDD.db の直近行を補完する。
#   2) PUSH DB flush 成功直後、1分足 summary を非同期で軽く再計算する。
#
# SAFE
#   - 既存関数は monkey patch で包むだけ。
#   - DB読み込みは read-only / 短時間 / limit付き。
#   - flush のロック内では再計算せず、daemon thread で実行する。
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Optional

import pandas as pd

logger = logging.getLogger(__name__)

_PATCHED = False
_ORIGINAL_RESOLVE_PUSH_SOURCE_DF = None
_ORIGINAL_STREAM_WRITER_FLUSH = None
_TRIGGER_LOCK = threading.RLock()
_TRIGGER_RUNNING = False
_LAST_TRIGGER_AT = 0.0


def _env_bool(name: str, default: bool) -> bool:
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
        return int(float(str(v).strip()))
    except Exception:
        return int(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).strip())
    except Exception:
        return float(default)


def _parse_intervals(value: str | None, default: Iterable[int] = (1,)) -> list[int]:
    if not value or not str(value).strip():
        return [int(x) for x in default]
    out: list[int] = []
    for part in str(value).replace(";", ",").split(","):
        s = part.strip().lower().replace("min", "").replace("m", "")
        if not s:
            continue
        try:
            n = int(float(s))
            if n > 0 and n not in out:
                out.append(n)
        except Exception:
            pass
    return out or [int(x) for x in default]


def _safe_latest_dt(df: Any) -> Optional[pd.Timestamp]:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty:
            return None
        for col in ("datetime", "received_at", "current_price_time", "time"):
            if col not in df.columns:
                continue
            s = pd.to_datetime(df[col], errors="coerce")
            if s.notna().any():
                ts = s.max()
                try:
                    ts = ts.tz_localize(None)
                except Exception:
                    pass
                return ts
    except Exception:
        pass
    return None


def _safe_symbol_count(df: Any) -> int:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty or "symbol" not in df.columns:
            return 0
        return int(df["symbol"].astype(str).nunique())
    except Exception:
        return 0


def _resolve_push_db_path() -> Optional[Path]:
    try:
        from config.paths import get_path

        base = Path(get_path("raw_push"))
        return base / f"push{dt.datetime.now().strftime('%Y%m%d')}.db"
    except Exception:
        logger.debug("[PUSH SUMMARY REALTIME] get_path(raw_push) failed", exc_info=True)
        return None


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    try:
        cur = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1", (table,))
        return cur.fetchone() is not None
    except Exception:
        return False


def _read_recent_push_db_rows(*, lookback_sec: int, limit: int) -> pd.DataFrame:
    db_path = _resolve_push_db_path()
    if db_path is None or not db_path.exists():
        return pd.DataFrame()

    cutoff = (dt.datetime.now() - dt.timedelta(seconds=max(60, int(lookback_sec)))).isoformat(sep=" ")

    try:
        conn = sqlite3.connect(str(db_path), timeout=3.0)
        try:
            conn.execute("PRAGMA query_only=ON;")
            conn.execute("PRAGMA busy_timeout=3000;")

            table = "stream_data_raw" if _table_exists(conn, "stream_data_raw") else "stream_data"
            if not _table_exists(conn, table):
                return pd.DataFrame()

            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
            select_cols = [
                c for c in (
                    "symbol", "symbolname", "datetime", "date", "time", "price", "volume",
                    "trading_value", "vwap", "previousclose", "opening_price", "high_price",
                    "low_price", "received_at",
                ) if c in cols
            ]
            if not {"symbol", "datetime", "price"}.issubset(set(select_cols)):
                logger.warning(
                    "[PUSH SUMMARY REALTIME] db fallback skipped required cols missing table=%s cols=%s",
                    table,
                    cols,
                )
                return pd.DataFrame()

            time_filter_col = "received_at" if "received_at" in cols else "datetime"
            sql = f"""
                SELECT {', '.join(select_cols)}
                FROM {table}
                WHERE {time_filter_col} >= ? OR datetime >= ?
                ORDER BY datetime DESC
                LIMIT ?
            """
            df = pd.read_sql_query(sql, conn, params=(cutoff, cutoff, int(limit)))
        finally:
            conn.close()
    except Exception:
        logger.debug("[PUSH SUMMARY REALTIME] db fallback read failed path=%s", db_path, exc_info=True)
        return pd.DataFrame()

    if df.empty:
        return df

    try:
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
        try:
            df["datetime"] = df["datetime"].dt.tz_localize(None)
        except Exception:
            pass
        if "received_at" in df.columns:
            df["received_at"] = pd.to_datetime(df["received_at"], errors="coerce")
            try:
                df["received_at"] = df["received_at"].dt.tz_localize(None)
            except Exception:
                pass

        df["symbol"] = df["symbol"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
        df = df[df["symbol"] != ""].copy()

        # push_summary_engine の normalizer が読む標準列へ寄せる。
        df["current_price"] = pd.to_numeric(df.get("price"), errors="coerce")
        df["close"] = df["current_price"]
        df["open"] = pd.to_numeric(df.get("opening_price"), errors="coerce").combine_first(df["close"])
        df["high"] = pd.to_numeric(df.get("high_price"), errors="coerce").combine_first(df["close"])
        df["low"] = pd.to_numeric(df.get("low_price"), errors="coerce").combine_first(df["close"])
        df["volume"] = pd.to_numeric(df.get("volume"), errors="coerce")
        if "symbolname" not in df.columns:
            df["symbolname"] = df["symbol"]
        df["source"] = "push_db_realtime_fallback"
        df = df.dropna(subset=["symbol", "datetime", "close"]).copy()
    except Exception:
        logger.debug("[PUSH SUMMARY REALTIME] db fallback normalize failed", exc_info=True)
        return pd.DataFrame()

    logger.info(
        "[PUSH SUMMARY REALTIME] db fallback rows=%s symbols=%s latest_dt=%s lookback=%s limit=%s path=%s",
        len(df),
        _safe_symbol_count(df),
        _safe_latest_dt(df),
        lookback_sec,
        limit,
        db_path,
    )
    return df.reset_index(drop=True)


def _merge_push_sources(primary: pd.DataFrame, fallback: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(primary, pd.DataFrame) or primary.empty:
        return fallback.copy() if isinstance(fallback, pd.DataFrame) else pd.DataFrame()
    if not isinstance(fallback, pd.DataFrame) or fallback.empty:
        return primary.copy()

    try:
        out = pd.concat([primary.copy(), fallback.copy()], ignore_index=True, sort=False)
        if "datetime" in out.columns:
            out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
        if "symbol" in out.columns and "datetime" in out.columns:
            out = out.sort_values(["symbol", "datetime"], kind="stable")
            out = out.drop_duplicates(subset=["symbol", "datetime"], keep="last")
        return out.reset_index(drop=True)
    except Exception:
        logger.debug("[PUSH SUMMARY REALTIME] merge fallback failed", exc_info=True)
        return primary.copy()


def _patch_push_summary_engine() -> bool:
    global _ORIGINAL_RESOLVE_PUSH_SOURCE_DF
    try:
        import trading.summary.engine.push_summary_engine as eng

        original = getattr(eng, "_resolve_push_source_df", None)
        if not callable(original):
            logger.warning("[PUSH SUMMARY REALTIME] _resolve_push_source_df not found")
            return False
        if getattr(original, "_push_summary_realtime_patched", False):
            return True

        _ORIGINAL_RESOLVE_PUSH_SOURCE_DF = original

        def _resolve_push_source_df_with_db_fallback() -> pd.DataFrame:
            primary = original()
            if not _env_bool("PUSH_SUMMARY_DB_FALLBACK_ENABLED", True):
                return primary

            lookback = _env_int("PUSH_SUMMARY_DB_FALLBACK_LOOKBACK_SEC", 900)
            limit = _env_int("PUSH_SUMMARY_DB_FALLBACK_LIMIT", 5000)
            fallback = _read_recent_push_db_rows(lookback_sec=lookback, limit=limit)
            if fallback.empty:
                return primary

            p_latest = _safe_latest_dt(primary)
            f_latest = _safe_latest_dt(fallback)
            use_fallback = (
                not isinstance(primary, pd.DataFrame)
                or primary.empty
                or p_latest is None
                or (f_latest is not None and p_latest is not None and f_latest > p_latest)
            )
            if not use_fallback:
                return primary

            merged = _merge_push_sources(primary, fallback)
            logger.warning(
                "[PUSH SUMMARY REALTIME] push source supplemented from db primary_rows=%s primary_latest=%s fallback_rows=%s fallback_latest=%s merged_rows=%s merged_latest=%s",
                len(primary) if isinstance(primary, pd.DataFrame) else 0,
                p_latest,
                len(fallback),
                f_latest,
                len(merged),
                _safe_latest_dt(merged),
            )
            return merged

        _resolve_push_source_df_with_db_fallback._push_summary_realtime_patched = True  # type: ignore[attr-defined]
        setattr(eng, "_resolve_push_source_df", _resolve_push_source_df_with_db_fallback)
        return True
    except Exception:
        logger.exception("[PUSH SUMMARY REALTIME] patch push_summary_engine failed")
        return False


def _summary_rebuild_worker(intervals: list[int], reason: str) -> None:
    global _TRIGGER_RUNNING, _LAST_TRIGGER_AT
    try:
        import trading.summary.engine.push_summary_engine as eng

        for interval in intervals:
            try:
                logger.warning("[PUSH SUMMARY REALTIME] rebuild start interval=%s reason=%s", interval, reason)
                df = eng.build_summary(interval=int(interval))
                logger.warning(
                    "[PUSH SUMMARY REALTIME] rebuild done interval=%s rows=%s symbols=%s latest_dt=%s reason=%s",
                    interval,
                    len(df) if isinstance(df, pd.DataFrame) else 0,
                    _safe_symbol_count(df),
                    _safe_latest_dt(df),
                    reason,
                )
            except Exception:
                logger.exception("[PUSH SUMMARY REALTIME] rebuild failed interval=%s reason=%s", interval, reason)
    finally:
        with _TRIGGER_LOCK:
            _TRIGGER_RUNNING = False
            _LAST_TRIGGER_AT = time.time()


def _trigger_summary_rebuild(reason: str) -> None:
    global _TRIGGER_RUNNING, _LAST_TRIGGER_AT
    if not _env_bool("PUSH_SUMMARY_REALTIME_REBUILD_ENABLED", True):
        return

    cooldown = _env_float("PUSH_SUMMARY_REALTIME_COOLDOWN_SEC", 20.0)
    now = time.time()
    with _TRIGGER_LOCK:
        if _TRIGGER_RUNNING:
            logger.debug("[PUSH SUMMARY REALTIME] rebuild skipped already running reason=%s", reason)
            return
        if now - float(_LAST_TRIGGER_AT or 0.0) < cooldown:
            logger.debug("[PUSH SUMMARY REALTIME] rebuild skipped cooldown reason=%s", reason)
            return
        _TRIGGER_RUNNING = True

    intervals = _parse_intervals(os.getenv("PUSH_SUMMARY_REALTIME_INTERVALS"), default=(1,))
    th = threading.Thread(
        target=_summary_rebuild_worker,
        args=(intervals, reason),
        daemon=True,
        name="PushSummaryRealtimeRebuild",
    )
    th.start()


def _patch_push_db_writer() -> bool:
    global _ORIGINAL_STREAM_WRITER_FLUSH
    try:
        import trading.push.push_db_writer as writer_mod

        cls = getattr(writer_mod, "StreamDBWriter", None)
        original = getattr(cls, "flush", None) if cls is not None else None
        if not callable(original):
            logger.warning("[PUSH SUMMARY REALTIME] StreamDBWriter.flush not found")
            return False
        if getattr(original, "_push_summary_realtime_patched", False):
            return True

        _ORIGINAL_STREAM_WRITER_FLUSH = original

        def _flush_with_realtime_summary(self, *args, **kwargs):
            ok = original(self, *args, **kwargs)
            try:
                if bool(ok):
                    delta = int(getattr(writer_mod.global_data, "last_flush_delta", 0) or 0)
                    rows = int(getattr(writer_mod.global_data, "last_flush_rows", 0) or 0)
                    if delta > 0 or rows > 0:
                        _trigger_summary_rebuild(reason=f"push_flush rows={rows} delta={delta}")
            except Exception:
                logger.debug("[PUSH SUMMARY REALTIME] flush post-trigger failed", exc_info=True)
            return ok

        _flush_with_realtime_summary._push_summary_realtime_patched = True  # type: ignore[attr-defined]
        setattr(cls, "flush", _flush_with_realtime_summary)
        return True
    except Exception:
        logger.exception("[PUSH SUMMARY REALTIME] patch push_db_writer failed")
        return False


def install() -> bool:
    global _PATCHED
    if _PATCHED:
        return True
    if not _env_bool("PUSH_SUMMARY_REALTIME_PATCH_ENABLED", True):
        logger.warning("[PUSH SUMMARY REALTIME] disabled by env")
        return False

    ok_engine = _patch_push_summary_engine()
    ok_writer = _patch_push_db_writer()
    _PATCHED = bool(ok_engine or ok_writer)
    logger.warning(
        "[PUSH SUMMARY REALTIME] installed ok=%s engine_db_fallback=%s flush_trigger=%s intervals=%s",
        _PATCHED,
        ok_engine,
        ok_writer,
        _parse_intervals(os.getenv("PUSH_SUMMARY_REALTIME_INTERVALS"), default=(1,)),
    )
    return _PATCHED


__all__ = ["install"]
