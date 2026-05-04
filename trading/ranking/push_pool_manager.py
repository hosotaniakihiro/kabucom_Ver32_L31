# push_pool_manager.py
CORE_LIMIT = 30
SCOUT_LIMIT = 20

core_push = set()
scout_push = set()

def promote_to_scout(symbol):
    if symbol in core_push:
        return
    scout_push.add(symbol)

def promote_to_core(symbol):
    if symbol in scout_push:
        scout_push.remove(symbol)
    core_push.add(symbol)

def can_entry(symbol):
    return symbol in core_push
