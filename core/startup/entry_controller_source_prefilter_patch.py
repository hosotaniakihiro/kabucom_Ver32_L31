# ============================================================
# File   : core/startup/entry_controller_source_prefilter_patch.py
# Version: V1-SOURCE-PREFILTER-BEFORE-CANDIDATE-SLICE
# ------------------------------------------------------------
# 目的:
#   entry_controller._build_scored_candidates() は entries[:MAX_CANDIDATES_PER_SYMBOL]
#   を先に切ってから pipeline_source/interval を照合する。
#
#   pending_root に SUMMARY/RANKING/TONOSAMA が混在していると、実行元と違う候補が
#   先頭10件を占有し、PIPELINE_FILTER_MISMATCH を大量に出したり、正しい候補が
#   後ろにあるのに評価されない。
#
# 対策:
#   - _build_scored_candidates() 呼び出し前に source/interval で候補を事前抽出する。
#   - 一致しない候補は削除しない。次の該当pipeline用に残す。
#   - これにより SUMMARY 実行が RANKING/TONOSAMA pending を消費しない。
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_INSTALLED = False
_ORIGINAL_BUILD = None
_TRUE = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
_FALSE = {"0", "false", "no", "n", "off", "ng", "disable", "disabled"}


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        s = str(v).strip().lower()
        if s in _TRUE:
            return True
        if s in _FALSE:
            return False
    except Exception:
        pass
    return bool(default)


def _norm_source(v: Any) -> str:
    try:
        return str(v or "").strip().upper()
    except Exception:
        return ""


def _norm_interval(v: Any) -> int | None:
    try:
        if v is None or str(v).strip() == "":
            return None
        return int(float(v))
    except Exception:
        return None


def _matches(entry: Any, pipeline_source: str | None, interval: int | None) -> bool:
    try:
        if not isinstance(entry, dict):
            return False
        if pipeline_source:
            if _norm_source(entry.get("source")) != _norm_source(pipeline_source):
                return False
        if interval is not None:
            ent_i = _norm_interval(entry.get("interval"))
            if ent_i is not None and ent_i != int(interval):
                return False
        return True
    except Exception:
        return False


def _describe(e: Any) -> dict[str, Any]:
    if not isinstance(e, dict):
        return {"type": type(e).__name__}
    return {
        "source": e.get("source"),
        "entry_type": e.get("entry_type"),
        "side": e.get("side"),
        "interval": e.get("interval"),
        "score": e.get("score"),
    }


def _patched_build_scored_candidates(*args, **kwargs):
    if not _env_bool("ENTRY_CONTROLLER_SOURCE_PREFILTER_ENABLED", True):
        return _ORIGINAL_BUILD(*args, **kwargs)

    try:
        entries = kwargs.get("entries")
        pipeline_source = kwargs.get("pipeline_source")
        interval = _norm_interval(kwargs.get("interval"))
        symbol = kwargs.get("symbol") or (args[0] if args else "")

        if entries is None and len(args) >= 2:
            entries = args[1]

        if isinstance(entries, list) and (pipeline_source or interval is not None):
            filtered = [e for e in entries if _matches(e, pipeline_source, interval)]
            if len(filtered) != len(entries):
                skipped = [_describe(e) for e in entries if not _matches(e, pipeline_source, interval)][:20]
                logger.warning(
                    "[ENTRY SOURCE PREFILTER] symbol=%s source=%s interval=%s before=%s after=%s skipped=%s",
                    symbol,
                    pipeline_source,
                    interval,
                    len(entries),
                    len(filtered),
                    skipped,
                )
            if not filtered:
                return []

            if "entries" in kwargs:
                kwargs["entries"] = filtered
                return _ORIGINAL_BUILD(*args, **kwargs)

            args2 = list(args)
            if len(args2) >= 2:
                args2[1] = filtered
                return _ORIGINAL_BUILD(*args2, **kwargs)

        return _ORIGINAL_BUILD(*args, **kwargs)
    except Exception:
        logger.exception("[ENTRY SOURCE PREFILTER] failed -> call original")
        return _ORIGINAL_BUILD(*args, **kwargs)


def install() -> bool:
    global _INSTALLED, _ORIGINAL_BUILD
    if _INSTALLED:
        return True
    try:
        import trading.handlers.entry_controller as ec

        cur = getattr(ec, "_build_scored_candidates", None)
        if not callable(cur):
            logger.warning("[ENTRY SOURCE PREFILTER] target missing")
            return False
        if getattr(cur, "_entry_source_prefilter_patch", False):
            _INSTALLED = True
            return True

        _ORIGINAL_BUILD = cur
        _patched_build_scored_candidates._entry_source_prefilter_patch = True  # type: ignore[attr-defined]
        _patched_build_scored_candidates._original = cur  # type: ignore[attr-defined]
        ec._build_scored_candidates = _patched_build_scored_candidates
        _INSTALLED = True
        logger.warning("[ENTRY SOURCE PREFILTER] installed v1 enabled=%s", _env_bool("ENTRY_CONTROLLER_SOURCE_PREFILTER_ENABLED", True))
        return True
    except Exception:
        logger.exception("[ENTRY SOURCE PREFILTER] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[ENTRY SOURCE PREFILTER] auto install failed")


__all__ = ["install"]
