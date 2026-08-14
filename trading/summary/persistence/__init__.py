# ============================================================
# File   : trading/summary/persistence/__init__.py
# Version: PRODUCTION-COMPAT-SUMMARY-PERSISTENCE-EXPORT-V5-VWAP-STATE-PATCH
# ------------------------------------------------------------
# Purpose:
#   - 既存互換APIを維持する
#   - summary_saver_bulk 読み込み時に日足MA-MTFパッチを自動インストールする
#   - summary_saver_bulk 読み込み時にMAクロス状態DB保存パッチを自動インストールする
#   - summary_saver_bulk 読み込み時にVWAP状態DB保存パッチを自動インストールする
#
# Notes:
#   - 元ファイルには ranking summary persistence 互換exportが含まれていたため維持
#   - 追加パッチは失敗しても import 自体を落とさない
# ============================================================

from __future__ import annotations

import importlib
import inspect
import logging
from typing import Any, Optional

import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# summary save patches bootstrap
# ============================================================




def _install_summary_save_guards_on_import() -> None:
    """保存前3段ガード (quality / date / lock pressure) を最外周へ被せる。

    旧 core/startup の summary_save_quality_guard_patch /
    summary_db_date_guard_patch / summary_db_lock_pressure_patch を
    trading/summary/persistence/summary_save_guards.py へ集約したもの。

    必ず上の enrichment 系 (daily_mtf / ma_cross / vwap) の "後" に呼ぶこと。
    quality guard は enrichment で vwap 等の列が付く前の df を見る必要があり、
    内側に入ると「指標あり」と誤判定して不完全データを保存してしまう。
    """
    try:
        from trading.summary.persistence.summary_save_guards import install as install_guards

        ok = install_guards()
        logger.warning("[SUMMARY PERSISTENCE] summary save guards installed=%s", ok)
    except Exception:
        logger.exception("[SUMMARY PERSISTENCE] summary save guards install failed")


# daily_mtf / ma_cross / vwap の 3 パッチは summary_saver_bulk._enrich_before_save() へ
# 本文化した (適用順 vwap -> ma_cross -> daily_mtf を保持)。install は不要。
# 3 段ガードだけは従来どおり enrichment の外側へ被せる。
_install_summary_save_guards_on_import()


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
