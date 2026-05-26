# ============================================================
# File   : core/startup/ranking_entry_flat_price_guard_patch.py
# Version: V1.0-RANKING-FLAT-PRICE-RANK-STRENGTH-GUARD
# ------------------------------------------------------------
# 目的:
#   ランキング由来エントリーで、価格が前回ランキング取得時と同値のため
#   BUY_PRICE_NOT_UP / SELL_PRICE_NOT_DOWN で大量DROPされる問題を緩和する。
#
# 方針:
#   - 価格横ばいでも、順位が上位または改善/維持なら一度だけ再判定する。
#   - 再判定では original filter を再利用し、出来高/売買代金/日中過熱/テクニカル/スコアは維持。
#   - volume=9.8 / 28.1 のようなランキング表示単位が最終フィルタ直前に残る場合、
#     x1000 補正を再適用する。
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Tuple

logger = logging.getLogger(__name__)

_PATCHED = False
_ORIGINAL_FILTER = None


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _f(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        x = float(str(v).replace(",", ""))
        return float(default) if x != x else x
    except Exception:
        return float(default)


def _i(v: Any, default: int = 999999) -> int:
    try:
        return int(float(v))
    except Exception:
        return int(default)


def _get_cfg() -> dict:
    try:
        from config.ranking_entry_config import RANKING_ENTRY_CONFIG
        return RANKING_ENTRY_CONFIG
    except Exception:
        return {}


def _repair_volume_units(row: Dict[str, Any]) -> Dict[str, Any]:
    cfg = _get_cfg()
    vol_cfg = cfg.get("VOLUME", {}) if isinstance(cfg, dict) else {}
    min_volume = _f(vol_cfg.get("MIN_VOLUME", 30000), 30000)
    min_turnover = _f(vol_cfg.get("MIN_TURNOVER", 10000000), 10000000)
    mul = _env_float("RANKING_ENTRY_LAST_CHANCE_VOLUME_MULTIPLIER", 1000.0)

    price = _f(row.get("price") or row.get("current_price") or row.get("close"), 0.0)
    volume = _f(row.get("volume") or row.get("trading_volume"), 0.0)
    turnover = _f(row.get("turnover") or row.get("trading_value"), 0.0)
    raw_v = volume
    raw_t = turnover

    if 0 < volume < min_volume and mul > 1:
        volume = volume * mul
        row["volume"] = volume
        row["trading_volume"] = volume
        row["ranking_last_chance_volume_fixed"] = True
        row["ranking_last_chance_volume_raw"] = raw_v

    implied = price * volume if price > 0 and volume > 0 else 0.0
    if implied > turnover:
        turnover = implied
    if 0 < turnover < min_turnover and price > 0 and volume > 0:
        turnover = max(turnover, implied)
    if turnover > 0:
        row["turnover"] = turnover
        row["trading_value"] = turnover

    if raw_v != volume or raw_t != turnover:
        logger.info(
            "[RANKING FLAT PRICE PATCH] last-chance volume normalize symbol=%s price=%s volume %s->%s turnover %s->%s min_volume=%s",
            row.get("symbol"), price, raw_v, volume, raw_t, turnover, min_volume,
        )
    return row


def _flat_price_allowed(row: Dict[str, Any], prev_h: Dict[str, Any], reason: str) -> bool:
    if not _env_bool("RANKING_ENTRY_ALLOW_FLAT_PRICE_IF_RANK_STRONG", True):
        return False
    if not (str(reason).startswith("BUY_PRICE_NOT_UP") or str(reason).startswith("SELL_PRICE_NOT_DOWN")):
        return False

    cfg = _get_cfg()
    rank_cfg = cfg.get("RANKING", {}) if isinstance(cfg, dict) else {}
    max_rank = _i(rank_cfg.get("FLAT_PRICE_ALLOW_MAX_RANK", 12), 12)
    rank = _i(row.get("rank_position"), 999999)
    prev_rank = _i(prev_h.get("last_rank_position"), 999999)
    consecutive = _i(prev_h.get("consecutive"), 0) + 1 if prev_h else 1
    min_consecutive = _i(rank_cfg.get("MIN_CONSECUTIVE_APPEAR", 2), 2)

    rank_not_worse = prev_rank < 999999 and rank <= prev_rank
    rank_top = rank <= max_rank
    return consecutive >= min_consecutive and (rank_not_worse or rank_top)


def _patched_filter(row: Dict[str, Any], side: str, prev_h: Dict[str, Any], score: float, parts: Dict[str, float]) -> Tuple[bool, str]:
    if callable(_ORIGINAL_FILTER):
        row = _repair_volume_units(row)
        ok, reason = _ORIGINAL_FILTER(row, side, prev_h, score, parts)
        if ok:
            return ok, reason

        if _flat_price_allowed(row, prev_h, reason):
            price = _f(row.get("price") or row.get("current_price"), 0.0)
            patched_prev = dict(prev_h or {})
            if price > 0:
                if str(side).upper() == "BUY":
                    patched_prev["last_price"] = price * 0.999999
                else:
                    patched_prev["last_price"] = price * 1.000001
            ok2, reason2 = _ORIGINAL_FILTER(row, side, patched_prev, score, parts)
            if ok2:
                logger.info(
                    "[RANKING FLAT PRICE PATCH] pass flat price symbol=%s side=%s rank=%s prev_rank=%s reason=%s",
                    row.get("symbol"), side, row.get("rank_position"), prev_h.get("last_rank_position"), reason,
                )
                return True, "OK_FLAT_PRICE_RANK_STRONG"
            return False, reason2
        return ok, reason
    return False, "ORIGINAL_FILTER_NOT_AVAILABLE"


def install() -> bool:
    global _PATCHED, _ORIGINAL_FILTER
    if _PATCHED:
        return True
    try:
        import trading.ranking.entry_from_ranking as efr
        cur = getattr(efr, "_passes_ranking_only_filters", None)
        if not callable(cur):
            logger.warning("[RANKING FLAT PRICE PATCH] original filter not callable")
            return False
        if getattr(cur, "_ranking_flat_price_patch", False):
            _PATCHED = True
            return True
        _ORIGINAL_FILTER = cur
        _patched_filter._ranking_flat_price_patch = True  # type: ignore[attr-defined]
        efr._passes_ranking_only_filters = _patched_filter
        _PATCHED = True
        logger.warning(
            "[RANKING FLAT PRICE PATCH] installed V1 allow_flat=%s max_rank=%s",
            _env_bool("RANKING_ENTRY_ALLOW_FLAT_PRICE_IF_RANK_STRONG", True),
            _env_float("RANKING_ENTRY_FLAT_PRICE_ALLOW_MAX_RANK", 12),
        )
        return True
    except Exception:
        logger.exception("[RANKING FLAT PRICE PATCH] install failed")
        return False


__all__ = ["install"]
