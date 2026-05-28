# ============================================================
# File   : trading/entry/tonosama/pending_writer.py
# Version: Ver1.8-TONOSAMA-PENDING-FINAL-LIQUIDITY-GUARD
# ------------------------------------------------------------
# 目的:
#   殿様イナゴの pending 登録と Discord 通知直前の最終安全ガード。
#
# Ver1.7:
#   - 1分足streak判定を廃止。
#   - BUY: 3分足または5分足が3本以上連続上昇していた後はエントリーしない。
#   - SELL: 3分足または5分足が3本以上連続下落していた後はエントリーしない。
#
# Ver1.8:
#   - 出来高が少ない銘柄が pending / Discord通知まで進むのを防ぐ。
#   - runner.py の一次フィルターに加えて、add_pending直前でも latest_volume を再確認。
#   - 既定は TONOSAMA_MIN_FINAL_LATEST_VOLUME=50000 株。
#   - 3m/5m出来高も entry_conditions とログへ残す。
# ============================================================
from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Any

import pandas as pd

from trading.entry.pending_manager import add_pending, get_bucket, replace_bucket, snapshot_root
from .config import TONOSAMA_EXPIRE_SEC
from .utils import normalize_symbol, safe_float

logger = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return float(default)


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, str(default))))
    except Exception:
        return int(default)


MAX_BUY_PRICE_CHANGE_PCT = _env_float("TONOSAMA_MAX_BUY_PRICE_CHANGE_PCT", 0.80)
MAX_BUY_CLOSE_POSITION_PCT = _env_float("TONOSAMA_MAX_BUY_CLOSE_POSITION_PCT", 90.0)
MAX_BUY_UPPER_WICK_PCT = _env_float("TONOSAMA_MAX_BUY_UPPER_WICK_PCT", 45.0)
BUY_REJECTED_CLOSE_POSITION_PCT = _env_float("TONOSAMA_BUY_REJECTED_CLOSE_POSITION_PCT", 35.0)
BUYING_CLIMAX_MIN_SURGE_RATIO = _env_float("TONOSAMA_BUYING_CLIMAX_MIN_SURGE_RATIO", 3.0)
BUYING_CLIMAX_MIN_PRICE_CHANGE_PCT = _env_float("TONOSAMA_BUYING_CLIMAX_MIN_PRICE_CHANGE_PCT", 0.50)

MAX_SELL_PRICE_DROP_PCT = _env_float("TONOSAMA_MAX_SELL_PRICE_DROP_PCT", 0.80)
MIN_SELL_CLOSE_POSITION_PCT = _env_float("TONOSAMA_MIN_SELL_CLOSE_POSITION_PCT", 10.0)
MAX_SELL_LOWER_WICK_PCT = _env_float("TONOSAMA_MAX_SELL_LOWER_WICK_PCT", 45.0)
SELL_REJECTED_CLOSE_POSITION_PCT = _env_float("TONOSAMA_SELL_REJECTED_CLOSE_POSITION_PCT", 65.0)
SELLING_CLIMAX_MIN_SURGE_RATIO = _env_float("TONOSAMA_SELLING_CLIMAX_MIN_SURGE_RATIO", 3.0)
SELLING_CLIMAX_MIN_PRICE_DROP_PCT = _env_float("TONOSAMA_SELLING_CLIMAX_MIN_PRICE_DROP_PCT", 0.50)

MAX_BUY_PREV_3M5M_UP_STREAK = _env_int("TONOSAMA_MAX_BUY_PREV_3M5M_UP_STREAK", 2)
MAX_SELL_PREV_3M5M_DOWN_STREAK = _env_int("TONOSAMA_MAX_SELL_PREV_3M5M_DOWN_STREAK", 2)

# 最終流動性ガード。一次フィルターが抜けても、pending/Discord直前で必ず止める。
MIN_FINAL_LATEST_VOLUME = _env_float("TONOSAMA_MIN_FINAL_LATEST_VOLUME", _env_float("TONOSAMA_MIN_LATEST_VOLUME", 50000.0))


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
                sym, reason, _expire_at(entry), entry.get("created_at"), entry.get("side"), entry.get("final_score") or entry.get("score"),
            )
            continue
        if isinstance(entry, dict):
            kept.append(entry)
    if removed:
        replace_bucket(sym, kept)
    return removed


def prune_expired_tonosama_pending(symbol: str | None = None, *, reason: str = "TONOSAMA_EXPIRED") -> int:
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


def _infer_side_from_row(row: pd.Series) -> str:
    max_chg = safe_float(row.get("_max_price_change_pct"), 0.0)
    signed_body = safe_float(row.get("_signed_body_change_pct"), max_chg)
    slope = safe_float(row.get("_slope"), 0.0)
    score_sell = safe_float(row.get("score_sell"), 0.0)
    score_buy = safe_float(row.get("score_buy"), 0.0)
    if max_chg < 0:
        return "SELL"
    if max_chg > 0:
        return "BUY"
    if signed_body < 0:
        return "SELL"
    if signed_body > 0:
        return "BUY"
    if slope < 0:
        return "SELL"
    if slope > 0:
        return "BUY"
    if score_sell > score_buy:
        return "SELL"
    return "BUY"


def _build_reason_ja(row: pd.Series, *, ai_reason: str, side: str) -> str:
    max_surge = safe_float(row.get("_max_volume_surge_ratio"), 0.0)
    max_chg = safe_float(row.get("_max_price_change_pct"), 0.0)
    chg_5s = safe_float(row.get("price_change_5s_pct"), 0.0)
    has_5s = bool(row.get("has_5sec_bar", False))
    slope = safe_float(row.get("_slope"), 0.0)
    latest_volume = safe_float(row.get("_latest_volume"), 0.0)
    volume_3m = safe_float(row.get("volume_3m"), 0.0)
    volume_5m = safe_float(row.get("volume_5m"), 0.0)
    tf = str(row.get("_surge_tf", ""))
    up3 = int(safe_float(row.get("prev_3m_up_streak"), 0.0))
    up5 = int(safe_float(row.get("prev_5m_up_streak"), 0.0))
    dn3 = int(safe_float(row.get("prev_3m_down_streak"), 0.0))
    dn5 = int(safe_float(row.get("prev_5m_down_streak"), 0.0))
    parts = [
        f"方向 {side}",
        f"{tf or '3m/5m'}で出来高急増 {max_surge:.2f}倍",
        f"価格変化 {max_chg:.2f}%",
        f"傾き {slope:.4f}",
        f"出来高 1m={latest_volume:.0f} / 3m={volume_3m:.0f} / 5m={volume_5m:.0f}",
        f"3分連続上昇 {up3}本 / 5分連続上昇 {up5}本 / 3分連続下落 {dn3}本 / 5分連続下落 {dn5}本",
    ]
    parts.append(f"5秒変化 {chg_5s:.3f}%" if has_5s else "5秒足なしのため3m/5m条件で判定")
    if ai_reason:
        parts.append(f"AI判定: {ai_reason}")
    return " / ".join(parts)


def _entry_conditions_from_row(row: pd.Series, *, ai_reason: str, side: str, expire_at: dt.datetime) -> dict[str, Any]:
    return {
        "expire_at": expire_at,
        "reason": _build_reason_ja(row, ai_reason=ai_reason, side=side),
        "reason_code": "tonosama_volume_surge_price_change_5sec_ai",
        "ai_reason": ai_reason,
        "side": side,
        "volume_surge_ratio_3m": safe_float(row.get("volume_surge_ratio_3m"), 0.0),
        "volume_surge_ratio_5m": safe_float(row.get("volume_surge_ratio_5m"), 0.0),
        "max_volume_surge_ratio": safe_float(row.get("_max_volume_surge_ratio"), 0.0),
        "price_change_pct_3m": safe_float(row.get("price_change_pct_3m"), 0.0),
        "price_change_pct_5m": safe_float(row.get("price_change_pct_5m"), 0.0),
        "max_price_change_pct": safe_float(row.get("_max_price_change_pct"), 0.0),
        "signed_body_change_pct": safe_float(row.get("_signed_body_change_pct"), safe_float(row.get("_max_price_change_pct"), 0.0)),
        "body_change_pct": safe_float(row.get("_body_change_pct"), 0.0),
        "intrabar_range_pct": safe_float(row.get("_intrabar_range_pct"), 0.0),
        "close_position_pct": safe_float(row.get("_close_position_pct"), 50.0),
        "upper_wick_pct": safe_float(row.get("_upper_wick_pct"), 0.0),
        "lower_wick_pct": safe_float(row.get("_lower_wick_pct"), 0.0),
        "prev_3m_up_streak": int(safe_float(row.get("prev_3m_up_streak"), 0.0)),
        "prev_5m_up_streak": int(safe_float(row.get("prev_5m_up_streak"), 0.0)),
        "prev_3m_down_streak": int(safe_float(row.get("prev_3m_down_streak"), 0.0)),
        "prev_5m_down_streak": int(safe_float(row.get("prev_5m_down_streak"), 0.0)),
        "prev_3m_last_delta_pct": safe_float(row.get("prev_3m_last_delta_pct"), 0.0),
        "prev_5m_last_delta_pct": safe_float(row.get("prev_5m_last_delta_pct"), 0.0),
        "latest_volume": safe_float(row.get("_latest_volume"), 0.0),
        "volume_3m": safe_float(row.get("volume_3m"), 0.0),
        "volume_5m": safe_float(row.get("volume_5m"), 0.0),
        "min_final_latest_volume": MIN_FINAL_LATEST_VOLUME,
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
    }


def _climax_reject_reason(entry: dict[str, Any]) -> str | None:
    try:
        cond = entry.get("entry_conditions") or {}
        side = str(entry.get("side") or cond.get("side") or "BUY").upper()
        surge = safe_float(cond.get("max_volume_surge_ratio"), 0.0)
        price_chg = safe_float(cond.get("max_price_change_pct"), 0.0)
        signed_body = safe_float(cond.get("signed_body_change_pct"), price_chg)
        slope = safe_float(cond.get("slope"), 0.0)
        close_pos = safe_float(cond.get("close_position_pct"), 50.0)
        upper_wick = safe_float(cond.get("upper_wick_pct"), 0.0)
        lower_wick = safe_float(cond.get("lower_wick_pct"), 0.0)
        latest_volume = safe_float(cond.get("latest_volume"), 0.0)
        up3 = int(safe_float(cond.get("prev_3m_up_streak"), 0.0))
        up5 = int(safe_float(cond.get("prev_5m_up_streak"), 0.0))
        dn3 = int(safe_float(cond.get("prev_3m_down_streak"), 0.0))
        dn5 = int(safe_float(cond.get("prev_5m_down_streak"), 0.0))

        if latest_volume < MIN_FINAL_LATEST_VOLUME:
            return "latest_volume_low_final_guard"

        if side == "BUY":
            buy_like = (price_chg > 0) or (signed_body > 0) or (slope > 0)
            if max(up3, up5) > MAX_BUY_PREV_3M5M_UP_STREAK:
                return "buy_after_3m5m_up_streak_guard"
            if buy_like and price_chg >= MAX_BUY_PRICE_CHANGE_PCT:
                return "buy_price_chase_too_late"
            if buy_like and close_pos >= MAX_BUY_CLOSE_POSITION_PCT and price_chg >= BUYING_CLIMAX_MIN_PRICE_CHANGE_PCT:
                return "buying_climax_high_zone"
            if buy_like and upper_wick >= MAX_BUY_UPPER_WICK_PCT and close_pos <= BUY_REJECTED_CLOSE_POSITION_PCT:
                return "buying_climax_upper_wick_reversal"
            if buy_like and surge >= BUYING_CLIMAX_MIN_SURGE_RATIO and upper_wick >= MAX_BUY_UPPER_WICK_PCT and close_pos <= 60.0:
                return "buying_climax_upper_wick_warning"
            if buy_like and surge >= BUYING_CLIMAX_MIN_SURGE_RATIO and (
                (price_chg >= BUYING_CLIMAX_MIN_PRICE_CHANGE_PCT and close_pos >= MAX_BUY_CLOSE_POSITION_PCT)
                or (upper_wick >= MAX_BUY_UPPER_WICK_PCT and close_pos <= BUY_REJECTED_CLOSE_POSITION_PCT)
            ):
                return "buying_climax_or_high_chase_guard"

        if side == "SELL":
            drop_abs = abs(price_chg)
            sell_like = (price_chg < 0) or (signed_body < 0) or (slope < 0)
            if max(dn3, dn5) > MAX_SELL_PREV_3M5M_DOWN_STREAK:
                return "sell_after_3m5m_down_streak_guard"
            if sell_like and drop_abs >= MAX_SELL_PRICE_DROP_PCT:
                return "sell_price_chase_too_late"
            if sell_like and close_pos <= MIN_SELL_CLOSE_POSITION_PCT and drop_abs >= SELLING_CLIMAX_MIN_PRICE_DROP_PCT:
                return "selling_climax_low_zone"
            if sell_like and lower_wick >= MAX_SELL_LOWER_WICK_PCT and close_pos >= SELL_REJECTED_CLOSE_POSITION_PCT:
                return "selling_climax_lower_wick_reversal"
            if sell_like and surge >= SELLING_CLIMAX_MIN_SURGE_RATIO and lower_wick >= MAX_SELL_LOWER_WICK_PCT and close_pos >= 40.0:
                return "selling_climax_lower_wick_warning"
            if sell_like and surge >= SELLING_CLIMAX_MIN_SURGE_RATIO and (
                (drop_abs >= SELLING_CLIMAX_MIN_PRICE_DROP_PCT and close_pos <= MIN_SELL_CLOSE_POSITION_PCT)
                or (lower_wick >= MAX_SELL_LOWER_WICK_PCT and close_pos >= SELL_REJECTED_CLOSE_POSITION_PCT)
            ):
                return "selling_climax_or_low_chase_guard"
    except Exception:
        logger.debug("[TONOSAMA PENDING GUARD] climax/liquidity check failed", exc_info=True)
    return None


def build_pending_entry(row: pd.Series, *, final_score: float, ai_prob: float, ai_reason: str) -> dict[str, Any]:
    now = dt.datetime.now()
    expire_at = now + dt.timedelta(seconds=TONOSAMA_EXPIRE_SEC)
    symbol = normalize_symbol(row.get("symbol"))
    side = _infer_side_from_row(row)
    conditions = _entry_conditions_from_row(row, ai_reason=ai_reason, side=side, expire_at=expire_at)
    final = safe_float(final_score, 0.0)
    return {
        "symbol": symbol,
        "symbolname": str(row.get("symbolname", "")),
        "side": side,
        "source": "TONOSAMA",
        "entry_type": "TONOSAMA",
        "price": safe_float(row.get("close"), 0.0),
        "raw_score": safe_float(row.get("_tonosama_score"), 0.0),
        "final_score": final,
        "display_score": final,
        "score": final if side == "BUY" else -abs(final),
        "score_buy": final if side == "BUY" else 0.0,
        "score_sell": final if side == "SELL" else 0.0,
        "ai_prob": safe_float(ai_prob, 0.0),
        "expire_at": expire_at,
        "entry_conditions": conditions,
        "created_at": now,
    }


def add_tonosama_pending(entry: dict[str, Any]) -> bool:
    try:
        prune_expired_tonosama_pending(entry.get("symbol"), reason="TONOSAMA_BEFORE_ADD_EXPIRED")
        reject = _climax_reject_reason(entry)
        if reject:
            cond = entry.get("entry_conditions") or {}
            logger.warning(
                "[TONOSAMA PENDING GUARD] reject symbol=%s side=%s reason=%s price_chg=%.3f surge=%.2f latest_volume=%.0f min_volume=%.0f volume_3m=%.0f volume_5m=%.0f close_pos=%.1f upper_wick=%.1f lower_wick=%.1f up3=%s up5=%s dn3=%s dn5=%s slope=%.6f",
                entry.get("symbol"), entry.get("side"), reject,
                safe_float(cond.get("max_price_change_pct"), 0.0),
                safe_float(cond.get("max_volume_surge_ratio"), 0.0),
                safe_float(cond.get("latest_volume"), 0.0),
                MIN_FINAL_LATEST_VOLUME,
                safe_float(cond.get("volume_3m"), 0.0),
                safe_float(cond.get("volume_5m"), 0.0),
                safe_float(cond.get("close_position_pct"), 50.0),
                safe_float(cond.get("upper_wick_pct"), 0.0),
                safe_float(cond.get("lower_wick_pct"), 0.0),
                int(safe_float(cond.get("prev_3m_up_streak"), 0.0)),
                int(safe_float(cond.get("prev_5m_up_streak"), 0.0)),
                int(safe_float(cond.get("prev_3m_down_streak"), 0.0)),
                int(safe_float(cond.get("prev_5m_down_streak"), 0.0)),
                safe_float(cond.get("slope"), 0.0),
            )
            return False
        return bool(add_pending(entry))
    except Exception:
        logger.exception("[TONOSAMA ENTRY] add_pending failed symbol=%s", entry.get("symbol"))
        return False


__all__ = ["has_tonosama_pending", "build_pending_entry", "add_tonosama_pending", "prune_expired_tonosama_pending"]
