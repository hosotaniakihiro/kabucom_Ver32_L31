# ============================================================
# File   : trading/ranking/ranking_pipeline.py
# Version: Ver14.2-FULL-COMPAT-PRODUCTION-HARDENED
# ------------------------------------------------------------
# ✔ Ver14.1 完全保持（削除ゼロ）
# ✔ スコア暴走修正維持
# ✔ velocity fallback強化維持
# ✔ debug完全保持
# ✔ 特徴量欠損の可視化を追加
# ✔ mom / trend / velocity の救済を強化
# ✔ 完全互換
# ============================================================

from __future__ import annotations

import logging
import pandas as pd
import numpy as np

from trading.ranking.core.dataframe_guard import sanitize_dataframe
from trading.ranking.core.normalize import normalize_symbol, normalize_datetime
from trading.ranking.core.latest_selector import select_latest_rows
from trading.ranking.core.numeric_sanitizer import sanitize_numeric

from trading.ranking.filters.market_filter_adapter import apply_market_filter
from trading.ranking.filters.liquidity_filter import apply_liquidity_filter

from trading.ranking.features.turnover import ensure_turnover
from trading.ranking.features.slope import ensure_slope
from trading.ranking.features.mtf_score import build_mtf_score

from trading.ranking.engines.velocity import apply_velocity
from trading.ranking.engines.institutional import apply_institutional
from trading.ranking.engines.smart_money import apply_smart_money
from trading.ranking.engines.ignition import apply_ignition
from trading.ranking.engines.entry_timing import apply_entry_timing

from trading.ranking.scoring.score_builder import build_ranking_score
from trading.ranking.scoring.scoring_guard import apply_scoring_guard

from trading.ranking.execution.symbol_rotation import rotate_symbols
from trading.ranking.logging.ranking_logger import log_ranking

logger = logging.getLogger(__name__)

TOP_N = 20


def _safe_numeric(df: pd.DataFrame, col: str) -> pd.Series:
    if col in df.columns:
        return (
            pd.to_numeric(df[col], errors="coerce")
            .replace([np.inf, -np.inf], 0)
            .fillna(0)
        )
    return pd.Series(0, index=df.index, dtype="float64")


def _attach_symbolname(df: pd.DataFrame) -> pd.DataFrame:
    if "symbolname" in df.columns:
        return df

    if "symbol" not in df.columns:
        return df

    for col in ["symbolname", "name", "stock_name"]:
        if col in df.columns:
            df["symbolname"] = df[col]
            return df

    df["symbolname"] = ""
    return df


def _log_column_profile(df: pd.DataFrame, cols: list[str], prefix: str = "[ranking_pipeline]") -> None:
    try:
        for c in cols:
            if c not in df.columns:
                logger.warning("%s missing col=%s", prefix, c)
                continue

            s = pd.to_numeric(df[c], errors="coerce")
            nonnull = int(s.notna().sum())
            zeros = int((s.fillna(0) == 0).sum())
            mean = float(s.fillna(0).mean()) if len(s) else 0.0
            std = float(s.fillna(0).std()) if len(s) > 1 else 0.0
            logger.info(
                "%s col=%s nonnull=%d/%d zero=%d mean=%.6f std=%.6f",
                prefix,
                c,
                nonnull,
                len(s),
                zeros,
                mean,
                std,
            )
    except Exception:
        logger.exception("%s column profile logging failed", prefix)


def _find_price_column(df: pd.DataFrame) -> str | None:
    for c in ["close", "close_price", "currentprice", "CurrentPrice", "price"]:
        if c in df.columns:
            return c
    return None


def _ensure_momentum(df: pd.DataFrame) -> pd.DataFrame:
    """
    mom を可能な限り救済する。
    優先順位:
      1. mom
      2. momentum
      3. price_momentum
      4. price列の symbol 単位 diff
    """
    try:
        if "mom" in df.columns:
            df["mom"] = _safe_numeric(df, "mom")
            return df

        if "momentum" in df.columns:
            logger.info("[ranking_pipeline] mom <- momentum")
            df["mom"] = _safe_numeric(df, "momentum")
            return df

        if "price_momentum" in df.columns:
            logger.info("[ranking_pipeline] mom <- price_momentum")
            df["mom"] = _safe_numeric(df, "price_momentum")
            return df

        price_col = _find_price_column(df)
        if price_col is not None and "symbol" in df.columns:
            logger.warning(
                "[ranking_pipeline] momentum missing -> fallback diff from %s",
                price_col,
            )

            work = df.copy()

            if "datetime" in work.columns:
                work = work.sort_values(["symbol", "datetime"], kind="mergesort")
            else:
                work = work.sort_values(["symbol"], kind="mergesort")

            price_s = pd.to_numeric(work[price_col], errors="coerce")
            work["_tmp_price_for_mom"] = price_s.fillna(method="ffill")

            work["mom"] = (
                work.groupby("symbol", sort=False)["_tmp_price_for_mom"]
                .diff()
                .replace([np.inf, -np.inf], 0)
                .fillna(0)
            )

            df["mom"] = work["mom"].reindex(df.index).fillna(0).astype("float64")
            return df

        logger.warning("[ranking_pipeline] momentum missing -> mom=0")
        df["mom"] = pd.Series(0.0, index=df.index, dtype="float64")
        return df

    except Exception:
        logger.exception("[ranking_pipeline] ensure momentum failed -> mom=0")
        df["mom"] = pd.Series(0.0, index=df.index, dtype="float64")
        return df


def _ensure_trend(df: pd.DataFrame) -> pd.DataFrame:
    """
    trend を可能な限り救済する。
    優先順位:
      1. trend
      2. ma5_slope + ma25_slope
      3. slope
    """
    try:
        if "trend" in df.columns:
            df["trend"] = _safe_numeric(df, "trend")
            return df

        ma5 = _safe_numeric(df, "ma5_slope")
        ma25 = _safe_numeric(df, "ma25_slope")

        if (ma5.abs().sum() > 0) or (ma25.abs().sum() > 0):
            logger.info("[ranking_pipeline] trend <- ma5_slope + ma25_slope")
            df["trend"] = ma5 + ma25
            return df

        if "slope" in df.columns:
            logger.warning("[ranking_pipeline] trend fallback <- slope")
            df["trend"] = _safe_numeric(df, "slope")
            return df

        logger.warning("[ranking_pipeline] trend source missing -> trend=0")
        df["trend"] = pd.Series(0.0, index=df.index, dtype="float64")
        return df

    except Exception:
        logger.exception("[ranking_pipeline] ensure trend failed -> trend=0")
        df["trend"] = pd.Series(0.0, index=df.index, dtype="float64")
        return df


def _ensure_velocity_debug_and_score(df: pd.DataFrame) -> pd.DataFrame:
    """
    _score_velocity を score加点と debug表示で一致させる。
    既存の _score_velocity が全ゼロなら volume fallback を使用し、
    必ず df['_score_velocity'] にも反映する。
    """
    try:
        if "_score_velocity" in df.columns:
            vel = _safe_numeric(df, "_score_velocity")
        else:
            logger.warning("[ranking_pipeline] _score_velocity missing")
            vel = pd.Series(0.0, index=df.index, dtype="float64")

        if vel.abs().sum() == 0:
            vol = _safe_numeric(df, "volume")
            if vol.abs().sum() > 0:
                logger.warning(
                    "[ranking_pipeline] _score_velocity all zero -> volume fallback applied"
                )
                vmax = float(vol.max()) if len(vol) else 0.0
                if vmax > 0:
                    vel = vol / (vmax + 1e-9)
                else:
                    vel = pd.Series(0.0, index=df.index, dtype="float64")
            else:
                logger.warning(
                    "[ranking_pipeline] _score_velocity all zero and volume unavailable"
                )
                vel = pd.Series(0.0, index=df.index, dtype="float64")

        df["_score_velocity"] = vel.astype("float64")
        df["score"] = _safe_numeric(df, "score") + df["_score_velocity"] * 1.5
        return df

    except Exception:
        logger.exception("[ranking_pipeline] velocity fallback failed")
        if "_score_velocity" not in df.columns:
            df["_score_velocity"] = pd.Series(0.0, index=df.index, dtype="float64")
        df["score"] = _safe_numeric(df, "score")
        return df


def _log_history_stats(df: pd.DataFrame) -> None:
    try:
        if "symbol" not in df.columns:
            return

        counts = df.groupby("symbol", sort=False).size()
        if len(counts) == 0:
            return

        logger.info(
            "[ranking_pipeline] history stats symbols=%d min=%d max=%d mean=%.2f short(<3)=%d",
            int(len(counts)),
            int(counts.min()),
            int(counts.max()),
            float(counts.mean()),
            int((counts < 3).sum()),
        )
    except Exception:
        logger.exception("[ranking_pipeline] history stats logging failed")


def run_ranking_pipeline(df: pd.DataFrame, interval: int, *, regime: str | None = None):
    try:
        logger.debug("[ranking_pipeline] start rows=%s", len(df) if df is not None else 0)

        df = sanitize_dataframe(df)
        if df.empty:
            logger.warning("[ranking_pipeline] empty after sanitize_dataframe")
            return df

        df = normalize_symbol(df)
        df = normalize_datetime(df)
        if df.empty:
            logger.warning("[ranking_pipeline] empty after normalize")
            return df

        _log_history_stats(df)

        df = apply_market_filter(df)
        if df.empty:
            logger.warning("[ranking_pipeline] empty after apply_market_filter")
            return df

        df = ensure_turnover(df)

        # 完全保持
        if "_score_base" not in df.columns:
            vol = _safe_numeric(df, "volume")
            if vol.sum() > 0:
                df["_score_base"] = vol / (vol.max() + 1e-9)
            else:
                df["_score_base"] = pd.Series(0.0, index=df.index, dtype="float64")

        df = apply_velocity(df)
        df = apply_institutional(df)
        df = apply_smart_money(df)

        df = apply_entry_timing(df)
        df = apply_ignition(df)

        df = apply_liquidity_filter(df)
        if df.empty:
            logger.warning("[ranking_pipeline] empty after apply_liquidity_filter")
            return df

        # 最新行選択前の列診断
        _log_column_profile(
            df,
            [
                "volume",
                "turnover",
                "_score_base",
                "_score_velocity",
                "momentum",
                "price_momentum",
                "ma5_slope",
                "ma25_slope",
                "slope",
                "close",
                "close_price",
            ],
            prefix="[ranking_pipeline][pre-latest]",
        )

        df = select_latest_rows(df)
        if df.empty:
            logger.warning("[ranking_pipeline] empty after select_latest_rows")
            return df

        df = ensure_slope(df)
        df = build_mtf_score(df)

        df = _ensure_momentum(df)
        df = _ensure_trend(df)

        df["mom"] = np.tanh(_safe_numeric(df, "mom") * 2)
        df["trend"] = np.tanh(_safe_numeric(df, "trend") * 3)

        # 最新行選択後の列診断
        _log_column_profile(
            df,
            [
                "mom",
                "trend",
                "_score_base",
                "_score_velocity",
                "score",
            ],
            prefix="[ranking_pipeline][post-latest]",
        )

        df = build_ranking_score(df, regime=regime)

        # ----------------------------------------------------
        # 表示用の寄与列を明示的に作る（NEW）
        # ----------------------------------------------------
        df["base"] = _safe_numeric(df, "_score_base")
        df["trend_component"] = _safe_numeric(df, "_score_trend")
        df["mom_component"] = _safe_numeric(df, "_score_momentum")
        df["velocity_component"] = _safe_numeric(df, "_score_velocity")

        # 既存ログ互換
        # 上段の log_ranking() が base/trend/mom を見る前提に合わせる
        df["trend"] = df["trend_component"]
        df["mom"] = df["mom_component"]

        # 完全保持
        neg_trend = (_safe_numeric(df, "trend") < 0).astype(int)
        neg_mom = (_safe_numeric(df, "mom") < 0).astype(int)

        df["direction_penalty"] = (neg_trend + neg_mom) * 3.0
        df["score"] = _safe_numeric(df, "score") - _safe_numeric(df, "direction_penalty")

        # velocity（非破壊強化）
        df = _ensure_velocity_debug_and_score(df)

        # 最小修正維持
        df["score"] = _safe_numeric(df, "score").clip(-200, 200)

        df = apply_scoring_guard(df)
        df = sanitize_numeric(df)

        df = _attach_symbolname(df)
        df = df.sort_values("score", ascending=False)

        df_entry = df.head(TOP_N).copy()

        logger.info(
            "[ranking_pipeline] interval=%s candidates=%s",
            interval,
            len(df_entry),
        )

        # debug完全保持
        debug_cols = [
            "symbol",
            "symbolname",
            "score",
            "base",
            "trend",
            "mom",
            "_score_base",
            "_score_trend",
            "_score_momentum",
            "_score_velocity",
            "direction_penalty",
        ]
        debug_cols = [c for c in debug_cols if c in df_entry.columns]

        logger.info("[SCORE DEBUG]\n%s", df_entry[debug_cols].head(10))

        try:
            logger.info(
                "[RANKING STATS DETAIL] interval=%s score_nonzero=%d mom_nonzero=%d trend_nonzero=%d vel_nonzero=%d penalty_nonzero=%d",
                interval,
                int((_safe_numeric(df_entry, "score") != 0).sum()),
                int((_safe_numeric(df_entry, "mom") != 0).sum()),
                int((_safe_numeric(df_entry, "trend") != 0).sum()),
                int((_safe_numeric(df_entry, "_score_velocity") != 0).sum()),
                int((_safe_numeric(df_entry, "direction_penalty") != 0).sum()),
            )
        except Exception:
            logger.exception("[ranking_pipeline] detail stats logging failed")

        log_ranking(df_entry, interval=interval)

        try:
            rotate_symbols(df_entry)
        except Exception:
            logger.exception("[ranking_pipeline] symbol rotation failed")

        print(df_entry[debug_cols].head(10))

        return df_entry.reset_index(drop=True)

    except Exception:
        logger.exception("[ranking_pipeline] failed interval=%s", interval)
        return pd.DataFrame()