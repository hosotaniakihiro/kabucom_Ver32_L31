# ============================================================
# File   : core/startup/discord_summary_kwarg_safety_patch.py
# Version: V1.0-DISPLAY-KWARG-SAFETY
# ------------------------------------------------------------
# 目的:
#   discord_summary_display_compact_patch 経由で
#   scheduler_jobs.summary.display.print_summary_top10() へ interval=1 が
#   渡され、TypeError で表示が失敗する問題を防ぐ。
#
# エラー:
#   TypeError: print_summary_top10() got an unexpected keyword argument 'interval'
#
# 対策:
#   - print_summary_top10 / print_ranking_summary_top10 / display系入口を
#     さらに外側から包む
#   - interval / interval_min / minutes / tf は interval_label に変換
#   - 下位関数へは interval 系kwargsを渡さない
# ============================================================

from __future__ import annotations

import inspect
import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)
_PATCHED = False
_ORIGINALS: dict[str, Callable] = {}


def _is_df_like(v: Any) -> bool:
    try:
        import pandas as pd
        return isinstance(v, pd.DataFrame)
    except Exception:
        return False


def _label_from_value(v: Any) -> str | None:
    if v is None:
        return None
    if _is_df_like(v):
        return None
    try:
        return f"{int(float(v))}min"
    except Exception:
        s = str(v).strip()
        if not s or len(s) > 40 or "\n" in s:
            return None
        return s


def _normalize(summary_df: Any, interval_label: Any, kwargs: dict[str, Any]) -> tuple[Any, str, dict[str, Any]]:
    kw = dict(kwargs or {})

    # display_xxx(label, df) の順序事故を救済
    if _is_df_like(interval_label) and not _is_df_like(summary_df):
        label = _label_from_value(summary_df) or "1min"
        summary_df = interval_label
    else:
        label = None
        for k in ("interval_label", "interval", "interval_min", "minutes", "tf"):
            if k in kw:
                label = _label_from_value(kw.get(k))
                if label:
                    break
        label = label or _label_from_value(interval_label) or "1min"

    for k in ("interval", "interval_min", "minutes", "tf", "interval_label"):
        kw.pop(k, None)
    return summary_df, label, kw


def _filter_kwargs(fn: Callable, kwargs: dict[str, Any]) -> dict[str, Any]:
    try:
        sig = inspect.signature(fn)
        params = sig.parameters
        if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
            return dict(kwargs or {})
        allowed = {
            name for name, p in params.items()
            if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
        }
        return {k: v for k, v in (kwargs or {}).items() if k in allowed}
    except Exception:
        return {}


def _safe_call(fn: Callable, summary_df: Any, interval_label: str, notify_discord: bool, kwargs: dict[str, Any]):
    safe_kwargs = _filter_kwargs(fn, kwargs)
    try:
        return fn(summary_df, interval_label=interval_label, notify_discord=notify_discord, **safe_kwargs)
    except TypeError as e:
        if "unexpected keyword argument" in str(e):
            logger.warning(
                "[DISCORD KWARG SAFETY] retry without optional kwargs fn=%s err=%s keys=%s",
                getattr(fn, "__name__", str(fn)), e, list(safe_kwargs.keys()),
            )
            return fn(summary_df, interval_label=interval_label, notify_discord=notify_discord)
        raise


def _wrap(fn: Callable) -> Callable:
    def _wrapped(summary_df=None, interval_label="1min", *, notify_discord=True, **kwargs):
        summary_df, interval_label, kwargs = _normalize(summary_df, interval_label, kwargs)
        return _safe_call(fn, summary_df, interval_label, notify_discord, kwargs)

    _wrapped._discord_kwarg_safety_patch = True  # type: ignore[attr-defined]
    _wrapped._original = fn  # type: ignore[attr-defined]
    return _wrapped


def install() -> bool:
    global _PATCHED
    if _PATCHED:
        return True
    try:
        import scheduler_jobs.summary.display as disp

        patched = 0
        for name in (
            "print_summary_top10",
            "print_ranking_summary_top10",
            "display_summary",
            "display_push_summary",
            "print_push_summary",
            "display_ranking_summary",
            "print_ranking_summary",
        ):
            fn = getattr(disp, name, None)
            if callable(fn) and not getattr(fn, "_discord_kwarg_safety_patch", False):
                _ORIGINALS[name] = fn
                setattr(disp, name, _wrap(fn))
                patched += 1

        _PATCHED = True
        logger.warning("[DISCORD KWARG SAFETY] installed V1 patched=%s", patched)
        return True
    except Exception:
        logger.exception("[DISCORD KWARG SAFETY] install failed")
        return False


__all__ = ["install"]
