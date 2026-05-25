# ============================================================
# File   : core/startup/ranking_entry_volume_unit_patch.py
# Version: V1-RANKING-ENTRY-VOLUME-TURNOVER-UNIT-NORMALIZE
# ------------------------------------------------------------
# 目的:
#   ランキング由来エントリーで VOLUME_NG / TURNOVER_NG が大量発生する問題を補正する。
#
# 背景:
#   kabu StationランキングAPI由来の売買高・売買代金は、画面/API上の単位が
#   「千株」「百万円」等の縮約値で来ることがある。
#   entry_from_ranking.py はその値を株数/円としてそのまま判定していたため、
#   例: volume=13316.0 が 13,316株扱いになり、MIN_VOLUME=30,000で落ちる。
#
# 対応:
#   - ranking row 正規化後に volume / turnover を実単位へ推定補正する。
#   - volume: 0 < volume < MIN_VOLUME の場合は千株単位とみなし *1000 を試す。
#   - turnover: 0 < turnover < MIN_TURNOVER の場合は百万円単位とみなし *1,000,000 を試す。
#   - turnover が無い場合は price * normalized_volume で補完する。
#   - 元値は ranking_raw_volume / ranking_raw_turnover に残す。
#   - 低位株の過剰排除を避けるため、ランキング由来の最低価格を 200円へ緩和する。
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)
_PATCHED = False

_TRUE = {"1", "true", "yes", "y", "on", "enable", "enabled"}
_FALSE = {"0", "false", "no", "n", "off", "disable", "disabled"}


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        s = str(v).strip().lower()
        if s in _TRUE:
            return True
        if s in _FALSE:
            return False
    except Exception:
        pass
    return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is not None and str(v).strip() != "":
            return float(v)
    except Exception:
        pass
    return float(default)


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        s = str(v).strip()
        if not s or s.lower() in {"nan", "none", "nat", "<na>"}:
            return default
        return float(s.replace(",", "").replace("%", ""))
    except Exception:
        return default


def _normalize_units(row: dict[str, Any], *, min_volume: float, min_turnover: float) -> dict[str, Any]:
    if not _env_bool("RANKING_ENTRY_NORMALIZE_VOLUME_UNITS", True):
        return row

    try:
        price = _safe_float(row.get("price") or row.get("current_price") or row.get("close_price") or row.get("close"), 0.0)
        raw_volume = _safe_float(row.get("volume") or row.get("trading_volume"), 0.0)
        raw_turnover = _safe_float(row.get("turnover") or row.get("trading_value"), 0.0)

        volume_multiplier = _env_float("RANKING_ENTRY_VOLUME_UNIT_MULTIPLIER", 1000.0)
        turnover_multiplier = _env_float("RANKING_ENTRY_TURNOVER_UNIT_MULTIPLIER", 1000000.0)

        volume = raw_volume
        turnover = raw_turnover
        volume_unit_fixed = False
        turnover_unit_fixed = False

        # ランキングの売買高は千株単位で来るケースがある。
        if 0.0 < raw_volume < min_volume:
            candidate = raw_volume * volume_multiplier
            if candidate >= min_volume or raw_volume >= 1.0:
                volume = candidate
                volume_unit_fixed = True

        # ランキングの売買代金は百万円単位で来るケースがある。
        if 0.0 < raw_turnover < min_turnover:
            candidate = raw_turnover * turnover_multiplier
            if candidate >= min_turnover or raw_turnover >= 1.0:
                turnover = candidate
                turnover_unit_fixed = True

        # turnoverが無い/小さすぎる場合は、補正後volumeから円換算で補完する。
        if price > 0 and volume > 0:
            calc_turnover = price * volume
            if turnover <= 0 or turnover < min_turnover <= calc_turnover:
                turnover = calc_turnover
                turnover_unit_fixed = turnover_unit_fixed or raw_turnover > 0

        row["ranking_raw_volume"] = raw_volume
        row["ranking_raw_turnover"] = raw_turnover
        row["ranking_volume_unit_fixed"] = int(volume_unit_fixed)
        row["ranking_turnover_unit_fixed"] = int(turnover_unit_fixed)
        row["volume"] = volume
        row["trading_volume"] = volume
        row["turnover"] = turnover
        row["trading_value"] = turnover

        if volume_unit_fixed or turnover_unit_fixed:
            logger.info(
                "[RANKING ENTRY UNIT FIX] symbol=%s price=%s volume %.3f->%.3f turnover %.3f->%.3f vol_fixed=%s turn_fixed=%s",
                row.get("symbol"), price, raw_volume, volume, raw_turnover, turnover, volume_unit_fixed, turnover_unit_fixed,
            )
    except Exception:
        logger.exception("[RANKING ENTRY UNIT FIX] normalize failed symbol=%s", row.get("symbol"))

    return row


def install() -> bool:
    global _PATCHED
    if _PATCHED:
        return True

    try:
        from config.ranking_entry_config import RANKING_ENTRY_CONFIG
        import trading.ranking.entry_from_ranking as target
    except Exception:
        logger.exception("[RANKING ENTRY UNIT FIX] import failed")
        return False

    try:
        # 低位株を完全排除しすぎない。明示ENVがあればそれを優先。
        price_cfg = RANKING_ENTRY_CONFIG.setdefault("PRICE", {})
        old_min = float(price_cfg.get("MIN", 300))
        new_min = _env_float("RANKING_ENTRY_PRICE_MIN", 200.0)
        price_cfg["MIN"] = new_min

        vol_cfg = RANKING_ENTRY_CONFIG.setdefault("VOLUME", {})
        min_volume = _env_float("RANKING_ENTRY_MIN_VOLUME", float(vol_cfg.get("MIN_VOLUME", 30000)))
        min_turnover = _env_float("RANKING_ENTRY_MIN_TURNOVER", float(vol_cfg.get("MIN_TURNOVER", 10000000)))
        vol_cfg["MIN_VOLUME"] = min_volume
        vol_cfg["MIN_TURNOVER"] = min_turnover

        old_norm = getattr(target, "_normalize_ranking_row_for_entry", None)
        if not callable(old_norm):
            logger.warning("[RANKING ENTRY UNIT FIX] target normalizer not callable")
            return False
        if getattr(old_norm, "_ranking_entry_unit_fix_patch", False):
            _PATCHED = True
            return True

        def _normalize_ranking_row_for_entry_patched(row: dict[str, Any]) -> dict[str, Any]:
            out = old_norm(row)
            if isinstance(out, dict):
                return _normalize_units(out, min_volume=min_volume, min_turnover=min_turnover)
            return out

        _normalize_ranking_row_for_entry_patched._ranking_entry_unit_fix_patch = True  # type: ignore[attr-defined]
        _normalize_ranking_row_for_entry_patched._original = old_norm  # type: ignore[attr-defined]
        target._normalize_ranking_row_for_entry = _normalize_ranking_row_for_entry_patched

        _PATCHED = True
        logger.warning(
            "[RANKING ENTRY UNIT FIX] installed price_min %.1f->%.1f min_volume=%.1f min_turnover=%.1f normalize_units=%s",
            old_min, new_min, min_volume, min_turnover, _env_bool("RANKING_ENTRY_NORMALIZE_VOLUME_UNITS", True),
        )
        return True
    except Exception:
        logger.exception("[RANKING ENTRY UNIT FIX] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[RANKING ENTRY UNIT FIX] auto install failed")


__all__ = ["install"]
