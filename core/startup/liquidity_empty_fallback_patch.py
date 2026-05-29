# ============================================================
# File   : core/startup/liquidity_empty_fallback_patch.py
# Version: V1-LIQUIDITY-EMPTY-FALLBACK-PUSH-DB-GUARD
# ------------------------------------------------------------
# 目的:
#   - ランキング由来の流動性フィルタが全件0件になった場合、
#     完全な unfiltered ではなく段階的に緩和して候補を残す。
#   - main_database.py / push_receiver_runner.py 側では PUSH DB writer を
#     明示的に有効化し、memory_only 誤判定を避ける。
#
# 背景ログ:
#   [PUSH RANKING SELECTOR] liquidity filter before=2753 after=0
#   [ranking_pipeline] empty after apply_liquidity_filter
#   [PUSH MONITOR] total_received>0 total_flushed=0 memory_only=True
# ============================================================

from __future__ import annotations

import logging
import os
import sys
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

_INSTALLED = False


def _is_database_like_process() -> bool:
    try:
        argv = " ".join(str(x).replace("\\", "/").lower() for x in sys.argv)
        if any(x in argv for x in ("main_database.py", "data_collectors_runner.py", "push_receiver_runner.py")):
            return True
    except Exception:
        pass
    return str(os.environ.get("AUTOSTOCK_DATA_COLLECTORS_PROCESS", "")).strip().lower() in {"1", "true", "yes", "on"}


def _enable_push_db_writer_env_if_database_process() -> None:
    if not _is_database_like_process():
        return
    os.environ["AUTOSTOCK_DATA_COLLECTORS_PROCESS"] = "1"
    os.environ["AUTOSTOCK_MAIN_DATABASE_PROCESS"] = "1"
    os.environ["AUTOSTOCK_EXTERNAL_DATA_COLLECTORS"] = "1"
    os.environ["PUSH_STREAM_DB_WRITE"] = "1"
    os.environ["PUSH_STREAM_ORDER_BOOK_WRITE"] = os.environ.get("PUSH_STREAM_ORDER_BOOK_WRITE", "1")
    os.environ["AUTOSTOCK_SUMMARY_SAVE_OWNER"] = os.environ.get("AUTOSTOCK_SUMMARY_SAVE_OWNER", "database")
    os.environ["AUTOSTOCK_SUMMARY_DB_WRITER"] = os.environ.get("AUTOSTOCK_SUMMARY_DB_WRITER", "1")
    logger.warning(
        "[LIQ EMPTY FALLBACK PATCH] database-like process detected -> PUSH DB writer env forced on argv=%s",
        sys.argv[:3],
    )


def _patch_push_ranking_selector() -> bool:
    try:
        import trading.push.subscription_manager.ranking_selector as rs
    except Exception:
        logger.debug("[LIQ EMPTY FALLBACK PATCH] ranking_selector import skipped", exc_info=True)
        return False

    if getattr(rs, "_LIQ_EMPTY_FALLBACK_PATCHED", False):
        return True

    original = getattr(rs, "_apply_min_liquidity", None)
    if not callable(original):
        return False

    def _patched_apply_min_liquidity(
        df: pd.DataFrame,
        *,
        min_price: float = None,
        min_trading_value: float = None,
        min_volume: float = None,
        min_tick_count: float = None,
        strict: bool = False,
    ) -> pd.DataFrame:
        # デフォルト値は実モジュール側の現在値を使う。
        if min_price is None:
            min_price = float(getattr(rs, "MIN_PRICE", 300.0))
        if min_trading_value is None:
            min_trading_value = float(getattr(rs, "MIN_TRADING_VALUE", 30000000.0))
        if min_volume is None:
            min_volume = float(getattr(rs, "MIN_VOLUME", 10000.0))
        if min_tick_count is None:
            min_tick_count = float(getattr(rs, "MIN_TICK_COUNT", 0.0))

        before = 0 if df is None else len(df)
        out = original(
            df,
            min_price=min_price,
            min_trading_value=min_trading_value,
            min_volume=min_volume,
            min_tick_count=min_tick_count,
            strict=strict,
        )
        if out is not None and not out.empty:
            return out
        if df is None or getattr(df, "empty", True):
            return pd.DataFrame()

        # 代金列の単位違い・未補完で after=0 になるケースがあるため段階緩和。
        relax_steps = [
            ("turnover_5m", 5_000_000.0, min_volume, min_tick_count),
            ("turnover_1m", 1_000_000.0, min_volume, 0.0),
            ("price_volume_only", 0.0, min_volume, 0.0),
            ("price_only", 0.0, 0.0, 0.0),
        ]
        for label, tv, vol, tick in relax_steps:
            try:
                cand = original(
                    df,
                    min_price=min_price,
                    min_trading_value=tv,
                    min_volume=vol,
                    min_tick_count=tick,
                    strict=False,
                )
            except Exception:
                logger.debug("[LIQ EMPTY FALLBACK PATCH] selector relax failed step=%s", label, exc_info=True)
                continue
            if cand is not None and not cand.empty:
                logger.warning(
                    "[LIQ EMPTY FALLBACK PATCH] PUSH selector liquidity relaxed step=%s before=%d after=%d original_min_value=%.1f min_price=%.1f min_volume=%.1f",
                    label,
                    before,
                    len(cand),
                    float(min_trading_value),
                    float(min_price),
                    float(vol),
                )
                return cand

        logger.warning("[LIQ EMPTY FALLBACK PATCH] PUSH selector liquidity still empty before=%d -> keep original empty", before)
        return out

    rs._apply_min_liquidity = _patched_apply_min_liquidity
    rs._LIQ_EMPTY_FALLBACK_PATCHED = True
    logger.warning("[LIQ EMPTY FALLBACK PATCH] patched push ranking_selector._apply_min_liquidity")
    return True


def _patch_ranking_liquidity_filter() -> bool:
    try:
        import trading.ranking.filters.liquidity_filter as lf
    except Exception:
        logger.debug("[LIQ EMPTY FALLBACK PATCH] ranking liquidity_filter import skipped", exc_info=True)
        return False

    if getattr(lf, "_LIQ_EMPTY_FALLBACK_PATCHED", False):
        return True

    original = getattr(lf, "apply_liquidity_filter", None)
    if not callable(original):
        return False

    def _patched_apply_liquidity_filter(
        df: pd.DataFrame,
        *,
        min_volume: int = None,
        min_turnover: int = None,
        min_price: float = None,
        max_price: float = None,
        require_price: bool = None,
        context: str = "",
    ) -> pd.DataFrame:
        if min_volume is None:
            min_volume = int(getattr(lf, "MIN_VOLUME", 10000))
        if min_turnover is None:
            min_turnover = int(getattr(lf, "MIN_TURNOVER", 5000000))
        if min_price is None:
            min_price = float(getattr(lf, "MIN_PRICE", 200.0))
        if max_price is None:
            max_price = float(getattr(lf, "MAX_PRICE", 10000.0))
        if require_price is None:
            require_price = bool(getattr(lf, "REQUIRE_PRICE_COLUMN", True))

        before = 0 if df is None else len(df)
        out = original(
            df,
            min_volume=min_volume,
            min_turnover=min_turnover,
            min_price=min_price,
            max_price=max_price,
            require_price=require_price,
            context=context,
        )
        if out is not None and not out.empty:
            return out
        if df is None or getattr(df, "empty", True):
            return pd.DataFrame()

        # ranking_pipelineを全件空にしないため、代金のみ段階緩和。
        for label, turnover, volume in (
            ("turnover_1m", 1_000_000, min_volume),
            ("price_volume_only", 0, min_volume),
            ("price_only", 0, 0),
        ):
            try:
                cand = original(
                    df,
                    min_volume=volume,
                    min_turnover=turnover,
                    min_price=min_price,
                    max_price=max_price,
                    require_price=require_price,
                    context=f"{context}|fallback:{label}" if context else f"fallback:{label}",
                )
            except Exception:
                logger.debug("[LIQ EMPTY FALLBACK PATCH] ranking relax failed step=%s", label, exc_info=True)
                continue
            if cand is not None and not cand.empty:
                logger.warning(
                    "[LIQ EMPTY FALLBACK PATCH] ranking liquidity relaxed step=%s context=%s before=%d after=%d original_min_turnover=%s min_price=%.1f min_volume=%s",
                    label,
                    context,
                    before,
                    len(cand),
                    min_turnover,
                    float(min_price),
                    volume,
                )
                return cand

        logger.warning("[LIQ EMPTY FALLBACK PATCH] ranking liquidity still empty context=%s before=%d", context, before)
        return out

    lf.apply_liquidity_filter = _patched_apply_liquidity_filter
    lf._LIQ_EMPTY_FALLBACK_PATCHED = True
    logger.warning("[LIQ EMPTY FALLBACK PATCH] patched ranking.filters.liquidity_filter.apply_liquidity_filter")
    return True


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    _enable_push_db_writer_env_if_database_process()
    ok1 = _patch_push_ranking_selector()
    ok2 = _patch_ranking_liquidity_filter()
    _INSTALLED = True
    logger.warning("[LIQ EMPTY FALLBACK PATCH] installed push_selector=%s ranking_filter=%s", ok1, ok2)
    return bool(ok1 or ok2 or _is_database_like_process())


__all__ = ["install"]
