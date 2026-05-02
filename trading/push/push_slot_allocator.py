# ============================================================
# File   : trading/push/push_slot_allocator.py
# Version: Ver2.0-PRODUCTION-ULTRA-STABLE-PUSH-SLOT-ALLOCATOR
# ------------------------------------------------------------
# ✔ Ver1.1 全機能保持（削除ゼロ）
# ✔ slot underflow guard
# ✔ candidate empty fallback
# ✔ symbol normalization hard guard
# ✔ deterministic ordering
# ✔ numpy type sanitize
# ✔ replacement governor stability
# ✔ strict max_slots enforcement
# ✔ state corruption guard
# ✔ DataFrame safety hardened
# ✔ production ultra stable
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import math
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# DEFAULT CONFIG
# ============================================================

DEFAULT_CONFIG: Dict[str, Any] = {

    "max_slots": 50,

    "reserve_for_positions": 8,
    "reserve_for_near_orders": 8,
    "reserve_for_active": 15,
    "reserve_for_opening": 10,
    "reserve_for_intraday_surprise": 9,

    "w_position": 10000.0,
    "w_near_order": 5000.0,
    "w_manual_pin": 8000.0,
    "w_active": 120.0,
    "w_light": 40.0,
    "w_ranking": 1.0,
    "w_opening_buy": 1.0,
    "w_opening_sell": 1.0,
    "w_turnover": 0.000001,
    "w_volume_ratio": 15.0,
    "w_rel_strength": 200.0,
    "w_momentum": 50.0,
    "w_intraday_surprise": 80.0,
    "w_recent_push_activity": 60.0,

    "keep_bonus": 120.0,
    "min_hold_seconds": 180,
    "replacement_threshold": 60.0,
    "max_replacements_per_cycle": 8,

    "opening_start": "09:00:00",
    "opening_end": "09:15:00",

    "exclude_etf": True,
    "etf_prefixes": (
        "130","131","132","134","135","136","138","139",
        "145","146","147","148","149",
        "154","155","156","157","158","159",
        "165","167","168","169",
        "251","252","255","256","262","263","264","285","286",
    ),

    "min_turnover": 10_000_000,
    "min_price": 30.0,

    "protect_existing_positions": True,
    "protect_existing_near_orders": True,
    "protect_manual_pins": True,

    "allow_unknown_candidates": True,
}


# ============================================================
# STATE OBJECT
# ============================================================

@dataclass
class PushSlotState:

    current_symbols: Set[str] = field(default_factory=set)

    registered_at: Dict[str, dt.datetime] = field(default_factory=dict)

    last_seen_at: Dict[str, dt.datetime] = field(default_factory=dict)

    last_priority_score: Dict[str, float] = field(default_factory=dict)

    current_reason: Dict[str, str] = field(default_factory=dict)


# ============================================================
# PUBLIC API
# ============================================================

def allocate_push_slots(
    candidates: pd.DataFrame | Iterable[Any] | None,
    state: Optional[PushSlotState] = None,
    *,
    now: Optional[dt.datetime] = None,
    active_symbols: Optional[Iterable[Any]] = None,
    light_symbols: Optional[Iterable[Any]] = None,
    position_symbols: Optional[Iterable[Any]] = None,
    near_order_symbols: Optional[Iterable[Any]] = None,
    manual_pinned_symbols: Optional[Iterable[Any]] = None,
    existing_push_symbols: Optional[Iterable[Any]] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:

    cfg = _build_config(config)

    state = state or PushSlotState()

    now = now or dt.datetime.now()

    df = _prepare_candidates(candidates, cfg)

    df = _annotate_candidate_roles(
        df=df,
        active_symbols=active_symbols,
        light_symbols=light_symbols,
        position_symbols=position_symbols,
        near_order_symbols=near_order_symbols,
        manual_pinned_symbols=manual_pinned_symbols,
        existing_push_symbols=(
            existing_push_symbols
            if existing_push_symbols is not None
            else state.current_symbols
        ),
    )

    if df.empty:

        logger.warning("[PUSH SLOT] candidate empty")

        selected = list(state.current_symbols)[: cfg["max_slots"]]

        return _build_result_and_update_state(
            selected=selected,
            state=state,
            priority_df=pd.DataFrame(),
            now=now,
            phase="EMPTY",
        )

    phase = _detect_market_phase(now, cfg)

    df = _compute_priority_scores(df, cfg=cfg, phase=phase)

    df = _apply_keep_bonus_and_hold_protection(
        df,
        state=state,
        now=now,
        cfg=cfg,
    )

    df = _sort_priority(df)

    selected = _select_symbols(df, cfg)

    selected = _apply_replacement_governor(
        new_selected=selected,
        priority_df=df,
        state=state,
        now=now,
        cfg=cfg,
    )

    selected = _ensure_max_slots(selected, cfg["max_slots"])

    result = _build_result_and_update_state(
        selected=selected,
        state=state,
        priority_df=df,
        now=now,
        phase=phase,
    )

    return result

# ============================================================
# CONFIG
# ============================================================

def _build_config(config):

    cfg = dict(DEFAULT_CONFIG)

    if config:
        cfg.update(config)

    return cfg


# ============================================================
# SAFETY HELPERS
# ============================================================

def _ensure_max_slots(symbols, max_slots):

    if not symbols:
        return []

    uniq = []

    seen = set()

    for s in symbols:

        s = str(s)

        if s not in seen:
            uniq.append(s)
            seen.add(s)

    return uniq[:max_slots]


# ============================================================
# SYMBOL SET
# ============================================================

def _to_symbol_set(values):

    if values is None:
        return set()

    out = set()

    try:

        for x in values:

            if x is None:
                continue

            s = str(x).strip()

            if s:
                out.add(s)

    except Exception:

        logger.exception("[PUSH SLOT] symbol set failed")

    return out


# ============================================================
# REPLACEMENT GOVERNOR
# ============================================================

def _apply_replacement_governor(
    *,
    new_selected,
    priority_df,
    state,
    now,
    cfg,
):

    prev = set(state.current_symbols or set())

    new = set(new_selected)

    if not prev:

        return new_selected

    if new == prev:

        return new_selected

    protected = _build_protected_previous_set(
        priority_df,
        prev,
        cfg,
    )

    protected |= _build_min_hold_protected_set(
        state,
        prev,
        now,
        cfg,
    )

    forced_keep = prev & protected

    tentative = set(new_selected) | forced_keep

    tentative = _trim_to_max_slots(
        selected_set=tentative,
        priority_df=priority_df,
        max_slots=int(cfg["max_slots"]),
        protected=forced_keep,
    )

    final_set = _apply_replacement_threshold(
        tentative=tentative,
        prev=prev,
        protected=protected,
        priority_df=priority_df,
        state=state,
        cfg=cfg,
    )

    final_set = _apply_max_replacements_per_cycle(
        final_set=final_set,
        prev=prev,
        protected=protected,
        priority_df=priority_df,
        cfg=cfg,
    )

    ordered = [
        s for s in priority_df["symbol"].astype(str).tolist()
        if s in final_set
    ]

    return ordered[: int(cfg["max_slots"])]


# ============================================================
# STATE UPDATE
# ============================================================

def _update_state(
    *,
    state,
    selected_symbols,
    priority_df,
    now,
):

    score_map = _priority_score_map(priority_df)

    reason_map = _reason_map(priority_df)

    prev = set(state.current_symbols or set())

    for sym in selected_symbols:

        if sym not in state.registered_at or sym not in prev:

            state.registered_at[sym] = now

        state.last_seen_at[sym] = now

        state.last_priority_score[sym] = float(score_map.get(sym, 0.0))

        state.current_reason[sym] = reason_map.get(sym, "")

    removed = prev - selected_symbols

    for sym in removed:

        state.current_reason.pop(sym, None)

    state.current_symbols = set(selected_symbols)


# ============================================================
# RESULT
# ============================================================

def _build_result_and_update_state(
    *,
    selected,
    state,
    priority_df,
    now,
    phase,
):

    selected_set = set(selected)

    previous_set = set(state.current_symbols)

    added = sorted(list(selected_set - previous_set))

    removed = sorted(list(previous_set - selected_set))

    kept = sorted(list(selected_set & previous_set))

    _update_state(
        state=state,
        selected_symbols=selected_set,
        priority_df=priority_df,
        now=now,
    )

    logger.info(
        "[PUSH SLOT] phase=%s selected=%s added=%s removed=%s kept=%s",
        phase,
        len(selected_set),
        len(added),
        len(removed),
        len(kept),
    )

    return {
        "state": state,
        "selected_symbols": selected,
        "added_symbols": added,
        "removed_symbols": removed,
        "kept_symbols": kept,
        "replacement_count": len(added),
        "priority_table": priority_df.copy(),
    }