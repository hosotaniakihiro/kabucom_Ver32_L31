# ============================================================
# File   : trading/ranking/active_symbols/protected.py
# Version: Ver1.2-ACTIVE-SYMBOLS-PROTECTED-OPEN-POSITIONS-ONLY
# ------------------------------------------------------------
# 【目的】
#   active_symbols から絶対に外してはいけない銘柄を返す。
#
# Ver1.2:
#   - pending_entries を原則 protected に含めない。
#   - 6266 のように「未保有・未約定・発注失敗後の pending」が
#     active protected に残り続ける問題を防ぐ。
#   - 本当に保護すべき対象は DB/Broker/global_data.open_positions の建玉。
#   - 必要な場合のみ ACTIVE_PROTECT_PENDING_SYMBOLS=1 で旧挙動を復活可能。
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any, Set

from global_state import global_data
from .normalize import normalize_symbol

logger = logging.getLogger(__name__)


def _env_bool(name: str, default: bool = False) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}
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
            for key in (
                "symbol",
                "Symbol",
                "銘柄コード",
                "code",
                "stock_code",
                "StockCode",
            ):
                if key in item:
                    ns = normalize_symbol(item.get(key))
                    if ns:
                        return ns
            return ""

        for attr in (
            "symbol",
            "Symbol",
            "code",
            "stock_code",
            "StockCode",
        ):
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
                # open_positions dict の value が簡易dictの場合は qty が無いこともあるので許容
                return True
            try:
                return float(qty) > 0
            except Exception:
                return True
        return True
    except Exception:
        return True


def _add_from_mapping_or_iterable(protected: Set[str], obj: Any, *, source: str) -> None:
    try:
        if obj is None:
            return

        if isinstance(obj, dict):
            for key, value in obj.items():
                # {symbol: position_dict} 形式を想定。
                # value が明確に closed/qty=0 なら保護しない。
                if _is_open_position_item(value):
                    _add_symbol(protected, key)
                    sym = _extract_symbol_from_item(value)
                    if sym:
                        protected.add(sym)
            return

        if isinstance(obj, (list, tuple, set)):
            for item in obj:
                if not _is_open_position_item(item):
                    continue
                sym = _extract_symbol_from_item(item)
                if sym:
                    protected.add(sym)
            return

        if _is_open_position_item(obj):
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
    """
    旧挙動の互換用。
    通常は pending を protected に入れない。
    """
    if not _env_bool("ACTIVE_PROTECT_PENDING_SYMBOLS", False):
        try:
            pending = getattr(global_data, "pending_entries", None)
            n = len(pending) if isinstance(pending, dict) else 0
            if n:
                logger.info(
                    "[ACTIVE PROTECTED] pending symbols not protected count=%s set ACTIVE_PROTECT_PENDING_SYMBOLS=1 to enable old behavior",
                    n,
                )
        except Exception:
            pass
        return

    try:
        _add_from_mapping_or_iterable(
            protected,
            getattr(global_data, "pending_entries", None),
            source="global_data.pending_entries",
        )
        logger.warning("[ACTIVE PROTECTED] pending protection enabled by env")
    except Exception:
        logger.debug("[ACTIVE PROTECTED] failed global_data.pending_entries", exc_info=True)


def get_protected_symbols() -> Set[str]:
    protected: Set[str] = set()

    # 最優先: DB positions(status=OPEN)
    try:
        db_positions = _load_db_open_positions_safe()
        _add_from_mapping_or_iterable(protected, db_positions, source="DB.positions")
    except Exception:
        logger.debug("[ACTIVE PROTECTED] failed DB positions", exc_info=True)

    # global_data.open_positions
    try:
        _add_from_mapping_or_iterable(
            protected,
            getattr(global_data, "open_positions", None),
            source="global_data.open_positions",
        )
    except Exception:
        logger.debug("[ACTIVE PROTECTED] failed global_data.open_positions", exc_info=True)

    # pending_entries は原則保護対象外。
    _add_pending_if_enabled(protected)

    if protected:
        logger.warning(
            "[ACTIVE PROTECTED] protected symbols count=%d symbols=%s",
            len(protected),
            sorted(protected),
        )

    return protected


__all__ = ["get_protected_symbols"]
