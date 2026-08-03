# ============================================================
# File   : core/startup/summary_global_context_mtf_enrich_patch.py
# Version: V1-GLOBAL-CONTEXT-PUSH-1M-MTF-ENRICH
# ------------------------------------------------------------
# Purpose:
#   summary_memory_1m_enrich_patch は memory build / publish 経路では効くが、
#   Tonosama などが GlobalContext.get_push_merged_summary() 経由で読む場合、
#   cached merged summary の mtf / score_mtf / mtf_score が 0 のまま残ることがある。
#
#   PUSH 1分summary で score/slope が非ゼロなのに MTF 系だけ全0なら、
#   既存の enrich_memory_1m_summary() を保存時・取得時の両方に適用する。
#
# Important:
#   - main.py の PUSH DB保存は再開しない。
#   - 低変動/低流動性ガードは緩和しない。
# ============================================================
from __future__ import annotations

import logging
from typing import Any, Callable

import pandas as pd

logger = logging.getLogger(__name__)

VERSION = "V1-GLOBAL-CONTEXT-PUSH-1M-MTF-ENRICH"
_INSTALLED = False
_ORIG: dict[str, Callable[..., Any]] = {}


def _is_tf1(tf: Any) -> bool:
    try:
        return str(tf).strip().lower() in {"1", "1m", "1min"}
    except Exception:
        return False


def _is_push_source(source: Any) -> bool:
    try:
        return str(source or "push").strip().lower() in {"push", "push-cache", "push-legacy-attr"}
    except Exception:
        return False


def _nz(df: pd.DataFrame, col: str) -> int:
    try:
        if col not in df.columns:
            return 0
        s = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
        return int((s.abs() > 1e-12).sum())
    except Exception:
        return 0


def _needs_enrich(df: Any, tf: Any, source: Any) -> bool:
    try:
        if not _is_tf1(tf) or not _is_push_source(source):
            return False
        if not isinstance(df, pd.DataFrame) or df.empty:
            return False
        # score/slope はあるのに MTF 系だけ0なら補正対象。
        if _nz(df, "score") <= 0 and _nz(df, "slope") <= 0:
            return False
        return _nz(df, "mtf") <= 0 or _nz(df, "score_mtf") <= 0 or _nz(df, "mtf_score") <= 0
    except Exception:
        return False


def _enrich(df: Any, *, tf: Any, source: Any, reason: str) -> Any:
    try:
        if not _needs_enrich(df, tf, source):
            return df
        from core.startup.summary_memory_1m_enrich_patch import enrich_memory_1m_summary

        out = enrich_memory_1m_summary(df, reason=reason)
        logger.warning(
            "[SUMMARY GC MTF ENRICH] applied reason=%s tf=%s source=%s rows=%s before_mtf=%s after_mtf=%s before_score_mtf=%s after_score_mtf=%s version=%s",
            reason,
            tf,
            source,
            len(out) if isinstance(out, pd.DataFrame) else -1,
            _nz(df, "mtf") if isinstance(df, pd.DataFrame) else -1,
            _nz(out, "mtf") if isinstance(out, pd.DataFrame) else -1,
            _nz(df, "score_mtf") if isinstance(df, pd.DataFrame) else -1,
            _nz(out, "score_mtf") if isinstance(out, pd.DataFrame) else -1,
            VERSION,
        )
        return out
    except Exception:
        logger.exception("[SUMMARY GC MTF ENRICH] enrich failed reason=%s tf=%s source=%s", reason, tf, source)
        return df


def _store_merged(ctx: Any, tf: Any, source: Any, df: Any) -> None:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty or not _is_tf1(tf) or not _is_push_source(source):
            return
        lock = getattr(ctx, "_lock", None)
        if lock is None:
            return
        with lock:
            if hasattr(ctx, "_merged_by_source") and isinstance(ctx._merged_by_source, dict):
                ctx._merged_by_source.setdefault("push", {})[1] = df.copy()
            if hasattr(ctx, "push_summary_cache") and isinstance(ctx.push_summary_cache, dict):
                ctx.push_summary_cache[1] = df.copy()
            setter = getattr(ctx, "_set_legacy_push_attr", None)
            if callable(setter):
                setter(1, df)
    except Exception:
        logger.exception("[SUMMARY GC MTF ENRICH] store merged failed")


def _store_history(ctx: Any, tf: Any, source: Any, df: Any) -> None:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty or not _is_tf1(tf) or not _is_push_source(source):
            return
        lock = getattr(ctx, "_lock", None)
        if lock is None:
            return
        with lock:
            if hasattr(ctx, "summary_history_cache") and isinstance(ctx.summary_history_cache, dict):
                ctx.summary_history_cache[1] = df.copy()
    except Exception:
        logger.exception("[SUMMARY GC MTF ENRICH] store history failed")


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        import core.global_context.context as ctx_mod

        cls = getattr(ctx_mod, "GlobalContext", None)
        if cls is None:
            logger.warning("[SUMMARY GC MTF ENRICH] GlobalContext missing")
            return False

        cur = getattr(cls, "set_merged_summary", None)
        if callable(cur) and not getattr(cur, "_summary_gc_mtf_enrich_v1", False):
            _ORIG["set_merged_summary"] = cur

            def set_merged_summary(self: Any, tf: Any, df: Any, source: str = "push") -> Any:
                df2 = _enrich(df, tf=tf, source=source, reason="set_merged_summary")
                return _ORIG["set_merged_summary"](self, tf, df2, source)

            set_merged_summary._summary_gc_mtf_enrich_v1 = True  # type: ignore[attr-defined]
            set_merged_summary._original = cur  # type: ignore[attr-defined]
            cls.set_merged_summary = set_merged_summary

        cur = getattr(cls, "get_merged_summary", None)
        if callable(cur) and not getattr(cur, "_summary_gc_mtf_enrich_v1", False):
            _ORIG["get_merged_summary"] = cur

            def get_merged_summary(self: Any, tf: Any, source: str | None = None) -> pd.DataFrame:
                df = _ORIG["get_merged_summary"](self, tf, source)
                df2 = _enrich(df, tf=tf, source=source or "push", reason="get_merged_summary")
                if df2 is not df:
                    _store_merged(self, tf, source or "push", df2)
                return df2

            get_merged_summary._summary_gc_mtf_enrich_v1 = True  # type: ignore[attr-defined]
            get_merged_summary._original = cur  # type: ignore[attr-defined]
            cls.get_merged_summary = get_merged_summary

        cur = getattr(cls, "set_summary_history", None)
        if callable(cur) and not getattr(cur, "_summary_gc_mtf_enrich_v1", False):
            _ORIG["set_summary_history"] = cur

            def set_summary_history(self: Any, tf: Any, df: Any, source: str = "push") -> Any:
                df2 = _enrich(df, tf=tf, source=source, reason="set_summary_history")
                return _ORIG["set_summary_history"](self, tf, df2, source)

            set_summary_history._summary_gc_mtf_enrich_v1 = True  # type: ignore[attr-defined]
            set_summary_history._original = cur  # type: ignore[attr-defined]
            cls.set_summary_history = set_summary_history

        cur = getattr(cls, "get_summary_history", None)
        if callable(cur) and not getattr(cur, "_summary_gc_mtf_enrich_v1", False):
            _ORIG["get_summary_history"] = cur

            def get_summary_history(self: Any, tf: Any, source: str = "push") -> pd.DataFrame:
                df = _ORIG["get_summary_history"](self, tf, source)
                df2 = _enrich(df, tf=tf, source=source, reason="get_summary_history")
                if df2 is not df:
                    _store_history(self, tf, source, df2)
                return df2

            get_summary_history._summary_gc_mtf_enrich_v1 = True  # type: ignore[attr-defined]
            get_summary_history._original = cur  # type: ignore[attr-defined]
            cls.get_summary_history = get_summary_history

        cur = getattr(cls, "set_push_summary", None)
        if callable(cur) and not getattr(cur, "_summary_gc_mtf_enrich_v1", False):
            _ORIG["set_push_summary"] = cur

            def set_push_summary(self: Any, tf: Any, df: Any) -> Any:
                df2 = _enrich(df, tf=tf, source="push", reason="set_push_summary")
                return _ORIG["set_push_summary"](self, tf, df2)

            set_push_summary._summary_gc_mtf_enrich_v1 = True  # type: ignore[attr-defined]
            set_push_summary._original = cur  # type: ignore[attr-defined]
            cls.set_push_summary = set_push_summary

        cur = getattr(cls, "get_push_summary", None)
        if callable(cur) and not getattr(cur, "_summary_gc_mtf_enrich_v1", False):
            _ORIG["get_push_summary"] = cur

            def get_push_summary(self: Any, tf: Any) -> Any:
                df = _ORIG["get_push_summary"](self, tf)
                df2 = _enrich(df, tf=tf, source="push", reason="get_push_summary")
                if df2 is not df:
                    _store_merged(self, tf, "push", df2)
                return df2

            get_push_summary._summary_gc_mtf_enrich_v1 = True  # type: ignore[attr-defined]
            get_push_summary._original = cur  # type: ignore[attr-defined]
            cls.get_push_summary = get_push_summary

        _INSTALLED = True
        logger.warning("[SUMMARY GC MTF ENRICH] installed version=%s", VERSION)
        return True
    except Exception:
        logger.exception("[SUMMARY GC MTF ENRICH] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY GC MTF ENRICH] auto install failed")


__all__ = ["install", "VERSION"]
