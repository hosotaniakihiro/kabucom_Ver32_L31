# ============================================================
# File   : trading/ranking/runtime_state.py
# Version: Ver1.0-RANKING-RUNTIME-STATE
# ------------------------------------------------------------
# ✔ global_data 周辺の helper
# ✔ symbolname 解決
# ✔ snapshot global 保存/取得
# ✔ runtime universe 解決
# ✔ state 更新
# ============================================================

from __future__ import annotations

import datetime as dt
import logging

import pandas as pd

from .normalizers import (
    normalize_symbol,
    coerce_symbol_list,
    to_snapshot_df,
)

try:
    from global_state import global_data  # type: ignore
except Exception:
    try:
        from core.global_context import global_data  # type: ignore
    except Exception:
        class _FallbackGlobalData:
            pass
        global_data = _FallbackGlobalData()

try:
    from trading.ranking.runtime_symbol_selector import (
        select_runtime_symbols,
        get_runtime_symbol_selector_status,
    )
except Exception:
    def select_runtime_symbols(*args, **kwargs):
        return []

    def get_runtime_symbol_selector_status():
        return {}

from trading.ranking.ranking_summary_engine import set_ranking_summary_universe

logger = logging.getLogger(__name__)

_RUNTIME_UNIVERSE_INITIALIZED = False


def ensure_global_defaults() -> None:
    if not hasattr(global_data, "ranking_pipeline_available"):
        global_data.ranking_pipeline_available = False

    if not hasattr(global_data, "ranking_scheduler_state"):
        global_data.ranking_scheduler_state = {}

    if not hasattr(global_data, "ranking_runtime_symbols_all"):
        global_data.ranking_runtime_symbols_all = []

    if not hasattr(global_data, "ranking_runtime_symbols_buy"):
        global_data.ranking_runtime_symbols_buy = []

    if not hasattr(global_data, "ranking_runtime_symbols_sell"):
        global_data.ranking_runtime_symbols_sell = []

    if not hasattr(global_data, "ranking_snapshot_1min"):
        global_data.ranking_snapshot_1min = pd.DataFrame()

    if not hasattr(global_data, "ranking_snapshot_last_time"):
        global_data.ranking_snapshot_last_time = None

    if not hasattr(global_data, "ranking_last_updated_at"):
        global_data.ranking_last_updated_at = None

    if not hasattr(global_data, "ranking_last_job_status"):
        global_data.ranking_last_job_status = {}

    if not hasattr(global_data, "ranking_scheduler_running"):
        global_data.ranking_scheduler_running = False

    if not hasattr(global_data, "ranking_last_saved_minute"):
        global_data.ranking_last_saved_minute = None

    if not hasattr(global_data, "ranking_last_raw_rows"):
        global_data.ranking_last_raw_rows = 0

    if not hasattr(global_data, "ranking_last_snapshot_rows"):
        global_data.ranking_last_snapshot_rows = 0

    if not hasattr(global_data, "ranking_ma_cache"):
        global_data.ranking_ma_cache = {}


def update_runtime_state(**kwargs) -> None:
    try:
        state = dict(getattr(global_data, "ranking_scheduler_state", {}) or {})
        state.update(kwargs)
        global_data.ranking_scheduler_state = state
    except Exception:
        pass


def resolve_symbolname_from_global(symbol: str) -> str:
    sym = normalize_symbol(symbol)
    if not sym:
        return ""

    candidates = [
        getattr(global_data, "symbol_name_map", None),
        getattr(global_data, "symbolname_map", None),
        getattr(global_data, "stock_name_map", None),
        getattr(global_data, "symbols_name_map", None),
    ]

    for mp in candidates:
        try:
            if isinstance(mp, dict) and mp:
                name = mp.get(sym)
                if name:
                    return str(name).strip()
        except Exception:
            continue

    return ""


def save_snapshot_to_global(snapshot_rows: list[dict]) -> None:
    try:
        df = to_snapshot_df(snapshot_rows)

        global_data.ranking_snapshot_1min = df.copy()
        global_data.ranking_snapshot = df.copy()
        global_data.latest_ranking_df = df.copy()

        try:
            global_data.set_latest_ranking_snapshot(df.copy())
        except Exception:
            pass

        latest_ts = None
        if not df.empty and "snapshot_time" in df.columns:
            s = pd.to_datetime(df["snapshot_time"], errors="coerce").dropna()
            if not s.empty:
                latest_ts = s.max()

        global_data.ranking_snapshot_last_time = latest_ts
        global_data.ranking_last_updated_at = dt.datetime.now()

    except Exception:
        logger.exception("[RANKING SNAPSHOT] save to global failed")


def get_existing_snapshot_df_from_global() -> pd.DataFrame:
    candidates = [
        getattr(global_data, "ranking_snapshot_1min", None),
        getattr(global_data, "latest_ranking_df", None),
        getattr(global_data, "ranking_snapshot", None),
    ]

    for src in candidates:
        try:
            if isinstance(src, pd.DataFrame) and not src.empty:
                df = src.copy()
                if "snapshot_time" in df.columns:
                    df["snapshot_time"] = pd.to_datetime(df["snapshot_time"], errors="coerce")
                if "symbol" in df.columns:
                    df["symbol"] = df["symbol"].map(normalize_symbol)
                    df = df[df["symbol"] != ""].copy()
                if not df.empty:
                    return df.reset_index(drop=True)
        except Exception:
            logger.exception("[RANKING SNAPSHOT] existing global snapshot normalize failed")

    return pd.DataFrame()


def resolve_runtime_universe_from_global_data() -> list[str]:
    candidates = [
        getattr(global_data, "ranking_runtime_symbols_buy", None),
        getattr(global_data, "ranking_runtime_symbols_all", None),
        getattr(global_data, "runtime_symbols", None),
        getattr(global_data, "ranking_runtime_symbols", None),
        getattr(global_data, "active_symbols", None),
        getattr(global_data, "push_symbols", None),
        getattr(global_data, "watch_symbols", None),
        getattr(global_data, "should_register_symbols", None),
    ]

    for src in candidates:
        xs = coerce_symbol_list(src)
        if xs:
            return xs

    return []


def refresh_runtime_symbols_if_needed(
    refresh_runtime_symbols_flag: bool = False,
    force: bool = False,
) -> list[str]:
    try:
        if not force and not refresh_runtime_symbols_flag:
            xs = resolve_runtime_universe_from_global_data()
            if xs:
                return xs

        xs = select_runtime_symbols(
            use_global_data=True,
            use_ranking_db=True,
            use_summary_db=True,
            require_margin=False,
            save_to_global=True,
            save_margin_to_global=True,
        )
        xs = coerce_symbol_list(xs)
        if xs:
            global_data.ranking_runtime_symbols_all = xs
            global_data.ranking_runtime_symbols_buy = xs
        return xs
    except Exception:
        logger.exception("[RUNTIME UNIVERSE] refresh failed")
        return []


def try_initialize_runtime_universe_once() -> None:
    global _RUNTIME_UNIVERSE_INITIALIZED

    if _RUNTIME_UNIVERSE_INITIALIZED:
        return

    try:
        universe = resolve_runtime_universe_from_global_data()
        if not universe:
            universe = refresh_runtime_symbols_if_needed(force=False)

        if universe:
            set_ranking_summary_universe(universe)
            _RUNTIME_UNIVERSE_INITIALIZED = True
            logger.info("[RANKING SUMMARY] universe initialized count=%d", len(universe))
            return

        logger.info("[RANKING SUMMARY] universe unresolved -> continue without filter")
        _RUNTIME_UNIVERSE_INITIALIZED = True

    except Exception:
        logger.exception("[RANKING SUMMARY] universe initialization failed")
        _RUNTIME_UNIVERSE_INITIALIZED = True


def get_global_data():
    return global_data


def get_runtime_symbol_selector_status_safe():
    try:
        return get_runtime_symbol_selector_status()
    except Exception:
        return {}