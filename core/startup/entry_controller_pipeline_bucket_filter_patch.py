# ============================================================
# File   : core/startup/entry_controller_pipeline_bucket_filter_patch.py
# Version: Ver01-PIPELINE-BUCKET-PREFILTER
# ------------------------------------------------------------
# entry_controller の pending bucket に interval違いの候補が混在し、
# MAX_CANDIDATES_PER_SYMBOL の先頭枠を別intervalが消費して
# PIPELINE_FILTER_MISMATCH ばかりになる問題を軽減する。
#
# 方針:
#   - run_entry_pipeline 呼び出し中だけ、get_bucket(symbol) の戻り値を
#     pipeline_source / interval に一致する pending へ事前フィルタする。
#   - interval=3 実行時は interval=3 の候補だけ entry_controller に見せる。
#   - interval=1 の候補は削除せず、次の interval=1 pipeline 用に残す。
#
# ENV:
#   ENTRY_PIPELINE_BUCKET_PREFILTER=1
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_INSTALLED = False
_ORIG_RUN = None
_ORIG_GET_BUCKET = None
_CTX: dict[str, Any] = {"active": False, "pipeline_source": None, "interval": None}


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok"}
    except Exception:
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


def _entry_matches(entry: Any, pipeline_source: str | None, interval: int | None) -> bool:
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


def _patched_get_bucket(symbol):
    bucket = _ORIG_GET_BUCKET(symbol) if callable(_ORIG_GET_BUCKET) else []
    if not _env_bool("ENTRY_PIPELINE_BUCKET_PREFILTER", True):
        return bucket
    if not _CTX.get("active"):
        return bucket

    pipeline_source = _CTX.get("pipeline_source")
    interval = _CTX.get("interval")
    if not pipeline_source and interval is None:
        return bucket

    try:
        filtered = [e for e in list(bucket or []) if _entry_matches(e, pipeline_source, interval)]
        if len(filtered) != len(bucket or []):
            logger.warning(
                "[ENTRY PIPELINE BUCKET FILTER] symbol=%s before=%s after=%s pipeline_source=%s interval=%s skipped=%s",
                symbol,
                len(bucket or []),
                len(filtered),
                pipeline_source,
                interval,
                [
                    {
                        "source": e.get("source"),
                        "entry_type": e.get("entry_type"),
                        "side": e.get("side"),
                        "interval": e.get("interval"),
                    }
                    for e in list(bucket or [])
                    if isinstance(e, dict) and not _entry_matches(e, pipeline_source, interval)
                ][:10],
            )
        return filtered
    except Exception:
        logger.exception("[ENTRY PIPELINE BUCKET FILTER] failed symbol=%s", symbol)
        return bucket


def _patched_run_entry_pipeline(*args, **kwargs):
    pipeline_source = kwargs.get("pipeline_source")
    interval = _norm_interval(kwargs.get("interval"))
    old = dict(_CTX)
    try:
        _CTX["active"] = True
        _CTX["pipeline_source"] = pipeline_source
        _CTX["interval"] = interval
        return _ORIG_RUN(*args, **kwargs)
    finally:
        _CTX.clear()
        _CTX.update(old)


def install() -> bool:
    global _INSTALLED, _ORIG_RUN, _ORIG_GET_BUCKET
    if _INSTALLED:
        return True
    try:
        import trading.handlers.entry_controller as ec
        import trading.entry.pending_manager as pm

        cur_run = getattr(ec, "run_entry_pipeline", None)
        if getattr(cur_run, "_entry_pipeline_bucket_filter_v1", False):
            _INSTALLED = True
            return True

        _ORIG_RUN = cur_run
        _ORIG_GET_BUCKET = getattr(pm, "get_bucket", None)

        _patched_run_entry_pipeline._entry_pipeline_bucket_filter_v1 = True  # type: ignore[attr-defined]
        _patched_get_bucket._entry_pipeline_bucket_filter_v1 = True  # type: ignore[attr-defined]

        ec.run_entry_pipeline = _patched_run_entry_pipeline
        # entry_controller は from pending_manager import get_bucket 済みなので、ec側参照も差し替える。
        ec.get_bucket = _patched_get_bucket

        _INSTALLED = True
        logger.warning("[ENTRY PIPELINE BUCKET FILTER] installed enabled=%s", _env_bool("ENTRY_PIPELINE_BUCKET_PREFILTER", True))
        return True
    except Exception as e:
        logger.exception("[ENTRY PIPELINE BUCKET FILTER] install failed err=%s", e)
        return False

try:
    install()
except Exception as e:
    logger.exception("[ENTRY PIPELINE BUCKET FILTER] auto install failed err=%s", e)

__all__ = ["install"]
