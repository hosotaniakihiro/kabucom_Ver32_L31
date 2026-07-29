# ============================================================
# File   : trading/summary/persistence/summary_saver_bulk.py
# Version: Ver34.4-PRODUCTION-SUMMARY-SAVER-BULK-OWNER-GUARD
# ------------------------------------------------------------
# 【概要】
#   summary DB への bulk UPSERT 保存入口
#
# 【主な機能】
#   ✔ bulk_upsert_summary(df, interval) API 維持
#   ✔ save_summary_bulk / save_summary_df 互換APIも提供
#   ✔ helper 群を modules へ委譲
#   ✔ symbol / datetime / OHLC alias / validation / dedupe / lock を委譲
#   ✔ 1分足は PUSH由来の不完全OHLCでも close ベースで保存継続
#   ✔ UPSERT前にDB実カラムへ列を整形
#   ✔ open/high/low/close/interval unknown column warning を抑止
#   ✔ open_price/high_price/low_price/close_price は保持
#   ✔ datetime warning を安全変換で抑止
#   ✔ latest_only=True で symbolごとの最新足だけDB保存
#   ✔ periodic / push / yahoo 定時保存時の全履歴UPSERTを防止
#   ✔ recovery / bootstrap / rebuild では latest_only=False で全履歴保存可能
#   ✔ latest_only 適用前後の rows / symbols / datetime 範囲をログ出力
#   ✔ SQLite database is locked 対策として summary DB 全体 write lock を使用
#   ✔ main.py / main_database.py の二重DB保存を owner guard で防止
#
# 【Ver34.4 修正】
#   ✔ AUTOSTOCK_SUMMARY_SAVE_OWNER を尊重
#   ✔ 既定 owner=database のため main.py 側のsummary DB保存はスキップ
#   ✔ main.pyは計算/表示/AI/entry、main_database.pyはDB保存に分離
#   ✔ bootstrap/recovery/rebuild/repair等の保守系保存は owner guard の対象外
#
# 【重要】
#   SQLite は同じDBファイルへの同時 writer が基本1つのみ。
#   interval別 lock だけでは stock_summary_1min / 3min / 5min の
#   同時書き込みを防げないため、DB単位の write lock が必要。
#   さらに main.py と main_database.py の二重保存も避ける。
# ============================================================

from __future__ import annotations

import logging
import os
import threading
import time
import warnings
from typing import Any, Optional

import pandas as pd

from trading.summary.persistence.helpers import (
    _ensure_dataframe,
    _safe_get_series,
    _ensure_identity_columns,
    _drop_invalid_ohlc_rows,
    _dedupe_before_save,
    _interval_lock,
    SummaryBusySkip,
    DEFAULT_LOCK_TIMEOUT_SEC,
)

try:
    from trading.summary.persistence.core.summary_db_write_lock import summary_db_write_lock
except Exception:
    summary_db_write_lock = None  # type: ignore

logger = logging.getLogger(__name__)


# ============================================================
# constants
# ============================================================

_KNOWN_OPTIONAL_ALIAS_COLS = [
    "open",
    "high",
    "low",
    "close",
    "interval",
]

_ALIAS_TO_DB_COLS = {
    "open": "open_price",
    "high": "high_price",
    "low": "low_price",
    "close": "close_price",
}

_FALLBACK_DROP_COLS = [
    "open",
    "high",
    "low",
    "close",
    "interval",
]


# ============================================================
# owner guard helpers
# ============================================================

def _is_data_collector_process_safe() -> bool:
    try:
        from data_collectors.split_mode import is_data_collector_process
        return bool(is_data_collector_process())
    except Exception:
        return False


def _summary_save_owner_safe() -> str:
    try:
        from data_collectors.split_mode import summary_save_owner
        return str(summary_save_owner())
    except Exception:
        try:
            import os
            return str(os.getenv("AUTOSTOCK_SUMMARY_SAVE_OWNER", "database")).strip().lower() or "database"
        except Exception:
            return "database"


def _is_maintenance_save_reason(save_reason: str) -> bool:
    try:
        r = str(save_reason or "").strip().lower()
    except Exception:
        r = ""
    if not r:
        return False
    maintenance_keywords = (
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
    return any(k in r for k in maintenance_keywords)


def _should_skip_summary_save_by_owner(save_reason: str) -> tuple[bool, str]:
    """
    main.py と main_database.py の二重保存を避ける。

    既定:
      AUTOSTOCK_SUMMARY_SAVE_OWNER=database
      main_database.py/data collector process だけ保存する。

    例外:
      bootstrap/recovery/rebuild/repair などの保守系保存は明示処理として通す。
    """
    if _is_maintenance_save_reason(save_reason):
        return False, "maintenance_save_reason"

    owner = _summary_save_owner_safe()
    is_db_proc = _is_data_collector_process_safe()

    if owner == "none":
        return True, "owner_none"

    if owner == "both":
        return False, "owner_both"

    if owner == "database":
        if not is_db_proc:
            return True, "owner_database_non_collector_process"
        return False, "owner_database_collector_process"

    if owner == "main":
        if is_db_proc:
            return True, "owner_main_collector_process"
        return False, "owner_main_non_collector_process"

    # 不正値は database 扱い
    if not is_db_proc:
        return True, "owner_invalid_as_database_non_collector_process"
    return False, "owner_invalid_as_database_collector_process"


# ============================================================
# datetime helpers
# ============================================================

def _strip_tz_keep_wallclock(v: Any):
    try:
        if v is None:
            return pd.NaT

        if isinstance(v, str):
            s = v.strip()
            if not s or s.lower() in {"nan", "none", "nat", "<na>", "null"}:
                return pd.NaT
            v = s

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            ts = pd.Timestamp(v)

        if pd.isna(ts):
            return pd.NaT

        if ts.tzinfo is not None:
            try:
                ts = ts.tz_localize(None)
            except Exception:
                try:
                    ts = pd.Timestamp(ts.replace(tzinfo=None))
                except Exception:
                    pass

        return pd.Timestamp(ts)

    except Exception:
        return pd.NaT


def _safe_to_datetime_naive_series(s: Any) -> pd.Series:
    try:
        if s is None:
            return pd.Series(dtype="datetime64[ns]")

        if isinstance(s, pd.DataFrame):
            if s.shape[1] <= 0:
                return pd.Series(dtype="datetime64[ns]")
            s = s.iloc[:, 0]

        if not isinstance(s, pd.Series):
            s = pd.Series(s)

        if pd.api.types.is_datetime64_any_dtype(s) and not pd.api.types.is_datetime64tz_dtype(s):
            out = pd.to_datetime(s, errors="coerce")
            try:
                out = out.dt.tz_localize(None)
            except Exception:
                pass
            return out

        out = s.map(_strip_tz_keep_wallclock)
        out = pd.to_datetime(out, errors="coerce")

        try:
            out = out.dt.tz_localize(None)
        except Exception:
            pass

        return out

    except Exception:
        logger.debug("[SUMMARY] safe datetime parse failed", exc_info=True)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                return pd.to_datetime(pd.Series(s), errors="coerce")
        except Exception:
            return pd.Series(dtype="datetime64[ns]")


# ============================================================
# db schema helpers
# ============================================================

def _summary_table_name(interval: int) -> str:
    return f"stock_summary_{int(interval)}min"


def _resolve_summary_engine():
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

    for resolver in candidates:
        try:
            engine = resolver()
            if engine is not None:
                return engine
        except Exception:
            continue

    return None


# ============================================================
# summary ranking schema repair
# (旧 core/startup/sqlite_memory_pragmas_patch.py の
#  _install_summary_ranking_schema_patch から移設)
#
# summary_saver_bulk は DataFrame の列を実テーブルへ整形するため、
# 古い summary DB に rank/ranking_score/ranking_type 列が無いと
# UPSERT前にこれらを drop してしまう。列読み取りと同じ接続内で
# 不足列を補修してから読み取る。
# ============================================================

SUMMARY_TABLES: tuple[str, ...] = (
    "stock_summary_1min",
    "stock_summary_3min",
    "stock_summary_5min",
)

SUMMARY_RANKING_COLUMNS: tuple[tuple[str, str], ...] = (
    ("rank", "REAL"),
    ("rank_no", "REAL"),
    ("best_rank", "REAL"),
    ("avg_rank", "REAL"),
    ("rank_types_count", "INTEGER"),
    ("ranking_score", "REAL"),
    ("ranking_score_total", "REAL"),
    ("ranking_type", "TEXT"),
    ("rank_types", "TEXT"),
    ("type", "TEXT"),
    ("ranking", "TEXT"),
    ("market", "TEXT"),
    ("current_price", "REAL"),
    ("change_rate", "REAL"),
    ("chg", "REAL"),
    ("trading_volume", "REAL"),
    ("trading_value", "REAL"),
    ("turnover", "REAL"),
    ("turn", "REAL"),
)


def _quote_ident(name: str) -> str:
    try:
        from database.sqlite import quote_ident
        return quote_ident(str(name))
    except Exception:
        return '"' + str(name).replace('"', '""') + '"'


def _schema_repair_env_bool(name: str, default: bool) -> bool:
    try:
        raw = str(os.getenv(name, "")).strip().lower()
        if raw in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}:
            return True
        if raw in {"0", "false", "no", "n", "off", "disable", "disabled"}:
            return False
    except Exception:
        pass
    return bool(default)


def _ensure_summary_ranking_columns(conn, table_name: str) -> None:
    if _schema_repair_env_bool("DISABLE_SUMMARY_RANKING_SCHEMA_REPAIR_PATCH", False):
        return
    try:
        q = _quote_ident(table_name)
        rows = conn.exec_driver_sql(f"PRAGMA table_info({q})").fetchall()
        if not rows:
            return
        existing = {
            str(r[1]).strip()
            for r in rows
            if len(r) > 1 and r[1] is not None and str(r[1]).strip()
        }
        added: list[str] = []
        for col, decl in SUMMARY_RANKING_COLUMNS:
            if col in existing:
                continue
            try:
                conn.exec_driver_sql(f"ALTER TABLE {q} ADD COLUMN {_quote_ident(col)} {decl}")
                existing.add(col)
                added.append(col)
            except Exception as e:
                msg = str(e).lower()
                if "duplicate column" in msg or "already exists" in msg:
                    existing.add(col)
                    continue
                raise
        if added:
            logger.warning(
                "[SUMMARY RANKING SCHEMA REPAIR] table=%s added_columns=%s",
                table_name,
                added,
            )
    except Exception:
        logger.debug("[SUMMARY RANKING SCHEMA REPAIR] engine repair failed table=%s", table_name, exc_info=True)


def _get_table_columns_from_engine(engine, table_name: str) -> Optional[set[str]]:
    if engine is None:
        return None

    try:
        with engine.begin() as conn:
            if str(table_name) in SUMMARY_TABLES:
                _ensure_summary_ranking_columns(conn, str(table_name))
            rows = conn.exec_driver_sql(f'PRAGMA table_info("{table_name}")').fetchall()
            cols = {
                str(r[1]).strip()
                for r in rows
                if len(r) > 1 and r[1] is not None and str(r[1]).strip()
            }
            return cols or None
    except Exception:
        logger.debug(
            "[SUMMARY] table column inspect failed table=%s",
            table_name,
            exc_info=True,
        )
        return None


def _get_summary_table_columns(interval: int) -> Optional[set[str]]:
    table_name = _summary_table_name(interval)
    engine = _resolve_summary_engine()
    cols = _get_table_columns_from_engine(engine, table_name)

    if cols:
        logger.debug(
            "[SUMMARY] detected table columns interval=%s table=%s cols=%s",
            interval,
            table_name,
            sorted(cols),
        )

    return cols


# ============================================================
# dataframe normalization before upsert
# ============================================================

def _coalesce_numeric_column(work: pd.DataFrame, dst: str, src: str) -> pd.DataFrame:
    if src not in work.columns:
        return work

    out = work.copy()

    try:
        src_s = pd.to_numeric(_safe_get_series(out, src), errors="coerce")

        if dst not in out.columns:
            out[dst] = src_s
        else:
            dst_s = pd.to_numeric(_safe_get_series(out, dst), errors="coerce")
            out[dst] = dst_s.where(dst_s.notna(), src_s)

    except Exception:
        logger.debug(
            "[SUMMARY] coalesce numeric column failed src=%s dst=%s",
            src,
            dst,
            exc_info=True,
        )

    return out


def _protect_ohlc_price_aliases(work: pd.DataFrame) -> pd.DataFrame:
    out = work.copy()

    for src, dst in _ALIAS_TO_DB_COLS.items():
        out = _coalesce_numeric_column(out, dst=dst, src=src)

    return out


def _normalize_time_columns(work: pd.DataFrame) -> pd.DataFrame:
    if work is None or work.empty:
        return work

    out = work.copy()

    for c in ("datetime", "start_time", "end_time", "last_update"):
        if c in out.columns:
            try:
                out[c] = _safe_to_datetime_naive_series(_safe_get_series(out, c))
            except Exception:
                logger.debug("[SUMMARY] datetime normalize failed col=%s", c, exc_info=True)

    return out


def _align_columns_to_table(work: pd.DataFrame, interval: int) -> pd.DataFrame:
    if work is None or work.empty:
        return work

    out = work.copy()
    interval = int(interval)
    table_name = _summary_table_name(interval)

    out = _protect_ohlc_price_aliases(out)

    table_cols = _get_summary_table_columns(interval)

    if table_cols:
        drop_cols = [c for c in out.columns if c not in table_cols]

        if drop_cols:
            logger.info(
                "[SUMMARY] align columns to table interval=%s table=%s dropped=%s",
                interval,
                table_name,
                drop_cols,
            )
            out = out.drop(columns=drop_cols, errors="ignore")

        return out

    fallback_drop = [c for c in _FALLBACK_DROP_COLS if c in out.columns]

    if fallback_drop:
        logger.info(
            "[SUMMARY] align columns fallback interval=%s table=%s dropped=%s",
            interval,
            table_name,
            fallback_drop,
        )
        out = out.drop(columns=fallback_drop, errors="ignore")

    return out


def _profile_columns(work: pd.DataFrame) -> dict[str, int]:
    profile: dict[str, int] = {}

    for c in [
        "symbol",
        "datetime",
        "date",
        "time_range",
        "start_time",
        "end_time",
        "open",
        "high",
        "low",
        "close",
        "open_price",
        "high_price",
        "low_price",
        "close_price",
        "volume",
        "rsi",
        "macd",
        "signal",
        "score",
        "score_buy",
        "score_sell",
        "display_ready",
        "technical_ready",
    ]:
        if c in work.columns:
            try:
                s = _safe_get_series(work, c)
                if s is not None:
                    profile[c] = int(s.notna().sum())
            except Exception:
                pass

    return profile


# ============================================================
# latest-only save helpers
# ============================================================

def _latest_per_symbol_for_periodic_save(
    work: pd.DataFrame,
    *,
    interval: int,
    reason: str = "",
) -> pd.DataFrame:
    if work is None or work.empty:
        return work

    if "symbol" not in work.columns or "datetime" not in work.columns:
        logger.warning(
            "[SUMMARY] latest_only skipped interval=%s reason=%s missing symbol/datetime cols=%s rows=%s",
            interval,
            reason,
            list(work.columns),
            len(work),
        )
        return work

    out = work.copy()

    try:
        out["symbol"] = out["symbol"].astype(str).str.strip()
        out["datetime"] = _safe_to_datetime_naive_series(_safe_get_series(out, "datetime"))

        out = out.dropna(subset=["symbol", "datetime"]).copy()
        out = out[out["symbol"] != ""].copy()

        if out.empty:
            logger.warning(
                "[SUMMARY] latest_only result empty interval=%s reason=%s",
                interval,
                reason,
            )
            return out

        before_rows = len(out)
        before_symbols = int(out["symbol"].nunique())
        dt_min = out["datetime"].min()
        dt_max = out["datetime"].max()

        out = out.sort_values(["symbol", "datetime"], kind="stable")
        out = out.groupby("symbol", as_index=False).tail(1).reset_index(drop=True)

        after_rows = len(out)
        after_symbols = int(out["symbol"].nunique())

        logger.warning(
            "[SUMMARY] latest_only applied interval=%s reason=%s "
            "rows_before=%s symbols_before=%s dt_min=%s dt_max=%s "
            "rows_after=%s symbols_after=%s",
            interval,
            reason,
            before_rows,
            before_symbols,
            dt_min,
            dt_max,
            after_rows,
            after_symbols,
        )

        return out

    except Exception:
        logger.exception(
            "[SUMMARY] latest_only failed interval=%s reason=%s -> keep original rows=%s",
            interval,
            reason,
            len(work),
        )
        return work


def _should_auto_latest_only(save_reason: str) -> bool:
    try:
        r = str(save_reason or "").strip().lower()
    except Exception:
        r = ""

    if not r:
        return False

    full_save_keywords = (
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
    if any(k in r for k in full_save_keywords):
        return False

    periodic_keywords = (
        "periodic",
        "tick",
        "push",
        "yahoo",
        "display",
        "latest",
        "scheduled",
        "scheduler",
        "regular",
    )
    return any(k in r for k in periodic_keywords)


# ============================================================
# DB write lock helper
# ============================================================

def _run_upsert_with_summary_db_lock(
    work: pd.DataFrame,
    *,
    interval: int,
    save_reason: str,
) -> tuple[int, float]:
    from trading.summary.persistence.core.upsert_engine import execute_upsert

    table_name = _summary_table_name(interval)

    if summary_db_write_lock is None:
        logger.warning(
            "[SUMMARY] summary_db_write_lock import failed -> execute without DB-global lock "
            "interval=%s table=%s rows=%s save_reason=%s",
            interval,
            table_name,
            len(work),
            save_reason,
        )
        execute_upsert(work, interval)
        return int(len(work)), 0.0

    with summary_db_write_lock(
        reason=save_reason or "summary_upsert",
        interval=int(interval),
        table_name=table_name,
        timeout_sec=None,
    ) as db_lock_wait:
        execute_upsert(work, interval)
        return int(len(work)), float(db_lock_wait)


# ============================================================
# public API
# ============================================================

def bulk_upsert_summary(
    df: pd.DataFrame,
    interval: int,
    lock_timeout_sec: float = DEFAULT_LOCK_TIMEOUT_SEC,
    skip_if_busy: bool = False,
    latest_only: bool = False,
    save_reason: str = "",
) -> int:
    total_started = time.monotonic()
    interval = int(interval)
    table_name = _summary_table_name(interval)

    skip_owner, owner_reason = _should_skip_summary_save_by_owner(save_reason)
    if skip_owner:
        try:
            rows = 0 if df is None else len(df)
        except Exception:
            rows = -1
        logger.warning(
            "[SUMMARY OWNER GUARD] skip DB save interval=%s table=%s rows=%s owner=%s is_data_collector=%s reason=%s save_reason=%s",
            interval,
            table_name,
            rows,
            _summary_save_owner_safe(),
            _is_data_collector_process_safe(),
            owner_reason,
            save_reason,
        )
        return 0

    try:
        from trading.summary.persistence.core.upsert_engine import execute_upsert  # noqa: F401
    except Exception:
        logger.exception("[SUMMARY] failed import execute_upsert")
        raise

    work = _ensure_dataframe(df)
    if work.empty:
        logger.debug("[SUMMARY] bulk_upsert_summary skip empty interval=%s", interval)
        return 0

    try:
        latest_dt = None
        earliest_dt = None
        symbols = 0

        if "datetime" in work.columns:
            s = _safe_to_datetime_naive_series(_safe_get_series(work, "datetime"))
            if s is not None and s.notna().any():
                latest_dt = s.max()
                earliest_dt = s.min()

        if "symbol" in work.columns:
            symbols = int(
                work["symbol"]
                .astype(str)
                .str.strip()
                .replace("", pd.NA)
                .dropna()
                .nunique()
            )

        logger.info(
            "[SUMMARY] upsert enter interval=%s table=%s rows=%s symbols=%s earliest=%s latest=%s "
            "tid=%s thread=%s lock_timeout=%.3fs skip_if_busy=%s latest_only=%s save_reason=%s owner_guard=%s",
            interval,
            table_name,
            len(work),
            symbols,
            earliest_dt,
            latest_dt,
            threading.get_ident(),
            threading.current_thread().name,
            float(lock_timeout_sec),
            bool(skip_if_busy),
            bool(latest_only),
            save_reason,
            owner_reason,
        )
    except Exception:
        logger.debug("[SUMMARY] enter log failed", exc_info=True)

    t_pre = time.monotonic()

    work = _ensure_identity_columns(work, interval=interval)
    work = _normalize_time_columns(work)
    work = _drop_invalid_ohlc_rows(work, interval=interval, stage="bulk-save-pre")
    work = _dedupe_before_save(work, interval=interval)

    auto_latest_only = _should_auto_latest_only(save_reason)
    effective_latest_only = bool(latest_only or auto_latest_only)

    if effective_latest_only:
        if auto_latest_only and not latest_only:
            logger.warning(
                "[SUMMARY] latest_only auto-enabled interval=%s save_reason=%s rows=%s",
                interval,
                save_reason,
                len(work),
            )

        work = _latest_per_symbol_for_periodic_save(
            work,
            interval=interval,
            reason=save_reason or "latest_only",
        )
    else:
        logger.info(
            "[SUMMARY] latest_only disabled interval=%s save_reason=%s rows=%s",
            interval,
            save_reason,
            len(work),
        )

    if work.empty:
        logger.warning("[SUMMARY] no valid rows before upsert interval=%s", interval)
        return 0

    rows_before_align = len(work)
    cols_before_align = list(work.columns)
    work = _align_columns_to_table(work, interval=interval)

    if work.empty:
        logger.warning(
            "[SUMMARY] no valid rows after column align interval=%s rows_before=%s cols_before=%s",
            interval,
            rows_before_align,
            cols_before_align,
        )
        return 0

    try:
        profile = _profile_columns(work)

        logger.info(
            "[SUMMARY] preprocess done interval=%s table=%s rows=%s cols=%s profile=%s "
            "latest_only=%s save_reason=%s elapsed=%.3fs",
            interval,
            table_name,
            len(work),
            list(work.columns),
            profile,
            effective_latest_only,
            save_reason,
            time.monotonic() - t_pre,
        )
    except Exception:
        logger.debug("[SUMMARY] preprocess profile failed", exc_info=True)

    try:
        with _interval_lock(
            interval,
            timeout_sec=float(lock_timeout_sec),
            skip_if_busy=bool(skip_if_busy),
        ) as interval_lock_wait:
            t_exec = time.monotonic()

            saved_rows, db_lock_wait = _run_upsert_with_summary_db_lock(
                work,
                interval=interval,
                save_reason=save_reason,
            )

            exec_elapsed = time.monotonic() - t_exec

            logger.info(
                "[SUMMARY] upsert leave interval=%s table=%s rows=%s tid=%s thread=%s "
                "interval_lock_wait=%.3fs db_lock_wait=%.3fs upsert=%.3fs total=%.3fs "
                "latest_only=%s save_reason=%s",
                interval,
                table_name,
                saved_rows,
                threading.get_ident(),
                threading.current_thread().name,
                float(interval_lock_wait),
                float(db_lock_wait),
                exec_elapsed,
                time.monotonic() - total_started,
                effective_latest_only,
                save_reason,
            )

            return int(saved_rows)

    except SummaryBusySkip:
        logger.warning(
            "[SUMMARY] upsert skipped busy interval=%s table=%s rows=%s timeout=%.3fs total=%.3fs "
            "latest_only=%s save_reason=%s",
            interval,
            table_name,
            len(work),
            float(lock_timeout_sec),
            time.monotonic() - total_started,
            effective_latest_only,
            save_reason,
        )
        return 0

    except TimeoutError:
        logger.exception(
            "[SUMMARY] summary db write lock timeout interval=%s table=%s rows=%s total=%.3fs "
            "latest_only=%s save_reason=%s",
            interval,
            table_name,
            len(work),
            time.monotonic() - total_started,
            effective_latest_only,
            save_reason,
        )
        raise

    except Exception:
        logger.exception(
            "[SUMMARY] upsert failed interval=%s table=%s rows=%s total=%.3fs "
            "latest_only=%s save_reason=%s",
            interval,
            table_name,
            len(work),
            time.monotonic() - total_started,
            effective_latest_only,
            save_reason,
        )
        raise


# ============================================================
# compatibility aliases
# ============================================================

def save_summary_bulk(
    df: pd.DataFrame,
    interval: int,
    lock_timeout_sec: float = DEFAULT_LOCK_TIMEOUT_SEC,
    skip_if_busy: bool = False,
    latest_only: bool = False,
    save_reason: str = "",
) -> int:
    return bulk_upsert_summary(
        df,
        interval=interval,
        lock_timeout_sec=lock_timeout_sec,
        skip_if_busy=skip_if_busy,
        latest_only=latest_only,
        save_reason=save_reason,
    )


def save_summary_df(
    df: pd.DataFrame,
    interval: int,
    lock_timeout_sec: float = DEFAULT_LOCK_TIMEOUT_SEC,
    skip_if_busy: bool = False,
    latest_only: bool = False,
    save_reason: str = "",
) -> int:
    return bulk_upsert_summary(
        df,
        interval=interval,
        lock_timeout_sec=lock_timeout_sec,
        skip_if_busy=skip_if_busy,
        latest_only=latest_only,
        save_reason=save_reason,
    )


__all__ = [
    "bulk_upsert_summary",
    "save_summary_bulk",
    "save_summary_df",
]