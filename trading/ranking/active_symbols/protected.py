# ============================================================
# File   : trading/ranking/active_symbols/protected.py
# Version: Ver1.1-ACTIVE-SYMBOLS-PROTECTED-DB-OPEN-POSITIONS
# ------------------------------------------------------------
# 【目的】
#   active_symbols から絶対に外してはいけない銘柄を返す。
#
# 【今回修正】
#   - DB positions(status=OPEN) を同期して protected に入れる
#   - global_data.open_positions が dict の場合に keys / values 両方を見る
#   - 既にエントリー済み銘柄をPUSH登録対象から外さない
# ============================================================

from __future__ import annotations

import logging
from typing import Any, Iterable, Set

from global_state import global_data
from .normalize import normalize_symbol

logger = logging.getLogger(__name__)


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


def _add_from_mapping_or_iterable(protected: Set[str], obj: Any, *, source: str) -> None:
    try:
        if obj is None:
            return

        if isinstance(obj, dict):
            for key, value in obj.items():
                # {symbol: position_dict} 形式を想定し、key と value の両方を見る。
                _add_symbol(protected, key)
                sym = _extract_symbol_from_item(value)
                if sym:
                    protected.add(sym)
            return

        if isinstance(obj, (list, tuple, set)):
            for item in obj:
                sym = _extract_symbol_from_item(item)
                if sym:
                    protected.add(sym)
            return

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

    # pending_entries
    try:
        _add_from_mapping_or_iterable(
            protected,
            getattr(global_data, "pending_entries", None),
            source="global_data.pending_entries",
        )
    except Exception:
        logger.debug("[ACTIVE PROTECTED] failed global_data.pending_entries", exc_info=True)

    if protected:
        logger.warning(
            "[ACTIVE PROTECTED] protected symbols count=%d symbols=%s",
            len(protected),
            sorted(protected),
        )

    return protected


__all__ = ["get_protected_symbols"]
