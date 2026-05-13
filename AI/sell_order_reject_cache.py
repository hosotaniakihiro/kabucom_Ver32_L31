# ============================================================
# File   : AI/sell_order_reject_cache.py
# Version: PRODUCTION-RUNTIME-SELL-REJECT-CACHE-V3-NO-100368-CACHE
# ------------------------------------------------------------
# kabuステーションAPIで「銘柄個別に」信用新規売りが拒否された銘柄を、
# 当日ランタイム中だけ SELL NG として記録する。
#
# 重要修正:
#   - Code=100368
#     「現在、株式信用新規の注文は抑止されております。」は、
#     銘柄個別SELL拒否キャッシュに入れない。
#     理由: 100368 は銘柄個別NGではなく、API/セッション側の信用新規抑止を
#           示す可能性が高く、ここで銘柄キャッシュすると次候補・次サイクルの
#           SELL候補が大量に事前除外される。
#   - Code=100033
#     「この銘柄のお取引は制限されています。」だけを、銘柄個別SELL NGとして扱う。
#   - 古いランタイムパッチ等で 100368 が cache に入っていても、
#     is_sell_rejected() 側で検出して自動解除する。
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)

# kabuステーションAPIコード
KABU_CODE_CREDIT_NEW_ORDER_REJECTED = "100368"
KABU_CODE_SYMBOL_TRADE_RESTRICTED = "100033"

# SELL拒否キャッシュへ入れるのは「銘柄個別制限」だけ。
_REJECT_CODES = {
    KABU_CODE_SYMBOL_TRADE_RESTRICTED,
}

# 銘柄個別制限だけを拾う文言。100368系の「信用新規抑止」は含めない。
_REJECT_MESSAGE_HINTS = (
    "この銘柄のお取引は制限",
    "お取引は制限",
    "取引制限",
    "取引注意情報",
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
    SELL拒否キャッシュ対象かどうかを判定する。

    注意:
      関数名は互換のため残しているが、V3では 100368 を False にする。
      100368 は send_order.py 側で「API拒否ログ」として扱い、
      SELL reject cache / trade_restricted には入れない。
    """
    c = _normalize_code(code)

    if c == KABU_CODE_CREDIT_NEW_ORDER_REJECTED:
        return False

    if c in _REJECT_CODES:
        return True

    msg = str(message or "")
    if not msg:
        return False

    # 100368系の文言は銘柄個別キャッシュ対象外。
    if "信用新規" in msg and "抑止" in msg:
        return False

    return any(h in msg for h in _REJECT_MESSAGE_HINTS)


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

    V3:
      100368 は登録しない。100033 等の銘柄個別制限だけ登録する。
    """
    sym = _normalize_symbol(symbol)
    if not sym:
        return False

    c = _normalize_code(code)
    if c == KABU_CODE_CREDIT_NEW_ORDER_REJECTED:
        logger.warning(
            "[SELL ORDER REJECT CACHE] ignore 100368 symbol=%s code=%s message=%s source=%s",
            sym,
            c,
            message,
            source,
        )
        return False

    if not is_credit_new_order_reject(code, message):
        return False

    now = dt.datetime.now()
    until = now + dt.timedelta(minutes=int(minutes or _DEFAULT_BLOCK_MINUTES))

    cache = _get_runtime_cache()
    cache[sym] = {
        "symbol": sym,
        "code": c,
        "message": str(message or ""),
        "source": source,
        "blocked_at": now,
        "blocked_until": until,
    }

    logger.warning(
        "[SELL ORDER REJECT CACHE] blocked symbol=%s code=%s until=%s message=%s source=%s",
        sym,
        c,
        until,
        message,
        source,
    )

    _prune_pending_sell_entries(sym, reason=f"SELL_ORDER_REJECT_{c or 'UNKNOWN'}")
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

    V3:
      古いコードや起動時パッチにより 100368 が入っていた場合は、
      検出時点で削除して False を返す。
    """
    sym = _normalize_symbol(symbol)
    if not sym:
        return False

    cache = _get_runtime_cache()
    rec = cache.get(sym)
    if not isinstance(rec, dict):
        return False

    code = _normalize_code(rec.get("code"))
    message = str(rec.get("message") or "")

    if code == KABU_CODE_CREDIT_NEW_ORDER_REJECTED or ("信用新規" in message and "抑止" in message):
        cache.pop(sym, None)
        logger.warning(
            "[SELL ORDER REJECT CACHE] removed stale 100368 cache symbol=%s code=%s message=%s",
            sym,
            code,
            message,
        )
        return False

    until = rec.get("blocked_until")
    if isinstance(until, dt.datetime) and dt.datetime.now() > until:
        cache.pop(sym, None)
        logger.info("[SELL ORDER REJECT CACHE] expired symbol=%s", sym)
        return False

    logger.info(
        "[SELL ORDER REJECT CACHE] active symbol=%s code=%s until=%s message=%s",
        sym,
        code,
        until,
        message,
    )
    return True


def get_sell_reject_reason(symbol: Any) -> str:
    sym = _normalize_symbol(symbol)
    cache = _get_runtime_cache()
    rec = cache.get(sym) if sym else None
    if not isinstance(rec, dict):
        return ""
    return f"code={rec.get('code')} message={rec.get('message')} until={rec.get('blocked_until')}"
