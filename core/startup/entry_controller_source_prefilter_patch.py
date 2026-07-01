# ============================================================
# File   : core/startup/entry_controller_source_prefilter_patch.py
# Version: V4-SUMMARY-AI-SUMMARY-COMPAT-VOL-RESCUE-SLOPE-RELAX
# ------------------------------------------------------------
# 目的:
#   entry_controller._build_scored_candidates() は entries[:MAX_CANDIDATES_PER_SYMBOL]
#   を先に切ってから pipeline_source/interval を照合する。
#
#   pending_root に SUMMARY/RANKING/TONOSAMA が混在していると、実行元と違う候補が
#   先頭10件を占有し、PIPELINE_FILTER_MISMATCH を大量に出したり、正しい候補が
#   後ろにあるのに評価されない。
#
# V4:
#   - V3の SUMMARY_AI/SUMMARY 互換と vol rescue install を維持。
#   - SUMMARY_AI VOL RESCUE の slope default を 0.001 -> 0.0002 に緩和。
#     7012/3436/6754 のように buy_score=4 かつ高売買代金でも、1分ATRだけで
#     落ちるケースを救済する。
# ============================================================

from __future__ import annotations

import copy
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_INSTALLED = False
_ORIGINAL_BUILD = None
_ORIGINAL_MATCHES = None
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


def _source_compatible(entry_source: Any, pipeline_source: str | None) -> bool:
    ps = _norm_source(pipeline_source)
    es = _norm_source(entry_source)
    if not ps:
        return True
    if es == ps:
        return True
    if ps == "SUMMARY_AI" and es in {"SUMMARY", "PUSH", ""}:
        return True
    if ps == "SUMMARY" and es == "SUMMARY_AI":
        return True
    return False


def _matches(entry: Any, pipeline_source: str | None, interval: int | None) -> bool:
    try:
        if not isinstance(entry, dict):
            return False
        if pipeline_source and not _source_compatible(entry.get("source"), pipeline_source):
            return False
        if interval is not None:
            ent_i = _norm_interval(entry.get("interval"))
            if ent_i is not None and ent_i != int(interval):
                return False
        return True
    except Exception:
        return False


def _normalize_entry_for_pipeline(entry: Any, pipeline_source: str | None) -> Any:
    if not isinstance(entry, dict):
        return entry
    ps = _norm_source(pipeline_source)
    if ps != "SUMMARY_AI":
        return entry
    es = _norm_source(entry.get("source"))
    et = _norm_source(entry.get("entry_type"))
    if es in {"SUMMARY", "PUSH", ""} or et in {"SUMMARY", "PUSH", ""}:
        e = copy.deepcopy(entry)
        e["source"] = "SUMMARY_AI"
        e["entry_type"] = "SUMMARY_AI"
        try:
            row = e.get("entry_row")
            if isinstance(row, dict):
                row["source"] = "SUMMARY_AI"
                row["entry_type"] = "SUMMARY_AI"
        except Exception:
            pass
        return e
    return entry


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


def _patched_entry_matches_pipeline(entry: dict, pipeline_source: str | None, interval: int | None) -> bool:
    try:
        return _matches(entry, pipeline_source, _norm_interval(interval))
    except Exception:
        logger.exception("[ENTRY SOURCE PREFILTER] patched _entry_matches_pipeline failed")
        if callable(_ORIGINAL_MATCHES):
            return bool(_ORIGINAL_MATCHES(entry, pipeline_source, interval))
        return False


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
            normalized = [_normalize_entry_for_pipeline(e, pipeline_source) for e in filtered]
            if len(filtered) != len(entries):
                skipped = [_describe(e) for e in entries if not _matches(e, pipeline_source, interval)][:20]
                logger.warning(
                    "[ENTRY SOURCE PREFILTER] symbol=%s source=%s interval=%s before=%s after=%s skipped=%s compat=summary_ai_summary",
                    symbol,
                    pipeline_source,
                    interval,
                    len(entries),
                    len(filtered),
                    skipped,
                )
            elif _norm_source(pipeline_source) == "SUMMARY_AI":
                logger.info(
                    "[ENTRY SOURCE PREFILTER] symbol=%s source=%s interval=%s compatible candidates=%s normalized_summary_to_summary_ai=True",
                    symbol,
                    pipeline_source,
                    interval,
                    len(normalized),
                )
            if not normalized:
                return []

            if "entries" in kwargs:
                kwargs["entries"] = normalized
                return _ORIGINAL_BUILD(*args, **kwargs)

            args2 = list(args)
            if len(args2) >= 2:
                args2[1] = normalized
                return _ORIGINAL_BUILD(*args2, **kwargs)

        return _ORIGINAL_BUILD(*args, **kwargs)
    except Exception:
        logger.exception("[ENTRY SOURCE PREFILTER] failed -> call original")
        return _ORIGINAL_BUILD(*args, **kwargs)


def _install_summary_ai_vol_rescue() -> bool:
    try:
        # Force before importing/installing rescue so setdefault in the rescue module keeps this value.
        os.environ["SUMMARY_AI_VOL_RESCUE_MIN_ABS_SLOPE"] = os.getenv(
            "SUMMARY_AI_VOL_RESCUE_MIN_ABS_SLOPE_OPERATOR",
            "0.0002",
        )
        from core.startup import summary_ai_volatility_rescue_patch as p
        fn = getattr(p, "install", None)
        ok = bool(fn()) if callable(fn) else False
        logger.warning(
            "[ENTRY SOURCE PREFILTER] summary_ai_volatility_rescue installed=%s min_abs_slope=%s",
            ok,
            os.getenv("SUMMARY_AI_VOL_RESCUE_MIN_ABS_SLOPE"),
        )
        return ok
    except Exception:
        logger.exception("[ENTRY SOURCE PREFILTER] summary_ai_volatility_rescue install failed")
        return False


def install() -> bool:
    global _INSTALLED, _ORIGINAL_BUILD, _ORIGINAL_MATCHES
    try:
        import trading.handlers.entry_controller as ec

        cur = getattr(ec, "_build_scored_candidates", None)
        if not callable(cur):
            logger.warning("[ENTRY SOURCE PREFILTER] target missing")
            _install_summary_ai_vol_rescue()
            return False

        if not getattr(cur, "_entry_source_prefilter_patch_v4", False):
            base = getattr(cur, "_original", cur)
            _ORIGINAL_BUILD = base
            _patched_build_scored_candidates._entry_source_prefilter_patch = True  # type: ignore[attr-defined]
            _patched_build_scored_candidates._entry_source_prefilter_patch_v2 = True  # type: ignore[attr-defined]
            _patched_build_scored_candidates._entry_source_prefilter_patch_v3 = True  # type: ignore[attr-defined]
            _patched_build_scored_candidates._entry_source_prefilter_patch_v4 = True  # type: ignore[attr-defined]
            _patched_build_scored_candidates._original = base  # type: ignore[attr-defined]
            ec._build_scored_candidates = _patched_build_scored_candidates

        match_cur = getattr(ec, "_entry_matches_pipeline", None)
        if callable(match_cur) and not getattr(match_cur, "_entry_source_prefilter_match_v4", False):
            _ORIGINAL_MATCHES = getattr(match_cur, "_original", match_cur)
            _patched_entry_matches_pipeline._entry_source_prefilter_match_v2 = True  # type: ignore[attr-defined]
            _patched_entry_matches_pipeline._entry_source_prefilter_match_v3 = True  # type: ignore[attr-defined]
            _patched_entry_matches_pipeline._entry_source_prefilter_match_v4 = True  # type: ignore[attr-defined]
            _patched_entry_matches_pipeline._original = _ORIGINAL_MATCHES  # type: ignore[attr-defined]
            ec._entry_matches_pipeline = _patched_entry_matches_pipeline

        vol_ok = _install_summary_ai_vol_rescue()
        _INSTALLED = True
        logger.warning(
            "[ENTRY SOURCE PREFILTER] installed v4 enabled=%s summary_ai_accepts_summary=True patched_match=True vol_rescue=%s min_abs_slope=%s",
            _env_bool("ENTRY_CONTROLLER_SOURCE_PREFILTER_ENABLED", True),
            vol_ok,
            os.getenv("SUMMARY_AI_VOL_RESCUE_MIN_ABS_SLOPE"),
        )
        return True
    except Exception:
        logger.exception("[ENTRY SOURCE PREFILTER] install failed")
        _install_summary_ai_vol_rescue()
        return False


try:
    install()
except Exception:
    logger.exception("[ENTRY SOURCE PREFILTER] auto install failed")


__all__ = ["install"]
