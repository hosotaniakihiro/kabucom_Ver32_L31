# ============================================================
# File   : AI/sell_order_reject_cache.py
# Version: PRODUCTION-RUNTIME-SELL-REJECT-CACHE-V1
# ------------------------------------------------------------
# kabuステーションAPIで信用新規売りが拒否された銘柄を、
# 当日ランタイム中だけ SELL NG として記録する。
#
# 主目的:
#   - Code=100368
#     「現在、株式信用新規の注文は抑止されております。」
#     が返った銘柄を再発注しない。
#   - symbol_flags.db 上は貸借 / sell_target=1 でも、API側で
#     実際に抑止される銘柄を学習して止める。
#   - pending_entries に残っている同銘柄 SELL 候補も掃除する。
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)

# kabuステーションAPIの信用新規注文抑止
KABU_CODE_CREDIT_NEW_ORDER_REJECTED = "100368"

# 念のため、同系統の文言でも拾う
_REJECT_MESSAGE_HINTS = (
    "株式信用新規の注文は抑止",
    "信用新規の注文は抑止",
    "信用新規",
    "抑止",
)

_DEFAULT_BLOCK_MINUTES = 360  # 当日中運用想定。ランタイム中は十分長めに止める。


def _normalize_symbol(v: Any) -> str:
    try:
        s = str(v).strip()
        if s.endswith(".0"):
            s = s[:-2]
        if s.lower() in ("", "none", "nan", "nat"):
            return ""
        return s
    except Exception:
        return ""


def _normalize_code(v: Any) -> str:
    try:
        if v is None:
            return ""
        s = str(v).strip()
        if s.endswith(".0"):
            s = s[:-2]
        return s
    except Exception:
        return ""


def is_credit_new_order_reject(code: Any = None, message: Any = None) -> bool:
    """
    kabu API の信用新規注文抑止かどうかを判定する。
    """
    c = _normalize_code(code)
    if c == KABU_CODE_CREDIT_NEW_ORDER_REJECTED:
        return True

    msg = str(message or "")
    if not msg:
        return False

    return all(h in msg for h in ("信用新規", "抑止")) or any(h in msg for h in _REJECT_MESSAGE_HINTS[:2])


def _get_runtime_cache() -> Dict[str, Dict[str, Any]]:
    try:
        from global_state import global_data

        cache = getattr(global_data, "sell_order_reject_cache", None)
        if not isinstance(cache, dict):
            cache = {}
            setattr(global_data, "sell_order_reject_cache", cache)
        return cache
    except Exception:
        logger.exception("[SELL ORDER REJECT CACHE] failed to get runtime cache")
        return {}


def mark_sell_rejected(
    symbol: Any,
    *,
    code: Any = None,
    message: Any = None,
    minutes: int = _DEFAULT_BLOCK_MINUTES,
    source: str = "kabu_api",
) -> bool:
    """
    銘柄をランタイム SELL NG として登録する。
    登録時に pending_entries の同銘柄 SELL も掃除する。
    """
    sym = _normalize_symbol(symbol)
    if not sym:
        return False

    if not is_credit_new_order_reject(code, message):
        return False

    now = dt.datetime.now()
    until = now + dt.timedelta(minutes=int(minutes or _DEFAULT_BLOCK_MINUTES))

    cache = _get_runtime_cache()
    cache[sym] = {
        "symbol": sym,
        "code": _normalize_code(code),
        "message": str(message or ""),
        "source": source,
        "blocked_at": now,
        "blocked_until": until,
    }

    logger.warning(
        "[SELL ORDER REJECT CACHE] blocked symbol=%s code=%s until=%s message=%s source=%s",
        sym,
        _normalize_code(code),
        until,
        message,
        source,
    )

    _prune_pending_sell_entries(sym, reason=f"SELL_ORDER_REJECT_{_normalize_code(code) or 'UNKNOWN'}")
    return True


def _prune_pending_sell_entries(symbol: str, *, reason: str) -> None:
    try:
        from trading.entry.pending_manager import prune_entries

        def _predicate(sym: str, entry: Dict[str, Any]) -> bool:
            if _normalize_symbol(sym) != symbol:
                return False
            if not isinstance(entry, dict):
                return False
            side = str(entry.get("side") or entry.get("entry_decision") or "").strip().upper()
            return side == "SELL"

        removed = prune_entries(_predicate, reason=reason)
        if removed:
            logger.warning(
                "[SELL ORDER REJECT CACHE] pending SELL entries pruned symbol=%s removed=%s reason=%s",
                symbol,
                removed,
                reason,
            )
    except Exception:
        logger.exception("[SELL ORDER REJECT CACHE] pending prune failed symbol=%s", symbol)


def is_sell_rejected(symbol: Any) -> bool:
    """
    ランタイム中に信用新規売り拒否が記録されているか。
    期限切れなら自動削除する。
    """
    sym = _normalize_symbol(symbol)
    if not sym:
        return False

    cache = _get_runtime_cache()
    rec = cache.get(sym)
    if not isinstance(rec, dict):
        return False

    until = rec.get("blocked_until")
    if isinstance(until, dt.datetime) and dt.datetime.now() > until:
        cache.pop(sym, None)
        logger.info("[SELL ORDER REJECT CACHE] expired symbol=%s", sym)
        return False

    logger.info(
        "[SELL ORDER REJECT CACHE] active symbol=%s code=%s until=%s message=%s",
        sym,
        rec.get("code"),
        until,
        rec.get("message"),
    )
    return True


def get_sell_reject_reason(symbol: Any) -> str:
    sym = _normalize_symbol(symbol)
    cache = _get_runtime_cache()
    rec = cache.get(sym) if sym else None
    if not isinstance(rec, dict):
        return ""
    return f"code={rec.get('code')} message={rec.get('message')} until={rec.get('blocked_until')}"
