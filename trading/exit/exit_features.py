# ============================================================
# File   : trading/exit/exit_features.py
# Version: V1.1-SELL-MIRROR-5SEC-FEATURES
# ------------------------------------------------------------
# 【概要】
#   EXIT判定に使う特徴量構築。
#
# 【役割】
#   - ctx.build_features(price)
#   - daily cache の注入
#   - 5秒足特徴量の抽出
#
# V1.1:
#   - Tonosama SELL EXIT 用に、連続陽線 / VWAP上抜け / 安値 after entry を取得。
#   - BUY側の既存キーはそのまま維持。
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

from trading.exit.exit_utils import (
    dict_get_any,
    get_feature_bool_or_none,
    get_feature_int_or_none,
    get_feature_value_or_none,
    safe_bool_or_none,
    safe_float,
    safe_float_or_none,
    safe_int_or_none,
    safe_str,
)

logger = logging.getLogger(__name__)


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return str(v).strip().lower() in {"1", "true", "yes", "on", "y"}


AI_EXIT_USE_DAILY_CACHE = _env_bool("AI_EXIT_USE_DAILY_CACHE", True)


def build_exit_features_safe(ctx: Any, price: float, pnl: float, collapse_prob: float = 0.0) -> Dict[str, Any]:
    features: Dict[str, Any] = {}

    try:
        if ctx is not None and hasattr(ctx, "build_features"):
            features.update(ctx.build_features(price) or {})
    except Exception:
        logger.exception("[AI_EXIT] ctx.build_features failed")

    try:
        features.setdefault("symbol", getattr(ctx, "symbol", ""))
        features.setdefault("side", getattr(ctx, "side", ""))
        features.setdefault("entry_price", getattr(ctx, "entry_price", 0.0))
        features.setdefault("price", price)
        features.setdefault("pnl", pnl)
        features.setdefault("collapse_prob", collapse_prob)

        if hasattr(ctx, "profit_pct"):
            features.setdefault("profit_pct", ctx.profit_pct(price))

        features.setdefault("atr_1min", getattr(ctx, "atr_1min", 0.0))
        features.setdefault("mfe", getattr(ctx, "mfe", 0.0))
        features.setdefault("mae", getattr(ctx, "mae", 0.0))
        features.setdefault("highest", getattr(ctx, "highest", 0.0))
        features.setdefault("lowest", getattr(ctx, "lowest", 0.0))
        features.setdefault("vwap", getattr(ctx, "vwap", 0.0))
        features.setdefault("state", getattr(ctx, "state", ""))

    except Exception:
        logger.exception("[AI_EXIT] feature enrich failed")

    return features


def inject_daily_features_safe(symbol: str, features: Dict[str, Any]) -> Dict[str, Any]:
    if not AI_EXIT_USE_DAILY_CACHE:
        return features

    try:
        from trading.daily.daily_signal_cache import (
            get_daily_decision,
            is_daily_cache_ready,
            warmup_daily_signal_cache,
            get_daily_cache_size,
        )

        if not is_daily_cache_ready():
            logger.warning("[AI_EXIT DAILY] daily cache not ready. fallback warmup now.")
            warmup_daily_signal_cache()

        dec = get_daily_decision(symbol)

        if dec is None:
            features.setdefault("daily_score", 0.0)
            features.setdefault("daily_buy_score", 0.0)
            features.setdefault("daily_sell_score", 0.0)
            features.setdefault("daily_ok_buy", False)
            features.setdefault("daily_ok_sell", False)
            features.setdefault("daily_exit_warn", False)
            features.setdefault("daily_reason", "")
            features.setdefault("daily_date", "")

            logger.debug(
                "[AI_EXIT DAILY] no daily decision symbol=%s cache_size=%s",
                symbol,
                get_daily_cache_size(),
            )
            return features

        features["daily_score"] = safe_float(getattr(dec, "daily_score", 0.0))
        features["daily_buy_score"] = safe_float(getattr(dec, "daily_buy_score", 0.0))
        features["daily_sell_score"] = safe_float(getattr(dec, "daily_sell_score", 0.0))
        features["daily_ok_buy"] = bool(getattr(dec, "daily_ok_buy", False))
        features["daily_ok_sell"] = bool(getattr(dec, "daily_ok_sell", False))
        features["daily_exit_warn"] = bool(getattr(dec, "daily_exit_warn", False))
        features["daily_reason"] = safe_str(getattr(dec, "reason", ""))
        features["daily_date"] = safe_str(getattr(dec, "date", ""))

        logger.debug(
            "[AI_EXIT DAILY] attached symbol=%s daily=%.2f buy=%.2f sell=%.2f "
            "ok_sell=%s exit_warn=%s date=%s",
            symbol,
            features["daily_score"],
            features["daily_buy_score"],
            features["daily_sell_score"],
            features["daily_ok_sell"],
            features["daily_exit_warn"],
            features["daily_date"],
        )

        return features

    except Exception:
        logger.exception("[AI_EXIT DAILY] attach failed symbol=%s", symbol)
        return features


def build_5sec_exit_features_safe(
    *,
    symbol: str,
    features: Dict[str, Any],
    ctx: Any,
    pos: Dict[str, Any],
    bar5s: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    殿様EXITへ渡す5秒足特徴量を作る。

    優先順位:
      1. bar5s dict
      2. features
      3. ctx attribute
      4. pos dict
    """

    bar5s = bar5s if isinstance(bar5s, dict) else {}

    def from_bar_float(*names: str) -> Optional[float]:
        raw = dict_get_any(bar5s, *names, default=None)
        return safe_float_or_none(raw)

    def from_bar_int(*names: str) -> Optional[int]:
        raw = dict_get_any(bar5s, *names, default=None)
        return safe_int_or_none(raw)

    def from_bar_bool(*names: str) -> Optional[bool]:
        raw = dict_get_any(bar5s, *names, default=None)
        return safe_bool_or_none(raw)

    bar5s_drop_pct = from_bar_float(
        "bar5s_drop_pct",
        "five_sec_drop_pct",
        "drop_pct",
        "pct_change",
        "change_pct",
        "return_pct",
    )
    if bar5s_drop_pct is None:
        bar5s_drop_pct = get_feature_value_or_none(
            features,
            ctx,
            pos,
            "bar5s_drop_pct",
            "five_sec_drop_pct",
            "drop_pct",
            "pct_change",
            "change_pct",
            "return_pct",
        )

    bar5s_consecutive_down = from_bar_int(
        "bar5s_consecutive_down",
        "five_sec_consecutive_down",
        "consecutive_down",
        "down_count",
        "down_bars",
    )
    if bar5s_consecutive_down is None:
        bar5s_consecutive_down = get_feature_int_or_none(
            features,
            ctx,
            pos,
            "bar5s_consecutive_down",
            "five_sec_consecutive_down",
            "consecutive_down",
            "down_count",
            "down_bars",
        )

    bar5s_consecutive_up = from_bar_int(
        "bar5s_consecutive_up",
        "five_sec_consecutive_up",
        "consecutive_up",
        "up_count",
        "up_bars",
    )
    if bar5s_consecutive_up is None:
        bar5s_consecutive_up = get_feature_int_or_none(
            features,
            ctx,
            pos,
            "bar5s_consecutive_up",
            "five_sec_consecutive_up",
            "consecutive_up",
            "up_count",
            "up_bars",
        )

    bar5s_volume_ratio = from_bar_float(
        "bar5s_volume_ratio",
        "five_sec_volume_ratio",
        "volume_ratio",
        "vol_ratio",
        "volume_to_avg",
        "vol_to_avg",
    )
    if bar5s_volume_ratio is None:
        bar5s_volume_ratio = get_feature_value_or_none(
            features,
            ctx,
            pos,
            "bar5s_volume_ratio",
            "five_sec_volume_ratio",
            "volume_ratio",
            "vol_ratio",
            "volume_to_avg",
            "vol_to_avg",
        )

    bar5s_vwap_break = from_bar_bool(
        "bar5s_vwap_break",
        "five_sec_vwap_break",
        "vwap_break",
        "is_vwap_break",
        "below_vwap",
    )
    if bar5s_vwap_break is None:
        bar5s_vwap_break = get_feature_bool_or_none(
            features,
            ctx,
            pos,
            "bar5s_vwap_break",
            "five_sec_vwap_break",
            "vwap_break",
            "is_vwap_break",
            "below_vwap",
        )

    bar5s_vwap_above = from_bar_bool(
        "bar5s_vwap_above",
        "five_sec_vwap_above",
        "vwap_above",
        "is_vwap_above",
        "above_vwap",
        "vwap_cross_up",
    )
    if bar5s_vwap_above is None:
        bar5s_vwap_above = get_feature_bool_or_none(
            features,
            ctx,
            pos,
            "bar5s_vwap_above",
            "five_sec_vwap_above",
            "vwap_above",
            "is_vwap_above",
            "above_vwap",
            "vwap_cross_up",
        )

    bar5s_high_after_entry = from_bar_float(
        "bar5s_high_after_entry",
        "five_sec_high_after_entry",
        "high_after_entry",
        "highest_after_entry",
        "highest",
        "high",
    )
    if bar5s_high_after_entry is None:
        bar5s_high_after_entry = get_feature_value_or_none(
            features,
            ctx,
            pos,
            "bar5s_high_after_entry",
            "five_sec_high_after_entry",
            "high_after_entry",
            "highest_after_entry",
            "highest",
        )

    bar5s_low_after_entry = from_bar_float(
        "bar5s_low_after_entry",
        "five_sec_low_after_entry",
        "low_after_entry",
        "lowest_after_entry",
        "lowest",
        "low",
    )
    if bar5s_low_after_entry is None:
        bar5s_low_after_entry = get_feature_value_or_none(
            features,
            ctx,
            pos,
            "bar5s_low_after_entry",
            "five_sec_low_after_entry",
            "low_after_entry",
            "lowest_after_entry",
            "lowest",
        )

    out = {
        "bar5s_drop_pct": bar5s_drop_pct,
        "bar5s_consecutive_down": bar5s_consecutive_down,
        "bar5s_consecutive_up": bar5s_consecutive_up,
        "bar5s_volume_ratio": bar5s_volume_ratio,
        "bar5s_vwap_break": bar5s_vwap_break,
        "bar5s_vwap_above": bar5s_vwap_above,
        "bar5s_high_after_entry": bar5s_high_after_entry,
        "bar5s_low_after_entry": bar5s_low_after_entry,
    }

    logger.debug(
        "[TONOSAMA 5SEC FEATURES] symbol=%s drop=%s down=%s up=%s vol_ratio=%s "
        "vwap_break=%s vwap_above=%s high_after=%s low_after=%s",
        symbol,
        out["bar5s_drop_pct"],
        out["bar5s_consecutive_down"],
        out["bar5s_consecutive_up"],
        out["bar5s_volume_ratio"],
        out["bar5s_vwap_break"],
        out["bar5s_vwap_above"],
        out["bar5s_high_after_entry"],
        out["bar5s_low_after_entry"],
    )

    return out


__all__ = [
    "build_exit_features_safe",
    "inject_daily_features_safe",
    "build_5sec_exit_features_safe",
]
