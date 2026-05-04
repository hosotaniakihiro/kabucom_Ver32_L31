# ============================================================
# File   : trading/summary/persistence/summary_saver_bulk.py
# Version: Ver34.3-PRODUCTION-SUMMARY-SAVER-BULK-DB-WRITE-LOCK
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
#
# 【Ver34.3 修正】
#   ✔ SQLite database is locked 対策を強化
#   ✔ interval別 lock に加えて summary DB 全体の write lock を追加
#   ✔ 1min / 3min / 5min が別テーブルでも同一DBへの同時書き込みを防止
#   ✔ bootstrap / recovery / periodic / yahoo 保存の競合を DB単位で直列化
#   ✔ lock_wait のログを interval lock / db write lock の両方で出力
#
# 【重要】
#   SQLite は同じDBファイルへの同時 writer が基本1つのみ。
#   interval別 lock だけでは stock_summary_1min / 3min / 5min の
#   同時書き込みを防げないため、DB単位の write lock が必要。
# ============================================================

from __future__ import annotations

import logging
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

# upsert_executor 側で warning になっていた代表列。
# DBに実在しない場合のみ落とす。
_KNOWN_OPTIONAL_ALIAS_COLS = [
    "open",
    "high",
    "low",
    "close",
    "interval",
]

# alias を落とす前に、DB正式列へ値を退避するための対応表。
_ALIAS_TO_DB_COLS = {
    "open": "open_price",
    "high": "high_price",
    "low": "low_price",
    "close": "close_price",
}

# どうしてもDBカラム取得に失敗した場合の安全drop。
# ログで実際に unknown になっていた列だけに限定する。
_FALLBACK_DROP_COLS = [
    "open",
    "high",
    "low",
    "close",
    "interval",
]


# ============================================================
# datetime helpers
# ============================================================

def _strip_tz_keep_wallclock(v: Any):
    """
    timezone付き datetime を UTC変換せず、壁時計時刻を維持して tz だけ外す。

    例:
      2026-04-20 10:51:00+09:00
          -> 2026-04-20 10:51:00
    """
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
    """
    UserWarning: Could not infer format... を出さずに datetime 化する。
    UTC変換はせず、JSTの壁時計時刻を維持する。
    """
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
    """
    summary DB の SQLAlchemy engine をできるだけ広く解決する。

    失敗しても保存処理自体は止めない。
    """
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


def _get_table_columns_from_engine(engine, table_name: str) -> Optional[set[str]]:
    """
    SQLAlchemy engine から SQLite PRAGMA で実カラムを取得する。
    """
    if engine is None:
        return None

    try:
        with engine.connect() as conn:
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
    """
    stock_summary_{interval}min の実カラムを取得する。
    """
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
    """
    src を dst に退避する。
    dst が既にあれば欠損のみ src で補完。
    """
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
    """
    open/high/low/close を落とす前に、
    open_price/high_price/low_price/close_price へ退避する。
    """
    out = work.copy()

    for src, dst in _ALIAS_TO_DB_COLS.items():
        out = _coalesce_numeric_column(out, dst=dst, src=src)

    return out


def _normalize_time_columns(work: pd.DataFrame) -> pd.DataFrame:
    """
    datetime / start_time / end_time / last_update の warning 抑止。
    """
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
    """
    UPSERT前にDB実カラムへ列を整形する。

    目的:
      - upsert_executor の
          [UPSERT] dropping unknown column ...
        を発生前に抑止する
      - open/high/low/close の値は *_price に退避してから落とす
      - interval はDBに無ければ落とす
    """
    if work is None or work.empty:
        return work

    out = work.copy()
    interval = int(interval)
    table_name = _summary_table_name(interval)

    # aliasを落とす前に正式列へ退避
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

    # DBカラム取得に失敗した場合は、ログで実際にunknownだった列だけ落とす。
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
    """
    定時サマリー保存用:
      - 履歴付きで計算した summary_df から
      - DB保存対象だけを symbolごとの最新足へ絞る

    目的:
      - 毎回 5万〜10万行を UPSERT しない
      - chunk=500/528 のような重い保存を防ぐ
      - 計算用履歴は維持し、保存だけ軽量化する

    注意:
      - recovery / bootstrap / full rebuild では latest_only=False を使う
      - periodic / push / yahoo 定時保存時だけ latest_only=True を使う
    """
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
    """
    呼び出し元が latest_only を明示していない場合でも、
    save_reason から定時系だと判断できるものは latest_only を有効化する。

    互換性:
      - デフォルトは False
      - recovery / bootstrap / rebuild / backfill / repair は対象外
      - 明示 latest_only=True が最優先
    """
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
    """
    execute_upsert を summary DB 単位の write lock 内で実行する。

    Returns
    -------
    tuple[int, float]
        saved_rows, db_lock_wait_sec
    """
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
    """
    summary DB へ bulk UPSERT する。

    Parameters
    ----------
    df:
        保存対象DataFrame。
    interval:
        1, 3, 5 など。
    lock_timeout_sec:
        interval単位の排他ロック待機秒数。
    skip_if_busy:
        True の場合、ロックが取れなければ保存をスキップ。
    latest_only:
        True の場合、symbolごとの最新 datetime 1行だけ保存する。
        定時サマリー保存では True 推奨。
        recovery / bootstrap / rebuild では False のまま使う。
    save_reason:
        ログ用の保存理由。
        例:
          - periodic_push_summary
          - yahoo_complement_periodic
          - bootstrap_rebuild
          - recovery_full

    Notes
    -----
    Ver34.3:
      interval lock の内側で summary DB 全体の write lock も取得する。
      これにより stock_summary_1min / 3min / 5min の同時UPSERTによる
      database is locked を抑止する。
    """
    total_started = time.monotonic()
    interval = int(interval)
    table_name = _summary_table_name(interval)

    # import確認だけ先に行う。
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
            "tid=%s thread=%s lock_timeout=%.3fs skip_if_busy=%s latest_only=%s save_reason=%s",
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
        )
    except Exception:
        logger.debug("[SUMMARY] enter log failed", exc_info=True)

    t_pre = time.monotonic()

    work = _ensure_identity_columns(work, interval=interval)
    work = _normalize_time_columns(work)
    work = _drop_invalid_ohlc_rows(work, interval=interval, stage="bulk-save-pre")
    work = _dedupe_before_save(work, interval=interval)

    # --------------------------------------------------------
    # latest-only save
    # --------------------------------------------------------
    # 明示 latest_only=True、または save_reason から定時系と判定できる場合のみ、
    # symbolごとの最新足だけに絞る。
    #
    # recovery / bootstrap / rebuild では save_reason をそれらにしておけば
    # auto latest-only は発動しない。
    # --------------------------------------------------------
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

    # UPSERT前にDBカラムに合わせる。
    # ここで open/high/low/close/interval unknown warning を事前抑止する。
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
        # ----------------------------------------------------
        # 1) interval別 lock
        # 2) summary DB全体 write lock
        #
        # interval別 lock:
        #   同じ interval の重複保存を防ぐ。
        #
        # DB全体 write lock:
        #   1min / 3min / 5min / recovery / yahoo など、
        #   同一 summary DB への同時 writer を防ぐ。
        # ----------------------------------------------------
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
    """
    旧API互換。
    """
    return bulk_upsert_summary(
        df=df,
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
    """
    旧API互換。
    """
    return bulk_upsert_summary(
        df=df,
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