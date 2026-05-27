# ============================================================
# File   : core/startup/ranking_entry_volume_unit_patch.py
# Version: V4.5-RANKING-ENTRY-UNIT-AUTO-PREFILTER
# ------------------------------------------------------------
# 目的:
#   ランキング由来エントリーの volume / turnover 単位を補正する。
#   ただし、同一銘柄が type × market で多数出るため、ログ大量出力と
#   同一値の繰り返し正規化で entry loop が重くならないようにする。
#
# V4.5:
#   - ranking_entry_source_prefilter_patch_v2 を同時installする。
#   - main.py へ追加しなくても、core.startup.__init__ 経由でprefilterが有効になる。
#
# V4.4:
#   - 通常ログは完全に既定OFF
#   - RANKING_ENTRY_UNIT_FIX_LOG_FIRST_N 既定 30 -> 0
#   - zero-volume / finalized / turnover clamped の銘柄別ログも既定抑制
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)
_PATCHED = False

_TRUE = {"1", "true", "yes", "y", "on", "enable", "enabled"}
_FALSE = {"0", "false", "no", "n", "off", "disable", "disabled"}
_CACHE: dict[tuple[str, float, float, float], dict[str, Any]] = {}
_LOGGED_KEYS: set[tuple[str, float, float, float, str]] = set()
_CACHE_MAX = 20000
_LOG_FIRST_N = 0
_LOG_COUNT = 0


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


def _key(symbol: Any, price: float, raw_volume: float, raw_turnover: float) -> tuple[str, float, float, float]:
    return (
        str(symbol or "").strip(),
        round(float(price), 4),
        round(float(raw_volume), 4),
        round(float(raw_turnover), 4),
    )


def _should_log_once(base_key: tuple[str, float, float, float], kind: str) -> bool:
    global _LOG_COUNT
    if _env_bool("RANKING_ENTRY_UNIT_FIX_LOG_EACH", False):
        return True
    limit = int(_env_float("RANKING_ENTRY_UNIT_FIX_LOG_FIRST_N", _LOG_FIRST_N))
    if limit <= 0:
        return False
    k = (base_key[0], base_key[1], base_key[2], base_key[3], kind)
    if k in _LOGGED_KEYS:
        return False
    _LOGGED_KEYS.add(k)
    _LOG_COUNT += 1
    return _LOG_COUNT <= limit


def _apply_cached(row: dict[str, Any], cached: dict[str, Any]) -> dict[str, Any]:
    try:
        for k, v in cached.items():
            row[k] = v
    except Exception:
        pass
    return row


def _normalize_units(row: dict[str, Any], *, min_volume: float, min_turnover: float) -> dict[str, Any]:
    if not _env_bool("RANKING_ENTRY_NORMALIZE_VOLUME_UNITS", True):
        return row

    try:
        if _flag_on(row.get("ranking_entry_units_finalized")):
            return row

        symbol = row.get("symbol")
        price = _safe_float(row.get("price") or row.get("current_price") or row.get("close_price") or row.get("close"), 0.0)
        raw_volume = _safe_float(row.get("volume") if row.get("volume") is not None else row.get("trading_volume"), 0.0)
        raw_turnover = _safe_float(row.get("turnover") if row.get("turnover") is not None else row.get("trading_value"), 0.0)
        base_key = _key(symbol, price, raw_volume, raw_turnover)

        cached = _CACHE.get(base_key)
        if cached is not None:
            return _apply_cached(row, cached)

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
        zero_volume_turnover_ignored = False

        if 0.0 < raw_volume < min_volume and volume_multiplier > 1:
            candidate_volume = raw_volume * volume_multiplier
            if candidate_volume >= min_volume:
                volume = candidate_volume
                volume_unit_fixed = True
                volume_unit_multiplier = volume_multiplier

        implied_turnover = price * volume if price > 0 and volume > 0 else 0.0
        can_fix_turnover = volume > 0 and implied_turnover > 0

        if can_fix_turnover:
            if 0.0 < raw_turnover < min_turnover and raw_turnover < yen_floor and turnover_multiplier > 1:
                candidate_turnover = raw_turnover * turnover_multiplier
                upper = max(min_turnover, implied_turnover * implied_max_ratio)
                if candidate_turnover >= min_turnover and candidate_turnover <= upper:
                    turnover = candidate_turnover
                    turnover_unit_fixed = True
                    turnover_unit_multiplier = turnover_multiplier

            if turnover <= 0:
                turnover = implied_turnover
            elif turnover < min_turnover <= implied_turnover:
                turnover = implied_turnover
            elif turnover > implied_turnover * implied_max_ratio:
                if _should_log_once(base_key, "turnover_clamped"):
                    logger.warning(
                        "[RANKING ENTRY UNIT FIX] turnover clamped symbol=%s price=%s volume=%s turnover=%s implied=%s",
                        symbol, price, volume, turnover, implied_turnover,
                    )
                turnover = implied_turnover
        else:
            if raw_turnover > 0:
                zero_volume_turnover_ignored = True
                if _should_log_once(base_key, "zero_volume"):
                    logger.info(
                        "[RANKING ENTRY UNIT FIX] zero-volume turnover ignored symbol=%s price=%s raw_volume=%.3f raw_turnover=%.3f",
                        symbol, price, raw_volume, raw_turnover,
                    )
            turnover = 0.0

        updates = {
            "ranking_raw_volume": raw_volume,
            "ranking_raw_turnover": raw_turnover,
            "ranking_volume_unit_fixed": int(volume_unit_fixed),
            "ranking_turnover_unit_fixed": int(turnover_unit_fixed),
            "ranking_volume_unit_multiplier": volume_unit_multiplier,
            "ranking_turnover_unit_multiplier": turnover_unit_multiplier,
            "ranking_entry_units_finalized": 1,
            "ranking_zero_volume_turnover_ignored": int(zero_volume_turnover_ignored),
            "volume": volume,
            "trading_volume": volume,
            "turnover": turnover,
            "trading_value": turnover,
        }
        row.update(updates)

        if len(_CACHE) >= int(_env_float("RANKING_ENTRY_UNIT_FIX_CACHE_MAX", _CACHE_MAX)):
            _CACHE.clear()
            _LOGGED_KEYS.clear()
        _CACHE[base_key] = dict(updates)

        changed = volume_unit_fixed or turnover_unit_fixed or raw_turnover != turnover or raw_volume != volume
        if changed and _should_log_once(base_key, "finalized"):
            logger.info(
                "[RANKING ENTRY UNIT FIX] finalized V4.5 symbol=%s price=%s volume %.3f->%.3f turnover %.3f->%.3f vol_mul=%.0f turn_mul=%.0f implied=%.3f can_fix_turnover=%s zero_volume_ignored=%s",
                symbol, price, raw_volume, volume, raw_turnover, turnover,
                volume_unit_multiplier, turnover_unit_multiplier, implied_turnover,
                can_fix_turnover, zero_volume_turnover_ignored,
            )
    except Exception:
        logger.exception("[RANKING ENTRY UNIT FIX] normalize failed symbol=%s", row.get("symbol"))

    return row


def _install_source_prefilter() -> bool:
    try:
        from core.startup import ranking_entry_source_prefilter_patch_v2 as prefilter
        fn = getattr(prefilter, "install", None)
        ok = fn() if callable(fn) else False
        logger.warning("[RANKING ENTRY UNIT FIX] source prefilter installed=%s", ok)
        return bool(ok)
    except Exception:
        logger.exception("[RANKING ENTRY UNIT FIX] source prefilter install failed")
        return False


def install() -> bool:
    global _PATCHED
    if _PATCHED:
        _install_source_prefilter()
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
            _install_source_prefilter()
            return False

        base_norm = _unwrap(old_norm)
        if getattr(old_norm, "_ranking_entry_unit_fix_patch_v45", False):
            _PATCHED = True
            _install_source_prefilter()
            return True

        def _normalize_ranking_row_for_entry_patched(row: dict[str, Any]) -> dict[str, Any]:
            out = base_norm(row)
            if isinstance(out, dict):
                return _normalize_units(out, min_volume=min_volume, min_turnover=min_turnover)
            return out

        _normalize_ranking_row_for_entry_patched._ranking_entry_unit_fix_patch_v45 = True  # type: ignore[attr-defined]
        _normalize_ranking_row_for_entry_patched._original = base_norm  # type: ignore[attr-defined]
        target._normalize_ranking_row_for_entry = _normalize_ranking_row_for_entry_patched

        prefilter_ok = _install_source_prefilter()
        _PATCHED = True
        logger.warning(
            "[RANKING ENTRY UNIT FIX] installed V4.5 price_min %.1f->%.1f min_volume=%.1f min_turnover=%.1f cache=True silent_default=True prefilter=%s log_each=%s log_first_n=%s",
            old_min, new_min, min_volume, min_turnover, prefilter_ok,
            _env_bool("RANKING_ENTRY_UNIT_FIX_LOG_EACH", False),
            int(_env_float("RANKING_ENTRY_UNIT_FIX_LOG_FIRST_N", _LOG_FIRST_N)),
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