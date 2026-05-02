# ============================================================
# File   : trading/ranking/summary/filters.py
# Version: Ver1.0-PRODUCTION-RANKING-SUMMARY-FILTERS
# ------------------------------------------------------------
# ranking summary 用 universe / runtime filter 群
# ranking_summary_engine.py から安全に切り出すためのモジュール
# ============================================================

from __future__ import annotations

import logging
from typing import Any, Iterable

import pandas as pd

logger = logging.getLogger(__name__)

# ============================================================
# global_data 互換解決
# ============================================================

try:
    from global_state import global_data  # type: ignore
except Exception:
    try:
        from core.global_context import global_data  # type: ignore
    except Exception:
        class _FallbackGlobalData:
            pass
        global_data = _FallbackGlobalData()


# ============================================================
# runtime selector import
# ============================================================

try:
    from trading.ranking.runtime_symbol_selector import (
        select_runtime_symbols,
    )
except Exception:
    def select_runtime_symbols(*args, **kwargs):
        return []


# ============================================================
# global slots
# ============================================================

def _ensure_global_slots() -> None:
    if not hasattr(global_data, "ranking_summary_by_interval"):
        global_data.ranking_summary_by_interval = {}

    if not hasattr(global_data, "latest_ranking_summary_by_interval"):
        global_data.latest_ranking_summary_by_interval = {}

    if not hasattr(global_data, "ranking_summary_last_announced_dt"):
        global_data.ranking_summary_last_announced_dt = {}

    if not hasattr(global_data, "ranking_summary_universe"):
        global_data.ranking_summary_universe = []

    if not hasattr(global_data, "ranking_summary_initialized"):
        global_data.ranking_summary_initialized = False

    if not hasattr(global_data, "ranking_summary_runtime_filter_enabled"):
        global_data.ranking_summary_runtime_filter_enabled = False

    if not hasattr(global_data, "ranking_summary_last_runtime_symbols"):
        global_data.ranking_summary_last_runtime_symbols = []

    if not hasattr(global_data, "ranking_summary_status_meta"):
        global_data.ranking_summary_status_meta = {}

    if not hasattr(global_data, "ranking_summary_use_universe_filter"):
        global_data.ranking_summary_use_universe_filter = False


# ============================================================
# basic helpers
# ============================================================

def _safe_str(v: Any) -> str:
    try:
        if v is None:
            return ""
        s = str(v).strip()
        if s.lower() in {"nan", "none", "nat"}:
            return ""
        return s
    except Exception:
        return ""


def _normalize_symbol(v: Any) -> str:
    s = _safe_str(v)
    if not s:
        return ""
    if "." in s:
        s = s.split(".", 1)[0].strip()
    return s


# ============================================================
# universe control
# ============================================================

def set_ranking_summary_universe(symbols: Iterable[Any]) -> list[str]:
    _ensure_global_slots()

    try:
        out: list[str] = []
        seen = set()

        for x in symbols or []:
            s = _normalize_symbol(x)
            if not s or s in seen:
                continue
            seen.add(s)
            out.append(s)

        global_data.ranking_summary_universe = out
        logger.info("[RANKING SUMMARY] universe updated count=%d", len(out))
        return out

    except Exception:
        logger.exception("[RANKING SUMMARY] set universe failed")
        return []


def get_ranking_summary_universe() -> list[str]:
    _ensure_global_slots()

    try:
        xs = getattr(global_data, "ranking_summary_universe", []) or []
        return [_normalize_symbol(x) for x in xs if _normalize_symbol(x)]
    except Exception:
        return []


def _is_universe_filter_enabled() -> bool:
    _ensure_global_slots()
    try:
        return bool(getattr(global_data, "ranking_summary_use_universe_filter", False))
    except Exception:
        return False


def get_ranking_summary_runtime_filter_enabled() -> bool:
    _ensure_global_slots()
    try:
        return bool(getattr(global_data, "ranking_summary_runtime_filter_enabled", False))
    except Exception:
        return False


def set_ranking_summary_runtime_filter_enabled(enabled: bool) -> None:
    _ensure_global_slots()
    try:
        global_data.ranking_summary_runtime_filter_enabled = bool(enabled)
        logger.info(
            "[RANKING SUMMARY] runtime filter enabled=%s",
            bool(enabled),
        )
    except Exception:
        logger.exception("[RANKING SUMMARY] set runtime filter enabled failed")


def get_ranking_summary_use_universe_filter() -> bool:
    _ensure_global_slots()
    try:
        return bool(getattr(global_data, "ranking_summary_use_universe_filter", False))
    except Exception:
        return False


def set_ranking_summary_use_universe_filter(enabled: bool) -> None:
    _ensure_global_slots()
    try:
        global_data.ranking_summary_use_universe_filter = bool(enabled)
        logger.info(
            "[RANKING SUMMARY] universe filter enabled=%s",
            bool(enabled),
        )
    except Exception:
        logger.exception("[RANKING SUMMARY] set universe filter enabled failed")


# ============================================================
# runtime symbol loader
# ============================================================

def _try_load_runtime_symbols(
    refresh: bool = False,
    require_margin: bool = False,
) -> list[str]:
    _ensure_global_slots()

    try:
        if refresh:
            syms = select_runtime_symbols(
                use_global_data=True,
                use_ranking_db=True,
                use_summary_db=True,
                require_margin=require_margin,
                save_to_global=True,
                save_margin_to_global=True,
            )
        else:
            syms = getattr(global_data, "runtime_symbols", None)
            if not syms:
                syms = select_runtime_symbols(
                    use_global_data=True,
                    use_ranking_db=True,
                    use_summary_db=True,
                    require_margin=require_margin,
                    save_to_global=True,
                    save_margin_to_global=True,
                )

        out = [_normalize_symbol(x) for x in (syms or []) if _normalize_symbol(x)]
        out = list(dict.fromkeys(out))

        global_data.ranking_summary_last_runtime_symbols = out
        return out

    except Exception:
        logger.exception("[RANKING SUMMARY] load runtime symbols failed")
        return []


def get_last_runtime_symbols() -> list[str]:
    _ensure_global_slots()
    try:
        xs = getattr(global_data, "ranking_summary_last_runtime_symbols", []) or []
        return [_normalize_symbol(x) for x in xs if _normalize_symbol(x)]
    except Exception:
        return []


# ============================================================
# filters
# ============================================================

def _safe_copy_df(df: Any) -> pd.DataFrame:
    try:
        if isinstance(df, pd.DataFrame):
            return df.copy()
    except Exception:
        pass
    return pd.DataFrame()


def _filter_by_universe(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    if not _is_universe_filter_enabled():
        logger.info(
            "[RANKING SUMMARY] universe filter disabled rows=%d",
            len(df),
        )
        return df.copy()

    universe = get_ranking_summary_universe()
    if not universe:
        logger.info(
            "[RANKING SUMMARY] universe filter enabled but universe empty -> pass through rows=%d",
            len(df),
        )
        return df.copy()

    try:
        if "symbol" not in df.columns:
            logger.warning("[RANKING SUMMARY] universe filter skipped: symbol column missing")
            return df.copy()

        before = len(df)
        out = df[df["symbol"].astype(str).isin(set(universe))].copy()
        logger.info("[RANKING SUMMARY] universe filter rows=%d -> %d", before, len(out))
        return out
    except Exception:
        logger.exception("[RANKING SUMMARY] universe filter failed")
        return df.copy()


def _filter_by_runtime_symbols(
    df: pd.DataFrame,
    use_runtime_filter: bool = False,
    refresh_runtime_symbols: bool = False,
) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    if not use_runtime_filter:
        return df.copy()

    runtime_symbols = _try_load_runtime_symbols(
        refresh=refresh_runtime_symbols,
        require_margin=False,
    )
    if not runtime_symbols:
        logger.warning("[RANKING SUMMARY] runtime_symbols empty -> skip filter")
        return df.copy()

    try:
        if "symbol" not in df.columns:
            logger.warning("[RANKING SUMMARY] runtime filter skipped: symbol column missing")
            return df.copy()

        before = len(df)
        out = df[df["symbol"].astype(str).isin(set(runtime_symbols))].copy()
        logger.info("[RANKING SUMMARY] runtime filter rows=%d -> %d", before, len(out))
        return out
    except Exception:
        logger.exception("[RANKING SUMMARY] runtime filter failed")
        return df.copy()


def apply_ranking_summary_filters(
    df: pd.DataFrame,
    *,
    use_runtime_filter: bool | None = None,
    refresh_runtime_symbols: bool = False,
) -> pd.DataFrame:
    """
    ranking summary 用の標準 filter 適用入口。
    先に universe filter、その後 runtime filter を適用する。
    """
    out = _safe_copy_df(df)
    if out.empty:
        return out

    if use_runtime_filter is None:
        use_runtime_filter = get_ranking_summary_runtime_filter_enabled()

    out = _filter_by_universe(out)
    if out.empty:
        return out

    out = _filter_by_runtime_symbols(
        out,
        use_runtime_filter=bool(use_runtime_filter),
        refresh_runtime_symbols=refresh_runtime_symbols,
    )
    return out


__all__ = [
    "_ensure_global_slots",
    "set_ranking_summary_universe",
    "get_ranking_summary_universe",
    "get_ranking_summary_runtime_filter_enabled",
    "set_ranking_summary_runtime_filter_enabled",
    "get_ranking_summary_use_universe_filter",
    "set_ranking_summary_use_universe_filter",
    "get_last_runtime_symbols",
    "apply_ranking_summary_filters",
    "_try_load_runtime_symbols",
    "_filter_by_universe",
    "_filter_by_runtime_symbols",
]