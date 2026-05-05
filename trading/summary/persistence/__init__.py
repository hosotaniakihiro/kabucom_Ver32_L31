# ============================================================
# File   : trading/summary/persistence/__init__.py
# Version: PRODUCTION-COMPAT-SUMMARY-PERSISTENCE-EXPORT-V3-DAILY-MTF-PATCH
# ------------------------------------------------------------
# Purpose:
#   - 既存互換APIを維持する
#   - summary_saver_bulk 読み込み時に日足MA-MTFパッチを自動インストールする
#
# Notes:
#   - 元ファイルには ranking summary persistence 互換exportが含まれていたため維持
#   - 日足MA-MTFパッチは失敗しても import 自体を落とさない
# ============================================================

from __future__ import annotations

import importlib
import inspect
import logging
from typing import Any, Optional

import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# daily MA-MTF patch bootstrap
# ============================================================

def _install_daily_mtf_patch_on_import() -> None:
    """
    summary_saver_bulk の bulk_upsert_summary / save_summary_bulk / save_summary_df を
    日足MA-MTF付与版へ安全にラップする。

    ここで失敗しても、既存のsummary保存処理は止めない。
    """
    try:
        from trading.summary.mtf.daily_mtf_summary_patch import (
            install_daily_mtf_summary_patch,
        )

        ok = install_daily_mtf_summary_patch()
        if ok:
            logger.warning("[SUMMARY PERSISTENCE] daily MA-MTF patch installed")
        else:
            logger.warning("[SUMMARY PERSISTENCE] daily MA-MTF patch not installed")

    except Exception:
        logger.exception("[SUMMARY PERSISTENCE] daily MA-MTF patch install failed")


_install_daily_mtf_patch_on_import()


# ============================================================
# internal resolver
# ============================================================

def _resolve_backend():
    """
    実際の保存関数を安全に解決する。

    優先順位:
      1. 同階層 saver.py の save_ranking_summary
      2. 同階層 writer.py の save_ranking_summary
      3. database.crud.crud_ranking_summary.insert_ranking_summary_1min
      4. database.crud.crud_ranking_summary.save_ranking_summary_1min
    """
    candidates = [
        ("trading.ranking.summary.persistence.saver", "save_ranking_summary"),
        ("trading.ranking.summary.persistence.writer", "save_ranking_summary"),
        ("database.crud.crud_ranking_summary", "insert_ranking_summary_1min"),
        ("database.crud.crud_ranking_summary", "save_ranking_summary_1min"),
    ]

    for module_name, func_name in candidates:
        try:
            mod = importlib.import_module(module_name)
            fn = getattr(mod, func_name, None)
            if callable(fn):
                logger.info(
                    "[ranking.summary.persistence] backend resolved %s.%s",
                    module_name,
                    func_name,
                )
                return fn
        except Exception:
            logger.debug(
                "[ranking.summary.persistence] backend resolve failed %s.%s",
                module_name,
                func_name,
                exc_info=True,
            )

    return None


def _call_backend_safely(backend, df: pd.DataFrame, **kwargs: Any) -> Any:
    """
    backend のシグネチャ差異を吸収して呼ぶ。
    """
    if not callable(backend):
        return 0

    try:
        sig = inspect.signature(backend)
        params = sig.parameters
        accepts_var_kw = any(
            p.kind == inspect.Parameter.VAR_KEYWORD
            for p in params.values()
        )

        if accepts_var_kw:
            return backend(df, **kwargs)

        call_kwargs = {k: v for k, v in kwargs.items() if k in params}
        return backend(df, **call_kwargs)

    except TypeError:
        try:
            return backend(df)
        except Exception:
            logger.exception(
                "[ranking.summary.persistence] backend call failed backend=%s",
                getattr(backend, "__name__", repr(backend)),
            )
            return 0

    except Exception:
        logger.exception(
            "[ranking.summary.persistence] backend call failed backend=%s",
            getattr(backend, "__name__", repr(backend)),
        )
        return 0


# ============================================================
# public api
# ============================================================

def save_ranking_summary(
    df: Optional[pd.DataFrame],
    *args: Any,
    interval: int | str = 1,
    source: str = "ranking",
    **kwargs: Any,
) -> int:
    """
    ランキングサマリー保存の互換入口。

    既存 runner / __init__ / bootstrap がこの名前を import するため、
    必ずこの関数を公開する。
    """
    if df is None:
        logger.warning("[ranking.summary.persistence] save skipped df=None")
        return 0

    try:
        if hasattr(df, "empty") and df.empty:
            logger.info(
                "[ranking.summary.persistence] save skipped empty df interval=%s",
                interval,
            )
            return 0
    except Exception:
        pass

    backend = _resolve_backend()
    if not callable(backend):
        logger.warning(
            "[ranking.summary.persistence] no backend available; save skipped interval=%s rows=%s",
            interval,
            len(df) if hasattr(df, "__len__") else None,
        )
        return 0

    call_kwargs = dict(kwargs)
    call_kwargs.setdefault("interval", interval)
    call_kwargs.setdefault("source", source)

    ret = _call_backend_safely(backend, df, **call_kwargs)

    if ret is None:
        try:
            return int(len(df))
        except Exception:
            return 0

    try:
        return int(ret)
    except Exception:
        try:
            return int(len(df))
        except Exception:
            return 0


def save_ranking_summary_df(
    df: Optional[pd.DataFrame],
    *args: Any,
    **kwargs: Any,
) -> int:
    return save_ranking_summary(df, *args, **kwargs)


def persist_ranking_summary(
    df: Optional[pd.DataFrame],
    *args: Any,
    **kwargs: Any,
) -> int:
    return save_ranking_summary(df, *args, **kwargs)


def save(
    df: Optional[pd.DataFrame],
    *args: Any,
    **kwargs: Any,
) -> int:
    return save_ranking_summary(df, *args, **kwargs)


__all__ = [
    "save_ranking_summary",
    "save_ranking_summary_df",
    "persist_ranking_summary",
    "save",
]
