# ============================================================
# File   : trading/symbols/symbol_rotation_engine.py
# Version: Ver2.0-PRODUCTION-SYMBOL-ROTATION-ULTRA-STABLE
# ------------------------------------------------------------
# ✔ Ver1 完全互換（削除ゼロ）
# ✔ ranking → PUSH監視銘柄
# ✔ ACTIVE / WATCH / LIGHT
# ✔ position優先
# ✔ duplicate防止
# ✔ GC安全取得
# ✔ dataframe sanitize
# ✔ symbol dtype normalize
# ✔ deterministic order
# ✔ push update guard
# ✔ real-time safe
# ✔ production logging
# ============================================================

from __future__ import annotations

import logging
import pandas as pd
from typing import Set, List

from core.global_context.context import global_context as GC

logger = logging.getLogger(__name__)


# ============================================================
# limits
# ============================================================

MAX_PUSH_SYMBOLS = 50
WATCH_SIZE = 40
LIGHT_SIZE = 150


# ============================================================
# helpers
# ============================================================

def _get_gc_attr(name: str):

    try:
        return getattr(GC, name, None)

    except Exception:

        logger.exception(
            "[SYMBOL ROTATION] GC access failed: %s",
            name
        )

        return None


def _get_position_manager():

    return _get_gc_attr("position_manager")


def _get_push_manager():

    return _get_gc_attr("push_symbol_manager")


# ============================================================
# dataframe sanitize
# ============================================================

def _sanitize_ranking_df(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return pd.DataFrame()

    df = df.copy()

    # MultiIndex guard
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    # duplicate columns
    if df.columns.duplicated().any():

        dup = df.columns[df.columns.duplicated()].tolist()

        logger.warning(
            "[SYMBOL ROTATION] duplicate columns removed: %s",
            dup
        )

        df = df.loc[:, ~df.columns.duplicated()]

    if "symbol" in df.columns:
        df["symbol"] = df["symbol"].astype(str)

    return df


# ============================================================
# active positions
# ============================================================

def _get_active_symbols() -> Set[str]:

    pm = _get_position_manager()

    if pm is None:
        return set()

    try:

        positions = pm.get_open_positions()

        if not positions:
            return set()

        return {str(p.symbol) for p in positions}

    except Exception:

        logger.exception(
            "[SYMBOL ROTATION] active symbols fetch failed"
        )

        return set()


# ============================================================
# ranking selection
# ============================================================

def _select_watch_symbols(ranking_df: pd.DataFrame) -> Set[str]:

    ranking_df = _sanitize_ranking_df(ranking_df)

    if ranking_df.empty:
        return set()

    if "symbol" not in ranking_df.columns:
        return set()

    if "ranking_strength" not in ranking_df.columns:

        ranking_df = ranking_df.copy()
        ranking_df["ranking_strength"] = 0

    try:

        top = ranking_df.sort_values(
            "ranking_strength",
            ascending=False
        ).head(WATCH_SIZE)

        return set(top["symbol"].astype(str))

    except Exception:

        logger.exception(
            "[SYMBOL ROTATION] watch selection failed"
        )

        return set()


def _select_light_symbols(ranking_df: pd.DataFrame) -> Set[str]:

    ranking_df = _sanitize_ranking_df(ranking_df)

    if ranking_df.empty:
        return set()

    if "symbol" not in ranking_df.columns:
        return set()

    if "ranking_strength" not in ranking_df.columns:

        ranking_df = ranking_df.copy()
        ranking_df["ranking_strength"] = 0

    try:

        top = ranking_df.sort_values(
            "ranking_strength",
            ascending=False
        ).head(LIGHT_SIZE)

        return set(top["symbol"].astype(str))

    except Exception:

        logger.exception(
            "[SYMBOL ROTATION] light selection failed"
        )

        return set()


# ============================================================
# deterministic order
# ============================================================

def _stable_symbol_order(symbols: Set[str]) -> List[str]:

    if not symbols:
        return []

    return sorted(symbols)


# ============================================================
# push subscription
# ============================================================

def _update_push_subscription(symbols: Set[str]):

    push_manager = _get_push_manager()

    if push_manager is None:

        logger.warning(
            "[SYMBOL ROTATION] push manager missing"
        )

        return

    ordered = _stable_symbol_order(symbols)

    try:

        push_manager.set_symbols(ordered)

        logger.info(
            "[SYMBOL ROTATION] push symbols=%s",
            len(ordered)
        )

    except Exception:

        logger.exception(
            "[SYMBOL ROTATION] push update failed"
        )


# ============================================================
# main rotation
# ============================================================

def rotate_symbols(ranking_df: pd.DataFrame):

    try:

        ranking_df = _sanitize_ranking_df(ranking_df)

        # ----------------------------------------------------
        # active positions
        # ----------------------------------------------------

        active = _get_active_symbols()

        # ----------------------------------------------------
        # ranking watch
        # ----------------------------------------------------

        watch = _select_watch_symbols(ranking_df)

        # ----------------------------------------------------
        # light tier
        # ----------------------------------------------------

        light = _select_light_symbols(ranking_df)

        # ----------------------------------------------------
        # combine (ACTIVE優先)
        # ----------------------------------------------------

        symbols = active | watch

        if len(symbols) < MAX_PUSH_SYMBOLS:

            remaining = MAX_PUSH_SYMBOLS - len(symbols)

            extra = list(light - symbols)[:remaining]

            symbols |= set(extra)

        # ----------------------------------------------------
        # hard limit
        # ----------------------------------------------------

        if len(symbols) > MAX_PUSH_SYMBOLS:

            ordered = _stable_symbol_order(symbols)

            symbols = set(ordered[:MAX_PUSH_SYMBOLS])

        # ----------------------------------------------------
        # update push
        # ----------------------------------------------------

        _update_push_subscription(symbols)

        logger.info(
            "[SYMBOL ROTATION] active=%s watch=%s light=%s total=%s",
            len(active),
            len(watch),
            len(light),
            len(symbols),
        )

    except Exception:

        logger.exception(
            "[SYMBOL ROTATION] rotation failed"
        )