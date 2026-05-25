# ============================================================
# File   : trading/entry/tonosama/pending_writer.py
# Version: Ver1.3-TONOSAMA-PENDING-JA-REASONS
# ------------------------------------------------------------
# Fix:
#   - TONOSAMA候補が毎回 duplicate 扱いになり、registered=0 のまま
#     entry_controller に流れない問題を軽減。
#   - pending entry の entry_conditions.expire_at / expire_at / created_at+TTL を見て期限切れを削除。
#   - has_tonosama_pending() の前に対象銘柄の期限切れTONOSAMAを prune する。
#   - ループ側から全体掃除できる prune_expired_tonosama_pending() も維持。
#   - pandas.Timestamp / datetime文字列 / timezone付き文字列を安全に解釈。
# Ver1.3:
#   - Discord表示用の理由を日本語化。
#   - pending は「難病保持」ではなく「発注待ち候補」であることが分かるようにする。
# ============================================================
from __future__ import annotations

import datetime as dt
import logging
from typing import Any

import pandas as pd

from trading.entry.pending_manager import add_pending, get_bucket, replace_bucket, snapshot_root
from .config import TONOSAMA_EXPIRE_SEC
from .utils import normalize_symbol, safe_float

logger = logging.getLogger(__name__)


def _norm_source(v: Any) -> str:
    try:
        return str(v or "").strip().upper()
    except Exception:
        return ""


def _is_tonosama_entry(entry: Any) -> bool:
    return isinstance(entry, dict) and _norm_source(entry.get("source")) == "TONOSAMA"


def _parse_dt(v: Any) -> dt.datetime | None:
    try:
        if v is None or v == "":
            return None
        if isinstance(v, pd.Timestamp):
            if pd.isna(v):
                return None
            py = v.to_pydatetime()
            return py.replace(tzinfo=None) if py.tzinfo else py
        if isinstance(v, dt.datetime):
            return v.replace(tzinfo=None) if v.tzinfo else v
        if isinstance(v, dt.date):
            return dt.datetime.combine(v, dt.time.min)
        s = str(v).strip()
        if not s:
            return None
        parsed = pd.to_datetime(s, errors="coerce")
        if pd.isna(parsed):
            return None
        py = parsed.to_pydatetime()
        return py.replace(tzinfo=None) if py.tzinfo else py
    except Exception:
        return None


def _expire_at(entry: dict[str, Any]) -> dt.datetime | None:
    try:
        ec = entry.get("entry_conditions") or {}
        if isinstance(ec, dict):
            exp = _parse_dt(ec.get("expire_at"))
            if exp is not None:
                return exp

        exp = _parse_dt(entry.get("expire_at"))
        if exp is not None:
            return exp

        created_at = _parse_dt(entry.get("created_at"))
        if created_at is not None:
            return created_at + dt.timedelta(seconds=int(TONOSAMA_EXPIRE_SEC or 180))
    except Exception:
        return None
    return None


def _is_expired_tonosama_entry(entry: dict[str, Any], *, now: dt.datetime | None = None) -> bool:
    try:
        if not _is_tonosama_entry(entry):
            return False
        exp = _expire_at(entry)
        if exp is None:
            # 期限が読めない古いTONOSAMAは、安全側で一旦残す。
            return False
        return (now or dt.datetime.now()) >= exp
    except Exception:
        return False


def _prune_symbol_expired_tonosama(symbol: str, *, reason: str, now: dt.datetime | None = None) -> int:
    sym = normalize_symbol(symbol)
    if not sym:
        return 0

    removed = 0
    now = now or dt.datetime.now()
    bucket = get_bucket(sym)
    kept: list[dict[str, Any]] = []

    for entry in bucket:
        if isinstance(entry, dict) and _is_expired_tonosama_entry(entry, now=now):
            removed += 1
            logger.warning(
                "[TONOSAMA PENDING] expired pruned symbol=%s reason=%s expire_at=%s created_at=%s side=%s score=%s",
                sym,
                reason,
                _expire_at(entry),
                entry.get("created_at"),
                entry.get("side"),
                entry.get("final_score") or entry.get("score"),
            )
            continue
        if isinstance(entry, dict):
            kept.append(entry)

    if removed:
        replace_bucket(sym, kept)
    return removed


def prune_expired_tonosama_pending(symbol: str | None = None, *, reason: str = "TONOSAMA_EXPIRED") -> int:
    """期限切れのTONOSAMA pendingを削除する。symbol指定時は対象銘柄のみ。"""
    now = dt.datetime.now()
    try:
        if symbol:
            return _prune_symbol_expired_tonosama(symbol, reason=reason, now=now)

        removed = 0
        for sym in list(snapshot_root().keys()):
            removed += _prune_symbol_expired_tonosama(sym, reason=reason, now=now)
        if removed:
            logger.warning("[TONOSAMA PENDING] expired pending prune done removed=%s reason=%s root=%s", removed, reason, snapshot_root())
        return int(removed or 0)
    except Exception:
        logger.exception("[TONOSAMA PENDING] prune expired failed symbol=%s reason=%s", symbol, reason)
        return 0


def has_tonosama_pending(symbol: str) -> bool:
    sym = normalize_symbol(symbol)
    if not sym:
        return False
    prune_expired_tonosama_pending(sym, reason="TONOSAMA_DUP_CHECK_EXPIRED")
    bucket = get_bucket(sym)
    return any(_is_tonosama_entry(e) for e in bucket if isinstance(e, dict))


def _build_reason_ja(row: pd.Series, *, ai_reason: str) -> str:
    max_surge = safe_float(row.get("_max_volume_surge_ratio"), 0.0)
    max_chg = safe_float(row.get("_max_price_change_pct"), 0.0)
    chg_5s = safe_float(row.get("price_change_5s_pct"), 0.0)
    has_5s = bool(row.get("has_5sec_bar", False))
    slope = safe_float(row.get("_slope"), 0.0)
    tf = str(row.get("_surge_tf", ""))

    parts = [
        f"{tf or '3m/5m'}で出来高急増 {max_surge:.2f}倍",
        f"価格変化 {max_chg:.2f}%",
        f"傾き {slope:.4f}",
    ]
    if has_5s:
        parts.append(f"5秒変化 {chg_5s:.3f}%")
    else:
        parts.append("5秒足なしのため3m/5m条件で判定")

    if ai_reason:
        parts.append(f"AI判定: {ai_reason}")
    return " / ".join(parts)


def build_pending_entry(row: pd.Series, *, final_score: float, ai_prob: float, ai_reason: str) -> dict[str, Any]:
    now = dt.datetime.now()
    expire_at = now + dt.timedelta(seconds=TONOSAMA_EXPIRE_SEC)
    symbol = normalize_symbol(row.get("symbol"))
    reason_ja = _build_reason_ja(row, ai_reason=ai_reason)
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
        "expire_at": expire_at,
        "entry_conditions": {
            "expire_at": expire_at,
            "reason": reason_ja,
            "reason_code": "tonosama_volume_surge_price_change_5sec_ai",
            "ai_reason": ai_reason,
            "volume_surge_ratio_3m": safe_float(row.get("volume_surge_ratio_3m"), 0.0),
            "volume_surge_ratio_5m": safe_float(row.get("volume_surge_ratio_5m"), 0.0),
            "max_volume_surge_ratio": safe_float(row.get("_max_volume_surge_ratio"), 0.0),
            "price_change_pct_3m": safe_float(row.get("price_change_pct_3m"), 0.0),
            "price_change_pct_5m": safe_float(row.get("price_change_pct_5m"), 0.0),
            "max_price_change_pct": safe_float(row.get("_max_price_change_pct"), 0.0),
            "body_change_pct": safe_float(row.get("_body_change_pct"), 0.0),
            "intrabar_range_pct": safe_float(row.get("_intrabar_range_pct"), 0.0),
            "latest_volume": safe_float(row.get("_latest_volume"), 0.0),
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
        prune_expired_tonosama_pending(entry.get("symbol"), reason="TONOSAMA_BEFORE_ADD_EXPIRED")
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
