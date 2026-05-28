# ============================================================
# File   : trading/entry/volume_direction_guard.py
# Version: V1-VOLUME-DIRECTION-ENTRY-GUARD
# ------------------------------------------------------------
# 目的:
#   SUMMARY / RANKING / TONOSAMA の3種類すべてのエントリーで、
#   出来高急増を単独評価せず、価格方向と直前トレンドを合わせて判定する。
#
# 考え方:
#   - BUYで「上がってきて出来高急増」は、買い遅れ・利確売り・天井掴みを警戒して拒否。
#   - SELLで「下がってきて出来高急増」は、売り遅れ・買戻し・底売りを警戒して拒否。
#   - 横横の場合は、直前の slope / 3m・5m streak / 直近delta から方向を推定する。
#
# 使い方:
#   core.startup.entry_volume_direction_guard_patch から AI final gate をwrapして使う。
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return bool(default)
        return str(raw).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return float(default)
        return float(raw)
    except Exception:
        return float(default)


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _safe_str(v: Any, default: str = "") -> str:
    try:
        if v is None:
            return default
        return str(v).strip()
    except Exception:
        return default


def _first_num(row: dict, names: list[str], default: float = 0.0) -> float:
    for n in names:
        if n in row:
            val = _safe_float(row.get(n), None)  # type: ignore[arg-type]
            if val is not None:
                return float(val)
    return float(default)


def _infer_side(row: dict) -> str:
    side = _safe_str(row.get("side") or row.get("entry_decision") or row.get("signal") or "").upper()
    if side in {"BUY", "SELL"}:
        return side
    sb = _first_num(row, ["score_buy", "buy_score"], 0.0)
    ss = _first_num(row, ["score_sell", "sell_score"], 0.0)
    if ss > sb and ss > 0:
        return "SELL"
    if sb > ss and sb > 0:
        return "BUY"
    score = _first_num(row, ["score", "final_score", "display_score"], 0.0)
    return "SELL" if score < 0 else "BUY"


def _price_change_pct(row: dict) -> float:
    # 既存の殿様featuresがあれば最優先。
    direct = _first_num(
        row,
        [
            "_max_price_change_pct",
            "price_change_pct",
            "price_change_pct_1m",
            "price_change_1m_pct",
            "price_change_5s_pct",
        ],
        0.0,
    )
    if abs(direct) > 0:
        return direct

    close = _first_num(row, ["close", "close_price", "current_price", "price"], 0.0)
    prev = _first_num(row, ["prev_close", "prev_close_1m", "prev_close_3m", "prev_close_5m"], 0.0)
    if close > 0 and prev > 0:
        return (close - prev) / prev * 100.0

    open_p = _first_num(row, ["open", "open_price"], 0.0)
    if close > 0 and open_p > 0:
        return (close - open_p) / open_p * 100.0
    return 0.0


def _volume_surge_ratio(row: dict) -> float:
    vals = [
        _first_num(row, ["_max_volume_surge_ratio"], 0.0),
        _first_num(row, ["volume_surge_ratio", "volume_surge_ratio_1m"], 0.0),
        _first_num(row, ["volume_surge_ratio_3m"], 0.0),
        _first_num(row, ["volume_surge_ratio_5m"], 0.0),
        _first_num(row, ["volume_surge_ratio_5s"], 0.0),
    ]
    return max([v for v in vals if v is not None] or [0.0])


def _trend_score(row: dict) -> float:
    """上向きなら正、下向きなら負。横横は0近辺。"""
    score = 0.0

    slope = _first_num(row, ["_slope", "slope", "slope_1m", "score_slope", "slope_atr_scaled"], 0.0)
    slope_min = _env_float("ENTRY_VOLUME_DIRECTION_SLOPE_EPS", 0.001)
    if slope > slope_min:
        score += 1.0
    elif slope < -slope_min:
        score -= 1.0

    for name in ["prev_3m_up_streak", "prev_5m_up_streak", "up_streak", "prev_up_streak"]:
        if _first_num(row, [name], 0.0) >= 2:
            score += 1.0
            break
    for name in ["prev_3m_down_streak", "prev_5m_down_streak", "down_streak", "prev_down_streak"]:
        if _first_num(row, [name], 0.0) >= 2:
            score -= 1.0
            break

    d3 = _first_num(row, ["prev_3m_last_delta_pct", "price_change_pct_3m"], 0.0)
    d5 = _first_num(row, ["prev_5m_last_delta_pct", "price_change_pct_5m"], 0.0)
    delta_eps = _env_float("ENTRY_VOLUME_DIRECTION_DELTA_EPS", 0.05)
    if d3 > delta_eps or d5 > delta_eps:
        score += 0.75
    elif d3 < -delta_eps or d5 < -delta_eps:
        score -= 0.75

    # MA位置も補助。横横時にどちらへ傾いているかを見る。
    close = _first_num(row, ["close", "close_price", "current_price", "price"], 0.0)
    ma5 = _first_num(row, ["ma5", "ma5_1m", "ma5_3m", "ma5_5m"], 0.0)
    if close > 0 and ma5 > 0:
        gap = (close - ma5) / ma5 * 100.0
        ma_gap_eps = _env_float("ENTRY_VOLUME_DIRECTION_MA_GAP_EPS", 0.05)
        if gap > ma_gap_eps:
            score += 0.5
        elif gap < -ma_gap_eps:
            score -= 0.5

    return score


def evaluate_volume_direction(row: dict) -> dict:
    """出来高急増と価格方向の整合性を評価する。

    return:
      {
        ok: bool,
        reason: str,
        side: BUY/SELL,
        volume_surge_ratio: float,
        price_change_pct: float,
        trend_direction: UP/DOWN/FLAT,
        action: PASS/REJECT/WARN
      }
    """
    if not _env_bool("ENTRY_VOLUME_DIRECTION_GUARD", True):
        return {"ok": True, "reason": "DISABLED", "action": "PASS"}
    if not isinstance(row, dict):
        return {"ok": True, "reason": "ROW_INVALID_PASS", "action": "PASS"}

    side = _infer_side(row)
    vol = _volume_surge_ratio(row)
    min_vol = _env_float("ENTRY_VOLUME_DIRECTION_MIN_SURGE_RATIO", 2.0)
    if vol < min_vol:
        return {
            "ok": True,
            "reason": "VOLUME_SURGE_LOW",
            "action": "PASS",
            "side": side,
            "volume_surge_ratio": vol,
        }

    move = _price_change_pct(row)
    move_eps = _env_float("ENTRY_VOLUME_DIRECTION_PRICE_EPS_PCT", 0.15)
    trend = _trend_score(row)
    trend_eps = _env_float("ENTRY_VOLUME_DIRECTION_TREND_EPS", 0.75)

    if move > move_eps:
        direction = "UP"
    elif move < -move_eps:
        direction = "DOWN"
    elif trend > trend_eps:
        direction = "UP_FROM_FLAT"
    elif trend < -trend_eps:
        direction = "DOWN_FROM_FLAT"
    else:
        direction = "FLAT_UNKNOWN"

    # ユーザー方針:
    # BUY: 上がってきて出来高増 = 売りサイン/天井警戒 → 拒否
    # SELL: 下がってきて出来高増 = 買い戻し/底警戒 → 拒否
    reject = False
    if side == "BUY" and direction in {"UP", "UP_FROM_FLAT"}:
        reject = True
        reason = "BUY_REJECT_VOLUME_SURGE_AFTER_UP_MOVE"
    elif side == "SELL" and direction in {"DOWN", "DOWN_FROM_FLAT"}:
        reject = True
        reason = "SELL_REJECT_VOLUME_SURGE_AFTER_DOWN_MOVE"
    else:
        reason = "VOLUME_DIRECTION_OK"

    if reject and not _env_bool("ENTRY_VOLUME_DIRECTION_REJECT", True):
        return {
            "ok": True,
            "reason": reason + "_WARN_ONLY",
            "action": "WARN",
            "side": side,
            "volume_surge_ratio": vol,
            "price_change_pct": move,
            "trend_score": trend,
            "trend_direction": direction,
        }

    return {
        "ok": not reject,
        "reason": reason,
        "action": "REJECT" if reject else "PASS",
        "side": side,
        "volume_surge_ratio": vol,
        "price_change_pct": move,
        "trend_score": trend,
        "trend_direction": direction,
    }


__all__ = ["evaluate_volume_direction"]
