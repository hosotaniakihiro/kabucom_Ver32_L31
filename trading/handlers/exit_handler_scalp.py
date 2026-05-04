# ============================================================
# exit_handler_scalp.py
# Ver25-FINAL-SCALP-EXIT-DELEGATED
# ------------------------------------------------------------
# ✔ EXIT 実行は exit_controller に完全委譲
# ✔ reasons(list) → exit_reason(str) に正規化
# ✔ ExitContext は global_data.exit_ctx を使用
# ✔ API / DB / AI / cooldown には一切触れない
# ============================================================

import logging

from global_state import global_data
from trading.handlers.exit_controller import _execute_exit
from trading.exit.exit_context import ExitContext

logger = logging.getLogger("exit_scalp")

# ===== 設定 =====
TP1_TICKS = 5
TP_EXT_TICKS = 8
SL_TICKS = 4
VWAP_EXT_THRESHOLD = 0.0005   # 0.05%


# ============================================================
# 5秒足ベース SCALP EXIT
# ============================================================
def scalp_exit_handler(symbol: str):

    # 最新 tick
    tick = global_data.get_latest_tick(symbol)
    if not tick:
        return

    price = tick.get("price")
    if not price:
        return

    # open_positions は DictGuard（symbol -> dict）
    pos_map = global_data.open_positions
    p0 = pos_map.get(symbol)
    if not isinstance(p0, dict):
        return

    side = p0.get("side")
    avg = p0.get("avg_price")
    entry_time = p0.get("entry_time")
    atr_1min = p0.get("atr_1min")

    if not all([side, avg, entry_time, atr_1min]):
        return

    # ExitContext（正本）
    ctx: ExitContext | None = global_data.exit_ctx.get(symbol)
    if ctx is None:
        ctx = ExitContext(
            symbol=symbol,
            side=side,
            entry_price=avg,
            atr_1min=atr_1min,
            entry_time=entry_time,
        )
        global_data.exit_ctx[symbol] = ctx

    # 5秒足
    bar5 = global_data.five_sec_bars.get(symbol)
    if not bar5:
        return

    # orderflow
    orderflow = global_data.orderflow.get(symbol, {})

    # summary（1分）
    summary_1m = {}
    df1 = global_data.get_all_1min_df()
    if df1 is not None and not df1.empty:
        row = df1[df1["symbol"] == symbol]
        if not row.empty:
            summary_1m = row.iloc[-1].to_dict()

    # =========================================================
    # EXIT 判定
    # =========================================================

    exit_reason: str | None = None

    # ---------------- BUY ----------------
    if side == "BUY":

        if price <= avg - SL_TICKS:
            exit_reason = f"SCALP_SL -{SL_TICKS}tick"

        elif price >= avg + TP1_TICKS:
            exit_reason = f"SCALP_TP1 +{TP1_TICKS}tick"

        elif (
            price >= avg + TP_EXT_TICKS
            and calc_extend_score_buy(symbol, bar5, summary_1m, orderflow) >= 2
        ):
            exit_reason = f"SCALP_TP_EXT +{TP_EXT_TICKS}tick"

        elif bar5["close"] < bar5["open"]:
            exit_reason = "SCALP_5S_REVERSAL"

    # ---------------- SELL ----------------
    else:

        if price >= avg + SL_TICKS:
            exit_reason = f"SCALP_SL +{SL_TICKS}tick"

        elif price <= avg - TP1_TICKS:
            exit_reason = f"SCALP_TP1 -{TP1_TICKS}tick"

        elif (
            price <= avg - TP_EXT_TICKS
            and calc_extend_score_sell(symbol, bar5, summary_1m, orderflow) >= 2
        ):
            exit_reason = f"SCALP_TP_EXT -{TP_EXT_TICKS}tick"

        elif bar5["close"] > bar5["open"]:
            exit_reason = "SCALP_5S_REVERSAL"

    if not exit_reason:
        return

    logger.info("🔥 SCALP EXIT REQUEST %s reason=%s", symbol, exit_reason)

    # =========================================================
    # EXIT 実行（完全委譲）
    # =========================================================
    _execute_exit(
        symbol=symbol,
        exit_price=price,
        pos_list=[p0],
        ctx=ctx,
        exit_reason=exit_reason,
        index_shock=0,
    )


# ============================================================
# BUY TP 拡張スコア
# ============================================================
def calc_extend_score_buy(symbol, bar5, summary_1m, orderflow):

    score = 0
    price = bar5["close"]
    vwap = summary_1m.get("vwap")
    prev_gap = global_data.prev_vwap_gap.get(symbol)

    if bar5["close"] > bar5["open"]:
        score += 1

    if orderflow.get("buy_count_3s", 0) >= 3:
        score += 1

    if vwap:
        gap = (price - vwap) / vwap
        if prev_gap is not None and (gap - prev_gap) >= VWAP_EXT_THRESHOLD:
            score += 1

    if orderflow.get("best_bid_size", 1) >= orderflow.get("best_ask_size", 999999) * 1.6:
        score += 1

    if global_data.five_sec_up_count.get(symbol, 0) >= 2:
        score += 1

    return score


# ============================================================
# SELL TP 拡張スコア
# ============================================================
def calc_extend_score_sell(symbol, bar5, summary_1m, orderflow):

    score = 0
    price = bar5["close"]
    vwap = summary_1m.get("vwap")
    prev_gap = global_data.prev_vwap_gap.get(symbol)

    if bar5["close"] < bar5["open"]:
        score += 1

    if orderflow.get("sell_count_3s", 0) >= 3:
        score += 1

    if vwap:
        gap = (price - vwap) / vwap
        if prev_gap is not None and (prev_gap - gap) >= VWAP_EXT_THRESHOLD:
            score += 1

    if orderflow.get("best_ask_size", 999999) >= orderflow.get("best_bid_size", 1) * 1.6:
        score += 1

    if global_data.five_sec_down_count.get(symbol, 0) >= 2:
        score += 1

    return score


