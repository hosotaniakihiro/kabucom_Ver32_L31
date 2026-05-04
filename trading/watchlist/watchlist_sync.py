# ============================================================
# watchlist_sync.py
# ------------------------------------------------------------
# 株ステーション監視銘柄（ATS / PUSH）を差分同期
# ============================================================

from ats import register_symbols, unregister_symbols
from global_state import global_data


def sync_watchlist(new_symbols: list[str]):
    """
    監視銘柄を差分で同期
    """
    new_set = set(new_symbols)
    old_set = set(global_data.symbols_active or [])

    to_add = new_set - old_set
    to_remove = old_set - new_set

    if to_remove:
        unregister_symbols(list(to_remove))

    if to_add:
        register_symbols(list(to_add))

    # --------------------------------------------------
    # global_state 更新
    # --------------------------------------------------
    now = global_data.now()
    for s in to_add:
        global_data.symbol_active_since[s] = now
    for s in to_remove:
        global_data.symbol_active_since.pop(s, None)

    global_data.symbols_active = new_set
