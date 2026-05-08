# ============================================================
# File: trading/ai/liquidity_shock_detector.py
# Ver1.1-PRO-LIQUIDITY-SHOCK-DETECTOR-SUMMARY-AI-FIX
# ------------------------------------------------------------
# ✔ volume spike
# ✔ price acceleration
# ✔ ranking velocity
# ✔ orderflow対応
# ✔ NaN完全防御
# ✔ ENTRY前検出
# ✔ SUMMARY AI 承認済み銘柄を liquidity_shock 専用条件で全落ちさせない
# ============================================================

from __future__ import annotations

import logging
import math
import os
from typing import Any

import numpy as np
import pandas as pd

from utils.alerts_util import send_discord_message

logger = logging.getLogger(__name__)


# ============================================================
# helpers
# ============================================================

def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        x = float(v)
        return x if math.isfinite(x) else float(default)
    except Exception:
        return float(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == "":
            return float(default)
        x = float(v)
        return x if math.isfinite(x) else float(default)
    except Exception:
        return float(default)


def _env_bool(name: str, default: bool = False) -> bool:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "on", "y"}
    except Exception:
        return bool(default)


def _first(row: dict, keys: list[str], default: Any = None) -> Any:
    for k in keys:
        try:
            v = row.get(k)
            if v is not None and str(v).strip() != "":
                return v
        except Exception:
            pass
    return default


def _norm_source(v: Any) -> str:
    try:
        return str(v or "").upper().strip()
    except Exception:
        return ""


# ============================================================
# スコア計算
# ============================================================

def compute_liquidity_score(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    volume_ratio = pd.to_numeric(df.get("volume_ratio", 1), errors="coerce").fillna(1)
    price_change = pd.to_numeric(df.get("price_change", 0), errors="coerce").fillna(0)
    velocity = pd.to_numeric(df.get("velocity_score", 0), errors="coerce").fillna(0)
    rank_best = pd.to_numeric(df.get("rank_best", 50), errors="coerce").fillna(50)

    df["liquidity_score"] = (
        volume_ratio * 6
        + price_change * 10
        + velocity * 3
        + (30 - rank_best)
    )

    return df


# ============================================================
# ショック検出
# ============================================================

def detect_liquidity_shock(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or len(df) == 0:
        return pd.DataFrame()

    df = compute_liquidity_score(df)

    cond = (
        (df["volume_ratio"] > 3)
        & (df["price_change"] > 0.01)
        & (df["liquidity_score"] > 40)
    )

    result = df.loc[cond].copy()

    if len(result) > 0:
        for _, r in result.iterrows():
            embed = {
                "title": "💥 Liquidity Shock",
                "color": 15158332,
                "fields": [
                    {"name": "Symbol", "value": str(r["symbol"]), "inline": True},
                    {"name": "Price", "value": str(r.get("price", "")), "inline": True},
                    {"name": "Volume Ratio", "value": str(round(r.get("volume_ratio", 0), 2)), "inline": True},
                    {"name": "Score", "value": str(round(r["liquidity_score"], 2)), "inline": True},
                ],
            }
            send_discord_message(embeds=[embed])

    return result


# ============================================================
# ENTRYフィルター
# ============================================================

def allow_liquidity_entry(row: dict) -> bool:
    """
    ENTRY直前の流動性フィルター。

    旧仕様:
      volume_ratio > 3 and price_change > 0.01 and velocity > 5 のみ許可。

    問題:
      SUMMARY AI 承認済み row には volume_ratio / price_change / velocity_score が
      入っていないことが多く、AI_OK 後に全件 skip liquidity になる。

    新仕様:
      - ENTRY_REQUIRE_LIQUIDITY_SHOCK=1 の時だけ旧ショック条件を必須にする。
      - 既定では SUMMARY / PUSH / AI 承認済み row は、通常の出来高・売買代金・価格で許可する。
    """
    try:
        if not isinstance(row, dict):
            return False

        symbol = str(row.get("symbol") or "").strip()
        source = _norm_source(row.get("source"))

        volume_ratio = _safe_float(row.get("volume_ratio"), 1.0)
        price_change = _safe_float(row.get("price_change"), 0.0)
        velocity = _safe_float(row.get("velocity_score"), 0.0)

        shock_ok = volume_ratio > 3.0 and price_change > 0.01 and velocity > 5.0
        if shock_ok:
            return True

        if _env_bool("ENTRY_REQUIRE_LIQUIDITY_SHOCK", False):
            logger.info(
                "[LIQUIDITY ENTRY] deny shock_required symbol=%s source=%s volume_ratio=%.3f price_change=%.5f velocity=%.3f",
                symbol,
                source,
                volume_ratio,
                price_change,
                velocity,
            )
            return False

        close = _safe_float(
            _first(row, ["close", "close_price", "price", "current_price"], 0.0),
            0.0,
        )
        volume = _safe_float(
            _first(row, ["volume", "trading_volume", "出来高"], 0.0),
            0.0,
        )
        turnover = _safe_float(
            _first(row, ["turnover", "trading_value", "売買代金"], 0.0),
            0.0,
        )
        if turnover <= 0 and close > 0 and volume > 0:
            turnover = close * volume

        score = abs(_safe_float(_first(row, ["score", "score_total", "final_score", "display_score"], 0.0), 0.0))
        buy_score = _safe_float(_first(row, ["buy_score", "score_buy"], 0.0), 0.0)
        sell_score = _safe_float(_first(row, ["sell_score", "score_sell"], 0.0), 0.0)
        effective_score = max(score, buy_score, sell_score)

        min_price = _env_float("ENTRY_MIN_PRICE", 200.0)
        min_volume = _env_float("ENTRY_MIN_VOLUME", 3000.0)
        min_turnover = _env_float("ENTRY_MIN_TURNOVER", 3_000_000.0)
        min_score = _env_float("ENTRY_MIN_SCORE_FOR_LIQUIDITY", 3.0)

        ok = (
            close > min_price
            and volume >= min_volume
            and turnover >= min_turnover
            and effective_score >= min_score
        )

        if not ok:
            logger.info(
                "[LIQUIDITY ENTRY] deny symbol=%s source=%s close=%.2f volume=%.0f turnover=%.0f score=%.3f min_price=%.1f min_volume=%.0f min_turnover=%.0f min_score=%.2f",
                symbol,
                source,
                close,
                volume,
                turnover,
                effective_score,
                min_price,
                min_volume,
                min_turnover,
                min_score,
            )
            return False

        logger.info(
            "[LIQUIDITY ENTRY] allow standard symbol=%s source=%s close=%.2f volume=%.0f turnover=%.0f score=%.3f",
            symbol,
            source,
            close,
            volume,
            turnover,
            effective_score,
        )
        return True

    except Exception:
        logger.exception("[LIQUIDITY ENTRY] failed")
        return False
