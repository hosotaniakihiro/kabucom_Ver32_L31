# ============================================================
# File   : trading/summary/engine/processors/indicator.py
# Version: Ver32-PRODUCTION-INDICATOR-PROCESSOR-INTERVAL-PASS-FULL
# ------------------------------------------------------------
# ✔ add_all_indicators 安全ラップ
# ✔ interval を indicator_calculator へ確実に伝搬
# ✔ duplicate columns 完全防止
# ✔ enhance_guard統合
# ✔ 非破壊設計
# ✔ 空DF安全
# ✔ crash防止
# ✔ production safe
# ✔ strict版ログ維持
# ✔ engine互換 safe_indicator API 維持
# ============================================================

from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from trading.summary.indicators.indicator_calculator import add_all_indicators
from trading.summary.engine.guards.enhance_guard import (
    enhance_guard,
    drop_duplicate_columns,
)

logger = logging.getLogger(__name__)


# ============================================================
# interval helper
# ============================================================

def _normalize_interval(interval: Any = "1min") -> str:
    """
    indicator_calculator 側へ渡す interval 名を正規化する。
    """
    try:
        s = str(interval).strip().lower()

        if s in ("1", "1m", "1min", "1minute"):
            return "1min"
        if s in ("3", "3m", "3min", "3minute"):
            return "3min"
        if s in ("5", "5m", "5min", "5minute"):
            return "5min"
        if s in ("10", "10m", "10min", "10minute"):
            return "10min"
        if s in ("15", "15m", "15min", "15minute"):
            return "15min"
        if s in ("30", "30m", "30min", "30minute"):
            return "30min"
        if s in ("60", "60m", "60min", "60minute", "1h", "1hour"):
            return "60min"

        if s:
            return s

    except Exception:
        logger.debug("[INDICATOR PROCESSOR] interval normalize failed", exc_info=True)

    return "1min"


def _profile_indicator_state(tag: str, df: pd.DataFrame) -> None:
    """
    軽量な状態ログ。計算前後の 0/NaN 混入確認用。
    """
    try:
        if df is None or df.empty:
            logger.info("[INDICATOR PROCESSOR] %s empty", tag)
            return

        def _nonnull(col: str) -> int:
            if col not in df.columns:
                return -1
            return int(pd.to_numeric(df[col], errors="coerce").notna().sum())

        def _nonzero(col: str) -> int:
            if col not in df.columns:
                return -1
            s = pd.to_numeric(df[col], errors="coerce")
            return int((s.fillna(0) != 0).sum())

        logger.info(
            "[INDICATOR PROCESSOR] %s rows=%s cols=%s "
            "rsi(nn=%s nz=%s) macd(nn=%s nz=%s) signal(nn=%s nz=%s) "
            "slope(nn=%s nz=%s) slope_atr_scaled(nn=%s nz=%s) "
            "ma75(nn=%s nz=%s) atr(nn=%s nz=%s)",
            tag,
            len(df),
            len(df.columns),
            _nonnull("rsi"), _nonzero("rsi"),
            _nonnull("macd"), _nonzero("macd"),
            _nonnull("signal"), _nonzero("signal"),
            _nonnull("slope"), _nonzero("slope"),
            _nonnull("slope_atr_scaled"), _nonzero("slope_atr_scaled"),
            _nonnull("ma75"), _nonzero("ma75"),
            _nonnull("atr"), _nonzero("atr"),
        )
    except Exception:
        logger.debug("[INDICATOR PROCESSOR] profile failed tag=%s", tag, exc_info=True)


# ============================================================
# core indicator processor
# ============================================================

def apply_indicator(df: pd.DataFrame, *, interval: Any = "1min") -> pd.DataFrame:
    """
    indicator 安全適用

    ✔ duplicate防止
    ✔ interval を add_all_indicators へ伝搬
    ✔ guard統合
    ✔ 落ちない設計
    ✔ 非破壊
    """
    if df is None or df.empty:
        return df

    interval_name = _normalize_interval(interval)

    try:
        src = df.copy()

        logger.info(
            "[INDICATOR PROCESSOR] start interval=%s rows=%s cols=%s",
            interval_name,
            len(src),
            len(src.columns),
        )
        _profile_indicator_state("before-drop-duplicate", src)

        # ----------------------------------------------------
        # 0. 事前防御
        # ----------------------------------------------------
        work = drop_duplicate_columns(src)
        _profile_indicator_state("after-drop-duplicate-before-ind", work)

        # ----------------------------------------------------
        # 1. indicator計算
        # ----------------------------------------------------
        work = add_all_indicators(work, interval=interval_name)
        _profile_indicator_state("after-add-all-indicators", work)

        # ----------------------------------------------------
        # 2. 事後防御
        # ----------------------------------------------------
        work = drop_duplicate_columns(work)
        _profile_indicator_state("after-drop-duplicate-after-ind", work)

        # ----------------------------------------------------
        # 3. guard
        # ----------------------------------------------------
        work = enhance_guard(work)
        _profile_indicator_state("after-enhance-guard", work)

        logger.info(
            "[INDICATOR PROCESSOR] finished interval=%s rows=%s cols=%s",
            interval_name,
            len(work),
            len(work.columns),
        )
        return work

    except Exception:
        logger.exception("[INDICATOR PROCESSOR] failed interval=%s", interval_name)
        return df


# ============================================================
# strict version（デバッグ用）
# ============================================================

def apply_indicator_strict(df: pd.DataFrame, *, interval: Any = "1min") -> pd.DataFrame:
    """
    デバッグ用（ログ強化）
    """
    if df is None or df.empty:
        return df

    interval_name = _normalize_interval(interval)

    try:
        before_cols = set(df.columns)
        before_len = len(df)

        out = apply_indicator(df, interval=interval_name)

        after_cols = set(out.columns) if isinstance(out, pd.DataFrame) else set()
        after_len = len(out) if isinstance(out, pd.DataFrame) else 0

        added = sorted(list(after_cols - before_cols))
        removed = sorted(list(before_cols - after_cols))

        logger.info(
            "[INDICATOR PROCESSOR STRICT] interval=%s rows=%s->%s cols=%s->%s",
            interval_name,
            before_len,
            after_len,
            len(before_cols),
            len(after_cols),
        )

        if added:
            logger.info(
                "[INDICATOR PROCESSOR STRICT] added columns interval=%s: %s",
                interval_name,
                added[:50],
            )

        if removed:
            logger.warning(
                "[INDICATOR PROCESSOR STRICT] removed columns interval=%s: %s",
                interval_name,
                removed[:50],
            )

        _profile_indicator_state("strict-final", out)
        return out

    except Exception:
        logger.exception("[INDICATOR PROCESSOR STRICT] failed interval=%s", interval_name)
        return df


# ============================================================
# safe wrapper（engine互換）
# ============================================================

def safe_indicator(df: pd.DataFrame, *, interval: Any = "1min") -> pd.DataFrame:
    """
    engine用ラッパー
    """
    return apply_indicator(df, interval=interval)


# ============================================================
# public API
# ============================================================

__all__ = [
    "apply_indicator",
    "apply_indicator_strict",
    "safe_indicator",
]