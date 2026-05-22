# ============================================================
# File   : core/startup/summary_display_label_guard_patch.py
# Version: V1.0-DISPLAY-INTERVAL-LABEL-DATAFRAME-GUARD
# ------------------------------------------------------------
# 【目的】
#   SUMMARY表示で interval_label に DataFrame が誤って渡り、
#   "AI PASSED BUY CANDIDATES (   symbol ... [47 rows x 127 columns])"
#   のように画面へDataFrame本体が表示される問題を防ぐ。
#
# 【方針】
#   - scheduler_jobs.summary.display の公開表示関数を薄くwrapする。
#   - interval_label が DataFrame / Series / dict / list / tuple の場合は表示名として使わない。
#   - kwargs の interval があれば "3min" のように復元する。
#   - それも無ければ "-" にする。
#   - 表示だけの修正で、エントリー判定・AI判定・DB保存には触らない。
# ============================================================

from __future__ import annotations

import logging
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)
_PATCHED = False


def _looks_bad_label(v: Any) -> bool:
    try:
        if v is None:
            return False
        if isinstance(v, (pd.DataFrame, pd.Series, dict, list, tuple, set)):
            return True
        s = str(v)
        # pandas DataFrame の文字列表現がタイトルに混ざる場合の保険
        if " rows x " in s and "columns" in s:
            return True
        if "   symbol" in s and "datetime" in s:
            return True
        if len(s) > 80:
            return True
        return False
    except Exception:
        return True


def _label_from_kwargs(kwargs: dict[str, Any], default: str = "-") -> str:
    try:
        lbl = kwargs.get("interval_label")
        if lbl is not None and not _looks_bad_label(lbl):
            return str(lbl)

        interval = kwargs.get("interval")
        if interval is not None and not _looks_bad_label(interval):
            try:
                return f"{int(float(interval))}min"
            except Exception:
                return str(interval)
    except Exception:
        pass
    return default


def _normalize_args_kwargs(args: tuple[Any, ...], kwargs: dict[str, Any]) -> tuple[tuple[Any, ...], dict[str, Any]]:
    args_l = list(args)
    kw = dict(kwargs)

    # interval_label がkeywordで壊れている場合
    if "interval_label" in kw and _looks_bad_label(kw.get("interval_label")):
        old_type = type(kw.get("interval_label")).__name__
        kw["interval_label"] = _label_from_kwargs(kw, default="-")
        logger.warning(
            "[SUMMARY DISPLAY LABEL GUARD] fixed bad keyword interval_label old_type=%s new=%s",
            old_type,
            kw.get("interval_label"),
        )

    # 第2 positional が interval_label の関数用。ここにDataFrameが入る事故を補正。
    if len(args_l) >= 2 and _looks_bad_label(args_l[1]):
        old_type = type(args_l[1]).__name__
        args_l[1] = _label_from_kwargs(kw, default="-")
        logger.warning(
            "[SUMMARY DISPLAY LABEL GUARD] fixed bad positional interval_label old_type=%s new=%s",
            old_type,
            args_l[1],
        )

    return tuple(args_l), kw


def _wrap(fn: Any, name: str):
    if not callable(fn):
        return fn
    if getattr(fn, "_summary_display_label_guard_v1", False):
        return fn

    def _wrapped(*args: Any, **kwargs: Any):
        a, kw = _normalize_args_kwargs(args, kwargs)
        return fn(*a, **kw)

    _wrapped._summary_display_label_guard_v1 = True  # type: ignore[attr-defined]
    _wrapped._original = fn  # type: ignore[attr-defined]
    _wrapped.__name__ = getattr(fn, "__name__", name)
    _wrapped.__qualname__ = getattr(fn, "__qualname__", name)
    _wrapped.__doc__ = getattr(fn, "__doc__", None)
    return _wrapped


def install() -> bool:
    global _PATCHED
    if _PATCHED:
        return True

    try:
        import scheduler_jobs.summary.display as disp
    except Exception:
        logger.exception("[SUMMARY DISPLAY LABEL GUARD] import display failed")
        return False

    names = [
        "print_summary_top10",
        "print_ranking_summary_top10",
        "print_push_summary",
        "print_ranking_summary",
        "display_ai_passed_summary",
        "display_summary",
        "display_push_summary",
        "display_ranking_summary",
    ]

    patched = []
    for name in names:
        try:
            old = getattr(disp, name, None)
            if callable(old):
                setattr(disp, name, _wrap(old, name))
                patched.append(name)
        except Exception:
            logger.debug("[SUMMARY DISPLAY LABEL GUARD] patch failed name=%s", name, exc_info=True)

    _PATCHED = bool(patched)
    logger.warning("[SUMMARY DISPLAY LABEL GUARD] installed=%s patched=%s", _PATCHED, patched)
    return _PATCHED


try:
    install()
except Exception:
    logger.exception("[SUMMARY DISPLAY LABEL GUARD] auto install failed")

__all__ = ["install"]
