# ============================================================
# File   : trading/ranking/summary/persistence/__init__.py
# Version: PRODUCTION-COMPAT-RANKING-SUMMARY-PERSISTENCE-EXPORT-V2
# ------------------------------------------------------------
# Purpose:
#   trading.ranking.summary.__init__ / runner / bootstrap から参照される
#   save_ranking_summary 系APIを互換exportする。
#
# Fix:
#   ImportError:
#     cannot import name 'save_ranking_summary'
#     from trading.ranking.summary.persistence
#
# Policy:
#   - 既存実装があればそれを優先
#   - なければ database.crud.crud_ranking_summary へフォールバック
#   - さらに失敗しても import 自体は落とさない
# ============================================================

from __future__ import annotations

import importlib
import inspect
import logging
from typing import Any, Optional

import pandas as pd

logger = logging.getLogger(__name__)


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
        # 旧実装向け
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

    Returns
    -------
    int
        保存行数。失敗時は 0。
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
    """
    別名互換。
    """
    return save_ranking_summary(df, *args, **kwargs)


def persist_ranking_summary(
    df: Optional[pd.DataFrame],
    *args: Any,
    **kwargs: Any,
) -> int:
    """
    別名互換。
    """
    return save_ranking_summary(df, *args, **kwargs)


def save(
    df: Optional[pd.DataFrame],
    *args: Any,
    **kwargs: Any,
) -> int:
    """
    短縮名互換。
    """
    return save_ranking_summary(df, *args, **kwargs)


__all__ = [
    "save_ranking_summary",
    "save_ranking_summary_df",
    "persist_ranking_summary",
    "save",
]