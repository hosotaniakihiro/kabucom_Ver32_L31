# ============================================================
# File   : trading/summary/mtf/daily_runtime_patch.py
# Version: PRODUCTION-STABLE-DAILY-MTF-RUNTIME-PATCH-V1
# ------------------------------------------------------------
# Purpose:
#   - 旧main.pyで読んでいた日足DB由来のMA/MTFを復活させる
#   - 起動時に日足DBを global_data.daily_mtf_df へロードする
#   - summary AI runner へ渡る直前の summary_df に日足MA/MTF列を自動mergeする
#
# Important:
#   - 既存summary作成処理は壊さない
#   - AI判定前にだけ日足列を付与する
#   - DB path/table は ENV で上書き可能
#
# ENV:
#   DAILY_MTF_ENABLED=1
#   DAILY_MTF_DB_PATH=\\192.168.0.22\kabu\stock_data\daily_db\stock_analysis.db
#   DAILY_MTF_TABLE=stock_analysis_latest
#   DAILY_MTF_MIN_CLOSE=1
# ============================================================

from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable, Optional

import pandas as pd

from global_state import global_data

from .daily_mtf_loader import (
    DEFAULT_DAILY_MTF_DB_PATH,
    DEFAULT_DAILY_MTF_TABLE,
    load_daily_mtf_latest_df,
)
from .daily_ma_mtf import attach_daily_ma_mtf_to_summary

logger = logging.getLogger(__name__)

_INSTALLED = False
_ORIGINALS: dict[str, Callable[..., Any]] = {}
_CACHE_DF: Optional[pd.DataFrame] = None
_CACHE_TS: float = 0.0


def _env_bool(name: str, default: bool) -> bool:
    try:
        v = os.environ.get(name)
        if v is None:
            return bool(default)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on"}:
            return True
        if s in {"0", "false", "no", "n", "off"}:
            return False
        return bool(default)
    except Exception:
        return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


def daily_mtf_enabled() -> bool:
    return _env_bool("DAILY_MTF_ENABLED", True)


def _daily_db_path() -> str:
    return str(os.environ.get("DAILY_MTF_DB_PATH") or DEFAULT_DAILY_MTF_DB_PATH)


def _daily_table() -> str:
    return str(os.environ.get("DAILY_MTF_TABLE") or DEFAULT_DAILY_MTF_TABLE)


def load_daily_mtf_runtime_df(*, force: bool = False) -> pd.DataFrame:
    """日足DBを読み、global_data.daily_mtf_df に保持する。"""
    global _CACHE_DF, _CACHE_TS

    if not daily_mtf_enabled():
        logger.warning("[DAILY MTF RUNTIME] disabled by DAILY_MTF_ENABLED=0")
        return pd.DataFrame()

    ttl = max(5, _env_int("DAILY_MTF_CACHE_TTL_SEC", 300))
    now = time.time()

    if not force and isinstance(_CACHE_DF, pd.DataFrame) and not _CACHE_DF.empty and (now - _CACHE_TS) < ttl:
        return _CACHE_DF.copy()

    db_path = _daily_db_path()
    table = _daily_table()
    min_close = _env_float("DAILY_MTF_MIN_CLOSE", 1.0)

    df = load_daily_mtf_latest_df(
        db_path=db_path,
        table_name=table,
        min_close=min_close,
    )

    if df is None:
        df = pd.DataFrame()

    _CACHE_DF = df.copy() if isinstance(df, pd.DataFrame) else pd.DataFrame()
    _CACHE_TS = now

    try:
        global_data.daily_mtf_df = _CACHE_DF.copy()
        global_data.daily_mtf_loaded_rows = int(len(_CACHE_DF))
        global_data.daily_mtf_db_path = db_path
        global_data.daily_mtf_table = table
        global_data.daily_mtf_latest_date = (
            _CACHE_DF["daily_date"].max()
            if isinstance(_CACHE_DF, pd.DataFrame) and not _CACHE_DF.empty and "daily_date" in _CACHE_DF.columns
            else None
        )
    except Exception:
        pass

    logger.warning(
        "[DAILY MTF RUNTIME] loaded daily_mtf rows=%s symbols=%s latest=%s db=%s table=%s",
        len(_CACHE_DF),
        _CACHE_DF["symbol"].nunique() if isinstance(_CACHE_DF, pd.DataFrame) and not _CACHE_DF.empty and "symbol" in _CACHE_DF.columns else 0,
        _CACHE_DF["daily_date"].max() if isinstance(_CACHE_DF, pd.DataFrame) and not _CACHE_DF.empty and "daily_date" in _CACHE_DF.columns else None,
        db_path,
        table,
    )

    return _CACHE_DF.copy()


def merge_daily_mtf_for_ai(summary_df: Any, *, source: Any = None) -> Any:
    """summary_dfへ日足MA/MTF列をmergeする。"""
    if not daily_mtf_enabled():
        return summary_df

    if not isinstance(summary_df, pd.DataFrame) or summary_df.empty:
        return summary_df

    try:
        daily_df = getattr(global_data, "daily_mtf_df", None)
        if not isinstance(daily_df, pd.DataFrame) or daily_df.empty:
            daily_df = load_daily_mtf_runtime_df(force=False)

        if daily_df is None or daily_df.empty:
            logger.warning("[DAILY MTF RUNTIME] daily df empty; AI summary merge skipped")
            return summary_df

        out = attach_daily_ma_mtf_to_summary(
            summary_df,
            daily_df,
            side="auto",
            overwrite_score_mtf=True,
            use_slope_bonus=True,
        )

        try:
            src = str(source or "").upper()
            logger.info(
                "[DAILY MTF RUNTIME] merged for AI source=%s rows=%s daily_hit=%s cols=%s",
                src,
                len(out),
                int((pd.to_numeric(out.get("daily_close", 0), errors="coerce").fillna(0) > 0).sum()) if isinstance(out, pd.DataFrame) else 0,
                len(out.columns) if isinstance(out, pd.DataFrame) else 0,
            )
        except Exception:
            pass

        return out

    except Exception:
        logger.exception("[DAILY MTF RUNTIME] merge daily mtf for AI failed")
        return summary_df


def _patch_runner_function(mod: Any, func_name: str) -> bool:
    fn = getattr(mod, func_name, None)
    if not callable(fn):
        return False

    key = f"{getattr(mod, '__name__', 'module')}.{func_name}"
    if key in _ORIGINALS:
        return True

    _ORIGINALS[key] = fn

    def wrapped(*args: Any, **kwargs: Any):
        try:
            # summary_df / df keyword を優先してmerge
            source = kwargs.get("source")

            if isinstance(kwargs.get("summary_df"), pd.DataFrame):
                kwargs = dict(kwargs)
                kwargs["summary_df"] = merge_daily_mtf_for_ai(kwargs["summary_df"], source=source)
            elif isinstance(kwargs.get("df"), pd.DataFrame):
                kwargs = dict(kwargs)
                kwargs["df"] = merge_daily_mtf_for_ai(kwargs["df"], source=source)
            elif args and isinstance(args[0], pd.DataFrame):
                args_list = list(args)
                args_list[0] = merge_daily_mtf_for_ai(args_list[0], source=source)
                args = tuple(args_list)
        except Exception:
            logger.exception("[DAILY MTF RUNTIME] wrapper pre-merge failed func=%s", key)

        return fn(*args, **kwargs)

    setattr(mod, func_name, wrapped)
    logger.warning("[DAILY MTF RUNTIME] patched %s", key)
    return True


def install_daily_mtf_runtime_patch() -> None:
    """summary AI runner 直前に日足MTFを自動mergeするpatchを入れる。"""
    global _INSTALLED
    if _INSTALLED:
        return

    if not daily_mtf_enabled():
        logger.warning("[DAILY MTF RUNTIME] patch not installed because DAILY_MTF_ENABLED=0")
        return

    # 起動時に一度ロードする
    load_daily_mtf_runtime_df(force=True)

    patched = 0

    try:
        from trading.entry.summary_ai import runner as runner_mod

        for name in (
            "run_summary_ai_entry_from_df",
            "run_push_summary_ai_entry",
            "run_yahoo_summary_ai_entry",
            "run_ranking_summary_ai_entry",
            "run_tonosama_summary_ai_entry",
        ):
            if _patch_runner_function(runner_mod, name):
                patched += 1
    except Exception:
        logger.exception("[DAILY MTF RUNTIME] failed to patch trading.entry.summary_ai.runner")

    try:
        import trading.entry.summary_ai as pkg_mod

        for name in (
            "run_summary_ai_entry_from_df",
            "run_push_summary_ai_entry",
            "run_yahoo_summary_ai_entry",
            "run_ranking_summary_ai_entry",
        ):
            if _patch_runner_function(pkg_mod, name):
                patched += 1
    except Exception:
        logger.debug("[DAILY MTF RUNTIME] package patch skipped", exc_info=True)

    _INSTALLED = True

    logger.warning(
        "[DAILY MTF RUNTIME] installed patched=%s loaded_rows=%s db=%s table=%s",
        patched,
        int(getattr(global_data, "daily_mtf_loaded_rows", 0) or 0),
        getattr(global_data, "daily_mtf_db_path", _daily_db_path()),
        getattr(global_data, "daily_mtf_table", _daily_table()),
    )


__all__ = [
    "load_daily_mtf_runtime_df",
    "merge_daily_mtf_for_ai",
    "install_daily_mtf_runtime_patch",
]
