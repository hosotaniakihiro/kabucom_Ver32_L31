# ============================================================
# File   : trading/ranking/ranking_summary_engine.py
# Version: Ver5.2-PRODUCTION-RANKING-SUMMARY-FACADE-COMPAT
# ------------------------------------------------------------
# 置き換え用:
#   旧巨大実装を facade 化し、実体は trading.ranking.summary.* へ委譲
#
# 目的:
#   - 既存 import path を壊さない
#   - scheduler / adapter 互換の公開関数名を維持する
#   - ranking summary の責務を分割して保守性を上げる
#   - update_ranking_summaries(..., announce=...) 互換を吸収
# ============================================================

from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from trading.ranking.summary.cache_store import (
    _ensure_global_slots,
    get_ranking_summary,
    set_ranking_summary,
    get_latest_ranking_summary,
    set_latest_ranking_summary,
    get_ranking_summary_initialized,
    set_ranking_summary_initialized,
    get_ranking_summary_status_meta,
    set_ranking_summary_status_meta,
)
from trading.ranking.summary.filters import (
    set_ranking_summary_universe,
    get_ranking_summary_universe,
    get_ranking_summary_runtime_filter_enabled,
    set_ranking_summary_runtime_filter_enabled,
    get_ranking_summary_use_universe_filter,
    set_ranking_summary_use_universe_filter,
    get_last_runtime_symbols,
    apply_ranking_summary_filters,
)
from trading.ranking.summary.announce import (
    set_indicator_mode,
    get_indicator_mode,
    announce_ranking_summary,
)
from trading.ranking.summary.aggregation import (
    update_ranking_summaries as _update_ranking_summaries_core,
    rebuild_ranking_summaries_from_dataframe,
)
from trading.ranking.summary.status import (
    get_ranking_summary_status,
)

logger = logging.getLogger(__name__)


# ============================================================
# Public summary builders
# ============================================================

def build_ranking_summary(
    interval: int = 1,
    *,
    topn: int = 10,
    announce: bool = False,
    use_discord: bool = False,
) -> pd.DataFrame:
    """
    指定 interval の latest ranking summary を返す。

    interval:
      1 / 3 / 5
    """
    _ensure_global_slots()

    try:
        interval = int(interval)
    except Exception:
        interval = 1

    if interval not in (1, 3, 5):
        logger.warning("[RANKING SUMMARY] unsupported interval=%s", interval)
        return pd.DataFrame()

    try:
        df = get_latest_ranking_summary(interval)

        if announce:
            try:
                announce_ranking_summary(
                    interval=interval,
                    topn=topn,
                    use_discord=use_discord,
                )
            except Exception:
                logger.exception("[RANKING SUMMARY] announce failed interval=%s", interval)

        return df.copy() if isinstance(df, pd.DataFrame) else pd.DataFrame()

    except Exception:
        logger.exception("[RANKING SUMMARY] build failed interval=%s", interval)
        return pd.DataFrame()


def run_ranking_summary(
    interval: int = 1,
    *,
    topn: int = 10,
    announce: bool = False,
    use_discord: bool = False,
) -> pd.DataFrame:
    """
    旧互換 alias
    """
    return build_ranking_summary(
        interval=interval,
        topn=topn,
        announce=announce,
        use_discord=use_discord,
    )


def run_ranking_summary_job(
    interval: int = 1,
    *,
    topn: int = 10,
    announce: bool = True,
    use_discord: bool = False,
) -> pd.DataFrame:
    """
    scheduler / adapter 互換の公開入口
    """
    return build_ranking_summary(
        interval=interval,
        topn=topn,
        announce=announce,
        use_discord=use_discord,
    )


def job_ranking_summary(
    interval: int = 1,
    *,
    topn: int = 10,
    announce: bool = True,
    use_discord: bool = False,
) -> pd.DataFrame:
    """
    scheduler / adapter 互換の旧呼び出し名
    """
    return run_ranking_summary_job(
        interval=interval,
        topn=topn,
        announce=announce,
        use_discord=use_discord,
    )


# ============================================================
# Compatibility wrapper
# ============================================================

def update_ranking_summaries(
    snapshot_rows: Any,
    *,
    use_runtime_filter: bool = False,
    refresh_runtime_symbols: bool = False,
    announce: bool | None = None,
    announce_1m: bool | None = None,
    announce_3m: bool | None = None,
    announce_5m: bool | None = None,
    use_discord: bool = False,
    **kwargs,
) -> dict[int, pd.DataFrame]:
    """
    facade 互換ラッパー。

    受けたい呼び出し:
      - update_ranking_summaries(..., announce=True)
      - update_ranking_summaries(..., announce_1m=True, announce_3m=True, ...)
      - 将来の余剰 kwargs が来ても極力落とさない

    announce 指定時の挙動:
      - True  -> 1m/3m/5m 全部 announce=True
      - False -> 1m/3m/5m 全部 announce=False
      - None  -> 個別 announce_* を優先、未指定は False
    """
    _ensure_global_slots()

    try:
        if announce is not None:
            announce_all = bool(announce)
            a1 = announce_all if announce_1m is None else bool(announce_1m)
            a3 = announce_all if announce_3m is None else bool(announce_3m)
            a5 = announce_all if announce_5m is None else bool(announce_5m)
        else:
            a1 = bool(announce_1m) if announce_1m is not None else False
            a3 = bool(announce_3m) if announce_3m is not None else False
            a5 = bool(announce_5m) if announce_5m is not None else False

        if kwargs:
            logger.info(
                "[RANKING SUMMARY] ignored extra kwargs keys=%s",
                sorted(kwargs.keys()),
            )

        logger.info(
            "[RANKING SUMMARY] facade update start announce=%s a1=%s a3=%s a5=%s runtime_filter=%s refresh_runtime_symbols=%s",
            announce,
            a1,
            a3,
            a5,
            use_runtime_filter,
            refresh_runtime_symbols,
        )

        result = _update_ranking_summaries_core(
            snapshot_rows,
            use_runtime_filter=bool(use_runtime_filter),
            refresh_runtime_symbols=bool(refresh_runtime_symbols),
            announce_1m=a1,
            announce_3m=a3,
            announce_5m=a5,
            use_discord=bool(use_discord),
        )

        if not isinstance(result, dict):
            logger.warning(
                "[RANKING SUMMARY] facade update returned non-dict type=%s",
                type(result).__name__,
            )
            return {}

        return result

    except Exception:
        logger.exception(
            "[RANKING SUMMARY] facade update failed announce=%s announce_1m=%s announce_3m=%s announce_5m=%s",
            announce,
            announce_1m,
            announce_3m,
            announce_5m,
        )
        return {}


# ============================================================
# Update / rebuild helpers
# ============================================================

def refresh_ranking_summaries_from_rows(
    snapshot_rows: Any,
    *,
    use_runtime_filter: bool | None = None,
    refresh_runtime_symbols: bool = False,
    announce_1m: bool = False,
    announce_3m: bool = False,
    announce_5m: bool = False,
    use_discord: bool = False,
) -> dict[int, pd.DataFrame]:
    """
    ranking snapshot rows から ranking summaries を更新する補助入口
    """
    _ensure_global_slots()

    if use_runtime_filter is None:
        use_runtime_filter = get_ranking_summary_runtime_filter_enabled()

    return update_ranking_summaries(
        snapshot_rows,
        use_runtime_filter=bool(use_runtime_filter),
        refresh_runtime_symbols=refresh_runtime_symbols,
        announce_1m=announce_1m,
        announce_3m=announce_3m,
        announce_5m=announce_5m,
        use_discord=use_discord,
    )


def rebuild_ranking_summaries(
    df_1m: pd.DataFrame,
    *,
    announce_1m: bool = False,
    announce_3m: bool = False,
    announce_5m: bool = False,
    use_discord: bool = False,
) -> dict[int, pd.DataFrame]:
    """
    既存 1min ranking history から再構築する補助入口
    """
    _ensure_global_slots()

    return rebuild_ranking_summaries_from_dataframe(
        df_1m,
        announce_1m=announce_1m,
        announce_3m=announce_3m,
        announce_5m=announce_5m,
        use_discord=use_discord,
    )


# ============================================================
# Backward-compatible alias
# ============================================================

ranking_summary_engine = build_ranking_summary


__all__ = [
    # cache
    "_ensure_global_slots",
    "get_ranking_summary",
    "set_ranking_summary",
    "get_latest_ranking_summary",
    "set_latest_ranking_summary",
    "get_ranking_summary_initialized",
    "set_ranking_summary_initialized",
    "get_ranking_summary_status_meta",
    "set_ranking_summary_status_meta",

    # filters / settings
    "set_ranking_summary_universe",
    "get_ranking_summary_universe",
    "get_ranking_summary_runtime_filter_enabled",
    "set_ranking_summary_runtime_filter_enabled",
    "get_ranking_summary_use_universe_filter",
    "set_ranking_summary_use_universe_filter",
    "get_last_runtime_symbols",
    "apply_ranking_summary_filters",

    # announce / indicator mode
    "set_indicator_mode",
    "get_indicator_mode",
    "announce_ranking_summary",

    # aggregation / rebuild
    "update_ranking_summaries",
    "rebuild_ranking_summaries_from_dataframe",
    "refresh_ranking_summaries_from_rows",
    "rebuild_ranking_summaries",

    # status
    "get_ranking_summary_status",

    # public facade
    "build_ranking_summary",
    "run_ranking_summary",
    "run_ranking_summary_job",
    "job_ranking_summary",
    "ranking_summary_engine",
]