# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
import os
from collections import Counter
from typing import Any, Iterable, Sequence

logger = logging.getLogger(__name__)

VERSION = "V1-SUMMARY-AI-EXECUTOR-DIAGNOSTICS"
_INSTALLED = False
_ORIGINAL_EXECUTE = None
_ORIGINAL_BUILD_APPROVED = None


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return bool(default)
        return str(raw).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _norm_symbol(v: Any) -> str:
    try:
        s = str(v or "").strip()
        if s.endswith(".0") and s[:-2].isdigit():
            return s[:-2]
        return s
    except Exception:
        return ""


def _symbol_from_row(row: Any) -> str:
    if not isinstance(row, dict):
        return ""
    for d in (row, row.get("ai_row"), row.get("source_row")):
        if isinstance(d, dict):
            for key in ("symbol", "Symbol", "銘柄コード", "code", "stock_code"):
                sym = _norm_symbol(d.get(key))
                if sym:
                    return sym
    return ""


def _side_from_row(row: Any) -> str:
    if not isinstance(row, dict):
        return ""
    for d in (row, row.get("ai_row"), row.get("source_row")):
        if isinstance(d, dict):
            for key in ("side", "ai_side", "entry_decision"):
                side = str(d.get(key) or "").strip().upper()
                if side in {"BUY", "SELL"}:
                    return side
    return ""


def _iter_dicts_deep(obj: Any, *, depth: int = 0) -> Iterable[dict[str, Any]]:
    if depth > 6:
        return
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from _iter_dicts_deep(value, depth=depth + 1)
    elif isinstance(obj, (list, tuple, set)):
        for value in obj:
            yield from _iter_dicts_deep(value, depth=depth + 1)


def _reason_counts(result: Any) -> dict[str, int]:
    counts: Counter[str] = Counter()
    try:
        for d in _iter_dicts_deep(result):
            for key in ("skip_reason", "reason", "error"):
                value = d.get(key)
                if value is not None and str(value).strip() != "":
                    counts[str(value).strip()] += 1
            skipped = d.get("skipped")
            if isinstance(skipped, dict):
                for key, value in skipped.items():
                    try:
                        n = int(value or 0)
                    except Exception:
                        n = 1 if value else 0
                    if n > 0:
                        counts[str(key)] += n
            if isinstance(d.get("detail"), dict):
                detail = d.get("detail") or {}
                for key in ("reason", "skip_reason", "error"):
                    value = detail.get(key)
                    if value is not None and str(value).strip() != "":
                        counts[str(value).strip()] += 1
    except Exception:
        logger.debug("[SUMMARY AI EXECUTOR DIAG] reason count failed", exc_info=True)
    return dict(counts.most_common(20))


def _sample_rows(rows: Sequence[Any] | None, limit: int = 20) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in list(rows or [])[:limit]:
        if not isinstance(row, dict):
            continue
        out.append({
            "symbol": _symbol_from_row(row),
            "side": _side_from_row(row),
            "allow": bool(row.get("allow")),
            "conf": row.get("confidence"),
            "score": row.get("score_total", row.get("final_score", row.get("score"))),
            "reason": row.get("reason") or row.get("skip_reason"),
        })
    return out


def _df_rows(df: Any) -> int:
    try:
        return int(len(df))
    except Exception:
        return -1


def _install_build_approved_diag(executor: Any) -> bool:
    global _ORIGINAL_BUILD_APPROVED
    old = getattr(executor, "build_ai_ok_approved_rows", None)
    if not callable(old):
        return False
    if getattr(old, "_summary_ai_executor_diag_v1", False):
        return True
    _ORIGINAL_BUILD_APPROVED = old

    def _patched_build_ai_ok_approved_rows(ai_results, *args, **kwargs):
        ai_results_list = list(ai_results or [])
        ok_items = [x for x in ai_results_list if isinstance(x, dict) and bool(x.get("allow"))]
        try:
            logger.warning(
                "[SUMMARY AI EXECUTOR DIAG] received ai_results=%s ai_ok=%s max_entries=%s ai_ok_sample=%s version=%s",
                len(ai_results_list),
                len(ok_items),
                kwargs.get("max_entries"),
                _sample_rows(ok_items, 12),
                VERSION,
            )
        except Exception:
            logger.debug("[SUMMARY AI EXECUTOR DIAG] pre approved log failed", exc_info=True)
        approved = _ORIGINAL_BUILD_APPROVED(ai_results_list, *args, **kwargs)
        try:
            logger.warning(
                "[SUMMARY AI EXECUTOR DIAG] approved_after_prefilter=%s approved_symbols=%s version=%s",
                len(approved or []),
                _sample_rows(approved, 20),
                VERSION,
            )
        except Exception:
            logger.debug("[SUMMARY AI EXECUTOR DIAG] post approved log failed", exc_info=True)
        return approved

    _patched_build_ai_ok_approved_rows._summary_ai_executor_diag_v1 = True  # type: ignore[attr-defined]
    _patched_build_ai_ok_approved_rows._original = old  # type: ignore[attr-defined]
    executor.build_ai_ok_approved_rows = _patched_build_ai_ok_approved_rows
    return True


def _install_execute_diag(executor: Any) -> bool:
    global _ORIGINAL_EXECUTE
    old = getattr(executor, "execute_ai_ok_entries_bulk", None)
    if not callable(old):
        return False
    if getattr(old, "_summary_ai_executor_diag_v1", False):
        return True
    _ORIGINAL_EXECUTE = old

    def _patched_execute_ai_ok_entries_bulk(ai_results, *args, **kwargs):
        ai_results_list = list(ai_results or [])
        ok_items = [x for x in ai_results_list if isinstance(x, dict) and bool(x.get("allow"))]
        interval = kwargs.get("interval", 1)
        max_entries = kwargs.get("max_entries")
        dry_run = kwargs.get("dry_run")
        df_summary = kwargs.get("df_summary")
        if df_summary is None and args:
            # The native signature receives df_summary as a keyword, but keep this safe for wrappers.
            for arg in args:
                if hasattr(arg, "columns") and hasattr(arg, "__len__"):
                    df_summary = arg
                    break
        logger.warning(
            "[SUMMARY AI EXECUTOR DIAG] execute_start ai_results=%s ai_ok=%s interval=%s max_entries=%s dry_run=%s df_rows=%s ai_ok_symbols=%s version=%s",
            len(ai_results_list),
            len(ok_items),
            interval,
            max_entries,
            dry_run,
            _df_rows(df_summary),
            _sample_rows(ok_items, 20),
            VERSION,
        )
        try:
            result = _ORIGINAL_EXECUTE(ai_results_list, *args, **kwargs)
        except Exception:
            logger.exception(
                "[SUMMARY AI EXECUTOR DIAG] execute_exception ai_ok=%s interval=%s max_entries=%s version=%s",
                len(ok_items), interval, max_entries, VERSION,
            )
            raise
        try:
            approved = []
            all_approved = []
            executed = None
            skip_reason = None
            pending_removed = None
            if isinstance(result, dict):
                approved = result.get("approved_rows") or []
                all_approved = result.get("all_approved_rows") or approved
                executed = result.get("executed")
                skip_reason = result.get("skip_reason")
                pending_removed = result.get("pending_removed")
            logger.warning(
                "[SUMMARY AI EXECUTOR DIAG] order_result executed=%s skip=%s approved_last=%s approved_total=%s pending_removed=%s reason_counts=%s approved_symbols=%s detail=%s version=%s",
                executed,
                skip_reason,
                len(approved or []),
                len(all_approved or []),
                pending_removed,
                _reason_counts(result),
                _sample_rows(all_approved, 30),
                result if isinstance(result, dict) else type(result).__name__,
                VERSION,
            )
        except Exception:
            logger.debug("[SUMMARY AI EXECUTOR DIAG] result log failed", exc_info=True)
        return result

    _patched_execute_ai_ok_entries_bulk._summary_ai_executor_diag_v1 = True  # type: ignore[attr-defined]
    _patched_execute_ai_ok_entries_bulk._original = old  # type: ignore[attr-defined]
    executor.execute_ai_ok_entries_bulk = _patched_execute_ai_ok_entries_bulk
    return True


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    if not _env_bool("SUMMARY_AI_EXECUTOR_DIAGNOSTICS_ENABLED", True):
        logger.warning("[SUMMARY AI EXECUTOR DIAG] disabled by env version=%s", VERSION)
        return False
    try:
        import trading.entry.summary_ai.executor as executor
        build_ok = _install_build_approved_diag(executor)
        execute_ok = _install_execute_diag(executor)
        _INSTALLED = bool(build_ok or execute_ok)
        logger.warning(
            "[SUMMARY AI EXECUTOR DIAG] installed build=%s execute=%s version=%s",
            build_ok, execute_ok, VERSION,
        )
        return _INSTALLED
    except Exception:
        logger.exception("[SUMMARY AI EXECUTOR DIAG] install failed version=%s", VERSION)
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI EXECUTOR DIAG] auto install failed")


__all__ = ["install", "VERSION"]
