# ============================================================
# File   : trading/ranking/active_symbols/protected.py
# Version: Ver1.4-PENDING-AND-EXIT-COOLDOWN
# ------------------------------------------------------------
# 【目的】
#   active_symbols から絶対に外してはいけない銘柄を返す。
#
# Ver1.4:
#   - pending_entries は board_missing 対策としてデフォルト保護。
#   - EXIT完了銘柄は即時に固定枠から外さず、同期遅延対策で
#     ACTIVE_EXIT_COOLDOWN_PROTECT_SEC 秒だけ保護。
#   - クールダウン期限切れ後は自動で固定枠から外す。
#   - 建玉 / pending / board retry / hot 候補は引き続き保護対象。
# ============================================================

from __future__ import annotations

import datetime as dt
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


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _now() -> dt.datetime:
    return dt.datetime.now()


def _to_dt(v: Any) -> dt.datetime | None:
    try:
        if isinstance(v, dt.datetime):
            return v
        if isinstance(v, (int, float)):
            return dt.datetime.fromtimestamp(float(v))
        if v:
            return dt.datetime.fromisoformat(str(v).replace("T", " "))
    except Exception:
        return None
    return None


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


def _add_expiring_map(protected: Set[str], attr: str, *, source: str) -> None:
    try:
        mp = getattr(global_data, attr, None)
        if not isinstance(mp, dict):
            return
        now = _now()
        expired = []
        kept = []
        for sym, until in list(mp.items()):
            ns = normalize_symbol(sym)
            if not ns:
                expired.append(sym)
                continue
            until_dt = _to_dt(until)
            if until_dt is None or until_dt <= now:
                expired.append(sym)
                continue
            protected.add(ns)
            kept.append(ns)
        for sym in expired:
            try:
                mp.pop(sym, None)
            except Exception:
                pass
        if kept:
            logger.warning("[ACTIVE PROTECTED] %s protected count=%s symbols=%s", source, len(kept), sorted(set(kept)))
        if expired:
            logger.info("[ACTIVE PROTECTED] %s expired removed=%s", source, len(expired))
    except Exception:
        logger.debug("[ACTIVE PROTECTED] failed expiring map source=%s attr=%s", source, attr, exc_info=True)


def _add_exit_cooldown_if_enabled(protected: Set[str]) -> None:
    if not _env_bool("ACTIVE_PROTECT_EXIT_COOLDOWN_SYMBOLS", True):
        return
    # exit_recent_protect_marker_patch が設定する正式名。
    _add_expiring_map(protected, "active_protected_exit_cooldown_until", source="exit_cooldown")
    # 互換: 他パッチ/手動で使える候補名。
    _add_expiring_map(protected, "recent_exit_protect_until", source="recent_exit")


def _add_board_retry_and_hot_if_enabled(protected: Set[str]) -> None:
    if _env_bool("ACTIVE_PROTECT_BOARD_RETRY_SYMBOLS", True):
        _add_expiring_map(protected, "active_protected_board_retry_until", source="board_retry")
        _add_expiring_map(protected, "board_missing_retry_until", source="board_retry_compat")
    if _env_bool("ACTIVE_PROTECT_HOT_SYMBOLS", True):
        _add_expiring_map(protected, "active_protected_hot_until", source="hot")
        _add_expiring_map(protected, "early_breakout_hot_until", source="early_breakout_hot")


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
    _add_exit_cooldown_if_enabled(protected)
    _add_board_retry_and_hot_if_enabled(protected)

    if protected:
        logger.warning("[ACTIVE PROTECTED] protected symbols count=%d symbols=%s", len(protected), sorted(protected))

    return protected


__all__ = ["get_protected_symbols"]
