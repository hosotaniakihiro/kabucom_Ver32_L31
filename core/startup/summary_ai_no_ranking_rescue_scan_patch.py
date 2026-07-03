# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V1-SUMMARY-AI-NO-RANKING-RESCUE-DB-SCAN"
_INSTALLED = False
_ORIG_LOOKS_RANKING_ENTRY = None
_ORIG_RANKING_MOVE_RESCUE = None

_SUMMARY_TOKENS = ("summary", "summary_ai", "push", "stock_summary", "src=summary")
_RANKING_TOKENS = ("ranking", "ランキング", "rank_type", "ranking_type")


def _row_to_dict(row: Any) -> dict[str, Any]:
    try:
        if row is None:
            return {}
        if isinstance(row, dict):
            return dict(row)
        if hasattr(row, "to_dict"):
            v = row.to_dict()
            if isinstance(v, dict):
                return dict(v)
    except Exception:
        pass
    return {}


def _is_summary_ai_row(entry_row: Any) -> bool:
    row = _row_to_dict(entry_row)
    text_parts: list[str] = []
    for key in (
        "source",
        "entry_source",
        "candidate_source",
        "pipeline_source",
        "entry_type",
        "reason",
        "ai_reason",
        "skip_reason",
        "model_used",
    ):
        try:
            v = row.get(key)
            if v is not None:
                text_parts.append(str(v))
        except Exception:
            pass
    text = " ".join(text_parts).lower()
    if any(tok in text for tok in _SUMMARY_TOKENS):
        return True
    try:
        if bool(row.get("ai_gate_allow")) or bool(row.get("preapproved")) or bool(row.get("summary_ai_ok")):
            return True
    except Exception:
        pass
    return False


def _has_explicit_ranking_identity(entry_row: Any) -> bool:
    row = _row_to_dict(entry_row)
    text_parts: list[str] = []
    for key in ("source", "entry_source", "candidate_source", "pipeline_source", "rank_type", "ranking_type"):
        v = row.get(key)
        if v is not None:
            text_parts.append(str(v))
    text = " ".join(text_parts).lower()
    if any(tok in text for tok in _RANKING_TOKENS):
        return True
    # Do not treat reason='rankScore=...' as ranking identity. SUMMARY_AI always has rankScore in reason.
    ranking_keys = {"rank", "ranking_type", "ranking_kind", "rank_type", "source_rank", "change_percentage", "change_rate", "change_ratio"}
    return bool(set(row.keys()) & ranking_keys)


def install() -> bool:
    global _INSTALLED, _ORIG_LOOKS_RANKING_ENTRY, _ORIG_RANKING_MOVE_RESCUE
    if _INSTALLED:
        return True
    try:
        import trading.filters.volatility_filter as vf

        cur_looks = getattr(vf, "_looks_ranking_entry", None)
        cur_rescue = getattr(vf, "_ranking_move_rescue", None)
        if not callable(cur_looks) or not callable(cur_rescue):
            logger.warning("[SUMMARY AI NO RANKING RESCUE] target missing version=%s", VERSION)
            return False

        _ORIG_LOOKS_RANKING_ENTRY = getattr(cur_looks, "_original", cur_looks)
        _ORIG_RANKING_MOVE_RESCUE = getattr(cur_rescue, "_original", cur_rescue)

        def _looks_ranking_entry_no_summary_rankscore(entry_row: Any) -> bool:
            if _is_summary_ai_row(entry_row):
                # SUMMARY_AI rows contain rankScore in reason, but that is not a Ranking source.
                return False
            if _has_explicit_ranking_identity(entry_row):
                return True
            try:
                return bool(_ORIG_LOOKS_RANKING_ENTRY(entry_row))
            except Exception:
                return False

        def _ranking_move_rescue_no_summary_scan(entry_row: Any, *, min_pct: float, label: str) -> bool:
            if _is_summary_ai_row(entry_row):
                row = _row_to_dict(entry_row)
                logger.info(
                    "[SUMMARY AI NO RANKING RESCUE] skip stale ranking DB scan symbol=%s label=%s source=%s entry_type=%s version=%s",
                    row.get("symbol") or row.get("Symbol"),
                    label,
                    row.get("source"),
                    row.get("entry_type"),
                    VERSION,
                )
                return False
            return bool(_ORIG_RANKING_MOVE_RESCUE(entry_row, min_pct=min_pct, label=label))

        _looks_ranking_entry_no_summary_rankscore._summary_ai_no_ranking_rescue_v1 = True  # type: ignore[attr-defined]
        _looks_ranking_entry_no_summary_rankscore._original = _ORIG_LOOKS_RANKING_ENTRY  # type: ignore[attr-defined]
        _ranking_move_rescue_no_summary_scan._summary_ai_no_ranking_rescue_v1 = True  # type: ignore[attr-defined]
        _ranking_move_rescue_no_summary_scan._original = _ORIG_RANKING_MOVE_RESCUE  # type: ignore[attr-defined]

        vf._looks_ranking_entry = _looks_ranking_entry_no_summary_rankscore
        vf._ranking_move_rescue = _ranking_move_rescue_no_summary_scan
        _INSTALLED = True
        logger.warning("[SUMMARY AI NO RANKING RESCUE] installed version=%s", VERSION)
        return True
    except Exception:
        logger.exception("[SUMMARY AI NO RANKING RESCUE] install failed version=%s", VERSION)
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI NO RANKING RESCUE] auto install failed")


__all__ = ["install", "VERSION"]
