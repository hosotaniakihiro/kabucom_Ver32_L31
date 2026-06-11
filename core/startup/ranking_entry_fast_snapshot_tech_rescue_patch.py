from __future__ import annotations

import datetime as dt
import logging
import os
import time
from typing import Any, Dict, List

logger = logging.getLogger(__name__)
_INSTALLED = False
_ORIG_LATEST_EXISTING = None


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _sf(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return float(default)
        s = str(v).strip().replace(",", "").replace("%", "")
        if not s or s.lower() in {"nan", "none", "nat", "<na>"}:
            return float(default)
        return float(s)
    except Exception:
        return float(default)


def _has_value(v: Any) -> bool:
    try:
        if v is None:
            return False
        s = str(v).strip()
        return bool(s) and s.lower() not in {"nan", "none", "nat", "<na>"}
    except Exception:
        return False


def _sym(row: dict[str, Any]) -> str:
    try:
        return str(row.get("symbol") or row.get("Symbol") or "").strip()
    except Exception:
        return ""


def _rt(row: dict[str, Any]) -> str:
    try:
        return str(row.get("rank_type") or row.get("ranking_type") or "").strip()
    except Exception:
        return ""


def _side(row: dict[str, Any]) -> str:
    try:
        s = str(row.get("side") or row.get("entry_decision") or "").upper().strip()
        if s in {"BUY", "SELL"}:
            return s
        rt = _rt(row)
        if "値下" in rt or "下落" in rt:
            return "SELL"
        day = _sf(row.get("day_change_pct") or row.get("change_percentage") or row.get("change_rate"), 0.0)
        return "SELL" if day < 0 else "BUY"
    except Exception:
        return "BUY"


def _copy_aliases(d: dict[str, Any]) -> dict[str, Any]:
    out = dict(d)
    for base in ("ma5", "ma25", "ma75", "rsi", "macd", "signal", "macd_hist", "atr", "slope", "slope_pct", "slope_atr_scaled", "price_change_pct", "technical_ready"):
        if not _has_value(out.get(base)):
            for tf in (1, 3, 5):
                k = f"{base}_{tf}m"
                if _has_value(out.get(k)):
                    out[base] = out.get(k)
                    break
    for p in ("close", "price", "current_price", "close_price"):
        if _has_value(out.get(p)):
            px = out.get(p)
            for q in ("close", "price", "current_price", "close_price"):
                out.setdefault(q, px)
            break
    ready = any(_sf(out.get(k), 0.0) != 0.0 for k in ("ma5", "ma5_1m", "macd", "macd_1m", "slope", "slope_1m", "atr", "atr_1m"))
    if ready or _has_value(out.get("ma5")):
        out["ranking_tech_ready"] = True
        out["technical_ready"] = True
        out.setdefault("ranking_tech_score", 1.0)
        out.setdefault("ranking_tech_reason", "fast_snapshot_rescue_from_ranking_snapshot_1min")
        out.setdefault("ranking_tech_source", "ranking_snapshot_1min_history_compute_fast_rescue")
    return out


def _snapshot_rescue(rows: List[dict[str, Any]], existing: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    if not _env_bool("RANKING_ENTRY_FAST_SNAPSHOT_TECH_RESCUE", True):
        return existing
    try:
        import core.startup.ranking_entry_snapshot_technical_bridge_patch as bridge
        loader = getattr(bridge, "_load_snapshot_tech_cached", None)
        if not callable(loader):
            return existing
        bucket = int(dt.datetime.now().timestamp() // max(10, int(float(os.getenv("RANKING_ENTRY_FAST_SNAPSHOT_TECH_RESCUE_CACHE_SEC", "30") or 30))))
        out: Dict[str, Dict[str, Any]] = dict(existing or {})
        requested = []
        filled = 0
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            symbol = _sym(row)
            if not symbol or symbol in out:
                continue
            requested.append(symbol)
            try:
                items = loader(symbol, _rt(row), _side(row), bucket)
                if not items:
                    continue
                item = _copy_aliases(dict(items))
                if item.get("ranking_tech_ready") or item.get("technical_ready"):
                    item["ranking_tech_datetime"] = item.get("datetime") or item.get("snapshot_time") or item.get("ranking_tech_datetime")
                    item["ranking_tech_db"] = item.get("ranking_tech_db") or "ranking_snapshot_1min"
                    item["ranking_tech_readonly"] = True
                    item["ranking_tech_snapshot_rescue"] = True
                    out[symbol] = item
                    filled += 1
            except Exception:
                logger.debug("[RANKING FAST SNAPSHOT TECH RESCUE] symbol rescue failed symbol=%s", symbol, exc_info=True)
        if requested:
            logger.warning(
                "[RANKING FAST SNAPSHOT TECH RESCUE] requested=%s existing=%s filled=%s total=%s sample=%s",
                len(requested), len(existing or {}), filled, len(out), requested[:8],
            )
        return out
    except Exception:
        logger.exception("[RANKING FAST SNAPSHOT TECH RESCUE] failed")
        return existing


def install() -> bool:
    global _INSTALLED, _ORIG_LATEST_EXISTING
    if _INSTALLED:
        return True
    if not _env_bool("RANKING_ENTRY_FAST_SNAPSHOT_TECH_RESCUE", True):
        logger.warning("[RANKING FAST SNAPSHOT TECH RESCUE] disabled by env")
        return False
    try:
        import core.startup.ranking_entry_fast_runtime_patch as fast
        cur = getattr(fast, "_latest_existing_technicals", None)
        if not callable(cur):
            logger.warning("[RANKING FAST SNAPSHOT TECH RESCUE] target missing")
            return False
        if getattr(cur, "_ranking_fast_snapshot_tech_rescue", False):
            _INSTALLED = True
            return True
        _ORIG_LATEST_EXISTING = cur

        def latest_existing_with_snapshot_rescue(rows, *args, **kwargs):
            base = _ORIG_LATEST_EXISTING(rows, *args, **kwargs)
            try:
                return _snapshot_rescue(list(rows or []), base or {})
            except Exception:
                logger.exception("[RANKING FAST SNAPSHOT TECH RESCUE] wrapper failed")
                return base or {}

        latest_existing_with_snapshot_rescue._ranking_fast_snapshot_tech_rescue = True  # type: ignore[attr-defined]
        latest_existing_with_snapshot_rescue._original = _ORIG_LATEST_EXISTING  # type: ignore[attr-defined]
        fast._latest_existing_technicals = latest_existing_with_snapshot_rescue
        _INSTALLED = True
        logger.warning("[RANKING FAST SNAPSHOT TECH RESCUE] installed v1")
        return True
    except Exception:
        logger.exception("[RANKING FAST SNAPSHOT TECH RESCUE] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[RANKING FAST SNAPSHOT TECH RESCUE] auto install failed")

__all__ = ["install"]
