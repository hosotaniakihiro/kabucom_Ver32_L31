# ============================================================
# File   : trading/board/board_state.py
# Version: Ver01-WALL-EATEN-STALL-ANALYZER
# ------------------------------------------------------------
# 複数段板スナップショットを短時間だけメモリ保持し、
# 「厚い板が食われているのに株価が止まる」状態を検知する。
#
# 目的:
#   - BUY保有中: 上の売り壁が減っているのに上抜けない → 反転警戒EXIT
#   - SELL保有中: 下の買い支えが減っているのに下抜けない → 反転警戒EXIT
#   - ENTRY側では、壁が食われて突破方向なら強い材料として利用可能
#
# 注意:
#   - DB保存なし。軽量なメモリリングだけ。
#   - API失敗時は None を返す。
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

_HISTORY: dict[str, deque[dict[str, Any]]] = defaultdict(lambda: deque(maxlen=30))


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None:
            return bool(default)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on"}:
            return True
        if s in {"0", "false", "no", "n", "off", ""}:
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


def _norm_symbol(symbol: Any) -> str:
    s = str(symbol or "").strip()
    if s.endswith(".0") and s[:-2].isdigit():
        return s[:-2]
    return s


def _now_price(snapshot: dict[str, Any], fallback_price: float = 0.0) -> float:
    p = _safe_float(snapshot.get("current_price"), 0.0)
    if p > 0:
        return p
    return _safe_float(fallback_price, 0.0)


def _sum_qty_near_price(snapshot: dict[str, Any], *, side: str, price: float, ticks: int = 3) -> float:
    """
    BUY目線: side='SELL' で現在値より上の売り板合計
    SELL目線: side='BUY' で現在値より下の買い板合計
    tick size は簡易に価格差の段数で扱うため、ladder先頭から近い順に ticks 本を見る。
    """
    ladder_key = "sell" if side.upper() == "SELL" else "buy"
    ladder = snapshot.get(ladder_key)
    if not isinstance(ladder, list):
        return 0.0
    rows = []
    for r in ladder:
        if not isinstance(r, dict):
            continue
        px = _safe_float(r.get("price"), 0.0)
        qty = _safe_float(r.get("qty"), 0.0)
        if px <= 0 or qty <= 0:
            continue
        if side.upper() == "SELL" and px >= price:
            rows.append((px, qty))
        elif side.upper() == "BUY" and px <= price:
            rows.append((px, qty))
    rows = sorted(rows, key=lambda x: abs(x[0] - price))[: max(1, int(ticks))]
    return sum(q for _px, q in rows)


def _best_price(snapshot: dict[str, Any], side: str) -> float:
    if side.upper() == "SELL":
        return _safe_float(snapshot.get("best_ask"), 0.0)
    return _safe_float(snapshot.get("best_bid"), 0.0)


def update_board_history(symbol: str, *, exchange: int = 1, fallback_price: float = 0.0) -> Optional[dict[str, Any]]:
    symbol_n = _norm_symbol(symbol)
    if not symbol_n:
        return None
    snap = fetch_board_snapshot(
        symbol_n,
        exchange=int(exchange),
        timeout=_env_float("BOARD_API_TIMEOUT_SEC", 1.0),
        levels=int(_env_float("BOARD_API_LEVELS", 10)),
    )
    if not snap:
        return None
    if _safe_float(snap.get("current_price"), 0.0) <= 0 and fallback_price > 0:
        snap["current_price"] = float(fallback_price)
    _HISTORY[symbol_n].append(snap)
    return snap


def get_board_history(symbol: str) -> list[dict[str, Any]]:
    return list(_HISTORY.get(_norm_symbol(symbol), []))


def analyze_wall_eaten_stall(
    symbol: str,
    *,
    position_side: str,
    current_price: float,
    exchange: int = 1,
) -> Optional[dict[str, Any]]:
    """
    保有中EXIT用。

    BUY保有:
      上の売り板が減っているのに、価格が上抜けず止まったら EXIT候補。

    SELL保有:
      下の買い板が減っているのに、価格が下抜けず止まったら EXIT候補。
    """
    if not _env_bool("EXIT_BOARD_WALL_STALL_ENABLED", True):
        return None

    symbol_n = _norm_symbol(symbol)
    snap = update_board_history(symbol_n, exchange=exchange, fallback_price=current_price)
    if not snap:
        return None

    hist = get_board_history(symbol_n)
    lookback_sec = _env_float("EXIT_BOARD_WALL_LOOKBACK_SEC", 8.0)
    min_eaten_ratio = _env_float("EXIT_BOARD_WALL_MIN_EATEN_RATIO", 0.35)
    min_wall_qty = _env_float("EXIT_BOARD_WALL_MIN_QTY", 1000.0)
    max_price_move_pct = _env_float("EXIT_BOARD_WALL_STALL_MAX_MOVE_PCT", 0.0008)
    near_levels = int(_env_float("EXIT_BOARD_WALL_NEAR_LEVELS", 3))

    now_dt = _parse_dt(snap.get("fetched_at")) or dt.datetime.now()
    old = None
    for h in reversed(hist):
        hdt = _parse_dt(h.get("fetched_at"))
        if hdt is None:
            continue
        if (now_dt - hdt).total_seconds() >= lookback_sec:
            old = h
            break
    if old is None and len(hist) >= 2:
        old = hist[0]
    if old is None or old is snap:
        return None

    side_u = str(position_side or "").upper()
    is_buy_pos = side_u.startswith("BUY") or side_u in {"2", "買", "買い"}
    is_sell_pos = side_u.startswith("SELL") or side_u.startswith("SHORT") or side_u in {"1", "売", "売り"}
    if not is_buy_pos and not is_sell_pos:
        return None

    now_p = _now_price(snap, current_price)
    old_p = _now_price(old, current_price)
    if now_p <= 0 or old_p <= 0:
        return None

    if is_buy_pos:
        old_wall = _sum_qty_near_price(old, side="SELL", price=old_p, ticks=near_levels)
        now_wall = _sum_qty_near_price(snap, side="SELL", price=now_p, ticks=near_levels)
        breakthrough_price = _best_price(old, "SELL")
        price_progress = (now_p - old_p) / old_p if old_p > 0 else 0.0
        broke = breakthrough_price > 0 and now_p >= breakthrough_price
        reason = "BOARD_BUY_WALL_EATEN_STALL_EXIT"
    else:
        old_wall = _sum_qty_near_price(old, side="BUY", price=old_p, ticks=near_levels)
        now_wall = _sum_qty_near_price(snap, side="BUY", price=now_p, ticks=near_levels)
        breakthrough_price = _best_price(old, "BUY")
        price_progress = (old_p - now_p) / old_p if old_p > 0 else 0.0
        broke = breakthrough_price > 0 and now_p <= breakthrough_price
        reason = "BOARD_SELL_SUPPORT_EATEN_STALL_EXIT"

    if old_wall < min_wall_qty:
        return None
    eaten_qty = max(0.0, old_wall - now_wall)
    eaten_ratio = eaten_qty / old_wall if old_wall > 0 else 0.0

    # 板は食われたが、価格進捗が小さい。または一度突破していない。
    stalled = price_progress <= max_price_move_pct or not broke
    if eaten_ratio >= min_eaten_ratio and stalled:
        detail = {
            "symbol": symbol_n,
            "side": side_u,
            "reason": reason,
            "old_price": old_p,
            "now_price": now_p,
            "price_progress": price_progress,
            "max_price_move_pct": max_price_move_pct,
            "old_wall_qty": old_wall,
            "now_wall_qty": now_wall,
            "eaten_qty": eaten_qty,
            "eaten_ratio": eaten_ratio,
            "min_eaten_ratio": min_eaten_ratio,
            "min_wall_qty": min_wall_qty,
            "breakthrough_price": breakthrough_price,
            "broke": broke,
            "lookback_sec": lookback_sec,
        }
        logger.warning("[BOARD WALL STALL EXIT] %s", detail)
        return detail

    logger.debug(
        "[BOARD WALL STALL HOLD] symbol=%s side=%s old_wall=%.0f now_wall=%.0f eaten=%.3f price_progress=%.5f broke=%s",
        symbol_n,
        side_u,
        old_wall,
        now_wall,
        eaten_ratio,
        price_progress,
        broke,
    )
    return None


__all__ = ["update_board_history", "get_board_history", "analyze_wall_eaten_stall"]
