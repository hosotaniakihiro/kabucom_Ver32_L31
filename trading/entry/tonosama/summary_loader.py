# ============================================================
# File   : trading/entry/tonosama/summary_loader.py
# Version: Ver1.6-TONOSAMA-SUMMARY-DB-FALLBACK
# ------------------------------------------------------------
# 目的:
#   殿様イナゴ用のサマリー読込。
#   main.py 側で summary_parent_tick を skip している運用では、global_data / 
#   global_context の completed summary が空になることがある。
#   その場合でも summaryYYYYMMDD.db の stock_summary_{1,3,5}min から
#   当日最新行を直接読み、Tonosama base feature empty を回避する。
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3

import pandas as pd

from global_state import global_data
from .utils import normalize_symbol, first_existing_col

logger = logging.getLogger(__name__)


def _safe_df(df) -> pd.DataFrame:
    if isinstance(df, pd.DataFrame):
        return df
    return pd.DataFrame()


def _env_bool(name: str, default: bool) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(str(v).strip()))
    except Exception:
        return int(default)


def _now_naive() -> dt.datetime:
    try:
        from scheduler_jobs.summary.time_utils import now_naive
        return now_naive().replace(tzinfo=None)
    except Exception:
        return dt.datetime.now()


def _latest_dt(df: pd.DataFrame):
    try:
        if isinstance(df, pd.DataFrame) and not df.empty and "datetime" in df.columns:
            return pd.to_datetime(df["datetime"], errors="coerce").max()
    except Exception:
        pass
    return None


def _latest_age_sec(df: pd.DataFrame) -> float | None:
    try:
        latest = _latest_dt(df)
        if latest is None or pd.isna(latest):
            return None
        latest_py = pd.Timestamp(latest).to_pydatetime().replace(tzinfo=None)
        return float((_now_naive() - latest_py).total_seconds())
    except Exception:
        return None


def _today_naive() -> dt.date:
    try:
        return _now_naive().date()
    except Exception:
        return dt.date.today()


def _today_yyyymmdd() -> str:
    return _today_naive().strftime("%Y%m%d")


def _reject_prev_day_history(df: pd.DataFrame, *, interval: int, via: str) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    if _env_bool("TONOSAMA_ALLOW_PREV_DAY_SUMMARY_HISTORY", False):
        return df
    latest = _latest_dt(df)
    if latest is None or pd.isna(latest):
        return df
    try:
        latest_date = pd.Timestamp(latest).to_pydatetime().date()
    except Exception:
        return df
    today = _today_naive()
    if latest_date != today:
        logger.warning(
            "[TONOSAMA ENTRY] reject stale summary fallback interval=%s rows=%s latest_dt=%s latest_date=%s today=%s via=%s allow_prev_day=%s",
            interval, len(df), latest, latest_date, today, via, _env_bool("TONOSAMA_ALLOW_PREV_DAY_SUMMARY_HISTORY", False),
        )
        return pd.DataFrame()
    return df


def _latest_slot_today(df: pd.DataFrame, *, interval: int, via: str) -> pd.DataFrame:
    if df is None or df.empty or "datetime" not in df.columns:
        return pd.DataFrame()
    try:
        x = df.copy()
        x["datetime"] = pd.to_datetime(x["datetime"], errors="coerce")
        x = x.dropna(subset=["datetime"])
        if x.empty:
            return pd.DataFrame()
        if not _env_bool("TONOSAMA_ALLOW_PREV_DAY_SUMMARY_HISTORY", False):
            today = _today_naive()
            x = x[x["datetime"].dt.date == today]
            if x.empty:
                return pd.DataFrame()
        latest = x["datetime"].max()
        out = x[x["datetime"] == latest].copy()
        out = _reject_prev_day_history(out, interval=interval, via=via)
        return out
    except Exception:
        logger.debug("[TONOSAMA ENTRY] latest slot extraction failed interval=%s via=%s", interval, via, exc_info=True)
        return pd.DataFrame()


def _call_global_context_method(name: str, interval: int, *, source: str = "push") -> pd.DataFrame:
    try:
        from core.global_context.context import global_context as GC
        fn = getattr(GC, name, None)
        if not callable(fn):
            return pd.DataFrame()
        try:
            return _safe_df(fn(int(interval), source=source))
        except TypeError:
            return _safe_df(fn(int(interval)))
    except Exception:
        logger.debug("[TONOSAMA ENTRY] global_context.%s failed interval=%s", name, interval, exc_info=True)
        return pd.DataFrame()


def _summary_db_path() -> str:
    base = os.getenv("AUTOSTOCK_SUMMARY_DB_DIR", r"\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\summary")
    return os.path.join(base, f"summary{_today_yyyymmdd()}.db")


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    try:
        row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        return row is not None
    except Exception:
        return False


def _load_summary_db_latest_slot(interval: int) -> pd.DataFrame:
    if not _env_bool("TONOSAMA_SUMMARY_DB_FALLBACK", True):
        return pd.DataFrame()
    interval = int(interval)
    table = f"stock_summary_{interval}min"
    db = _summary_db_path()
    if not os.path.exists(db):
        return pd.DataFrame()
    try:
        max_age = max(60.0, _env_float("TONOSAMA_SUMMARY_DB_FALLBACK_MAX_AGE_SEC", 600.0))
        max_rows = max(50, _env_int("TONOSAMA_SUMMARY_DB_FALLBACK_MAX_ROWS", 5000))
        today = _today_naive().isoformat()
        with sqlite3.connect(db, timeout=2.0) as conn:
            conn.row_factory = sqlite3.Row
            if not _table_exists(conn, table):
                logger.warning("[TONOSAMA ENTRY] summary db fallback table missing interval=%s table=%s db=%s", interval, table, db)
                return pd.DataFrame()
            cols = {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            dt_col = "datetime" if "datetime" in cols else ("time" if "time" in cols else None)
            if dt_col is None:
                return pd.DataFrame()
            sql_latest = f"SELECT MAX({dt_col}) FROM {table} WHERE date({dt_col})=?"
            latest = conn.execute(sql_latest, (today,)).fetchone()[0]
            if not latest:
                return pd.DataFrame()
            latest_ts = pd.to_datetime(latest, errors="coerce")
            if pd.isna(latest_ts):
                return pd.DataFrame()
            age = (_now_naive() - pd.Timestamp(latest_ts).to_pydatetime().replace(tzinfo=None)).total_seconds()
            if age > max_age:
                logger.warning("[TONOSAMA ENTRY] summary db fallback stale interval=%s latest=%s age=%.1fs max_age=%.1fs db=%s", interval, latest, age, max_age, db)
                return pd.DataFrame()
            # latest slotが薄い場合があるので少し遡る。ただし当日・max_rows内に制限。
            lookback_min = max(interval * 6, _env_int("TONOSAMA_SUMMARY_DB_FALLBACK_LOOKBACK_MIN", 30))
            start = (pd.Timestamp(latest_ts).to_pydatetime().replace(tzinfo=None) - dt.timedelta(minutes=lookback_min)).strftime("%Y-%m-%d %H:%M:%S")
            sql = f"SELECT * FROM {table} WHERE {dt_col}>=? AND date({dt_col})=? ORDER BY {dt_col} DESC LIMIT ?"
            rows = conn.execute(sql, (start, today, max_rows)).fetchall()
            if not rows:
                return pd.DataFrame()
            df = pd.DataFrame([dict(r) for r in rows])
            if dt_col != "datetime":
                df["datetime"] = df[dt_col]
            df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
            df = df.dropna(subset=["datetime"])
            if df.empty:
                return pd.DataFrame()
            logger.warning(
                "[TONOSAMA ENTRY] loaded summary db fallback interval=%s rows=%s symbols=%s latest=%s age=%.1fs db=%s table=%s",
                interval, len(df), df["symbol"].nunique() if "symbol" in df.columns else 0, df["datetime"].max(), age, db, table,
            )
            return df.sort_values(["symbol", "datetime"]) if "symbol" in df.columns else df
    except Exception:
        logger.debug("[TONOSAMA ENTRY] summary db fallback failed interval=%s db=%s", interval, db, exc_info=True)
        return pd.DataFrame()


def _load_history_latest_slot(interval: int) -> tuple[pd.DataFrame, str]:
    interval = int(interval)
    providers: list[tuple[str, callable]] = [
        ("global_context.get_summary_history_push", lambda: _call_global_context_method("get_summary_history", interval, source="push")),
    ]
    try:
        fn = getattr(global_data, "get_summary_history", None)
        if callable(fn):
            def _gd_hist():
                try:
                    return _safe_df(fn(interval, source="push"))
                except TypeError:
                    return _safe_df(fn(interval))
            providers.append(("global_data.get_summary_history_push", _gd_hist))
    except Exception:
        pass
    try:
        fn = getattr(global_data, "get_rejected_merged_summary", None)
        if callable(fn):
            def _gd_rejected():
                try:
                    return _safe_df(fn(interval, source="push"))
                except TypeError:
                    return _safe_df(fn(interval))
            providers.append(("global_data.get_rejected_merged_summary_push", _gd_rejected))
    except Exception:
        pass
    providers.append(("summary_db_latest_slot", lambda: _load_summary_db_latest_slot(interval)))

    best = pd.DataFrame()
    best_via = "none"
    best_latest = None
    for via, loader in providers:
        try:
            raw = _safe_df(loader())
            latest_slot = _latest_slot_today(raw, interval=interval, via=via) if via != "summary_db_latest_slot" else raw
            if latest_slot.empty:
                continue
            latest = _latest_dt(latest_slot)
            if latest is not None and not pd.isna(latest) and (best_latest is None or latest > best_latest):
                best = latest_slot
                best_via = via
                best_latest = latest
        except Exception:
            logger.debug("[TONOSAMA ENTRY] history latest provider failed via=%s interval=%s", via, interval, exc_info=True)
    return best, best_via


def _maybe_replace_stale_with_history(df: pd.DataFrame, *, interval: int, via: str) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    if not _env_bool("TONOSAMA_REPLACE_STALE_MERGED_WITH_HISTORY", True):
        return df
    max_age = max(30.0, _env_float("TONOSAMA_WAIT_PUSH_SUMMARY_MAX_AGE_SEC", 180.0))
    history_max_age = max(max_age, _env_float("TONOSAMA_HISTORY_FALLBACK_MAX_AGE_SEC", 600.0))
    age = _latest_age_sec(df)
    latest = _latest_dt(df)
    if age is None or age <= max_age:
        return df
    hist, hist_via = _load_history_latest_slot(interval)
    hist_age = _latest_age_sec(hist)
    hist_latest = _latest_dt(hist)
    if hist is not None and not hist.empty and hist_age is not None and hist_age <= history_max_age:
        logger.warning(
            "[TONOSAMA ENTRY] merged summary stale -> history/db latest fallback interval=%s merged_via=%s merged_latest=%s merged_age=%.1fs hist_via=%s hist_latest=%s hist_age=%.1fs rows=%s max_age=%.1fs history_max_age=%.1fs",
            interval, via, latest, age, hist_via, hist_latest, hist_age, len(hist), max_age, history_max_age,
        )
        return hist
    logger.warning(
        "[TONOSAMA ENTRY] merged summary stale and history/db latest unavailable interval=%s via=%s latest=%s age=%.1fs hist_via=%s hist_latest=%s hist_age=%s hist_rows=%s max_age=%.1fs history_max_age=%.1fs",
        interval, via, latest, age, hist_via, hist_latest, None if hist_age is None else round(float(hist_age), 1), 0 if hist is None else len(hist), max_age, history_max_age,
    )
    return df


def _call_summary_getter(interval: int) -> pd.DataFrame | None:
    interval = int(interval)
    try:
        fn = getattr(global_data, "get_push_merged_summary", None)
        if callable(fn):
            df = _safe_df(fn(interval))
            if not df.empty:
                df = _maybe_replace_stale_with_history(df, interval=interval, via="get_push_merged_summary")
                logger.info("[TONOSAMA ENTRY] loaded push merged summary interval=%s rows=%s latest_dt=%s via=get_push_merged_summary", interval, len(df), _latest_dt(df))
                return df
    except Exception:
        logger.debug("[TONOSAMA ENTRY] get_push_merged_summary failed interval=%s", interval, exc_info=True)

    try:
        fn = getattr(global_data, "get_merged_summary", None)
        if callable(fn):
            try:
                df = _safe_df(fn(interval, source="push"))
            except TypeError:
                df = pd.DataFrame()
            if not df.empty:
                df = _maybe_replace_stale_with_history(df, interval=interval, via="get_merged_summary_source_push")
                logger.info("[TONOSAMA ENTRY] loaded push merged summary interval=%s rows=%s latest_dt=%s via=get_merged_summary_source_push", interval, len(df), _latest_dt(df))
                return df
    except Exception:
        logger.debug("[TONOSAMA ENTRY] get_merged_summary(source=push) failed interval=%s", interval, exc_info=True)

    df = _call_global_context_method("get_rejected_merged_summary", interval, source="push")
    if not df.empty:
        df = _latest_slot_today(df, interval=interval, via="global_context.rejected")
        if not df.empty:
            logger.warning("[TONOSAMA ENTRY] loaded rejected push summary interval=%s rows=%s latest_dt=%s via=global_context.rejected fallback_recent_filter_required", interval, len(df), _latest_dt(df))
            return df

    df = _call_global_context_method("get_summary_history", interval, source="push")
    if not df.empty:
        df = _latest_slot_today(df, interval=interval, via="global_context.get_summary_history_push")
        if not df.empty:
            logger.warning("[TONOSAMA ENTRY] loaded push summary history latest-slot fallback interval=%s rows=%s latest_dt=%s via=global_context.get_summary_history_push", interval, len(df), _latest_dt(df))
            return df

    try:
        fn = getattr(global_data, "get_summary_history", None)
        if callable(fn):
            try:
                df = _safe_df(fn(interval, source="push"))
            except TypeError:
                df = _safe_df(fn(interval))
            if not df.empty:
                df = _latest_slot_today(df, interval=interval, via="global_data.get_summary_history_push")
                if not df.empty:
                    logger.warning("[TONOSAMA ENTRY] loaded push summary history latest-slot fallback interval=%s rows=%s latest_dt=%s via=global_data.get_summary_history_push", interval, len(df), _latest_dt(df))
                    return df
    except Exception:
        logger.debug("[TONOSAMA ENTRY] get_summary_history(source=push) failed interval=%s", interval, exc_info=True)

    try:
        fn = getattr(global_data, "get_merged_summary", None)
        if callable(fn):
            df = _safe_df(fn(interval))
            if not df.empty:
                df = _reject_prev_day_history(df, interval=interval, via="legacy_no_source")
                if not df.empty:
                    df = _maybe_replace_stale_with_history(df, interval=interval, via="legacy_no_source")
                    logger.warning("[TONOSAMA ENTRY] loaded merged summary interval=%s rows=%s latest_dt=%s via=legacy_no_source fallback_may_be_stale", interval, len(df), _latest_dt(df))
                    return df
    except Exception:
        logger.debug("[TONOSAMA ENTRY] get_merged_summary legacy failed interval=%s", interval, exc_info=True)

    # 最後の保険: main.pyメモリが空でもmain_database.pyが保存したsummary DBを読む。
    df = _load_summary_db_latest_slot(interval)
    if not df.empty:
        return df
    return None


def load_merged_summary(interval: int) -> pd.DataFrame:
    try:
        df = _call_summary_getter(int(interval))
        if df is None or getattr(df, "empty", True):
            logger.info("[TONOSAMA ENTRY] merged summary empty interval=%s", interval)
            return pd.DataFrame()
        if not isinstance(df, pd.DataFrame):
            logger.warning("[TONOSAMA ENTRY] merged summary is not DataFrame interval=%s type=%s", interval, type(df).__name__)
            return pd.DataFrame()
        out = df.copy()
        out["_interval"] = int(interval)
        return out
    except Exception:
        logger.exception("[TONOSAMA ENTRY] load merged summary failed interval=%s", interval)
        return pd.DataFrame()


def normalize_summary_base(df: pd.DataFrame, *, interval: int) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    x = df.copy()
    x["_interval"] = int(interval)
    if "symbol" not in x.columns:
        return pd.DataFrame()
    x["symbol"] = x["symbol"].map(normalize_symbol)
    x = x[x["symbol"] != ""]
    if x.empty:
        return pd.DataFrame()
    if "symbolname" not in x.columns:
        name_col = first_existing_col(x, ["name", "symbol_name", "SymbolName"])
        x["symbolname"] = x[name_col].astype(str) if name_col else ""
    x["datetime"] = pd.to_datetime(x["datetime"], errors="coerce") if "datetime" in x.columns else pd.NaT
    close_col = first_existing_col(x, ["close", "close_price", "current_price", "price", "last_price"])
    if close_col is None:
        return pd.DataFrame()
    volume_col = first_existing_col(x, ["volume", "trading_volume", "Volume"])
    x["volume"] = pd.to_numeric(x[volume_col], errors="coerce").fillna(0.0) if volume_col else 0.0
    x["close"] = pd.to_numeric(x[close_col], errors="coerce")
    x = x.dropna(subset=["close"])
    x = x[x["close"] > 0]
    numeric_cols = [
        "score", "score_buy", "score_sell", "score_total", "final_score", "display_score", "disp_score",
        "ranking_score", "rsi", "macd", "signal", "slope", "slope_atr_scaled", "mtf", "score_mtf",
        "mtf_score", "ma5", "ma25", "ma75", "ma25_conf", "ma75_conf", "ai_prob", "ranking_momentum",
        "rank_improve", "volume_delta", "change_percentage", "open", "high", "low", "volume",
    ]
    for c in numeric_cols:
        if c in x.columns:
            x[c] = pd.to_numeric(x[c], errors="coerce")
    return x.sort_values(["symbol", "datetime"])


__all__ = ["load_merged_summary", "normalize_summary_base"]
