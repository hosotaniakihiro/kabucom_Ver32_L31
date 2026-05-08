# ============================================================
# File: AI/entry_gate_sell_tonosama.py
# Version: PRODUCTION-STABLE-V2-COMPAT-SYMBOL-OR-FEATURE-ROW
# ------------------------------------------------------------
# 殿様イナゴ（SELL）専用 ENTRY ゲート
#
# ✔ LightGBM 60秒分類モデル（下げ）を使用
# ✔ feature_row dict / symbol str の両方を受け付ける
# ✔ entry_controller が allow_sell_tonosama_entry(symbol) と呼ぶ既存実装に対応
# ✔ pending_entries の TONOSAMA entry から最低限の特徴量を復元
# ✔ モデル/特徴量不足時は fail-closed
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import math
import os
from typing import Any, Dict

import lightgbm as lgb
import numpy as np

logger = logging.getLogger(__name__)


# ============================================================
# 設定
# ============================================================

MODEL_PATH = os.environ.get("SELL_TONOSAMA_MODEL_PATH", "sell_tonosama_lgbm.txt")

FEATURES = [
    "price_velocity",
    "volume_drop",
    "rank_fall",
    "sell_pressure",
    "spread_ratio",
    "minute_from_open",
]

MIN_PROB = 0.55
MIN_SCORE = 1.30
MAX_SPREAD_RATIO = 0.004
NO_SELL_BEFORE_MIN = 10

_model: lgb.Booster | None = None


# ============================================================
# helpers
# ============================================================

def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        if isinstance(v, float) and math.isnan(v):
            return default
        if isinstance(v, str) and v.strip() == "":
            return default
        x = float(v)
        return x if math.isfinite(x) else default
    except Exception:
        return default


def _safe_str(v: Any, default: str = "") -> str:
    try:
        if v is None:
            return default
        return str(v).strip()
    except Exception:
        return default


def _minute_from_open(now: dt.datetime | None = None) -> int:
    now = now or dt.datetime.now()
    open_dt = now.replace(hour=9, minute=0, second=0, microsecond=0)
    return max(int((now - open_dt).total_seconds() // 60), 0)


def _normalize_symbol(v: Any) -> str:
    return _safe_str(v).upper().replace(".T", "").replace(".0", "")


def _first(row: Dict[str, Any], keys: tuple[str, ...], default: Any = None) -> Any:
    for k in keys:
        try:
            if k in row and row.get(k) is not None and str(row.get(k)).strip() != "":
                return row.get(k)
        except Exception:
            pass
    return default


def _get_pending_entry_for_symbol(symbol: str) -> Dict[str, Any]:
    try:
        from global_state import global_data

        sym = _normalize_symbol(symbol)
        root = getattr(global_data, "pending_entries", {}) or {}
        bucket = root.get(sym) or root.get(str(symbol)) or []
        if isinstance(bucket, dict):
            bucket = [bucket]
        if not isinstance(bucket, list):
            return {}

        for e in bucket:
            if not isinstance(e, dict):
                continue
            src = _safe_str(e.get("source")).upper()
            typ = _safe_str(e.get("entry_type")).upper()
            side = _safe_str(e.get("side") or e.get("entry_decision")).upper()
            if side == "SELL" and (src == "TONOSAMA" or typ == "TONOSAMA"):
                return dict(e)

        for e in bucket:
            if isinstance(e, dict):
                return dict(e)
    except Exception:
        logger.debug("[TONOSAMA SELL] pending lookup failed symbol=%s", symbol, exc_info=True)
    return {}


def _feature_from_entry_row(row: Dict[str, Any]) -> Dict[str, float]:
    if not isinstance(row, dict) or not row:
        return {}

    if all(f in row for f in FEATURES):
        return {f: _safe_float(row.get(f), 0.0) for f in FEATURES}

    close = _safe_float(_first(row, ("close", "close_price", "current_price", "price"), 0.0), 0.0)
    rank_now = _safe_float(_first(row, ("rank_now", "rank", "rank_position", "ranking_rank"), 0.0), 0.0)
    rank_prev = _safe_float(_first(row, ("rank_prev", "prev_rank", "rank_previous"), 0.0), 0.0)

    price_velocity = _safe_float(
        _first(row, ("price_velocity", "slope", "slope_atr_scaled", "score_slope"), 0.0),
        0.0,
    )
    if abs(price_velocity) > 1.0:
        price_velocity = price_velocity / 100.0

    volume_drop = _safe_float(_first(row, ("volume_drop", "volume_peakout", "volume_ratio"), 1.0), 1.0)
    if volume_drop <= 0:
        volume_drop = 1.0

    if rank_now > 0 and rank_prev > 0:
        rank_fall = rank_now - rank_prev
    else:
        rank_fall = _safe_float(row.get("rank_fall"), 0.0)

    sell_pressure = _safe_float(_first(row, ("sell_pressure", "dominant_ratio", "board_sell_pressure"), 1.0), 1.0)
    if sell_pressure <= 0:
        sell_pressure = 1.0

    spread_ratio = _safe_float(_first(row, ("spread_ratio", "board_spread_ratio"), 0.0), 0.0)
    spread = _safe_float(_first(row, ("spread", "board_spread"), 0.0), 0.0)
    if spread_ratio <= 0 and spread > 0 and close > 0:
        spread_ratio = spread / close

    return {
        "price_velocity": float(price_velocity),
        "volume_drop": float(volume_drop),
        "rank_fall": float(rank_fall),
        "sell_pressure": float(sell_pressure),
        "spread_ratio": float(spread_ratio),
        "minute_from_open": float(_safe_float(row.get("minute_from_open"), _minute_from_open())),
    }


def _resolve_feature_row(feature_or_symbol: Any) -> Dict[str, float]:
    if isinstance(feature_or_symbol, dict):
        return _feature_from_entry_row(feature_or_symbol)

    symbol = _normalize_symbol(feature_or_symbol)
    if not symbol:
        return {}

    pending = _get_pending_entry_for_symbol(symbol)
    if pending:
        return _feature_from_entry_row(pending)

    logger.info("[TONOSAMA SELL] feature row unavailable symbol=%s", symbol)
    return {}


# ============================================================
# model
# ============================================================

def _load_model() -> lgb.Booster:
    global _model
    if _model is None:
        if not os.path.exists(MODEL_PATH):
            raise FileNotFoundError(f"model not found: {MODEL_PATH}")
        _model = lgb.Booster(model_file=MODEL_PATH)
    return _model


# ============================================================
# public API
# ============================================================

def allow_sell_tonosama_entry(feature_row: Dict[str, float] | str) -> bool:
    """
    殿様イナゴ SELL の ENTRY 可否を判定する。

    互換性:
      - allow_sell_tonosama_entry(feature_row_dict)
      - allow_sell_tonosama_entry(symbol_str)
    """
    row = _resolve_feature_row(feature_row)
    if not row:
        return False

    for f in FEATURES:
        if f not in row:
            logger.info("[TONOSAMA SELL] missing feature=%s row=%s", f, row)
            return False

    try:
        minute_from_open = _safe_float(row.get("minute_from_open"), 0.0)
        if minute_from_open < NO_SELL_BEFORE_MIN:
            logger.info("[TONOSAMA SELL] blocked before open+%smin minute=%.1f", NO_SELL_BEFORE_MIN, minute_from_open)
            return False

        x = np.array([[float(row[f]) for f in FEATURES]], dtype=float)
        model = _load_model()
        prob = float(model.predict(x)[0])

        sell_pressure = _safe_float(row.get("sell_pressure"), 0.0)
        volume_drop = max(_safe_float(row.get("volume_drop"), 1.0), 1e-6)
        spread_ratio = _safe_float(row.get("spread_ratio"), 0.0)
        score = prob * sell_pressure / volume_drop

        ok = bool(prob >= MIN_PROB and score >= MIN_SCORE and spread_ratio <= MAX_SPREAD_RATIO)
        logger.info(
            "[TONOSAMA SELL] gate ok=%s prob=%.4f score=%.4f sell_pressure=%.4f volume_drop=%.4f spread_ratio=%.6f minute=%.1f",
            ok,
            prob,
            score,
            sell_pressure,
            volume_drop,
            spread_ratio,
            minute_from_open,
        )
        return ok

    except Exception:
        logger.exception("[TONOSAMA SELL] gate failed row=%s", row)
        return False


__all__ = ["allow_sell_tonosama_entry"]
