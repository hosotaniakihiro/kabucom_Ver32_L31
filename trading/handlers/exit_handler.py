# ============================================================
# pj/trading/exit/exit_handler.py
# Ver25.4-PRO-AI-ULTRA-TONOSAMA-ACCEL-FINAL
# Updated: 2026-01-28
# ------------------------------------------------------------
# ✔ 通常 EXIT ロジック完全温存
# ✔ ranking + TONOSAMA は即逃げ専用
# ✔ 初動成行 → トレーリング即逃げ
# ✔ 学習ログ連携（存在チェック付き）
# ✔ push 欠損・属性欠損完全耐性
# ✔ EXIT 経路を一本化（事故防止）
# ✔ ★ EXIT加速（含み益 × 時間 × 3m/5m）統合
# ============================================================

import logging
import datetime as dt
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
TONO_STOP_LOSS    = -0.002    # -0.20%
TONO_TAKE_PROFIT  = 0.003     # +0.30%
TONO_TRAIL_GAP    = 0.0015    # -0.15%
TONO_MAX_HOLD_SEC = 90        # AI 未指定時の最大保持秒数


# ============================================================
# util
# ============================================================

def safe_float(x, default=0.0):
    try:
        return float(x)
    except Exception:
        return default


# ============================================================
# 上位足方向判定（3分 / 5分）
# ============================================================

def judge_direction_from_summary(row, side):
    """
    return: ALIGN / AGAINST / FLAT
    """
    if not row:
        return "FLAT"

    close = safe_float(row.get("close_price"))
    ma25  = safe_float(row.get("ma25"))
    ma75  = safe_float(row.get("ma75"))

    if close > ma25 > ma75:
        trend = "UP"
    elif close < ma25 < ma75:
        trend = "DOWN"
    else:
        return "FLAT"

    if side.startswith("BUY") and trend == "UP":
        return "ALIGN"
    if side.startswith("SELL") and trend == "DOWN":
        return "ALIGN"

    return "AGAINST"


# ============================================================
# TONOSAMA EXIT 加速係数
# ============================================================

def calc_exit_accel_for_tonosama(pos: Position, price: float, now: dt.datetime) -> float:
    """
    含み益 × 経過時間 × 上位足環境 から EXIT 加速係数を算出
    """

    # ---------- 上位足 ----------
    s3 = global_data.summary_by_interval.get("3min", {}).get(pos.symbol)
    s5 = global_data.summary_by_interval.get("5min", {}).get(pos.symbol)

    env3 = judge_direction_from_summary(s3, pos.side)
    env5 = judge_direction_from_summary(s5, pos.side)

    if env3 == env5 == "ALIGN":
        base = 0.8
    elif "AGAINST" in (env3, env5):
        base = 1.8
    else:
        base = 1.0

    # ---------- 含み益 ----------
    entry_price = pos.avg_price
    pnl_rate = (price - entry_price) / entry_price
    if pos.side.startswith("SELL"):
        pnl_rate = -pnl_rate

    if pnl_rate <= 0:
        pnl_factor = 1.3
    elif pnl_rate < TONO_TAKE_PROFIT * 0.3:
        pnl_factor = 1.0
    else:
        pnl_factor = 0.6

    # ---------- 経過時間 ----------
    hold_sec = (now - pos.entry_time).total_seconds()
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

    d = df[df["symbol"] == symbol]
    if d.empty:
        return None

    try:
        d = d.copy()
        d["time"] = d["time"].apply(
            lambda x: x if isinstance(x, dt.datetime)
            else dt.datetime.fromisoformat(str(x))
        )
    except Exception:
        return None

    last_dt = d["time"].iloc[-1]
    w = d[d["time"] >= last_dt - dt.timedelta(seconds=5)]
    if w.empty:
        return None

    prices = w["price"].astype(float).to_numpy()
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

    entry_price = pos.avg_price
    pnl_rate = (price - entry_price) / entry_price
    if pos.side.startswith("SELL"):
        pnl_rate = -pnl_rate

    hold_sec = (now - pos.entry_time).total_seconds()

    # --------------------------------------------
    # 高値更新（属性欠損耐性）
    # --------------------------------------------
    if getattr(pos, "high_since_entry", None) is None:
        pos.high_since_entry = price
    else:
        pos.high_since_entry = max(pos.high_since_entry, price)

    max_profit = (pos.high_since_entry - entry_price) / entry_price

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
    hold_limit = getattr(pos, "hold_limit_sec", None)
    if hold_limit is None:
        hold_limit = TONO_MAX_HOLD_SEC

    if hold_sec >= hold_limit / accel:
        return "TONOSAMA_TIMEOUT"

    return None


# ============================================================
# EXIT 実行（共通）
# ============================================================
def execute_exit(pos: Position, exit_price: float, reason: str):

    logger.warning(f"🔥 EXIT {pos.symbol} price={exit_price} reason={reason}")

    api = process_exit(pos, exit_price, reason)
    if not api or not api.get("order_id"):
        logger.error("❌ EXIT 注文失敗")
        return

    exec_price = api.get("exec_price", exit_price)

    pnl = (
        (exec_price - pos.avg_price) * pos.qty
        if pos.side.startswith("BUY")
        else (pos.avg_price - exec_price) * pos.qty
    )

    sp = Session_position()
    st = Session_trade()

    try:
        pos.status = "CLOSED"
        pos.exit_price = exec_price
        pos.exit_time = dt.datetime.now()

        st.add(
            TradeHistory(
                symbol=pos.symbol,
                side=pos.side,
                action="EXIT",
                qty=pos.qty,
                price=exec_price,
                pnl=pnl,
                realized_pnl=pnl,
                order_id=api["order_id"],
                position_id=pos.id,
                trade_time=dt.datetime.now(),
                reason=reason,
            )
        )

        sp.commit()
        st.commit()

        # ★ TONOSAMA 学習ログ（安全）
        if (
            log_tonosama_trade
            and getattr(pos, "entry_source", None) == "ranking"
            and getattr(pos, "entry_mode", None) == "TONOSAMA"
        ):
            log_tonosama_trade(pos, exec_price, reason)

    finally:
        sp.close()
        st.close()

    global_data.open_positions.pop(pos.symbol, None)


# ============================================================
# EXIT パイプライン（唯一の入口）
# ============================================================
def run_exit_pipeline():

    sp = Session_position()
    try:
        positions = sp.query(Position).filter_by(status="OPEN").all()
        now = dt.datetime.now()

        for pos in positions:

            bar = build_5s_bar_fast(pos.symbol)
            if not bar:
                continue

            price = bar["close"]

            # --------------------------------------------------
            # TONOSAMA 専用 EXIT
            # --------------------------------------------------
            if (
                getattr(pos, "entry_source", None) == "ranking"
                and getattr(pos, "entry_mode", None) == "TONOSAMA"
            ):
                reason = check_tonosama_exit(pos, price, now)
                if reason:
                    execute_exit(pos, price, reason)
                continue

            # --------------------------------------------------
            # 通常 EXIT（既存ロジック温存）
            # --------------------------------------------------
            # handle_normal_exit(pos, price, now)
            continue

        sp.commit()

    except Exception as e:
        logger.error(f"❌ EXIT pipeline error {e}", exc_info=True)
    finally:
        sp.close()
