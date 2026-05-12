# ============================================================
# pj/trading/exit/exit_handler.py
# Ver26.0-PRO-EXIT-SCHEDULER-GC-POSITIONS-NORMAL-EXIT
# Updated: 2026-05-12
# ------------------------------------------------------------
# ✔ 通常 EXIT ロジック完全温存方針
# ✔ ranking + TONOSAMA は即逃げ専用
# ✔ 初動成行 → トレーリング即逃げ
# ✔ 学習ログ連携（存在チェック付き）
# ✔ push 欠損・属性欠損完全耐性
# ✔ EXIT 経路を一本化（事故防止）
# ✔ EXIT加速（含み益 × 時間 × 3m/5m）統合
# ✔ GC.positions.open_positions も EXIT 対象に追加
# ✔ 通常 SUMMARY_AI / RANKING / EARLY_SCALP 用の最低限 EXIT 判定を有効化
# ✔ Session_trade 未定義バグを解消し Session_position に統一
# ✔ EXIT pipeline heartbeat / scan ログ追加
# ============================================================

from __future__ import annotations

import logging
import datetime as dt
from types import SimpleNamespace
from typing import Any, Iterable

import numpy as np
from numba import njit

from global_state import global_data
from database import Session_position
from database.models import Position, TradeHistory
from kabu_api.close import process_exit

try:
    from trading.entry.tonosama_logger import log_tonosama_trade
except Exception:
    log_tonosama_trade = None

logger = logging.getLogger("exit_ultra")

# ============================================================
# TONOSAMA フォールバックパラメータ（最後の保険）
# ============================================================
TONO_STOP_LOSS = -0.002      # -0.20%
TONO_TAKE_PROFIT = 0.003    # +0.30%
TONO_TRAIL_GAP = 0.0015     # -0.15%
TONO_MAX_HOLD_SEC = 90      # AI 未指定時の最大保持秒数

# ============================================================
# 通常 EXIT フォールバックパラメータ
# ============================================================
NORMAL_STOP_LOSS = -0.006     # -0.60%
NORMAL_TAKE_PROFIT = 0.008    # +0.80%
NORMAL_TRAIL_START = 0.006    # +0.60% からトレーリング開始
NORMAL_TRAIL_GAP = 0.003      # 高値/安値から -0.30%
NORMAL_MAX_HOLD_SEC = 300     # 5分

_EXIT_HEARTBEAT_EVERY_SEC = 10
_last_exit_heartbeat_at: dt.datetime | None = None


# ============================================================
# util
# ============================================================

def safe_float(x, default=0.0):
    try:
        if x is None or x == "":
            return default
        return float(x)
    except Exception:
        return default


def _safe_int(x, default=0):
    try:
        if x is None or x == "":
            return default
        return int(float(x))
    except Exception:
        return default


def _get(obj: Any, name: str, default=None):
    try:
        if isinstance(obj, dict):
            return obj.get(name, default)
        return getattr(obj, name, default)
    except Exception:
        return default


def _set(obj: Any, name: str, value: Any) -> None:
    try:
        if isinstance(obj, dict):
            obj[name] = value
        else:
            setattr(obj, name, value)
    except Exception:
        pass


def _is_buy_side(side: Any) -> bool:
    return str(side or "").upper().startswith("BUY")


def _is_sell_side(side: Any) -> bool:
    s = str(side or "").upper()
    return s.startswith("SELL") or s.startswith("SHORT")


def _is_dict_position(pos: Any) -> bool:
    return isinstance(pos, dict)


def _as_position_object(pos: Any):
    """
    process_exit は Position ORM 風の属性アクセスを想定する可能性があるため、
    dict の場合は SimpleNamespace に変換して渡す。
    """
    if isinstance(pos, dict):
        return SimpleNamespace(**pos)
    return pos


def _position_symbol(pos: Any) -> str:
    return str(
        _get(pos, "symbol")
        or _get(pos, "Symbol")
        or _get(pos, "stock_code")
        or ""
    ).strip()


def _position_side(pos: Any) -> str:
    return str(_get(pos, "side") or _get(pos, "Side") or "BUY").upper()


def _position_qty(pos: Any) -> int:
    return _safe_int(_get(pos, "qty") or _get(pos, "quantity") or _get(pos, "Qty"), 0)


def _position_entry_price(pos: Any) -> float:
    return safe_float(
        _get(pos, "avg_price")
        or _get(pos, "entry_price")
        or _get(pos, "price")
        or _get(pos, "current_price"),
        0.0,
    )


def _position_entry_time(pos: Any) -> dt.datetime:
    v = _get(pos, "entry_time")
    if isinstance(v, dt.datetime):
        return v
    try:
        if v:
            return dt.datetime.fromisoformat(str(v))
    except Exception:
        pass
    return dt.datetime.now()


def _pnl_rate(pos: Any, price: float) -> float:
    entry_price = _position_entry_price(pos)
    if entry_price <= 0:
        return 0.0

    rate = (price - entry_price) / entry_price
    if _is_sell_side(_position_side(pos)):
        rate = -rate
    return rate


def _heartbeat(msg: str, *args):
    global _last_exit_heartbeat_at
    now = dt.datetime.now()
    if _last_exit_heartbeat_at is None or (now - _last_exit_heartbeat_at).total_seconds() >= _EXIT_HEARTBEAT_EVERY_SEC:
        logger.info(msg, *args)
        _last_exit_heartbeat_at = now


# ============================================================
# 上位足方向判定（3分 / 5分）
# ============================================================

def judge_direction_from_summary(row, side):
    """
    return: ALIGN / AGAINST / FLAT
    """
    if not row:
        return "FLAT"

    close = safe_float(row.get("close_price") or row.get("close"))
    ma25 = safe_float(row.get("ma25"))
    ma75 = safe_float(row.get("ma75"))

    if close > ma25 > ma75:
        trend = "UP"
    elif close < ma25 < ma75:
        trend = "DOWN"
    else:
        return "FLAT"

    if str(side).upper().startswith("BUY") and trend == "UP":
        return "ALIGN"
    if str(side).upper().startswith("SELL") and trend == "DOWN":
        return "ALIGN"

    return "AGAINST"


# ============================================================
# TONOSAMA EXIT 加速係数
# ============================================================

def calc_exit_accel_for_tonosama(pos: Position, price: float, now: dt.datetime) -> float:
    """
    含み益 × 経過時間 × 上位足環境 から EXIT 加速係数を算出
    """

    symbol = _position_symbol(pos)
    side = _position_side(pos)

    # ---------- 上位足 ----------
    summary_by_interval = getattr(global_data, "summary_by_interval", {}) or {}
    s3 = summary_by_interval.get("3min", {}).get(symbol)
    s5 = summary_by_interval.get("5min", {}).get(symbol)

    env3 = judge_direction_from_summary(s3, side)
    env5 = judge_direction_from_summary(s5, side)

    if env3 == env5 == "ALIGN":
        base = 0.8
    elif "AGAINST" in (env3, env5):
        base = 1.8
    else:
        base = 1.0

    # ---------- 含み益 ----------
    pnl_rate = _pnl_rate(pos, price)

    if pnl_rate <= 0:
        pnl_factor = 1.3
    elif pnl_rate < TONO_TAKE_PROFIT * 0.3:
        pnl_factor = 1.0
    else:
        pnl_factor = 0.6

    # ---------- 経過時間 ----------
    entry_time = _position_entry_time(pos)
    hold_sec = (now - entry_time).total_seconds()
    if hold_sec < 30:
        time_factor = 1.0
    elif hold_sec < 90:
        time_factor = 1.1
    else:
        time_factor = 1.4

    accel = base * pnl_factor * time_factor
    accel = max(0.6, min(accel, 2.5))

    return accel


# ============================================================
# 高速 5 秒足（PUSH 依存・安全）
# ============================================================
@njit
def calc_ohlc(prices):
    return prices[0], prices.max(), prices.min(), prices[-1]


def build_5s_bar_fast(symbol):
    if not hasattr(global_data, "get_push_df"):
        return None

    df = global_data.get_push_df()
    if df is None or df.empty:
        return None

    try:
        sym = str(symbol)
        if "symbol" not in df.columns:
            return None
        d = df[df["symbol"].astype(str) == sym]
    except Exception:
        return None

    if d.empty:
        return None

    try:
        d = d.copy()
        time_col = "time" if "time" in d.columns else "datetime" if "datetime" in d.columns else None
        if not time_col:
            return None

        d[time_col] = d[time_col].apply(
            lambda x: x if isinstance(x, dt.datetime)
            else dt.datetime.fromisoformat(str(x))
        )
    except Exception:
        return None

    last_dt = d[time_col].iloc[-1]
    w = d[d[time_col] >= last_dt - dt.timedelta(seconds=5)]
    if w.empty:
        return None

    price_col = "price" if "price" in w.columns else "current_price" if "current_price" in w.columns else "close" if "close" in w.columns else None
    if not price_col:
        return None

    prices = w[price_col].astype(float).to_numpy()
    if len(prices) <= 0:
        return None

    vol = float(w["volume"].sum()) if "volume" in w.columns else 0.0

    o, h, l, c = calc_ohlc(prices)
    return {
        "open": o,
        "high": h,
        "low": l,
        "close": c,
        "volume": vol,
        "dt": last_dt,
    }


# ============================================================
# TONOSAMA EXIT 判定（唯一）
# ============================================================
def check_tonosama_exit(pos: Position, price: float, now: dt.datetime):
    accel = calc_exit_accel_for_tonosama(pos, price, now)

    entry_price = _position_entry_price(pos)
    if entry_price <= 0:
        return None

    pnl_rate = _pnl_rate(pos, price)
    hold_sec = (now - _position_entry_time(pos)).total_seconds()

    # --------------------------------------------
    # 高値更新（属性欠損耐性）
    # --------------------------------------------
    high_since_entry = _get(pos, "high_since_entry")
    if high_since_entry is None:
        _set(pos, "high_since_entry", price)
        high_since_entry = price
    else:
        high_since_entry = max(safe_float(high_since_entry, price), price)
        _set(pos, "high_since_entry", high_since_entry)

    max_profit = (high_since_entry - entry_price) / entry_price
    if _is_sell_side(_position_side(pos)):
        # SELL は安値方向が利益。既存属性名を維持しつつ、pnl_rateベースで扱う。
        max_profit = max(safe_float(_get(pos, "max_profit_rate"), 0.0), pnl_rate)
        _set(pos, "max_profit_rate", max_profit)

    # --------------------------------------------
    # ① 即損切（加速）
    # --------------------------------------------
    if pnl_rate <= TONO_STOP_LOSS * accel:
        return "TONOSAMA_STOP"

    # --------------------------------------------
    # ② 利確後トレーリング（加速）
    # --------------------------------------------
    if max_profit >= TONO_TAKE_PROFIT:
        if pnl_rate <= max_profit - (TONO_TRAIL_GAP * accel):
            return "TONOSAMA_TRAIL"

    # --------------------------------------------
    # ③ 保持時間（短縮）
    # --------------------------------------------
    hold_limit = _get(pos, "hold_limit_sec")
    if hold_limit is None:
        hold_limit = TONO_MAX_HOLD_SEC

    if hold_sec >= safe_float(hold_limit, TONO_MAX_HOLD_SEC) / accel:
        return "TONOSAMA_TIMEOUT"

    return None


# ============================================================
# 通常 EXIT 判定
# ============================================================
def check_normal_exit(pos: Any, price: float, now: dt.datetime):
    entry_price = _position_entry_price(pos)
    if entry_price <= 0 or price <= 0:
        return None

    pnl_rate = _pnl_rate(pos, price)
    hold_sec = (now - _position_entry_time(pos)).total_seconds()

    symbol = _position_symbol(pos)
    side = _position_side(pos)

    # 損切り
    if pnl_rate <= NORMAL_STOP_LOSS:
        return "NORMAL_STOP"

    # 利確
    if pnl_rate >= NORMAL_TAKE_PROFIT:
        return "NORMAL_TAKE_PROFIT"

    # トレーリング
    max_profit_rate = safe_float(_get(pos, "max_profit_rate"), 0.0)
    max_profit_rate = max(max_profit_rate, pnl_rate)
    _set(pos, "max_profit_rate", max_profit_rate)

    if max_profit_rate >= NORMAL_TRAIL_START and pnl_rate <= max_profit_rate - NORMAL_TRAIL_GAP:
        return "NORMAL_TRAIL"

    # 時間切れ
    if hold_sec >= NORMAL_MAX_HOLD_SEC:
        return "NORMAL_TIMEOUT"

    logger.debug(
        "[EXIT HOLD] symbol=%s side=%s price=%s entry=%s pnl_rate=%.4f hold_sec=%.1f max_profit=%.4f",
        symbol,
        side,
        price,
        entry_price,
        pnl_rate,
        hold_sec,
        max_profit_rate,
    )

    return None


# ============================================================
# EXIT 対象収集
# ============================================================
def _load_db_open_positions(sp) -> list[Any]:
    try:
        return list(sp.query(Position).filter_by(status="OPEN").all())
    except Exception:
        logger.exception("[EXIT] DB open positions load failed")
        return []


def _load_gc_open_positions() -> list[Any]:
    try:
        snap = {}
        try:
            from core.global_context.context import global_context as GC
            if hasattr(GC.positions, "snapshot_open"):
                snap = GC.positions.snapshot_open()
            elif hasattr(GC.positions, "snapshot_dict"):
                snap = GC.positions.snapshot_dict()
        except Exception:
            snap = getattr(global_data, "open_positions", {}) or {}

        if isinstance(snap, dict):
            return [dict(v) for v in snap.values() if isinstance(v, dict)]
        if isinstance(snap, list):
            return snap
        return []

    except Exception:
        logger.exception("[EXIT] GC open positions load failed")
        return []


def _merge_positions(db_positions: list[Any], gc_positions: list[Any]) -> list[Any]:
    out: list[Any] = []
    seen: set[str] = set()

    for pos in db_positions + gc_positions:
        sym = _position_symbol(pos)
        if not sym:
            continue
        key = f"{sym}:{_position_side(pos)}"
        if key in seen:
            continue
        seen.add(key)
        out.append(pos)

    return out


def _remove_open_position(symbol: str) -> None:
    try:
        from core.global_context.context import global_context as GC
        if hasattr(GC.positions, "remove"):
            GC.positions.remove(str(symbol))
    except Exception:
        pass

    try:
        op = getattr(global_data, "open_positions", None)
        if isinstance(op, dict):
            op.pop(str(symbol), None)
    except Exception:
        pass


# ============================================================
# EXIT 実行（共通）
# ============================================================
def execute_exit(pos: Position, exit_price: float, reason: str):
    symbol = _position_symbol(pos)
    side = _position_side(pos)
    qty = _position_qty(pos)
    avg_price = _position_entry_price(pos)

    logger.warning("🔥 EXIT %s side=%s qty=%s price=%s reason=%s", symbol, side, qty, exit_price, reason)

    api = process_exit(_as_position_object(pos), exit_price, reason)
    if not api or not api.get("order_id"):
        logger.error("❌ EXIT 注文失敗 symbol=%s reason=%s api=%s", symbol, reason, api)
        return False

    exec_price = safe_float(api.get("exec_price", exit_price), exit_price)

    pnl = (
        (exec_price - avg_price) * qty
        if _is_buy_side(side)
        else (avg_price - exec_price) * qty
    )

    # DB ORM position の場合のみ DB を更新する。
    if not _is_dict_position(pos):
        sp = Session_position()
        try:
            try:
                db_pos = sp.merge(pos)
            except Exception:
                db_pos = pos

            db_pos.status = "CLOSED"
            db_pos.exit_price = exec_price
            db_pos.exit_time = dt.datetime.now()

            try:
                sp.add(
                    TradeHistory(
                        symbol=symbol,
                        side=side,
                        action="EXIT",
                        qty=qty,
                        price=exec_price,
                        pnl=pnl,
                        realized_pnl=pnl,
                        order_id=api["order_id"],
                        position_id=getattr(db_pos, "id", None),
                        trade_time=dt.datetime.now(),
                        reason=reason,
                    )
                )
            except Exception:
                logger.exception("[EXIT] TradeHistory add failed symbol=%s", symbol)

            sp.commit()

            # TONOSAMA 学習ログ（安全）
            if (
                log_tonosama_trade
                and getattr(db_pos, "entry_source", None) == "ranking"
                and getattr(db_pos, "entry_mode", None) == "TONOSAMA"
            ):
                log_tonosama_trade(db_pos, exec_price, reason)

        except Exception:
            sp.rollback()
            logger.exception("[EXIT] DB close update failed symbol=%s", symbol)
        finally:
            sp.close()

    _remove_open_position(symbol)

    logger.warning(
        "✅ EXIT_ORDER_SENT symbol=%s side=%s qty=%s exec_price=%s pnl=%s reason=%s order_id=%s",
        symbol,
        side,
        qty,
        exec_price,
        pnl,
        reason,
        api.get("order_id"),
    )
    return True


# ============================================================
# EXIT パイプライン（唯一の入口）
# ============================================================
def run_exit_pipeline():
    sp = Session_position()
    try:
        db_positions = _load_db_open_positions(sp)
        gc_positions = _load_gc_open_positions()
        positions = _merge_positions(db_positions, gc_positions)

        _heartbeat(
            "[EXIT PIPELINE HEARTBEAT] db_positions=%s gc_positions=%s merged=%s",
            len(db_positions),
            len(gc_positions),
            len(positions),
        )

        if not positions:
            return

        now = dt.datetime.now()

        for pos in positions:
            symbol = _position_symbol(pos)
            if not symbol:
                continue

            bar = build_5s_bar_fast(symbol)
            if not bar:
                logger.debug("[EXIT SKIP] no 5s bar symbol=%s", symbol)
                continue

            price = safe_float(bar.get("close"), 0.0)
            if price <= 0:
                logger.debug("[EXIT SKIP] invalid price symbol=%s bar=%s", symbol, bar)
                continue

            entry_source = str(_get(pos, "entry_source") or _get(pos, "source") or "").lower()
            entry_mode = str(_get(pos, "entry_mode") or _get(pos, "entry_type") or "").upper()

            # --------------------------------------------------
            # TONOSAMA 専用 EXIT
            # --------------------------------------------------
            if entry_source == "ranking" and entry_mode == "TONOSAMA":
                reason = check_tonosama_exit(pos, price, now)
                if reason:
                    execute_exit(pos, price, reason)
                continue

            # --------------------------------------------------
            # 通常 EXIT
            # --------------------------------------------------
            reason = check_normal_exit(pos, price, now)
            if reason:
                execute_exit(pos, price, reason)
                continue

        try:
            sp.commit()
        except Exception:
            sp.rollback()
            logger.exception("[EXIT] session commit failed")

    except Exception as e:
        logger.error("❌ EXIT pipeline error %s", e, exc_info=True)
    finally:
        sp.close()
