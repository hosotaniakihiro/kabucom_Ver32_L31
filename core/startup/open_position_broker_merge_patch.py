# ============================================================
# File   : core/startup/open_position_broker_merge_patch.py
# Version: V1.0-OPEN-POSITION-BROKER-MERGE-PATCH
# ------------------------------------------------------------
# positions.db の未決済建玉と、broker reader の実建玉を合成して
# global_data.open_positions / protected / EXIT監視へ渡す runtime patch。
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)

_INSTALLED = False
_ORIGINAL_SYNC = None


def _normalize_symbol(v: Any) -> str:
    try:
        if v is None:
            return ""
        s = str(v).strip()
        if s.endswith(".0"):
            s = s[:-2]
        return s
    except Exception:
        return ""


def _ensure_open_positions() -> Dict[str, Dict[str, Any]]:
    try:
        from global_state import global_data

        d = getattr(global_data, "open_positions", None)
        if isinstance(d, dict):
            return d
        d = {}
        setattr(global_data, "open_positions", d)
        return d
    except Exception:
        logger.debug("[OPEN POSITION BROKER PATCH] ensure open_positions failed", exc_info=True)
        return {}


def _read_broker_positions() -> Dict[str, Dict[str, Any]]:
    try:
        from trading.position.kabu_position_reader import read_kabu_open_positions

        rows = read_kabu_open_positions() or {}
        out: Dict[str, Dict[str, Any]] = {}
        for k, v in rows.items():
            s = _normalize_symbol(k or (v or {}).get("symbol"))
            if s and isinstance(v, dict):
                out[s] = v
        return out
    except Exception:
        logger.warning("[OPEN POSITION BROKER PATCH] broker reader failed", exc_info=True)
        return {}


def _merge_and_publish(db_positions: Dict[str, Dict[str, Any]], broker_positions: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    merged: Dict[str, Dict[str, Any]] = {}

    for k, v in (db_positions or {}).items():
        s = _normalize_symbol(k or (v or {}).get("symbol"))
        if s and isinstance(v, dict):
            merged[s] = v

    for k, v in (broker_positions or {}).items():
        s = _normalize_symbol(k or (v or {}).get("symbol"))
        if s and isinstance(v, dict):
            merged[s] = v

    gd_positions = _ensure_open_positions()
    before_keys = {_normalize_symbol(k) for k in gd_positions.keys()}
    merged_keys = set(merged.keys())

    for s, pos in merged.items():
        gd_positions[s] = pos

    if merged:
        for k in list(gd_positions.keys()):
            s = _normalize_symbol(k)
            if s and s not in merged:
                try:
                    src = str((gd_positions.get(k) or {}).get("_position_source") or "")
                    if src.startswith("DB.positions") or src.startswith("KABU.positions"):
                        gd_positions.pop(k, None)
                except Exception:
                    pass

    try:
        from global_state import global_data

        global_data.open_positions_synced_at = dt.datetime.now()
        global_data.open_positions_synced_count = len(merged)
    except Exception:
        pass

    changed = before_keys != merged_keys
    logger.warning(
        "[OPEN POSITION BROKER PATCH] merged open positions count=%d changed=%s db_count=%d broker_count=%d symbols=%s",
        len(merged),
        changed,
        len(db_positions or {}),
        len(broker_positions or {}),
        sorted(merged.keys()),
    )
    return merged


def install() -> bool:
    global _INSTALLED, _ORIGINAL_SYNC

    if _INSTALLED:
        return True

    try:
        import trading.position.open_position_sync as target
    except Exception:
        logger.exception("[OPEN POSITION BROKER PATCH] import target failed")
        return False

    original = getattr(target, "sync_open_positions_from_db", None)
    if not callable(original):
        logger.warning("[OPEN POSITION BROKER PATCH] target sync function unavailable")
        return False

    _ORIGINAL_SYNC = original

    def patched_sync_open_positions_from_db(*, force_log: bool = False):
        try:
            db_positions = _ORIGINAL_SYNC(force_log=force_log) or {}
        except Exception:
            logger.exception("[OPEN POSITION BROKER PATCH] original sync failed")
            db_positions = {}

        broker_positions = _read_broker_positions()
        return _merge_and_publish(db_positions, broker_positions)

    target.sync_open_positions_from_db = patched_sync_open_positions_from_db

    try:
        target.load_open_positions_from_broker = _read_broker_positions
    except Exception:
        pass

    _INSTALLED = True
    logger.warning("[OPEN POSITION BROKER PATCH] installed")
    return True


__all__ = ["install"]
