from global_state import global_data
from trading.handlers.entry_controller import run_entry_pipeline
import logging

logger = logging.getLogger(__name__)

def confirm_ranking_entries():
    pending = getattr(global_data, "pending_entries", {})
    if not pending:
        return

    confirmed = []

    for sym, e in list(pending.items()):
        if e.get("volume_speed", 0) < 3000:
            continue

        logger.info(
            f"[RANK CONFIRM] {sym} {e['symbolname']} "
            f"vol_speed={e['volume_speed']:.0f}"
        )
        confirmed.append(sym)

        # ★ 確定：ENTRY pipeline に流す
        run_entry_pipeline(source="ranking", symbol=sym)

        pending.pop(sym, None)

    if confirmed:
        logger.info(f"[RANK CONFIRM] confirmed={confirmed}")
