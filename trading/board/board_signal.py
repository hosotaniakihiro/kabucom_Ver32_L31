# ============================================================
# File   : trading/board/board_signal.py
# Version: Ver02-BOARD-IMBALANCE-COLLAPSE-PASSIVE-PRICE
# ------------------------------------------------------------
# 板情報をエントリー直前ガード、保有中EXIT、発注指値補正に使う。
#
# ENTRY:
#   - BUYなのに買い板が弱すぎる
#   - SELLなのに買い板が強すぎる
#   - 進行方向の反対側に巨大壁がある
#
# ORDER PRICE:
#   - BUY: 厚い売り板の1ティック下へ指値候補を寄せる
#   - SELL: 厚い買い板の1ティック上へ指値候補を寄せる
#   - 板が取れない/壁がない/補正が危険な場合は従来価格を返す
#
# EXIT:
#   - BUY保有中に買い支え板が崩れる
#   - SELL保有中に売り支え板が崩れる
#
# API失敗時は None を返して fail-open。既存ロジックを止めない。
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import math
import os
from collections import defaultdict, deque
from typing import Any, Optional

from trading.board.board_client import fetch_board_snapshot

logger = logging.getLogger(__name__)

_HISTORY: dict[str, deque[dict[str, Any]]] = defaultdict(lambda: deque(maxlen=40))


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None:
            return bool(default)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on", "enable", "enabled"}:
            return True
        if s in {"0", "false", "no", "n", "off", "", "disable", "disabled"}:
            return False
        return bool(default)
    except Exception:
        return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        x = float(v)
        if math.isnan(x) or math.isinf(x):
            return float(default)
        return x
    except Exception:
        return float(default)


def _norm_symbol(v: Any) -> str:
    try:
        s = str(v or "").strip().upper()
        if s.endswith(".0") and s[:-2].isdigit():
            s = s[:-2]
        if s.endswith(".T"):
            s = s[:-2]
        return s
    except Exception:
        return ""


def _parse_dt(v: Any) -> Optional[dt.datetime]:
    try:
        if isinstance(v, dt.datetime):
            return v.replace(tzinfo=None) if v.tzinfo else v
        if not v:
            return None
        s = str(v).replace("T", " ").split("+", 1)[0]
        if s.endswith("Z"):
            s = s[:-1]
        return dt.datetime.fromisoformat(s)
    except Exception:
        return None


def _tick_size(price: float) -> float:
    """東証呼値単位の安全側近似。"""
    p = float(price or 0.0)
    if p <= 0:
        return 1.0
    if p <= 1000:
        return 0.1
    if p <= 3000:
        return 0.5
    if p <= 5000:
        return 1.0
    if p <= 30000:
        return 5.0
    if p <= 50000:
        return 10.0
    if p <= 300000:
        return 50.0
    if p <= 500000:
        return 100.0
    if p <= 3000000:
        return 500.0
    return 1000.0


def _round_to_tick(price: float, *, side: str) -> float:
    tick = _tick_size(price)
    if tick <= 0:
        return float(price)
    x = float(price) / tick
    if str(side).upper() == "BUY":
        return math.floor(x + 1e-9) * tick
    return math.ceil(x - 1e-9) * tick


def _fetch_and_remember(symbol: str, *, exchange: int, fallback_price: float = 0.0) -> Optional[dict[str, Any]]:
    sym = _norm_symbol(symbol)
    if not sym:
        return None
    snap = fetch_board_snapshot(
        sym,
        exchange=int(exchange),
        timeout=_env_float("BOARD_API_TIMEOUT_SEC", 1.0),
        levels=_env_int("BOARD_API_LEVELS", 10),
    )
    if not snap:
        return None
    if _safe_float(snap.get("current_price"), 0.0) <= 0 and fallback_price > 0:
        snap["current_price"] = float(fallback_price)
    _HISTORY[sym].append(snap)
    return snap


def _sum_ladder(snapshot: dict[str, Any], key: str, levels: int) -> float:
    rows = snapshot.get(key)
    if not isinstance(rows, list):
        return 0.0
    total = 0.0
    for r in rows[: max(1, int(levels))]:
        if isinstance(r, dict):
            total += max(0.0, _safe_float(r.get("qty"), 0.0))
    return total


def _imbalance(snapshot: dict[str, Any], levels: int) -> tuple[float, float, float]:
    bid_sum = _sum_ladder(snapshot, "buy", levels)
    ask_sum = _sum_ladder(snapshot, "sell", levels)
    ratio = bid_sum / max(ask_sum, 1.0)
    return bid_sum, ask_sum, ratio


def analyze_entry_board_imbalance(symbol: str, *, side: str, exchange: int = 1) -> Optional[dict[str, Any]]:
    """発注直前用。不利な板だけNGとして返す。OK/取得不可は None。"""
    if not _env_bool("ENTRY_BOARD_IMBALANCE_ENABLED", True):
        return None

    sym = _norm_symbol(symbol)
    side_u = str(side or "").strip().upper()
    if side_u not in {"BUY", "SELL"}:
        return None

    snap = _fetch_and_remember(sym, exchange=exchange)
    if not snap:
        return None

    levels = _env_int("ENTRY_BOARD_IMBALANCE_LEVELS", 5)
    bid_sum, ask_sum, ratio = _imbalance(snap, levels)
    min_buy_ratio = _env_float("ENTRY_BOARD_BUY_MIN_BID_ASK_RATIO", 0.70)
    max_sell_ratio = _env_float("ENTRY_BOARD_SELL_MAX_BID_ASK_RATIO", 1.30)
    wall_ratio = _env_float("ENTRY_BOARD_WALL_REJECT_RATIO", 3.00)
    wall_min_qty = _env_float("ENTRY_BOARD_WALL_MIN_QTY", 1500.0)
    wall_enabled = _env_bool("ENTRY_BOARD_WALL_REJECT_ENABLED", True)

    reason = None
    if side_u == "BUY" and ratio < min_buy_ratio:
        reason = "ENTRY_BOARD_BUY_WEAK_BID"
    elif side_u == "SELL" and ratio > max_sell_ratio:
        reason = "ENTRY_BOARD_SELL_STRONG_BID"
    elif wall_enabled and side_u == "BUY" and ask_sum >= bid_sum * wall_ratio and ask_sum >= wall_min_qty:
        reason = "ENTRY_BOARD_BUY_ASK_WALL"
    elif wall_enabled and side_u == "SELL" and bid_sum >= ask_sum * wall_ratio and bid_sum >= wall_min_qty:
        reason = "ENTRY_BOARD_SELL_BID_WALL"

    detail = {"symbol": sym, "side": side_u, "bid_sum": bid_sum, "ask_sum": ask_sum, "bid_ask_ratio": ratio, "levels": levels, "best_bid": _safe_float(snap.get("best_bid"), 0.0), "best_ask": _safe_float(snap.get("best_ask"), 0.0), "reason": reason}
    if reason:
        logger.warning("[BOARD ENTRY IMBALANCE NG] %s", detail)
        return detail
    logger.info("[BOARD ENTRY IMBALANCE OK] %s", detail)
    return None


def suggest_passive_entry_price(symbol: str, *, side: str, bid: float = 0.0, ask: float = 0.0, exchange: int = 1) -> Optional[dict[str, Any]]:
    """厚い板の1ティック手前に指値候補を置く。BUY=売り壁1ティック下、SELL=買い壁1ティック上。"""
    if not _env_bool("ENTRY_BOARD_PASSIVE_PRICE_ENABLED", True):
        return None
    sym = _norm_symbol(symbol)
    side_u = str(side or "").strip().upper()
    if side_u not in {"BUY", "SELL"}:
        return None
    snap = _fetch_and_remember(sym, exchange=exchange)
    if not snap:
        return None
    best_bid = _safe_float(snap.get("best_bid"), bid) or _safe_float(bid, 0.0)
    best_ask = _safe_float(snap.get("best_ask"), ask) or _safe_float(ask, 0.0)
    if best_bid <= 0 or best_ask <= 0:
        return None

    levels = _env_int("ENTRY_BOARD_PASSIVE_WALL_LEVELS", 10)
    min_wall_qty = _env_float("ENTRY_BOARD_PASSIVE_MIN_WALL_QTY", 1500.0)
    wall_mult = _env_float("ENTRY_BOARD_PASSIVE_WALL_MULTIPLIER", 2.5)
    max_aggress_ticks = _env_int("ENTRY_BOARD_PASSIVE_MAX_AGGRESS_TICKS", 3)
    allow_cross = _env_bool("ENTRY_BOARD_PASSIVE_ALLOW_MARKETABLE", True)

    ladder_key = "sell" if side_u == "BUY" else "buy"
    ladder = snap.get(ladder_key)
    if not isinstance(ladder, list) or not ladder:
        return None
    qtys = [_safe_float(r.get("qty"), 0.0) for r in ladder[:levels] if isinstance(r, dict)]
    avg_qty = (sum(qtys) / len(qtys)) if qtys else 0.0
    threshold = max(min_wall_qty, avg_qty * wall_mult)
    wall = None
    for r in ladder[:levels]:
        if not isinstance(r, dict):
            continue
        px = _safe_float(r.get("price"), 0.0)
        qty = _safe_float(r.get("qty"), 0.0)
        if px <= 0 or qty < threshold:
            continue
        if side_u == "BUY" and px >= best_ask:
            wall = {"price": px, "qty": qty}
            break
        if side_u == "SELL" and px <= best_bid:
            wall = {"price": px, "qty": qty}
            break
    if not wall:
        return None

    wall_price = _safe_float(wall.get("price"), 0.0)
    tick = _tick_size(wall_price)
    if side_u == "BUY":
        candidate = _round_to_tick(wall_price - tick, side="BUY")
        base = best_ask
        max_price = best_ask + tick * max(0, max_aggress_ticks)
        if not allow_cross:
            max_price = best_ask
        candidate = min(candidate, max_price)
        candidate = max(candidate, best_bid)
    else:
        candidate = _round_to_tick(wall_price + tick, side="SELL")
        base = best_bid
        min_price = best_bid - tick * max(0, max_aggress_ticks)
        if not allow_cross:
            min_price = best_bid
        candidate = max(candidate, min_price)
        candidate = min(candidate, best_ask)
    if candidate <= 0:
        return None

    detail = {"symbol": sym, "side": side_u, "price": float(candidate), "base_price": float(base), "best_bid": float(best_bid), "best_ask": float(best_ask), "wall_price": float(wall_price), "wall_qty": _safe_float(wall.get("qty"), 0.0), "threshold": threshold, "avg_qty": avg_qty, "tick": tick, "levels": levels, "marketable_allowed": allow_cross, "reason": "ENTRY_BOARD_PASSIVE_ONE_TICK_BEFORE_WALL"}
    logger.warning("[BOARD PASSIVE ENTRY PRICE] %s", detail)
    return detail


def analyze_exit_board_collapse(symbol: str, *, position_side: str, current_price: float = 0.0, exchange: int = 1) -> Optional[dict[str, Any]]:
    """保有中用。支え板崩壊/逆板優勢ならEXIT理由を返す。"""
    if not _env_bool("EXIT_BOARD_COLLAPSE_ENABLED", True):
        return None
    sym = _norm_symbol(symbol)
    side_u = str(position_side or "").strip().upper()
    is_buy = side_u.startswith("BUY") or side_u in {"2", "買", "買い"}
    is_sell = side_u.startswith("SELL") or side_u.startswith("SHORT") or side_u in {"1", "売", "売り"}
    if not sym or (not is_buy and not is_sell):
        return None
    snap = _fetch_and_remember(sym, exchange=exchange, fallback_price=current_price)
    if not snap:
        return None
    levels = _env_int("EXIT_BOARD_COLLAPSE_LEVELS", 5)
    bid_sum, ask_sum, ratio = _imbalance(snap, levels)
    now_dt = _parse_dt(snap.get("fetched_at")) or dt.datetime.now()
    lookback_sec = _env_float("EXIT_BOARD_COLLAPSE_LOOKBACK_SEC", 6.0)
    min_support_qty = _env_float("EXIT_BOARD_COLLAPSE_MIN_SUPPORT_QTY", 800.0)
    support_drop_ratio = _env_float("EXIT_BOARD_COLLAPSE_SUPPORT_DROP_RATIO", 0.45)
    buy_min_ratio = _env_float("EXIT_BOARD_BUY_MIN_BID_ASK_RATIO", 0.55)
    sell_max_ratio = _env_float("EXIT_BOARD_SELL_MAX_BID_ASK_RATIO", 1.80)
    old = None
    hist = list(_HISTORY.get(sym, []))
    for h in reversed(hist):
        hdt = _parse_dt(h.get("fetched_at"))
        if hdt and (now_dt - hdt).total_seconds() >= lookback_sec:
            old = h
            break
    if old is None and len(hist) >= 2:
        old = hist[0]
    old_bid = old_ask = old_ratio = 0.0
    if isinstance(old, dict) and old is not snap:
        old_bid, old_ask, old_ratio = _imbalance(old, levels)
    reason = None
    if is_buy:
        if ratio < buy_min_ratio:
            reason = "EXIT_BOARD_BUY_SUPPORT_WEAK"
        elif old_bid >= min_support_qty and bid_sum <= old_bid * max(0.0, 1.0 - support_drop_ratio):
            reason = "EXIT_BOARD_BUY_SUPPORT_COLLAPSE"
    else:
        if ratio > sell_max_ratio:
            reason = "EXIT_BOARD_SELL_ASK_SUPPORT_WEAK"
        elif old_ask >= min_support_qty and ask_sum <= old_ask * max(0.0, 1.0 - support_drop_ratio):
            reason = "EXIT_BOARD_SELL_SUPPORT_COLLAPSE"
    if not reason:
        return None
    detail = {"symbol": sym, "side": side_u, "reason": reason, "bid_sum": bid_sum, "ask_sum": ask_sum, "bid_ask_ratio": ratio, "old_bid_sum": old_bid, "old_ask_sum": old_ask, "old_bid_ask_ratio": old_ratio, "levels": levels, "lookback_sec": lookback_sec, "current_price": current_price}
    logger.warning("[BOARD EXIT COLLAPSE] %s", detail)
    return detail


__all__ = ["analyze_entry_board_imbalance", "suggest_passive_entry_price", "analyze_exit_board_collapse"]
