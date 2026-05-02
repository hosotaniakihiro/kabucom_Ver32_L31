# ============================================================
# File   : trading/features/add_features.py
# Version: Ver2.0-FULL-FEATURE-INJECTOR-PRODUCTION
# ------------------------------------------------------------
# ✔ Ver1.0 全機能保持（削除ゼロ）
# ✔ feature 一括注入
# ✔ indicator 後 / flag_builder 前に実行
# ✔ RuntimeLoop 保護
# ✔ NaN / inf 耐性
# ✔ module 不在でも安全
# ✔ logging
# ✔ price / volume / liquidity / ranking / market 統合
# ✔ 将来 feature 追加耐性
# ============================================================

import logging
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


# ------------------------------------------------------------
# feature modules
# ------------------------------------------------------------

# -------------------------
# PRICE
# -------------------------

try:
    from trading.features.price.price_acceleration import apply_price_acceleration_features
except Exception:
    apply_price_acceleration_features = None


# -------------------------
# VOLUME
# -------------------------

try:
    from trading.features.volume.volume_spike import apply_volume_indicators
except Exception:
    apply_volume_indicators = None

try:
    from trading.features.volume.volume_trend import apply_volume_trend_features
except Exception:
    apply_volume_trend_features = None

try:
    from trading.features.volume.accumulation import apply_accumulation_features
except Exception:
    apply_accumulation_features = None

try:
    from trading.features.volume.distribution import apply_distribution_features
except Exception:
    apply_distribution_features = None

try:
    from trading.features.volume.relative_volume import apply_relative_volume_features
except Exception:
    apply_relative_volume_features = None


# -------------------------
# LIQUIDITY
# -------------------------

try:
    from trading.features.liquidity.liquidity_void import apply_liquidity_void_features
except Exception:
    apply_liquidity_void_features = None


# -------------------------
# RANKING
# -------------------------

try:
    from trading.features.ranking.ranking_strength import apply_ranking_strength_features
except Exception:
    apply_ranking_strength_features = None

try:
    from trading.features.ranking.ranking_velocity import apply_ranking_velocity_features
except Exception:
    apply_ranking_velocity_features = None


# -------------------------
# MARKET
# -------------------------

try:
    from trading.features.market.market_regime import apply_market_regime_features
except Exception:
    apply_market_regime_features = None


# ------------------------------------------------------------
# safe apply helper
# ------------------------------------------------------------

def _safe_apply(df: pd.DataFrame, fn, name: str):

    if fn is None:
        logger.debug(f"[FEATURE] {name} skipped (module missing)")
        return df

    try:

        before_cols = set(df.columns)

        df = fn(df)

        after_cols = set(df.columns)

        new_cols = list(after_cols - before_cols)

        logger.debug(
            f"[FEATURE] {name} applied | new_cols={new_cols[:5]}"
        )

    except Exception:

        logger.exception(f"[FEATURE] {name} failed")

    return df


# ------------------------------------------------------------
# main
# ------------------------------------------------------------

def add_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    indicator 計算後に呼び出す feature injector

    pipeline:

    indicators
        ↓
    price features
        ↓
    volume features
        ↓
    liquidity features
        ↓
    ranking features
        ↓
    market regime
    """

    if df is None or df.empty:
        return df

    if not isinstance(df, pd.DataFrame):
        logger.error("[FEATURE] input not DataFrame")
        return df

    df = df.copy()

    # --------------------------------------------------------
    # NaN / inf guard
    # --------------------------------------------------------

    df = df.replace([np.inf, -np.inf], np.nan)

    # --------------------------------------------------------
    # PRICE FEATURES
    # --------------------------------------------------------

    df = _safe_apply(df, apply_price_acceleration_features, "price_acceleration")

    # --------------------------------------------------------
    # VOLUME FEATURES
    # --------------------------------------------------------

    df = _safe_apply(df, apply_volume_indicators, "volume_spike")

    df = _safe_apply(df, apply_volume_trend_features, "volume_trend")

    df = _safe_apply(df, apply_accumulation_features, "accumulation")

    df = _safe_apply(df, apply_distribution_features, "distribution")

    df = _safe_apply(df, apply_relative_volume_features, "relative_volume")

    # --------------------------------------------------------
    # LIQUIDITY FEATURES
    # --------------------------------------------------------

    df = _safe_apply(df, apply_liquidity_void_features, "liquidity_void")

    # --------------------------------------------------------
    # RANKING FEATURES
    # --------------------------------------------------------

    df = _safe_apply(df, apply_ranking_strength_features, "ranking_strength")

    df = _safe_apply(df, apply_ranking_velocity_features, "ranking_velocity")

    # --------------------------------------------------------
    # MARKET FEATURES
    # --------------------------------------------------------

    df = _safe_apply(df, apply_market_regime_features, "market_regime")

    logger.info(
        f"[FEATURE] injected features rows={len(df)} cols={len(df.columns)}"
    )

    return df