# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
import os
from functools import wraps
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V1-ACTIVE-SYMBOL-TARGET-FILL"
_INSTALLED = False


def _env_bool(name: str, default: bool = True) -> bool:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return bool(default)
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}


def _flag_ok(v: Any) -> bool:
    try:
        if isinstance(v, bool):
            return bool(v)
        return str(v or "").strip().lower() in {"1", "true", "yes", "y", "on", "ok", "可能", "可"}
    except Exception:
        return False


def _norm(v: Any) -> str:
    try:
        s = str(v or "").strip()
        if s.endswith(".0") and s[:-2].isdigit():
            s = s[:-2]
        return s
    except Exception:
        return ""


def _dedupe(values) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for v in values or []:
        s = _norm(v)
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _float(v: Any) -> float:
    try:
        if v is None:
            return 0.0
        if isinstance(v, str):
            v = v.replace(",", "").strip()
            if not v:
                return 0.0
        return float(v)
    except Exception:
        return 0.0


def _score(sym: str, liquidity_map: dict[str, dict[str, Any]], flag_info: dict[str, Any], protected: set[str]) -> tuple:
    info = liquidity_map.get(sym, {}) or {}
    flags = flag_info.get(sym, {}) if isinstance(flag_info, dict) else {}
    if not isinstance(flags, dict):
        flags = {}
    value = _float(info.get("trading_value") or info.get("turnover"))
    volume = _float(info.get("trading_volume") or info.get("volume"))
    tick = _float(info.get("tick_count"))
    price = _float(info.get("current_price") or info.get("price") or info.get("close"))
    quality = int(_flag_ok(flags.get("ats_ok"))) + int(_flag_ok(flags.get("buy_target"))) + int(_flag_ok(flags.get("sell_target"))) + int(_flag_ok(flags.get("short_ok")))
    return (1 if sym in protected else 0, value, volume, tick, price, quality, sym)


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        import trading.ranking.active_symbols.manager as mgr

        old = getattr(mgr, "_supplement_active_to_target", None)
        if not callable(old) or getattr(old, "_target_fill_v1", False):
            _INSTALLED = True
            return True

        @wraps(old)
        def _wrapped(active, *, target, premarket_mode, allowed_universe, eligible_symbols, flag_info, protected, liquidity_map, prev_active, hot_symbols, primary_candidates):
            active, diag = old(
                active,
                target=target,
                premarket_mode=premarket_mode,
                allowed_universe=allowed_universe,
                eligible_symbols=eligible_symbols,
                flag_info=flag_info,
                protected=protected,
                liquidity_map=liquidity_map,
                prev_active=prev_active,
                hot_symbols=hot_symbols,
                primary_candidates=primary_candidates,
            )
            try:
                if len(active) >= int(target) or not _env_bool("ACTIVE_SYMBOL_TARGET_FILL_ENABLED", True):
                    return active, diag
                if premarket_mode and not _env_bool("ACTIVE_SYMBOL_TARGET_FILL_PREMARKET", False):
                    return active, diag
                candidates = []
                for seq in (prev_active, hot_symbols, primary_candidates, allowed_universe, eligible_symbols):
                    candidates.extend(_dedupe(seq))
                rows = []
                for sym in _dedupe(candidates):
                    if sym in active:
                        continue
                    if sym not in eligible_symbols and sym not in protected:
                        continue
                    rows.append(sym)
                rows.sort(key=lambda s: _score(s, liquidity_map or {}, flag_info or {}, set(protected or set())), reverse=True)
                added = []
                for sym in rows:
                    active.add(sym)
                    added.append(sym)
                    if len(active) >= int(target):
                        break
                if added:
                    if not isinstance(diag, dict):
                        diag = {}
                    diag["target_fill_added"] = added
                    logger.warning("[ACTIVE SYMBOL TARGET FILL] before=%s after=%s target=%s added=%s", len(active) - len(added), len(active), target, added[:20])
            except Exception:
                logger.debug("[ACTIVE SYMBOL TARGET FILL] skipped", exc_info=True)
            return active, diag

        _wrapped._target_fill_v1 = True  # type: ignore[attr-defined]
        _wrapped._original = old  # type: ignore[attr-defined]
        mgr._supplement_active_to_target = _wrapped
        _INSTALLED = True
        logger.warning("[ACTIVE SYMBOL TARGET FILL] installed version=%s", VERSION)
        return True
    except Exception:
        logger.exception("[ACTIVE SYMBOL TARGET FILL] install failed")
        return False

try:
    install()
except Exception:
    logger.exception("[ACTIVE SYMBOL TARGET FILL] auto install failed")

__all__ = ["VERSION", "install"]
