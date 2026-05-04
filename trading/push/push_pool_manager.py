# ============================================================
# trading/push/push_pool_manager.py（FINAL-COMPLETE）
# ============================================================

# ============================================================
# trading/push/push_pool_manager.py
# ============================================================

import logging
from datetime import datetime
from typing import Set

from global_state import global_data

logger = logging.getLogger("push_pool_manager")


# ============================================================
# 設定
# ============================================================
CORE_LIMIT = 30
SCOUT_LIMIT = 20
MAX_PUSH = CORE_LIMIT + SCOUT_LIMIT

SCOUT_MIN_LIFETIME_SEC = 180     # 3分
SCOUT_TO_CORE_MIN_SEC = 300      # 5分
CORE_MIN_LIFETIME_SEC = 600      # 10分

CORE_DROP_NO_ACTIVITY_SEC = 900  # 15分（失速判定）

# ============================================================
# 内部状態
# ============================================================
_core: Set[str] = set()
_scout: Set[str] = set()
_entered_at = {}      # symbol -> datetime
_last_active = {}     # symbol -> datetime

# ============================================================
# 内部 util
# ============================================================
def _now():
    return datetime.now()


def _alive_long_enough(symbol, sec):
    t = _entered_at.get(symbol)
    return not t or (_now() - t).total_seconds() >= sec


def _total():
    return len(_core) + len(_scout)


# ============================================================
# 低レベル登録
# ============================================================
# ============================================================
# trading/push/push_pool_manager.py（ATS非直接操作版）
# ============================================================

def _register(symbol):
    global_data.push_symbols.add(symbol)
    _entered_at[symbol] = _now()
    _last_active[symbol] = _now()
    logger.info(f"[PUSH ADD REQUEST] {symbol}")


def _unregister(symbol):
    global_data.push_symbols.discard(symbol)
    _entered_at.pop(symbol, None)
    _last_active.pop(symbol, None)
    logger.info(f"[PUSH REMOVE REQUEST] {symbol}")



# ============================================================
# Scout / Core 操作
# ============================================================
def promote_to_scout(symbol):
    symbol = str(symbol)
    if symbol in _core or symbol in _scout:
        return False

    if len(_scout) >= SCOUT_LIMIT:
        _evict_scout()

    if _total() >= MAX_PUSH:
        return False

    _scout.add(symbol)
    _register(symbol)
    logger.info(f"[SCOUT ADD] {symbol}")
    return True


def promote_to_core(symbol):
    symbol = str(symbol)

    if symbol in _core:
        return False

    # Scout在籍時間チェック
    if symbol in _scout:
        if not _alive_long_enough(symbol, SCOUT_TO_CORE_MIN_SEC):
            return False
        _scout.remove(symbol)

    if len(_core) >= CORE_LIMIT:
        _evict_core()

    if symbol not in global_data.push_symbols:
        if _total() >= MAX_PUSH:
            return False
        _register(symbol)

    _core.add(symbol)
    _entered_at[symbol] = _now()
    logger.warning(f"[CORE ADD] {symbol}")
    return True


def demote_core(symbol):
    symbol = str(symbol)
    if symbol not in _core:
        return False

    if not _alive_long_enough(symbol, CORE_MIN_LIFETIME_SEC):
        return False

    _core.remove(symbol)

    if len(_scout) < SCOUT_LIMIT:
        _scout.add(symbol)
        logger.info(f"[CORE → SCOUT] {symbol}")
    else:
        _unregister(symbol)
        logger.info(f"[CORE DROP] {symbol}")
    return True


def remove(symbol):
    symbol = str(symbol)
    if symbol not in global_data.push_symbols:
        return False

    if symbol in _core and not _alive_long_enough(symbol, CORE_MIN_LIFETIME_SEC):
        return False
    if symbol in _scout and not _alive_long_enough(symbol, SCOUT_MIN_LIFETIME_SEC):
        return False

    _core.discard(symbol)
    _scout.discard(symbol)
    _unregister(symbol)
    return True


# ============================================================
# Activity 更新（PUSH受信時に呼ぶ）
# ============================================================
def mark_active(symbol):
    _last_active[str(symbol)] = _now()


# ============================================================
# 自動メンテナンス
# ============================================================
def maintenance():
    now = _now()

    # Core失速チェック
    for sym in list(_core):
        last = _last_active.get(sym)
        if last and (now - last).total_seconds() >= CORE_DROP_NO_ACTIVITY_SEC:
            logger.warning(f"[CORE STALE] {sym}")
            demote_core(sym)

    # Scout期限切れ
    for sym in list(_scout):
        if _alive_long_enough(sym, SCOUT_MIN_LIFETIME_SEC):
            continue


def _evict_scout():
    if not _scout:
        return
    sym = min(_scout, key=lambda s: _entered_at.get(s, datetime.min))
    if _alive_long_enough(sym, SCOUT_MIN_LIFETIME_SEC):
        _scout.remove(sym)
        _unregister(sym)
        logger.info(f"[SCOUT EVICT] {sym}")


def _evict_core():
    if not _core:
        return
    sym = min(_core, key=lambda s: _entered_at.get(s, datetime.min))
    if _alive_long_enough(sym, CORE_MIN_LIFETIME_SEC):
        _core.remove(sym)
        _unregister(sym)
        logger.warning(f"[CORE EVICT] {sym}")


# ============================================================
# ENTRY判定
# ============================================================
def can_entry(symbol):
    return str(symbol) in _core


# ============================================================
# 状態表示
# ============================================================
def dump_status():
    logger.info(
        f"[PUSH STATUS] CORE={len(_core)} SCOUT={len(_scout)} TOTAL={_total()}"
    )
    logger.info(f" CORE : {sorted(_core)}")
    logger.info(f" SCOUT: {sorted(_scout)}")
