# ============================================================
# File   : trading/entry/tonosama/notifier.py
# Version: Ver1.0-TONOSAMA-ENTRY-NOTIFIER
# ============================================================
from __future__ import annotations
import logging
from typing import Any
from .config import DISCORD_NOTIFY_ON_PENDING
from .utils import safe_float
logger = logging.getLogger(__name__)

def notify_discord_tonosama_pending(entry: dict[str, Any]) -> None:
    if not DISCORD_NOTIFY_ON_PENDING:
        return
    try:
        from utils.alerts_util import send_discord_notify
        cond = entry.get("entry_conditions") or {}
        msg = ("🔥 **TONOSAMA ENTRY PENDING**\n" f"銘柄: `{entry.get('symbol')}` {entry.get('symbolname', '')}\n" f"価格: `{safe_float(entry.get('price'), 0.0):.1f}`\n" f"score: `{safe_float(entry.get('final_score'), 0.0):.2f}`  AI: `{safe_float(entry.get('ai_prob'), 0.0):.3f}`\n" f"出来高急増: 3m=`{safe_float(cond.get('volume_surge_ratio_3m'), 0.0):.2f}x` 5m=`{safe_float(cond.get('volume_surge_ratio_5m'), 0.0):.2f}x` max=`{safe_float(cond.get('max_volume_surge_ratio'), 0.0):.2f}x`\n" f"価格変化: 3m=`{safe_float(cond.get('price_change_pct_3m'), 0.0):.2f}%` 5m=`{safe_float(cond.get('price_change_pct_5m'), 0.0):.2f}%` 5s=`{safe_float(cond.get('price_change_5s_pct'), 0.0):.3f}%`\n" f"TF: `{cond.get('surge_tf', '')}`  slope=`{safe_float(cond.get('slope'), 0.0):.4f}`\n" f"理由: `{cond.get('reason', '')}`\n" f"AI理由: `{cond.get('ai_reason', '')}`")
        send_discord_notify(msg)
        logger.info("[TONOSAMA ENTRY] discord notified symbol=%s", entry.get("symbol"))
    except Exception:
        logger.exception("[TONOSAMA ENTRY] discord notify failed")
