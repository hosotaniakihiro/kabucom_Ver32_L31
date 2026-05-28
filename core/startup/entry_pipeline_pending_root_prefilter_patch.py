# ============================================================
# File   : core/startup/entry_pipeline_pending_root_prefilter_patch.py
# Version: V1-PIPELINE-PENDING-ROOT-PREFILTER
# ------------------------------------------------------------
# 目的:
#   entry_controller.run_entry_pipeline(pipeline_source=...) 実行時に、
#   pending_entries 全体を毎回全銘柄スキャンしない。
#
# 背景:
#   2026-05-28 10:31ログで、TONOSAMA pipeline 実行時にも
#   RANKING/SUMMARY pending が混在した pending_root 全体を走査し、
#   ENTRY PIPELINE BUCKET FILTER により各symbolで after=0 になっていた。
#   この不要スキャンで TONOSAMA が 19秒前後かかり、次回スケジュールに
#   previous_still_running を発生させやすい。
#
# 方針:
#   - run_entry_pipeline の呼び出し中だけ global_data.pending_entries を
#     pipeline_source / interval に一致する候補だけの一時rootに差し替える。
#   - 実行中にpopされた候補は、元rootにも反映する。
#   - 非対象のRANKING/SUMMARY/TONOSAMA候補は削除しない。
#   - entry_controller本体の大規模置換は避ける。
# ============================================================

from __future__ import annotations

import logging
import os
from copy import deepcopy
from typing import Any

logger = logging.getLogger(__name__)
_PATCHED = False
_ORIG_RUN = None


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return bool(default)
        return str(raw).strip().lower() in {"1", "true", "yes", "y", "on", "enable", "enabled"}
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


def _entry_key(entry: Any) -> tuple:
    """候補の同一性判定用key。created_at差異にもある程度耐える。"""
    if not isinstance(entry, dict):
        return (id(entry),)
    return (
        _norm_source(entry.get("source")),
        _norm_source(entry.get("entry_type")),
        _norm_source(entry.get("side") or entry.get("entry_decision")),
        _norm_interval(entry.get("interval")),
        str(entry.get("symbol") or entry.get("Symbol") or "").strip(),
        str(entry.get("created_at") or entry.get("datetime") or entry.get("time") or ""),
        str(entry.get("score") or entry.get("final_score") or ""),
    )


def _filter_root(root: Any, pipeline_source: str | None, interval: int | None) -> tuple[dict[str, list[dict]], int, int]:
    if not isinstance(root, dict) or not root:
        return {}, 0, 0
    out: dict[str, list[dict]] = {}
    before = 0
    after = 0
    for sym, bucket in list(root.items()):
        try:
            entries = list(bucket or [])
        except Exception:
            entries = []
        before += len(entries)
        kept = [e for e in entries if _entry_matches(e, pipeline_source, interval)]
        if kept:
            out[str(sym)] = kept
            after += len(kept)
    return out, before, after


def _reconcile_popped(original_root: dict, filtered_before: dict, filtered_after: dict) -> dict:
    """一時rootでpopされた候補を元rootからも削除する。"""
    if not isinstance(original_root, dict):
        return original_root
    try:
        for sym, before_bucket in filtered_before.items():
            before_keys = [_entry_key(e) for e in list(before_bucket or [])]
            after_keys = [_entry_key(e) for e in list((filtered_after or {}).get(sym, []) or [])]
            removed_keys = []
            tmp_after = list(after_keys)
            for k in before_keys:
                if k in tmp_after:
                    tmp_after.remove(k)
                else:
                    removed_keys.append(k)
            if not removed_keys:
                continue
            orig_bucket = list(original_root.get(sym, []) or [])
            new_bucket = []
            remove_pool = list(removed_keys)
            for e in orig_bucket:
                k = _entry_key(e)
                if k in remove_pool:
                    remove_pool.remove(k)
                    continue
                new_bucket.append(e)
            if new_bucket:
                original_root[sym] = new_bucket
            else:
                original_root.pop(sym, None)
    except Exception:
        logger.exception("[ENTRY PENDING ROOT PREFILTER] reconcile failed")
    return original_root


def _patched_run_entry_pipeline(*args, **kwargs):
    if not _env_bool("ENTRY_PIPELINE_PENDING_ROOT_PREFILTER", True):
        return _ORIG_RUN(*args, **kwargs)

    pipeline_source = kwargs.get("pipeline_source")
    interval = _norm_interval(kwargs.get("interval"))
    pipeline_source_n = _norm_source(pipeline_source)

    # source/interval指定が無い通常呼び出しは触らない。
    if not pipeline_source_n and interval is None:
        return _ORIG_RUN(*args, **kwargs)

    try:
        from global_state import global_data
    except Exception:
        logger.exception("[ENTRY PENDING ROOT PREFILTER] import global_data failed")
        return _ORIG_RUN(*args, **kwargs)

    original_root = getattr(global_data, "pending_entries", {})
    if not isinstance(original_root, dict) or not original_root:
        return _ORIG_RUN(*args, **kwargs)

    filtered_root, before_count, after_count = _filter_root(original_root, pipeline_source_n, interval)
    if before_count == after_count:
        return _ORIG_RUN(*args, **kwargs)

    logger.warning(
        "[ENTRY PENDING ROOT PREFILTER] apply pipeline_source=%s interval=%s symbols %s->%s entries %s->%s",
        pipeline_source_n,
        interval,
        len(original_root),
        len(filtered_root),
        before_count,
        after_count,
    )

    if after_count <= 0:
        # 対象sourceのpendingが無いなら、entry_controllerの重い全体scanを避ける。
        logger.info(
            "[ENTRY PENDING ROOT PREFILTER] no matching pending -> skip run pipeline_source=%s interval=%s original_symbols=%s",
            pipeline_source_n,
            interval,
            len(original_root),
        )
        return None

    filtered_before = deepcopy(filtered_root)
    try:
        global_data.pending_entries = filtered_root
        ret = _ORIG_RUN(*args, **kwargs)
        filtered_after = getattr(global_data, "pending_entries", filtered_root)
        new_root = _reconcile_popped(original_root, filtered_before, filtered_after)
        global_data.pending_entries = new_root
        try:
            logger.warning(
                "[ENTRY PENDING ROOT PREFILTER] restore pipeline_source=%s interval=%s root_symbols=%s filtered_after_symbols=%s",
                pipeline_source_n,
                interval,
                len(new_root or {}),
                len(filtered_after or {}) if isinstance(filtered_after, dict) else -1,
            )
        except Exception:
            pass
        return ret
    except Exception:
        try:
            global_data.pending_entries = original_root
        except Exception:
            pass
        raise


def install() -> bool:
    global _PATCHED, _ORIG_RUN
    if _PATCHED:
        return True
    try:
        import trading.handlers.entry_controller as ec
        cur = getattr(ec, "run_entry_pipeline", None)
        if not callable(cur):
            logger.warning("[ENTRY PENDING ROOT PREFILTER] run_entry_pipeline not callable")
            return False
        if getattr(cur, "_entry_pending_root_prefilter_v1", False):
            _PATCHED = True
            return True
        _ORIG_RUN = cur
        _patched_run_entry_pipeline._entry_pending_root_prefilter_v1 = True  # type: ignore[attr-defined]
        ec.run_entry_pipeline = _patched_run_entry_pipeline
        _PATCHED = True
        logger.warning("[ENTRY PENDING ROOT PREFILTER] installed enabled=%s", _env_bool("ENTRY_PIPELINE_PENDING_ROOT_PREFILTER", True))
        return True
    except Exception:
        logger.exception("[ENTRY PENDING ROOT PREFILTER] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[ENTRY PENDING ROOT PREFILTER] auto install failed")


__all__ = ["install"]
