# ============================================================
# File   : core/startup/ranking_entry_volume_unit_patch.py
# Version: V4-RANKING-ENTRY-UNIT-FINALIZE-NO-ZERO-VOLUME-TURNOVER
# ------------------------------------------------------------
# 目的:
#   ランキング由来エントリーで VOLUME_NG / TURNOVER_NG が大量発生する問題を補正する。
#   ただし、補正の二重掛け・過剰掛けで流動性判定をすり抜ける事故を防ぐ。
#
# V4:
#   - volume=0 の行では turnover を一切単位補正しない
#   - ranking_entry_units_finalized=1 を付与し、後続patchの二重補正を防ぐ
#   - ranking_volume_unit_multiplier / ranking_turnover_unit_multiplier を常に明示保存
#   - turnover は price*volume と矛盾しない範囲に丸める
#   - 旧wrapperを _original で辿って外してから再patchする
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


def _flag_on(v: Any) -> bool:
    try:
        return str(v).strip().lower() in _TRUE
    except Exception:
        return False


def _unwrap(fn: Any) -> Any:
    try:
        seen = set()
        cur = fn
        while callable(getattr(cur, "_original", None)) and id(cur) not in seen:
            seen.add(id(cur))
            cur = getattr(cur, "_original")
        return cur
    except Exception:
        return fn


def _normalize_units(row: dict[str, Any], *, min_volume: float, min_turnover: float) -> dict[str, Any]:
    if not _env_bool("RANKING_ENTRY_NORMALIZE_VOLUME_UNITS", True):
        return row

    try:
        if _flag_on(row.get("ranking_entry_units_finalized")):
            return row

        price = _safe_float(row.get("price") or row.get("current_price") or row.get("close_price") or row.get("close"), 0.0)
        raw_volume = _safe_float(row.get("volume") if row.get("volume") is not None else row.get("trading_volume"), 0.0)
        raw_turnover = _safe_float(row.get("turnover") if row.get("turnover") is not None else row.get("trading_value"), 0.0)

        volume_multiplier = _env_float("RANKING_ENTRY_VOLUME_UNIT_MULTIPLIER", 1000.0)
        turnover_multiplier = _env_float("RANKING_ENTRY_TURNOVER_UNIT_MULTIPLIER", 1000000.0)
        implied_max_ratio = max(1.0, _env_float("RANKING_ENTRY_TURNOVER_IMPLIED_MAX_RATIO", 20.0))
        yen_floor = _env_float("RANKING_ENTRY_TURNOVER_YEN_FLOOR", 100000.0)

        volume = raw_volume
        turnover = raw_turnover
        volume_unit_fixed = False
        turnover_unit_fixed = False
        volume_unit_multiplier = 1.0
        turnover_unit_multiplier = 1.0

        if 0.0 < raw_volume < min_volume and volume_multiplier > 1:
            candidate = raw_volume * volume_multiplier
            if candidate >= min_volume:
                volume = candidate
                volume_unit_fixed = True
                volume_unit_multiplier = volume_multiplier

        implied_turnover = price * volume if price > 0 and volume > 0 else 0.0

        # volume が無い行では turnover だけを百万円補正しない。
        if volume > 0 and 0.0 < raw_turnover < min_turnover and raw_turnover < yen_floor and turnover_multiplier > 1:
            candidate_turnover = raw_turnover * turnover_multiplier
            upper = implied_turnover * implied_max_ratio if implied_turnover > 0 else candidate_turnover
            if candidate_turnover >= min_turnover and candidate_turnover <= max(upper, min_turnover):
                turnover = candidate_turnover
                turnover_unit_fixed = True
                turnover_unit_multiplier = turnover_multiplier

        if implied_turnover > 0:
            if turnover <= 0:
                turnover = implied_turnover
            elif turnover < min_turnover <= implied_turnover:
                turnover = implied_turnover
            elif turnover > implied_turnover * implied_max_ratio:
                logger.warning(
                    "[RANKING ENTRY UNIT FIX] turnover clamped symbol=%s price=%s volume=%s turnover=%s implied=%s",
                    row.get("symbol"), price, volume, turnover, implied_turnover,
                )
                turnover = implied_turnover

        row["ranking_raw_volume"] = raw_volume
        row["ranking_raw_turnover"] = raw_turnover
        row["ranking_volume_unit_fixed"] = int(volume_unit_fixed)
        row["ranking_turnover_unit_fixed"] = int(turnover_unit_fixed)
        row["ranking_volume_unit_multiplier"] = volume_unit_multiplier
        row["ranking_turnover_unit_multiplier"] = turnover_unit_multiplier
        row["ranking_entry_units_finalized"] = 1
        row["volume"] = volume
        row["trading_volume"] = volume
        row["turnover"] = turnover
        row["trading_value"] = turnover

        if volume_unit_fixed or turnover_unit_fixed or raw_turnover != turnover:
            logger.info(
                "[RANKING ENTRY UNIT FIX] finalized symbol=%s price=%s volume %.3f->%.3f turnover %.3f->%.3f vol_mul=%.0f turn_mul=%.0f implied=%.3f",
                row.get("symbol"), price, raw_volume, volume, raw_turnover, turnover,
                volume_unit_multiplier, turnover_unit_multiplier, implied_turnover,
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

        base_norm = _unwrap(old_norm)
        if getattr(old_norm, "_ranking_entry_unit_fix_patch_v4", False):
            _PATCHED = True
            return True

        def _normalize_ranking_row_for_entry_patched(row: dict[str, Any]) -> dict[str, Any]:
            out = base_norm(row)
            if isinstance(out, dict):
                return _normalize_units(out, min_volume=min_volume, min_turnover=min_turnover)
            return out

        _normalize_ranking_row_for_entry_patched._ranking_entry_unit_fix_patch_v4 = True  # type: ignore[attr-defined]
        _normalize_ranking_row_for_entry_patched._original = base_norm  # type: ignore[attr-defined]
        target._normalize_ranking_row_for_entry = _normalize_ranking_row_for_entry_patched

        _PATCHED = True
        logger.warning(
            "[RANKING ENTRY UNIT FIX] installed V4 price_min %.1f->%.1f min_volume=%.1f min_turnover=%.1f finalized_marker=True no_zero_volume_turnover=True",
            old_min, new_min, min_volume, min_turnover,
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
