# ============================================================
# File   : core/startup/entry_controller_runtime_reject_patch.py
# Version: V6-FULLY-INLINED
# ------------------------------------------------------------
# V6: kabu APIエラー診断 (Code=100368 信用新規抑止 / Code=100033 銘柄別制限) を含む
#     _execute_best_candidate 本体は trading/handlers/entry_controller.py の
#     _execute_best_candidate_dispatch (Ver2.9) へインライン化済みのため撤去した。
#     起動時の古い100368 SELL拒否キャッシュ削除だけはここに残す。
# ============================================================

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_PATCHED = False

KABU_CODE_CREDIT_NEW_ORDER_SUPPRESSED = "100368"


def _norm_code(v: Any) -> str:
    try:
        if v is None:
            return ""
        s = str(v).strip()
        if s.endswith(".0"):
            s = s[:-2]
        return s
    except Exception:
        return ""


def _cleanup_stale_100368_sell_cache() -> int:
    """
    古いバージョンや起動時パッチで 100368 が sell_order_reject_cache に入っていた場合、
    起動直後に削除する。

    100368 は銘柄個別SELL NGではなく、API側の信用新規抑止応答として扱うため、
    キャッシュに残すと翌サイクル以降の候補が不当に消える。
    """
    removed = 0
    try:
        from global_state import global_data

        cache = getattr(global_data, "sell_order_reject_cache", None)
        if not isinstance(cache, dict):
            return 0

        for sym, rec in list(cache.items()):
            if not isinstance(rec, dict):
                continue
            code = _norm_code(rec.get("code"))
            msg = str(rec.get("message") or "")
            if code == KABU_CODE_CREDIT_NEW_ORDER_SUPPRESSED or ("信用新規" in msg and "抑止" in msg):
                cache.pop(sym, None)
                removed += 1

        if removed:
            logger.warning(
                "[ENTRY REJECT PATCH] stale 100368 sell reject cache cleaned removed=%s",
                removed,
            )
        return removed
    except Exception:
        logger.exception("[ENTRY REJECT PATCH] stale 100368 cache cleanup failed")
        return removed


def install() -> bool:
    global _PATCHED
    _cleanup_stale_100368_sell_cache()
    _PATCHED = True
    return True


try:
    install()
except Exception:
    logger.exception("[ENTRY REJECT PATCH] auto install failed")
