# ============================================================
# File   : trading/ranking/active_symbols/protected.py
# Version: Ver1.3-PROTECT-PENDING-FOR-BOARD
# ------------------------------------------------------------
# 【目的】
#   active_symbols から絶対に外してはいけない銘柄を返す。
#
# Ver1.3:
#   - Summary AI approved / pending 銘柄で board_missing が発生し、
#     発注直前に止まる問題への対策。
#   - pending_entries をデフォルトで protected に戻す。
#   - ただし不要なら ACTIVE_PROTECT_PENDING_SYMBOLS=0 で無効化可能。
#   - 建玉保護は従来通り最優先。
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any, Set

from global_state import global_data
from .normalize import normalize_symbol

logger = logging.getLogger(__name__)


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _add_symbol(protected: Set[str], value: Any) -> None:
    try:
        ns = normalize_symbol(value)
        if ns:
            protected.add(ns)
    except Exception:
        pass


def _extract_symbol_from_item(item: Any) -> str:
    try:
        if item is None:
            return ""
        if isinstance(item, dict):
            for key in ("symbol", "Symbol", "銘柄コード", "code", "stock_code", "StockCode"):
                if key in item:
                    ns = normalize_symbol(item.get(key))
                    if ns:
                        return ns
            return ""
        for attr in ("symbol", "Symbol", "code", "stock_code", "StockCode"):
            try:
                if hasattr(item, attr):
                    ns = normalize_symbol(getattr(item, attr))
                    if ns:
                        return ns
            except Exception:
                pass
        return normalize_symbol(item)
    except Exception:
        return ""


def _is_open_position_item(item: Any) -> bool:
    """pending候補ではなく、建玉らしいものだけを true にする。"""
    try:
        if item is None:
            return False
        if isinstance(item, dict):
            status = str(item.get("status") or item.get("Status") or "").upper()
            if status and status not in {"OPEN", "ACTIVE", "HOLD", "HOLDING", "建玉", "保有"}:
                return False
            qty = item.get("qty", item.get("HoldQty", item.get("LeavesQty", item.get("Quantity"))))
            if qty is None:
                return True
            try:
                return float(qty) > 0
            except Exception:
                return True
        return True
    except Exception:
        return True


def _add_from_mapping_or_iterable(protected: Set[str], obj: Any, *, source: str, require_open_position: bool = True) -> None:
    try:
        if obj is None:
            return
        if isinstance(obj, dict):
            for key, value in obj.items():
                if require_open_position and not _is_open_position_item(value):
                    continue
                _add_symbol(protected, key)
                sym = _extract_symbol_from_item(value)
                if sym:
                    protected.add(sym)
            return
        if isinstance(obj, (list, tuple, set)):
            for item in obj:
                if require_open_position and not _is_open_position_item(item):
                    continue
                sym = _extract_symbol_from_item(item)
                if sym:
                    protected.add(sym)
            return
        if (not require_open_position) or _is_open_position_item(obj):
            sym = _extract_symbol_from_item(obj)
            if sym:
                protected.add(sym)
    except Exception:
        logger.debug("[ACTIVE PROTECTED] failed to read source=%s", source, exc_info=True)


def _load_db_open_positions_safe() -> dict[str, dict[str, Any]]:
    try:
        from trading.position.open_position_sync import sync_open_positions_from_db
        return sync_open_positions_from_db(force_log=False) or {}
    except Exception:
        logger.debug("[ACTIVE PROTECTED] DB open position sync failed", exc_info=True)
        return {}


def _add_pending_if_enabled(protected: Set[str]) -> None:
    # board_missing対策として、pending銘柄はデフォルトで保護する。
    if not _env_bool("ACTIVE_PROTECT_PENDING_SYMBOLS", True):
        try:
            pending = getattr(global_data, "pending_entries", None)
            n = len(pending) if isinstance(pending, dict) else 0
            if n:
                logger.info("[ACTIVE PROTECTED] pending symbols not protected count=%s ACTIVE_PROTECT_PENDING_SYMBOLS=0", n)
        except Exception:
            pass
        return
    try:
        before = len(protected)
        _add_from_mapping_or_iterable(
            protected,
            getattr(global_data, "pending_entries", None),
            source="global_data.pending_entries",
            require_open_position=False,
        )
        added = len(protected) - before
        if added:
            logger.warning("[ACTIVE PROTECTED] pending protection enabled added=%s", added)
    except Exception:
        logger.debug("[ACTIVE PROTECTED] failed global_data.pending_entries", exc_info=True)


def get_protected_symbols() -> Set[str]:
    protected: Set[str] = set()

    try:
        db_positions = _load_db_open_positions_safe()
        _add_from_mapping_or_iterable(protected, db_positions, source="DB.positions", require_open_position=True)
    except Exception:
        logger.debug("[ACTIVE PROTECTED] failed DB positions", exc_info=True)

    try:
        _add_from_mapping_or_iterable(
            protected,
            getattr(global_data, "open_positions", None),
            source="global_data.open_positions",
            require_open_position=True,
        )
    except Exception:
        logger.debug("[ACTIVE PROTECTED] failed global_data.open_positions", exc_info=True)

    _add_pending_if_enabled(protected)

    if protected:
        logger.warning("[ACTIVE PROTECTED] protected symbols count=%d symbols=%s", len(protected), sorted(protected))

    return protected


__all__ = ["get_protected_symbols"]
