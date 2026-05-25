# ============================================================
# File   : trading/entry/tonosama/pending_writer.py
# Version: Ver1.1-TONOSAMA-PENDING-EXPIRE-PRUNE
# ------------------------------------------------------------
# Fix:
#   - TONOSAMA候補が毎回 duplicate 扱いになり、registered=0 のまま
#     entry_controller に流れない問題を軽減。
#   - pending entry の entry_conditions.expire_at を見て期限切れを削除。
#   - has_tonosama_pending() の前に期限切れTONOSAMAを prune する。
# ============================================================
from __future__ import annotations

import datetime as dt
import logging
from typing import Any

import pandas as pd

from trading.entry.pending_manager import add_pending, has_source, prune_entries
from .config import TONOSAMA_EXPIRE_SEC
from .utils import normalize_symbol, safe_float

logger = logging.getLogger(__name__)


def _parse_dt(v: Any) -> dt.datetime | None:
    try:
        if v is None:
            return None
        if isinstance(v, dt.datetime):
            return v
        if isinstance(v, str) and v.strip():
            return dt.datetime.fromisoformat(v.strip().replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None
    return None


def _is_expired_tonosama_entry(entry: dict[str, Any], *, now: dt.datetime | None = None) -> bool:
    try:
        if str(entry.get("source", "")).strip().upper() != "TONOSAMA":
            return False
        ec = entry.get("entry_conditions") or {}
        expire_at = _parse_dt(ec.get("expire_at")) if isinstance(ec, dict) else None
        if expire_at is None:
            created_at = _parse_dt(entry.get("created_at"))
            if created_at is None:
                return False
            expire_at = created_at + dt.timedelta(seconds=int(TONOSAMA_EXPIRE_SEC or 180))
        return (now or dt.datetime.now()) >= expire_at
    except Exception:
        return False


def prune_expired_tonosama_pending(*, reason: str = "TONOSAMA_EXPIRED") -> int:
    now = dt.datetime.now()
    try:
        removed = prune_entries(
            lambda _sym, e: _is_expired_tonosama_entry(e, now=now),
            reason=reason,
        )
        if removed:
            logger.warning("[TONOSAMA PENDING] expired pending pruned removed=%s reason=%s", removed, reason)
        return int(removed or 0)
    except Exception:
        logger.exception("[TONOSAMA PENDING] prune expired failed")
        return 0


def has_tonosama_pending(symbol: str) -> bool:
    prune_expired_tonosama_pending(reason="TONOSAMA_DUP_CHECK_EXPIRED")
    return bool(has_source(normalize_symbol(symbol), "TONOSAMA"))


def build_pending_entry(row: pd.Series, *, final_score: float, ai_prob: float, ai_reason: str) -> dict[str, Any]:
    now = dt.datetime.now()
    expire_at = now + dt.timedelta(seconds=TONOSAMA_EXPIRE_SEC)
    symbol = normalize_symbol(row.get("symbol"))
    return {
        "symbol": symbol,
        "symbolname": str(row.get("symbolname", "")),
        "side": "BUY",
        "source": "TONOSAMA",
        "entry_type": "TONOSAMA",
        "price": safe_float(row.get("close"), 0.0),
        "raw_score": safe_float(row.get("_tonosama_score"), 0.0),
        "final_score": safe_float(final_score, 0.0),
        "display_score": safe_float(final_score, 0.0),
        "score": safe_float(final_score, 0.0),
        "score_buy": safe_float(final_score, 0.0),
        "score_sell": 0.0,
        "ai_prob": safe_float(ai_prob, 0.0),
        "entry_conditions": {
            "expire_at": expire_at,
            "reason": "3m_5m_volume_surge_price_change_5sec_ai",
            "ai_reason": ai_reason,
            "volume_surge_ratio_3m": safe_float(row.get("volume_surge_ratio_3m"), 0.0),
            "volume_surge_ratio_5m": safe_float(row.get("volume_surge_ratio_5m"), 0.0),
            "max_volume_surge_ratio": safe_float(row.get("_max_volume_surge_ratio"), 0.0),
            "price_change_pct_3m": safe_float(row.get("price_change_pct_3m"), 0.0),
            "price_change_pct_5m": safe_float(row.get("price_change_pct_5m"), 0.0),
            "max_price_change_pct": safe_float(row.get("_max_price_change_pct"), 0.0),
            "has_5sec_bar": bool(row.get("has_5sec_bar", False)),
            "latest_5sec_close": safe_float(row.get("latest_5sec_close"), 0.0),
            "latest_5sec_volume": safe_float(row.get("latest_5sec_volume"), 0.0),
            "price_change_5s_pct": safe_float(row.get("price_change_5s_pct"), 0.0),
            "volume_surge_ratio_5s": safe_float(row.get("volume_surge_ratio_5s"), 0.0),
            "is_5sec_confirm_ok": bool(row.get("is_5sec_confirm_ok", False)),
            "surge_tf": str(row.get("_surge_tf", "")),
            "slope": safe_float(row.get("_slope"), 0.0),
            "rsi": safe_float(row.get("rsi"), 0.0),
            "macd": safe_float(row.get("macd"), 0.0),
            "signal": safe_float(row.get("signal"), 0.0),
            "mtf": safe_float(row.get("mtf"), 0.0),
            "score_mtf": safe_float(row.get("score_mtf"), 0.0),
        },
        "created_at": now,
    }


def add_tonosama_pending(entry: dict[str, Any]) -> bool:
    try:
        prune_expired_tonosama_pending(reason="TONOSAMA_BEFORE_ADD_EXPIRED")
        return bool(add_pending(entry))
    except Exception:
        logger.exception("[TONOSAMA ENTRY] add_pending failed symbol=%s", entry.get("symbol"))
        return False


__all__ = [
    "has_tonosama_pending",
    "build_pending_entry",
    "add_tonosama_pending",
    "prune_expired_tonosama_pending",
]
