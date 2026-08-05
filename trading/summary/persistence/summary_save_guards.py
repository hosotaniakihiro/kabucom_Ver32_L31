# ============================================================
# File   : trading/summary/persistence/summary_save_guards.py
# ------------------------------------------------------------
# summary DB への bulk 保存にかける3段のガード。
#
# 旧構成では以下の3パッチが core/startup から summary_saver_bulk の
# bulk_upsert_summary / save_summary_bulk / save_summary_df を
# 順に monkeypatch して重ねがけしていた:
#
#   1. summary_db_lock_pressure_patch   (runtime_env_defaults_patch 経由)
#   2. summary_db_date_guard_patch      (sitecustomize BACKGROUND / main.py)
#   3. summary_save_quality_guard_patch (同上、date guard の後 = 最外周)
#
# 実行順は quality -> date -> lock_pressure -> (vwap/ma_cross/daily_mtf) -> 本体。
# 後ろ3つは trading/summary/persistence/__init__.py がパッケージ import 時に
# install する enrichment 系で、df に vwap 等の列を足してから本体を呼ぶ。
# そのため quality guard を本体側 (最内周) へ移すと、enrichment 済みの df を見て
# 「指標あり」と誤判定し、OHLCだけの不完全データを保存してしまう。
#
# よってガードは本文へ埋め込まず、この 1 モジュールへ集約したうえで
# __init__.py が enrichment の後に install する (= 最外周) 構成にした。
# 3段が 1 つの関数に入ったので、実行順がこのファイルだけで読み切れる。
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import re
import sqlite3
import sys
import threading
from typing import Any, Optional

import pandas as pd

logger = logging.getLogger(__name__)

_INSTALLED = False
_ORIG_BULK = None
_ORIG_SAVE_BULK = None
_ORIG_SAVE_DF = None
_ORIG_SHOULD_AUTO_LATEST_ONLY = None
_CLEANUP_DONE: set[str] = set()
_CLEANUP_LOCK = threading.RLock()
_TABLES = ("stock_summary_1min", "stock_summary_3min", "stock_summary_5min")

_TRUE = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}

_DB_PROCESS_NAMES = (
    "main_database.py",
    "data_collectors_runner.py",
    "db_prepare_runner.py",
    "push_receiver_runner.py",
    "yahoo_complement_runner.py",
    "summary_database_runner.py",
    "ranking_collector_runner.py",
)


def _env_bool(name: str, default: bool = True) -> bool:
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
        return int(float(str(v).strip()))
    except Exception:
        return int(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _is_database_process() -> bool:
    """sitecustomize._is_database_process と同じ判定。"""
    try:
        argv = " ".join(str(x).replace("\\", "/").lower() for x in sys.argv)
    except Exception:
        argv = ""
    if any(x in argv for x in _DB_PROCESS_NAMES):
        return True
    return any(
        _env_bool(x, False)
        for x in (
            "AUTOSTOCK_DATA_COLLECTORS_PROCESS",
            "AUTOSTOCK_SUMMARY_DB_WRITER",
            "AUTOSTOCK_MAIN_DATABASE_PROCESS",
            "AUTOSTOCK_RANKING_COLLECTOR_PROCESS",
        )
    )


def _main_only_enabled(override_env: str) -> bool:
    """旧 BACKGROUND_MAIN_PATCHES 相当のスコープ (main.py のみ)。"""
    try:
        raw = os.getenv(override_env)
        if raw is not None and str(raw).strip() != "":
            return _env_bool(override_env, True)
        if _env_bool("CORE_STARTUP_FORCE_FULL_INIT_PATCHES", False):
            return True
        return not _is_database_process()
    except Exception:
        return False



# ============================================================
# 1. quality guard
# ============================================================

_SCORE_COLS = [
    "score", "score_total", "final_score", "display_score",
    "score_buy", "score_sell", "score_slope", "score_mtf", "mtf_score",
]
_TECH_COLS = [
    "slope", "slope_atr_scaled", "rsi", "macd", "signal", "hist",
    "mtf", "score_mtf", "ma5", "ma25", "ma75", "atr", "vwap",
]
_PRICE_COLS = ["close", "close_price", "price", "current_price", "open", "open_price"]
_MAINT_WORDS = (
    "bootstrap", "rebuild", "recovery", "recover", "backfill", "repair",
    "migrate", "migration", "historical", "history", "catchup", "startup",
    "yahoo", "night", "lunch",
)
_TARGET_WORDS = (
    "push", "ranking", "periodic", "tick", "display", "latest", "cache_writer", "scheduled", "scheduler",
)




def _is_maintenance_reason(reason: str) -> bool:
    r = str(reason or "").strip().lower()
    return any(w in r for w in _MAINT_WORDS)


def _is_target_reason(reason: str) -> bool:
    r = str(reason or "").strip().lower()
    if not r:
        return True
    return any(w in r for w in _TARGET_WORDS)


def _num_sum_abs(df: pd.DataFrame, cols: list[str]) -> float:
    use = [c for c in cols if c in df.columns]
    if not use:
        return 0.0
    total = 0.0
    for c in use:
        try:
            s = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
            total += float(s.abs().sum())
        except Exception:
            pass
    return float(total)


def _has_price(df: pd.DataFrame) -> bool:
    for c in _PRICE_COLS:
        if c not in df.columns:
            continue
        try:
            s = pd.to_numeric(df[c], errors="coerce")
            if bool((s.notna() & (s != 0)).any()):
                return True
        except Exception:
            pass
    return False


def _looks_uncomputed_for_db(df: Any, *, save_reason: str = "", interval: int | None = None) -> tuple[bool, dict[str, Any]]:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return False, {"reason": "empty_or_not_df"}

    if _is_maintenance_reason(save_reason):
        return False, {"reason": "maintenance_save_reason", "save_reason": save_reason}

    if not _is_target_reason(save_reason):
        return False, {"reason": "non_target_save_reason", "save_reason": save_reason}

    has_price = _has_price(df)
    score_abs = _num_sum_abs(df, _SCORE_COLS)
    tech_abs = _num_sum_abs(df, _TECH_COLS)
    score_cols = [c for c in _SCORE_COLS if c in df.columns]
    tech_cols = [c for c in _TECH_COLS if c in df.columns]

    bad = bool(has_price and score_abs == 0.0 and tech_abs == 0.0)

    diag = {
        "rows": len(df),
        "cols": len(df.columns),
        "interval": interval,
        "save_reason": save_reason,
        "has_price": has_price,
        "score_abs": score_abs,
        "tech_abs": tech_abs,
        "score_cols": score_cols,
        "tech_cols": tech_cols,
    }
    try:
        if "datetime" in df.columns:
            dt = pd.to_datetime(df["datetime"], errors="coerce")
            diag["latest_dt"] = str(dt.max())
            diag["earliest_dt"] = str(dt.min())
        if "symbol" in df.columns:
            diag["symbols"] = int(df["symbol"].astype(str).nunique())
    except Exception:
        pass
    return bad, diag



# ============================================================
# 2. date guard
# ============================================================

def _safe_datetime_series(s: Any) -> pd.Series:
    try:
        if isinstance(s, pd.DataFrame):
            s = s.iloc[:, 0] if s.shape[1] else pd.Series(dtype="object")
        if not isinstance(s, pd.Series):
            s = pd.Series(s)
        out = pd.to_datetime(s, errors="coerce")
        try:
            out = out.dt.tz_localize(None)
        except Exception:
            pass
        return out
    except Exception:
        return pd.Series(dtype="datetime64[ns]")


def _engine_url_to_path(engine: Any) -> Optional[str]:
    try:
        url = getattr(engine, "url", None)
        if url is None:
            return None
        database = getattr(url, "database", None)
        if database:
            return str(database)
        s = str(url)
        if s.startswith("sqlite:///"):
            return s.replace("sqlite:///", "", 1).replace("/", "\\")
    except Exception:
        pass
    return None


def _resolve_summary_engine() -> Any:
    candidates = []
    try:
        from database.session import get_summary_engine
        candidates.append(get_summary_engine)
    except Exception:
        pass
    try:
        from database.session import summary_engine
        candidates.append(lambda: summary_engine)
    except Exception:
        pass
    try:
        from database.session import Session_summary
        candidates.append(lambda: getattr(Session_summary, "bind", None))
    except Exception:
        pass
    try:
        from trading.summary.persistence.core import upsert_engine as ue
        candidates.append(lambda: getattr(ue, "summary_engine", None))
        candidates.append(lambda: getattr(ue, "engine", None))
    except Exception:
        pass

    for fn in candidates:
        try:
            engine = fn()
            if engine is not None:
                return engine
        except Exception:
            continue
    return None


def _resolve_summary_db_path() -> Optional[str]:
    try:
        engine = _resolve_summary_engine()
        p = _engine_url_to_path(engine)
        if p:
            return p
    except Exception:
        pass
    for key in ("SUMMARY_DB_PATH", "AUTOSTOCK_SUMMARY_DB_PATH", "KABU_SUMMARY_DB_PATH"):
        try:
            v = os.environ.get(key, "").strip()
            if v:
                return v
        except Exception:
            pass
    return None


def _target_date_from_path(path: Optional[str]) -> Optional[dt.date]:
    if not path:
        return None
    m = re.search(r"summary(\d{8})\.db", str(path), flags=re.IGNORECASE)
    if not m:
        return None
    try:
        return dt.datetime.strptime(m.group(1), "%Y%m%d").date()
    except Exception:
        return None


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    try:
        row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1", (table,)).fetchone()
        return bool(row)
    except Exception:
        return False


def _is_sqlite_locked(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return "database is locked" in msg or "database table is locked" in msg or "locked" in msg


def _cleanup_wrong_date_rows(db_path: str, target_date: dt.date, *, reason: str = "manual") -> None:
    if not _env_bool("SUMMARY_DB_DATE_GUARD_CLEANUP_ENABLED", False):
        return
    if _is_main_py_process() and not _env_bool("SUMMARY_DB_DATE_GUARD_CLEANUP_IN_MAIN", False):
        logger.info("[SUMMARY DB DATE GUARD] cleanup skipped in main.py reason=%s db=%s", reason, db_path)
        return

    key = f"{db_path}|{target_date.isoformat()}|{reason}"
    with _CLEANUP_LOCK:
        if key in _CLEANUP_DONE:
            return
        _CLEANUP_DONE.add(key)

    if not db_path or not os.path.exists(db_path):
        return

    target = target_date.isoformat()
    timeout_sec = max(0.1, _env_float("SUMMARY_DB_DATE_GUARD_CLEANUP_TIMEOUT_SEC", 1.0))
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(db_path, timeout=timeout_sec)
        try:
            conn.execute(f"PRAGMA busy_timeout={int(timeout_sec * 1000)}")
        except Exception:
            pass
        total_deleted = 0
        for table in _TABLES:
            if not _table_exists(conn, table):
                continue
            try:
                before = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                wrong = conn.execute(
                    f'SELECT COUNT(*) FROM "{table}" WHERE datetime IS NOT NULL AND substr(CAST(datetime AS TEXT),1,10) <> ?',
                    (target,),
                ).fetchone()[0]
                if int(wrong) > 0:
                    conn.execute(
                        f'DELETE FROM "{table}" WHERE datetime IS NOT NULL AND substr(CAST(datetime AS TEXT),1,10) <> ?',
                        (target,),
                    )
                    after = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                    deleted = int(before) - int(after)
                    total_deleted += deleted
                    logger.warning(
                        "[SUMMARY DB DATE GUARD] cleanup deleted table=%s db=%s target_date=%s before=%s wrong=%s after=%s deleted=%s reason=%s",
                        table, db_path, target, before, wrong, after, deleted, reason,
                    )
                else:
                    logger.info(
                        "[SUMMARY DB DATE GUARD] cleanup ok table=%s db=%s target_date=%s rows=%s wrong=0 reason=%s",
                        table, db_path, target, before, reason,
                    )
            except sqlite3.OperationalError as exc:
                if _is_sqlite_locked(exc):
                    logger.warning(
                        "[SUMMARY DB DATE GUARD] cleanup skipped locked table=%s db=%s reason=%s timeout=%.1fs",
                        table, db_path, reason, timeout_sec,
                    )
                    continue
                logger.exception("[SUMMARY DB DATE GUARD] cleanup failed table=%s db=%s", table, db_path)
            except Exception:
                logger.exception("[SUMMARY DB DATE GUARD] cleanup failed table=%s db=%s", table, db_path)
        conn.commit()
        if total_deleted:
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass
    except sqlite3.OperationalError as exc:
        if _is_sqlite_locked(exc):
            logger.warning(
                "[SUMMARY DB DATE GUARD] cleanup skipped locked db=%s target_date=%s reason=%s timeout=%.1fs",
                db_path, target, reason, timeout_sec,
            )
        else:
            logger.exception("[SUMMARY DB DATE GUARD] cleanup db failed db=%s target_date=%s", db_path, target)
    except Exception:
        logger.exception("[SUMMARY DB DATE GUARD] cleanup db failed db=%s target_date=%s", db_path, target)
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass


def _filter_df_to_summary_db_date(df: pd.DataFrame, *, interval: int, save_reason: str) -> pd.DataFrame:
    if df is None or getattr(df, "empty", True):
        return df
    if "datetime" not in df.columns:
        logger.warning(
            "[SUMMARY DB DATE GUARD] datetime column missing -> cannot date-filter interval=%s rows=%s save_reason=%s",
            interval, len(df), save_reason,
        )
        return df

    db_path = _resolve_summary_db_path()
    target_date = _target_date_from_path(db_path)
    if target_date is None:
        logger.warning(
            "[SUMMARY DB DATE GUARD] target date unresolved -> skip date filter interval=%s db_path=%s rows=%s save_reason=%s",
            interval, db_path, len(df), save_reason,
        )
        return df

    # 保存前cleanupは既定OFF。別日行の新規保存防止だけは必ず行う。
    if _env_bool("SUMMARY_DB_DATE_GUARD_CLEANUP_BEFORE_SAVE", False):
        try:
            _cleanup_wrong_date_rows(str(db_path), target_date, reason=f"before_save:{save_reason or 'unknown'}")
        except Exception:
            logger.debug("[SUMMARY DB DATE GUARD] cleanup call failed", exc_info=True)

    out = df.copy()
    dt_s = _safe_datetime_series(out["datetime"])
    mask_valid = dt_s.notna()
    mask_target = mask_valid & (dt_s.dt.date == target_date)
    before = len(out)
    wrong = int((mask_valid & ~mask_target).sum())
    invalid = int((~mask_valid).sum())

    if wrong or invalid:
        sample = []
        try:
            sample = (
                out.loc[mask_valid & ~mask_target, [c for c in ("symbol", "datetime") if c in out.columns]]
                .head(10)
                .to_dict("records")
            )
        except Exception:
            pass
        logger.warning(
            "[SUMMARY DB DATE GUARD] drop wrong-date rows interval=%s db=%s target_date=%s rows_before=%s wrong_date=%s invalid_dt=%s sample=%s save_reason=%s",
            interval, db_path, target_date.isoformat(), before, wrong, invalid, sample, save_reason,
        )
    out = out.loc[mask_target].copy()
    after = len(out)
    if after != before:
        logger.warning(
            "[SUMMARY DB DATE GUARD] filtered interval=%s rows_before=%s rows_after=%s target_date=%s save_reason=%s",
            interval, before, after, target_date.isoformat(), save_reason,
        )
    return out

# ============================================================
# 3. lock pressure
# ============================================================

_MAINTENANCE_KEYWORDS = (
    "bootstrap",
    "rebuild",
    "recovery",
    "recover",
    "backfill",
    "full",
    "repair",
    "migrate",
    "migration",
    "historical",
    "history",
    "catchup",
    "startup",
)

_REALTIME_KEYWORDS = (
    "periodic",
    "tick",
    "push",
    "yahoo",
    "display",
    "latest",
    "scheduled",
    "scheduler",
    "regular",
    "summary_upsert",
    "auto_realtime",
)


def _lock_env_bool(name: str, default: bool) -> bool:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enabled"}
    except Exception:
        return bool(default)


def _lock_env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except Exception:
        return float(default)


def _lock_env_int(name: str, default: int) -> int:
    try:
        return int(float(os.environ.get(name, str(default))))
    except Exception:
        return int(default)


def _lock_reason_text(save_reason: Any) -> str:
    try:
        return str(save_reason or "").strip().lower()
    except Exception:
        return ""


def _lock_is_maintenance_reason(save_reason: Any) -> bool:
    r = _lock_reason_text(save_reason)
    return bool(r and any(k in r for k in _MAINTENANCE_KEYWORDS))


def _lock_is_realtime_reason(save_reason: Any) -> bool:
    r = _lock_reason_text(save_reason)
    if not r:
        return True
    if _lock_is_maintenance_reason(r):
        return False
    return any(k in r for k in _REALTIME_KEYWORDS)


def _lock_row_count(df: Any) -> int:
    try:
        return int(len(df))
    except Exception:
        return -1


def _patched_should_auto_latest_only(save_reason: str) -> bool:
    if _lock_is_maintenance_reason(save_reason):
        return False
    if _lock_is_realtime_reason(save_reason):
        return True
    try:
        if callable(_ORIGINAL_SHOULD_AUTO_LATEST_ONLY):
            return bool(_ORIGINAL_SHOULD_AUTO_LATEST_ONLY(save_reason))
    except Exception:
        logger.debug("[SUMMARY LOCK PRESSURE] original latest_only detector failed", exc_info=True)
    return False


# ============================================================
# 3段まとめた保存ガード + install
# ============================================================

def _guarded_bulk_upsert(
    orig,
    df,
    interval: int,
    lock_timeout_sec=None,
    skip_if_busy: bool = False,
    latest_only: bool = False,
    save_reason: str = "",
):
    """quality -> date -> lock_pressure の順に適用してから元実装を呼ぶ。

    旧3パッチが重ねがけしていた順序をこの1関数で表現している。
    """
    # --- 1. quality guard (main.py 専用スコープ)
    if _main_only_enabled("SUMMARY_SAVE_QUALITY_GUARD_SCOPE_ENABLED") and _env_bool(
        "SUMMARY_SAVE_ZERO_TECH_GUARD", True
    ):
        bad, diag = _looks_uncomputed_for_db(df, save_reason=save_reason, interval=int(interval))
        if bad:
            logger.warning(
                "[SUMMARY SAVE QUALITY GUARD] skip DB save uncomputed-zero-tech interval=%s diag=%s",
                interval, diag,
            )
            return 0

    # --- 2. date guard (main.py 専用スコープ)
    if _main_only_enabled("SUMMARY_DB_DATE_GUARD_SCOPE_ENABLED") and _env_bool(
        "SUMMARY_DB_DATE_GUARD_ENABLED", True
    ):
        filtered = _filter_df_to_summary_db_date(df, interval=int(interval), save_reason=save_reason)
        if filtered is None or getattr(filtered, "empty", False):
            try:
                rows = 0 if df is None else len(df)
            except Exception:
                rows = -1
            logger.warning(
                "[SUMMARY DB DATE GUARD] all rows removed -> skip upsert interval=%s original_rows=%s save_reason=%s",
                interval, rows, save_reason,
            )
            return 0
        df = filtered

    # --- 3. lock pressure (旧パッチは広いスコープで install されていた)
    timeout_cap = _lock_env_float("SUMMARY_REALTIME_LOCK_TIMEOUT_SEC", 2.0)
    if _env_bool("SUMMARY_DB_LOCK_PRESSURE_ENABLED", True) and not _env_bool(
        "DISABLE_SUMMARY_DB_LOCK_PRESSURE_PATCH", False
    ):
        try:
            reason = _lock_reason_text(save_reason)
            rows = _lock_row_count(df)
            threshold = _lock_env_int("SUMMARY_REALTIME_LATEST_ONLY_ROW_THRESHOLD", 500)
            force_latest = _lock_env_bool("SUMMARY_REALTIME_FORCE_LATEST_ONLY", True)
            quick_skip = _lock_env_bool("SUMMARY_REALTIME_SKIP_IF_BUSY", True)

            maintenance = _lock_is_maintenance_reason(reason)
            realtime_like = _lock_is_realtime_reason(reason)
            should_shrink = (not maintenance) and force_latest and (realtime_like or rows >= threshold)

            if should_shrink:
                latest_only = True
                if quick_skip:
                    skip_if_busy = True
                if not reason:
                    save_reason = "auto_realtime_empty_reason"
                try:
                    if lock_timeout_sec is None:
                        lock_timeout_sec = timeout_cap
                    else:
                        lock_timeout_sec = min(float(lock_timeout_sec), timeout_cap)
                except Exception:
                    lock_timeout_sec = timeout_cap

                if rows >= threshold or not reason:
                    logger.warning(
                        "[SUMMARY LOCK PRESSURE] realtime save compressed interval=%s rows=%s latest_only=%s "
                        "skip_if_busy=%s timeout=%.2fs reason=%s",
                        interval, rows, latest_only, skip_if_busy,
                        float(lock_timeout_sec or timeout_cap), save_reason,
                    )
        except Exception:
            logger.debug("[SUMMARY LOCK PRESSURE] precheck failed", exc_info=True)

    return orig(
        df,
        interval=interval,
        lock_timeout_sec=lock_timeout_sec if lock_timeout_sec is not None else timeout_cap,
        skip_if_busy=bool(skip_if_busy),
        latest_only=bool(latest_only),
        save_reason=save_reason,
    )


def install() -> bool:
    """summary_saver_bulk の保存入口へ3段ガードを被せる。

    trading/summary/persistence/__init__.py が enrichment 系
    (daily_mtf / ma_cross / vwap) を install した「後」に呼ぶこと。
    quality guard は enrichment 前の df を見る必要があるため、
    このガードは必ず最外周でなければならない。
    """
    global _INSTALLED, _ORIG_BULK, _ORIG_SAVE_BULK, _ORIG_SAVE_DF, _ORIG_SHOULD_AUTO_LATEST_ONLY
    if _INSTALLED:
        return True
    try:
        from trading.summary.persistence import summary_saver_bulk as saver

        cur = getattr(saver, "bulk_upsert_summary", None)
        if not callable(cur):
            logger.warning("[SUMMARY SAVE GUARDS] bulk_upsert_summary not callable")
            return False
        if getattr(cur, "_summary_save_guards_installed", False):
            _INSTALLED = True
            return True

        _ORIG_BULK = cur
        _ORIG_SAVE_BULK = getattr(saver, "save_summary_bulk", None)
        _ORIG_SAVE_DF = getattr(saver, "save_summary_df", None)
        _ORIG_SHOULD_AUTO_LATEST_ONLY = getattr(saver, "_should_auto_latest_only", None)

        def guarded(df, interval, lock_timeout_sec=None, skip_if_busy=False, latest_only=False, save_reason=""):
            return _guarded_bulk_upsert(
                _ORIG_BULK, df, interval,
                lock_timeout_sec=lock_timeout_sec,
                skip_if_busy=skip_if_busy,
                latest_only=latest_only,
                save_reason=save_reason,
            )

        guarded._summary_save_guards_installed = True  # type: ignore[attr-defined]
        guarded._original = _ORIG_BULK  # type: ignore[attr-defined]

        saver.bulk_upsert_summary = guarded
        saver.save_summary_bulk = guarded
        saver.save_summary_df = guarded
        saver._should_auto_latest_only = _patched_should_auto_latest_only

        _INSTALLED = True
        logger.warning(
            "[SUMMARY SAVE GUARDS] installed quality=%s date=%s lock_pressure=%s",
            _main_only_enabled("SUMMARY_SAVE_QUALITY_GUARD_SCOPE_ENABLED"),
            _main_only_enabled("SUMMARY_DB_DATE_GUARD_SCOPE_ENABLED"),
            _env_bool("SUMMARY_DB_LOCK_PRESSURE_ENABLED", True),
        )
        return True
    except Exception:
        logger.exception("[SUMMARY SAVE GUARDS] install failed")
        return False


__all__ = ["install", "run_date_cleanup"]


def run_date_cleanup(db_path: str, target_date, *, reason: str = "manual") -> None:
    """別日行のクリーンアップ (旧 date guard の公開機能)。"""
    _cleanup_wrong_date_rows(db_path, target_date, reason=reason)
