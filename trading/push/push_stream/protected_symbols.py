# ============================================================
# File   : trading/push/push_stream/protected_symbols.py
# Version: Ver01-PROTECTED-PUSH-SYMBOLS
# ------------------------------------------------------------
# PUSH登録から絶対に外したくない銘柄を解決する。
#
# 対象:
#   - 保有中銘柄
#   - 未約定注文中銘柄
#   - 直近ENTRY候補/直近AI OK候補
#
# 目的:
#   - 50銘柄制限/A-Bローテーション中でも、売買中銘柄を優先登録する
#   - 5秒EXIT / trail exit / 未約定cancel の監視漏れを減らす
# ============================================================

from __future__ import annotations

import importlib
import logging
import os
from typing import Any, Iterable, List

from .normalize import _normalize_symbol

logger = logging.getLogger(__name__)


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return bool(default)
    s = str(v).strip().lower()
    if s in {'1', 'true', 'yes', 'y', 'on', 'ok', 'enable', 'enabled'}:
        return True
    if s in {'0', 'false', 'no', 'n', 'off', 'ng', 'disable', 'disabled', ''}:
        return False
    return bool(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == '':
            return int(default)
        return int(float(str(v).strip()))
    except Exception:
        return int(default)


ENABLE_PROTECTED_PUSH_SYMBOLS = _env_bool('ENABLE_PROTECTED_PUSH_SYMBOLS', True)
PROTECTED_PUSH_MAX_SYMBOLS = _env_int('PROTECTED_PUSH_MAX_SYMBOLS', 20)


def _normalize(s: Any) -> str | None:
    try:
        x = _normalize_symbol(s)
    except Exception:
        x = str(s).strip().upper() if s is not None else ''
    if not x:
        return None
    x = str(x).strip().upper()
    if x.endswith('.T'):
        x = x[:-2]
    if x.endswith('.0') and x[:-2].isdigit():
        x = x[:-2]
    if not x or not x.isalnum() or not (3 <= len(x) <= 5):
        return None
    if x in {'NONE', 'NULL', 'NAN', 'NA', '-', '0'}:
        return None
    return x


def _dedupe(items: Iterable[Any]) -> List[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        s = _normalize(item)
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _symbols_from_any(obj: Any) -> list[str]:
    if obj is None:
        return []

    try:
        if hasattr(obj, 'columns'):
            cols = list(getattr(obj, 'columns', []))
            for c in ('symbol', 'Symbol', 'code', 'Code', '銘柄コード'):
                if c in cols:
                    return _dedupe(obj[c].tolist())
    except Exception:
        pass

    if isinstance(obj, dict):
        items: list[Any] = []
        # dict values にposition/order dictが入るケース
        for k, v in obj.items():
            if isinstance(v, dict):
                items.append(v.get('symbol') or v.get('Symbol') or v.get('code') or v.get('Code') or k)
            else:
                items.append(k if isinstance(k, (str, int)) else v)
        return _dedupe(items)

    if isinstance(obj, str):
        return _dedupe([obj])

    try:
        seq = list(obj)
    except Exception:
        return []

    items: list[Any] = []
    for x in seq:
        if isinstance(x, dict):
            items.append(x.get('symbol') or x.get('Symbol') or x.get('code') or x.get('Code'))
        else:
            items.append(x)
    return _dedupe(items)


def _get_global_data() -> Any:
    for mod_name, attr in (('global_state', 'global_data'), ('core.global_context.context', 'global_data')):
        try:
            mod = importlib.import_module(mod_name)
            gd = getattr(mod, attr, None)
            if gd is not None:
                return gd
        except Exception:
            continue
    return None


def _from_global_data() -> list[str]:
    gd = _get_global_data()
    if gd is None:
        return []

    attrs = (
        # 保有中
        'open_positions',
        'positions',
        'current_positions',
        'position_state_map',
        # 未約定/注文中
        'pending_orders',
        'pending_order_map',
        'entry_pending_orders',
        'active_orders',
        # 直近候補
        'recent_entry_symbols',
        'recent_ai_ok_symbols',
        'last_entry_candidates',
        'ai_ok_symbols',
    )

    out: list[str] = []
    for attr in attrs:
        try:
            src = getattr(gd, attr, None)
            if callable(src):
                src = src()
            syms = _symbols_from_any(src)
            if syms:
                logger.info('[PROTECTED PUSH] global_data.%s symbols=%s', attr, syms[:20])
                out.extend(syms)
        except Exception:
            logger.debug('[PROTECTED PUSH] read global_data.%s failed', attr, exc_info=True)
    return _dedupe(out)


def _from_global_context_positions() -> list[str]:
    try:
        from core.global_context.context import global_context as GC
        positions = getattr(GC, 'positions', None)
        if positions is None:
            return []
        if hasattr(positions, 'snapshot_open'):
            return _symbols_from_any(positions.snapshot_open())
        if hasattr(positions, 'snapshot_dict'):
            return _symbols_from_any(positions.snapshot_dict())
    except Exception:
        logger.debug('[PROTECTED PUSH] global_context positions failed', exc_info=True)
    return []


def _from_runtime_state() -> list[str]:
    out: list[str] = []
    try:
        from trading.runtime_persistence.runtime_state_store import load_open_positions, load_pending_orders
        out.extend(_symbols_from_any(load_open_positions()))
        out.extend(_symbols_from_any(load_pending_orders()))
    except Exception:
        logger.debug('[PROTECTED PUSH] runtime_state load failed', exc_info=True)
    return _dedupe(out)


def resolve_protected_push_symbols() -> list[str]:
    if not ENABLE_PROTECTED_PUSH_SYMBOLS:
        return []

    out: list[str] = []
    out.extend(_from_global_context_positions())
    out.extend(_from_global_data())
    out.extend(_from_runtime_state())

    out = _dedupe(out)
    if PROTECTED_PUSH_MAX_SYMBOLS > 0:
        out = out[:PROTECTED_PUSH_MAX_SYMBOLS]

    if out:
        logger.warning('[PROTECTED PUSH] resolved count=%d symbols=%s', len(out), out)
    else:
        logger.info('[PROTECTED PUSH] resolved empty')
    return out


def merge_protected_first(targets: Iterable[Any], *, max_symbols: int = 100) -> list[str]:
    protected = resolve_protected_push_symbols()
    normal = _symbols_from_any(targets)
    merged = _dedupe(list(protected) + list(normal))
    if max_symbols > 0:
        merged = merged[:max_symbols]
    logger.warning(
        '[PROTECTED PUSH] merge protected=%d normal=%d merged=%d max=%d head=%s',
        len(protected),
        len(normal),
        len(merged),
        max_symbols,
        merged[:20],
    )
    return merged


__all__ = [
    'resolve_protected_push_symbols',
    'merge_protected_first',
]
