# ============================================================
# File   : trading/summary/mtf/daily_mtf_summary.py
# Version: PRODUCTION-STABLE-DAILY-MTF-SUMMARY-PATCH-REV1.2
# ------------------------------------------------------------
# Purpose:
#   - summary_saver_bulk の保存入口を安全にラップする
#   - stock_analysis_latest の日足 MA_5 / MA_25 / MA_75 を読み込む
#   - 保存直前の summary df に daily_ma / daily_mtf を付与する
#   - summary DB 側に不足列があれば自動追加する
#
# Design:
#   - 既存 summary_saver_bulk.py の大規模差し替えを避ける
#   - 日足DB読み込みに失敗しても、元のsummary保存は止めない
#   - DB列追加に失敗しても、元のsummary保存は止めない
#   - すでに patch 済みなら二重patchしない
#   - SQLAlchemy raw_connection() は context manager にしない
#   - lock_timeout / lock_timeout_sec の別名差を吸収する
# ============================================================

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Optional

import pandas as pd

from trading.summary.mtf.daily_ma_mtf import (
    attach_daily_ma_mtf_to_summary,
    ensure_daily_mtf_columns_sqlite,
)
from trading.summary.mtf.daily_mtf_loader import load_daily_mtf_latest_df

logger = logging.getLogger(__name__)

_PATCHED = False
_DAILY_CACHE_DF: Optional[pd.DataFrame] = None
_DAILY_CACHE_TS: float = 0.0
_DAILY_CACHE_MTIME: Optional[float] = None

# 日足 (stock_analysis_latest) は 1 日 1 回しか更新されない。
# 旧実装は 300 秒 TTL だったため 1 日あたり 70 回以上、
# 5.17GB の DB から WHERE も LIMIT も無い SELECT で 4,192 行を読み直していた
# (実測 1 回 24 秒 = 1 日およそ 29 分)。
# TTL を延ばしたうえで、DB ファイルの更新時刻が変わったら即座に読み直す。
_DAILY_CACHE_TTL_SEC: float = 1800.0


def _daily_cache_ttl_sec() -> float:
    try:
        return max(0.0, float(os.getenv("DAILY_MTF_CACHE_TTL_SEC", str(_DAILY_CACHE_TTL_SEC))))
    except Exception:
        return _DAILY_CACHE_TTL_SEC


def _daily_db_mtime() -> Optional[float]:
    """日足DBの更新時刻。取れなければ None (その場合は TTL だけで判断する)。"""
    try:
        from trading.summary.mtf.daily_mtf_loader import DEFAULT_DAILY_MTF_DB_PATH

        raw = os.getenv("DAILY_MTF_DB_PATH") or DEFAULT_DAILY_MTF_DB_PATH
        db = Path(str(raw))
        return db.stat().st_mtime if db.exists() else None
    except Exception:
        return None


def reset_daily_cache() -> None:
    global _DAILY_CACHE_DF, _DAILY_CACHE_TS, _DAILY_CACHE_MTIME
    _DAILY_CACHE_DF = None
    _DAILY_CACHE_TS = 0.0
    _DAILY_CACHE_MTIME = None


def _get_cached_daily_df() -> pd.DataFrame:
    """stock_analysis_latest を毎回読みに行かないためのキャッシュ。

    日足は 1 日 1 回しか変わらないので TTL は長めにし、
    DB ファイルの更新時刻が変わったときは TTL 内でも読み直す。
    """
    global _DAILY_CACHE_DF, _DAILY_CACHE_TS, _DAILY_CACHE_MTIME

    now = time.monotonic()
    mtime = _daily_db_mtime()

    if _DAILY_CACHE_DF is not None and not _DAILY_CACHE_DF.empty:
        fresh = (now - _DAILY_CACHE_TS) <= _daily_cache_ttl_sec()
        unchanged = (mtime is None) or (_DAILY_CACHE_MTIME == mtime)
        if fresh and unchanged:
            return _DAILY_CACHE_DF
        if not unchanged:
            logger.info(
                "[DAILY MTF PATCH] daily db updated -> reload cache mtime=%s -> %s",
                _DAILY_CACHE_MTIME, mtime,
            )

    df = load_daily_mtf_latest_df()
    _DAILY_CACHE_DF = df
    _DAILY_CACHE_TS = now
    _DAILY_CACHE_MTIME = mtime
    return df


def _summary_table_name(interval: int) -> str:
    return f"stock_summary_{int(interval)}min"


def _ensure_summary_daily_mtf_columns(summary_saver_bulk_module: Any, interval: int) -> None:
    """
    summary DB の対象テーブルに daily_mtf 用カラムを追加する。
    summary_saver_bulk._resolve_summary_engine() が使える場合だけ実行。

    Notes
    -----
    SQLAlchemy の engine.raw_connection() が返す _ConnectionFairy は、
    環境によって context manager 非対応のため、with ではなく明示 close する。
    """
    raw_conn = None
    try:
        resolver = getattr(summary_saver_bulk_module, "_resolve_summary_engine", None)
        if not callable(resolver):
            logger.debug("[DAILY MTF PATCH] _resolve_summary_engine unavailable")
            return

        engine = resolver()
        if engine is None:
            logger.debug("[DAILY MTF PATCH] summary engine unresolved")
            return

        table_name = _summary_table_name(interval)

        # SQLAlchemy engine想定。raw sqlite connection に落として列追加する。
        # raw_connection() は context manager として使わない。
        raw_conn = engine.raw_connection()  # type: ignore[attr-defined]
        ensure_daily_mtf_columns_sqlite(raw_conn, table_name)

        try:
            raw_conn.commit()
        except Exception:
            # ensure_daily_mtf_columns_sqlite 側で commit 済みの実装にも対応。
            logger.debug("[DAILY MTF PATCH] raw_conn.commit skipped/failed", exc_info=True)

    except Exception:
        logger.exception(
            "[DAILY MTF PATCH] ensure summary columns failed interval=%s",
            interval,
        )
        try:
            if raw_conn is not None:
                raw_conn.rollback()
        except Exception:
            pass

    finally:
        try:
            if raw_conn is not None:
                raw_conn.close()
        except Exception:
            pass


def _attach_daily_mtf_safely(df: pd.DataFrame, *, interval: int, save_reason: str = "") -> pd.DataFrame:
    """
    保存前dfへ日足MTFを付与する。失敗時は元dfを返す。
    """
    try:
        if df is None or df.empty:
            return df

        daily_df = _get_cached_daily_df()
        if daily_df is None or daily_df.empty:
            logger.warning(
                "[DAILY MTF PATCH] daily df empty -> skip attach interval=%s save_reason=%s",
                interval,
                save_reason,
            )
            return df

        out = attach_daily_ma_mtf_to_summary(
            df,
            daily_df=daily_df,
            side="auto",
            overwrite_score_mtf=True,
            use_slope_bonus=True,
        )

        try:
            logger.info(
                "[DAILY MTF PATCH] attached interval=%s save_reason=%s rows=%s daily_hit=%s score_mtf_daily_nonzero=%s",
                interval,
                save_reason,
                len(out),
                int((pd.to_numeric(out.get("daily_close"), errors="coerce").fillna(0) > 0).sum()) if "daily_close" in out.columns else 0,
                int((pd.to_numeric(out.get("score_mtf_daily"), errors="coerce").fillna(0) != 0).sum()) if "score_mtf_daily" in out.columns else 0,
            )
        except Exception:
            pass

        return out

    except Exception:
        logger.exception(
            "[DAILY MTF PATCH] attach failed -> keep original interval=%s save_reason=%s",
            interval,
            save_reason,
        )
        return df


def _pop_lock_timeout_aliases(kwargs: dict[str, Any], lock_timeout_sec: Any) -> Any:
    """
    呼び出し元によって lock_timeout / lock_timeout_sec の名前が混在している。

    summary_saver_bulk.bulk_upsert_summary 本体は lock_timeout_sec を受けるため、
    MTFラッパーで別名を吸収し、元関数へ未知keywordを渡さない。
    """
    if lock_timeout_sec is None and "lock_timeout_sec" in kwargs:
        lock_timeout_sec = kwargs.pop("lock_timeout_sec")
    else:
        kwargs.pop("lock_timeout_sec", None)

    if lock_timeout_sec is None and "lock_timeout" in kwargs:
        lock_timeout_sec = kwargs.pop("lock_timeout")
    else:
        kwargs.pop("lock_timeout", None)

    return lock_timeout_sec


def prepare_df(saver, df, interval, *, save_reason=""):
    """保存直前の日足 MTF 列付与 + スキーマ確認 (旧ラッパー内の処理)。"""
    try:
        _ensure_summary_daily_mtf_columns(saver, int(interval))
    except Exception:
        logger.debug("[DAILY MTF PATCH] schema ensure outer failed", exc_info=True)
    return _attach_daily_mtf_safely(df, interval=int(interval), save_reason=save_reason)


__all__ = ["prepare_df"]

