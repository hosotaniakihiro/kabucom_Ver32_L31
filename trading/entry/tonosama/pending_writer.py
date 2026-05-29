# ============================================================
# File   : trading/entry/tonosama/pending_writer.py
# Version: Ver2.0-TONOSAMA-PENDING-TIME-AND-DIRECTION-FINAL-GUARD
# ------------------------------------------------------------
# 目的:
#   殿様イナゴの pending 登録と Discord 通知直前の最終安全ガード。
#
# Ver2.0:
#   - 通知理由に「判定時刻」「特徴量時刻」「5秒足時刻」を追加。
#   - 実データと通知内容のズレを追えるよう、entry_conditionsにも時刻を保存。
#   - BUYは 5m価格変化がマイナスなら最終拒否。
#   - SELLは 5m価格変化がプラスなら最終拒否。
#   - 5秒足が存在するのに 5s=0.000% 近辺なら最終拒否。
#   - AI fallbackが古い/緩い状態で通しても add_pending 直前で止める。
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


def _env_bool(name: str, default: bool) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}:
            return True
        if s in {"0", "false", "no", "n", "off", "ng", "disable", "disabled"}:
            return False
        return bool(default)
    except Exception:
        return bool(default)


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
MIN_FINAL_LATEST_VOLUME = _env_float("TONOSAMA_MIN_FINAL_LATEST_VOLUME", _env_float("TONOSAMA_MIN_LATEST_VOLUME", 50000.0))

# Ver2.0 final direction / 5sec guard
MIN_FINAL_5SEC_CHANGE_PCT = _env_float("TONOSAMA_FINAL_MIN_5SEC_CHANGE_PCT", _env_float("TONOSAMA_MIN_5SEC_PRICE_CHANGE_PCT", 0.01))
REJECT_ZERO_5SEC_FINAL = _env_bool("TONOSAMA_FINAL_REJECT_ZERO_5SEC", True)
MIN_BUY_5M_CHANGE_FINAL = _env_float("TONOSAMA_FINAL_MIN_BUY_5M_CHANGE", 0.0)
MAX_SELL_5M_CHANGE_FINAL = _env_float("TONOSAMA_FINAL_MAX_SELL_5M_CHANGE", 0.0)


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


def _fmt_dt(v: Any) -> str:
    d = _parse_dt(v)
    if d is None:
        return "不明"
    try:
        return d.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(d)


def _first_dt_from_row(row: pd.Series, names: list[str]) -> Any:
    try:
        for n in names:
            if n in row.index:
                v = row.get(n)
                if v is not None and str(v).strip() != "":
                    return v
    except Exception:
        pass
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
    chg_3m = safe_float(row.get("price_change_pct_3m"), 0.0)
    chg_5m = safe_float(row.get("price_change_pct_5m"), 0.0)
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

    decision_at = dt.datetime.now()
    feature_dt = _first_dt_from_row(row, ["datetime", "dt", "summary_dt", "bar_dt", "latest_dt", "_latest_dt"])
    five_sec_dt = _first_dt_from_row(row, ["latest_5sec_dt", "five_sec_dt", "bar_5s_dt", "dt_5s", "timestamp_5s"])

    parts = [
        f"判定時刻 {decision_at.strftime('%Y-%m-%d %H:%M:%S')}",
        f"特徴量時刻 {_fmt_dt(feature_dt)}",
        f"5秒足時刻 {_fmt_dt(five_sec_dt) if has_5s else 'なし'}",
        f"方向 {side}",
        f"{tf or '3m/5m'}で出来高急増 {max_surge:.2f}倍",
        f"価格変化 max={max_chg:.2f}% / 3m={chg_3m:.2f}% / 5m={chg_5m:.2f}%",
        f"傾き {slope:.4f}",
        f"出来高 1m={latest_volume:.0f} / 3m={volume_3m:.0f} / 5m={volume_5m:.0f}",
        f"3分連続上昇 {up3}本 / 5分連続上昇 {up5}本 / 3分連続下落 {dn3}本 / 5分連続下落 {dn5}本",
        "ランキングスナップショットMA方向は通知直前に判定",
    ]
    parts.append(f"5秒変化 {chg_5s:.3f}%" if has_5s else "5秒足なしのため3m/5m条件で判定")
    if ai_reason:
        parts.append(f"AI判定: {ai_reason}")
    return " / ".join(parts)


def _entry_conditions_from_row(row: pd.Series, *, ai_reason: str, side: str, expire_at: dt.datetime) -> dict[str, Any]:
    feature_dt = _first_dt_from_row(row, ["datetime", "dt", "summary_dt", "bar_dt", "latest_dt", "_latest_dt"])
    five_sec_dt = _first_dt_from_row(row, ["latest_5sec_dt", "five_sec_dt", "bar_5s_dt", "dt_5s", "timestamp_5s"])
    return {
        "expire_at": expire_at,
        "decision_at": dt.datetime.now(),
        "feature_dt": _fmt_dt(feature_dt),
        "five_sec_dt": _fmt_dt(five_sec_dt) if bool(row.get("has_5sec_bar", False)) else "なし",
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


def _ranking_ma_reject_reason(symbol: str, side: str) -> tuple[str | None, dict[str, Any]]:
    try:
        from .ranking_snapshot_ma_guard import reject_reason_for_side
        return reject_reason_for_side(symbol, side)
    except Exception:
        logger.warning("[TONOSAMA PENDING GUARD] ranking snapshot MA guard failed symbol=%s side=%s", symbol, side, exc_info=True)
        return None, {"reason": "ranking_ma_guard_exception"}


def _climax_reject_reason(entry: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    ma_info: dict[str, Any] = {}
    try:
        cond = entry.get("entry_conditions") or {}
        side = str(entry.get("side") or cond.get("side") or "BUY").upper()
        symbol = normalize_symbol(entry.get("symbol"))
        ma_reject, ma_info = _ranking_ma_reject_reason(symbol, side)
        if ma_reject:
            return ma_reject, ma_info

        surge = safe_float(cond.get("max_volume_surge_ratio"), 0.0)
        price_chg = safe_float(cond.get("max_price_change_pct"), 0.0)
        chg_3m = safe_float(cond.get("price_change_pct_3m"), 0.0)
        chg_5m = safe_float(cond.get("price_change_pct_5m"), 0.0)
        chg_5s = safe_float(cond.get("price_change_5s_pct"), 0.0)
        has_5s = bool(cond.get("has_5sec_bar", False))
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
            return "latest_volume_low_final_guard", ma_info

        if has_5s and REJECT_ZERO_5SEC_FINAL and abs(chg_5s) < MIN_FINAL_5SEC_CHANGE_PCT:
            return "five_sec_stopped_final_guard", ma_info

        if side == "BUY":
            buy_like = (price_chg > 0) or (signed_body > 0) or (slope > 0)
            if chg_5m < MIN_BUY_5M_CHANGE_FINAL:
                return "buy_5m_reverse_final_guard", ma_info
            if chg_3m < 0:
                return "buy_3m_reverse_final_guard", ma_info
            if max(up3, up5) > MAX_BUY_PREV_3M5M_UP_STREAK:
                return "buy_after_3m5m_up_streak_guard", ma_info
            if buy_like and price_chg >= MAX_BUY_PRICE_CHANGE_PCT:
                return "buy_price_chase_too_late", ma_info
            if buy_like and close_pos >= MAX_BUY_CLOSE_POSITION_PCT and price_chg >= BUYING_CLIMAX_MIN_PRICE_CHANGE_PCT:
                return "buying_climax_high_zone", ma_info
            if buy_like and upper_wick >= MAX_BUY_UPPER_WICK_PCT and close_pos <= BUY_REJECTED_CLOSE_POSITION_PCT:
                return "buying_climax_upper_wick_reversal", ma_info
            if buy_like and surge >= BUYING_CLIMAX_MIN_SURGE_RATIO and upper_wick >= MAX_BUY_UPPER_WICK_PCT and close_pos <= 60.0:
                return "buying_climax_upper_wick_warning", ma_info
            if buy_like and surge >= BUYING_CLIMAX_MIN_SURGE_RATIO and (
                (price_chg >= BUYING_CLIMAX_MIN_PRICE_CHANGE_PCT and close_pos >= MAX_BUY_CLOSE_POSITION_PCT)
                or (upper_wick >= MAX_BUY_UPPER_WICK_PCT and close_pos <= BUY_REJECTED_CLOSE_POSITION_PCT)
            ):
                return "buying_climax_or_high_chase_guard", ma_info

        if side == "SELL":
            drop_abs = abs(price_chg)
            sell_like = (price_chg < 0) or (signed_body < 0) or (slope < 0)
            if chg_5m > MAX_SELL_5M_CHANGE_FINAL:
                return "sell_5m_reverse_final_guard", ma_info
            if chg_3m > 0:
                return "sell_3m_reverse_final_guard", ma_info
            if max(dn3, dn5) > MAX_SELL_PREV_3M5M_DOWN_STREAK:
                return "sell_after_3m5m_down_streak_guard", ma_info
            if sell_like and drop_abs >= MAX_SELL_PRICE_DROP_PCT:
                return "sell_price_chase_too_late", ma_info
            if sell_like and close_pos <= MIN_SELL_CLOSE_POSITION_PCT and drop_abs >= SELLING_CLIMAX_MIN_PRICE_DROP_PCT:
                return "selling_climax_low_zone", ma_info
            if sell_like and lower_wick >= MAX_SELL_LOWER_WICK_PCT and close_pos >= SELL_REJECTED_CLOSE_POSITION_PCT:
                return "selling_climax_lower_wick_reversal", ma_info
            if sell_like and surge >= SELLING_CLIMAX_MIN_SURGE_RATIO and lower_wick >= MAX_SELL_LOWER_WICK_PCT and close_pos >= 40.0:
                return "selling_climax_lower_wick_warning", ma_info
            if sell_like and surge >= SELLING_CLIMAX_MIN_SURGE_RATIO and (
                (drop_abs >= SELLING_CLIMAX_MIN_PRICE_DROP_PCT and close_pos <= MIN_SELL_CLOSE_POSITION_PCT)
                or (lower_wick >= MAX_SELL_LOWER_WICK_PCT and close_pos >= SELL_REJECTED_CLOSE_POSITION_PCT)
            ):
                return "selling_climax_or_low_chase_guard", ma_info
    except Exception:
        logger.debug("[TONOSAMA PENDING GUARD] climax/liquidity/ma check failed", exc_info=True)
    return None, ma_info


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
        reject, ma_info = _climax_reject_reason(entry)
        if reject:
            cond = entry.get("entry_conditions") or {}
            logger.warning(
                "[TONOSAMA PENDING GUARD] reject symbol=%s side=%s reason=%s feature_dt=%s five_sec_dt=%s price_chg=%.3f chg3m=%.3f chg5m=%.3f chg5s=%.3f surge=%.2f latest_volume=%.0f min_volume=%.0f volume_3m=%.0f volume_5m=%.0f close_pos=%.1f upper_wick=%.1f lower_wick=%.1f up3=%s up5=%s dn3=%s dn5=%s slope=%.6f ranking_ma=%s",
                entry.get("symbol"), entry.get("side"), reject,
                cond.get("feature_dt"), cond.get("five_sec_dt"),
                safe_float(cond.get("max_price_change_pct"), 0.0),
                safe_float(cond.get("price_change_pct_3m"), 0.0),
                safe_float(cond.get("price_change_pct_5m"), 0.0),
                safe_float(cond.get("price_change_5s_pct"), 0.0),
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
                ma_info,
            )
            return False
        return bool(add_pending(entry))
    except Exception:
        logger.exception("[TONOSAMA ENTRY] add_pending failed symbol=%s", entry.get("symbol"))
        return False


__all__ = ["has_tonosama_pending", "build_pending_entry", "add_tonosama_pending", "prune_expired_tonosama_pending"]
