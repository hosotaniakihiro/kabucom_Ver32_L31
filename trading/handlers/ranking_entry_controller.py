# ============================================================
# ranking_entry_controller.py（Ver6.1）
# ------------------------------------------------------------
# ・ランキング由来 ENTRY（最強点火ロジック統合版）
# ・ランキングDBの trading_volume / turnover を利用
# ・ランキング由来は tick 非依存（成り行き）
# ============================================================

import logging
import pandas as pd
import datetime as dt
from global_state import global_data

from trading.entry.ranking_score import (
    calc_buy_ranking_score,
    calc_sell_ranking_score
)

from trading.handlers.entry_handler import (
    place_entry_buy,
    place_entry_sell
)

from trading.summary.position_filter import can_entry_symbol
from entry_restriction import is_trade_restricted_pushdf


logger = logging.getLogger("ranking_entry")

# ============================================================
# ▼ 理由整形
# ============================================================
def normalize_reasons(reason):
    if reason is None:
        return ""
    if isinstance(reason, (list, tuple)):
        cleaned = [str(x).strip() for x in reason if x]
        return ", ".join(cleaned)
    return str(reason)


# ============================================================
# ▼ AI学習イベント保存
# ============================================================
def save_entry_event(symbol, side, score, features):
    import sqlite3
    conn = sqlite3.connect("ai_entry_events.db")

    df = pd.DataFrame([{
        **features,
        "symbol": symbol,
        "side": side,
        "score": score,
        "datetime": dt.datetime.now()
    }])

    df.to_sql("entry_events", conn, if_exists="append", index=False)
    conn.close()


# ============================================================
# ▼ 三段点火（BUY）
# ============================================================
three_stage_buy = {}

def detect_three_stage_buy(symbol, price, vol_1m, ret_1m, avg3m):
    st = three_stage_buy.setdefault(
        symbol, {"state": 0, "first_high": 0, "first_vol": 0}
    )

    if st["state"] == 0:
        if vol_1m >= avg3m * 3 and ret_1m >= 0.3:
            st["state"] = 1
            st["first_high"] = price
            st["first_vol"] = vol_1m
            return 1

    if st["state"] == 1:
        if price <= st["first_high"] * 0.995:
            st["state"] = 2
            return 2

    if st["state"] == 2:
        if price >= st["first_high"] and vol_1m >= st["first_vol"] * 0.7:
            st["state"] = 3
            return 3

    return st["state"]


# ============================================================
# ▼ 逆三段点火（SELL）
# ============================================================
three_stage_sell = {}

def detect_three_stage_sell(symbol, price, vol_1m, ret_1m, avg3m):
    st = three_stage_sell.setdefault(
        symbol, {"state": 0, "first_low": 0, "first_vol": 0}
    )

    if st["state"] == 0:
        if vol_1m >= avg3m * 3 and ret_1m <= -0.3:
            st["state"] = 1
            st["first_low"] = price
            st["first_vol"] = vol_1m
            return 1

    if st["state"] == 1:
        if price >= st["first_low"] * 1.005:
            st["state"] = 2
            return 2

    if st["state"] == 2:
        if price <= st["first_low"]:
            st["state"] = 3
            return 3

    return st["state"]


# ============================================================
# ▼ 5秒足解析
# ============================================================
def analyze_5s(symbol):
    bar = global_data.latest_5s_bar.get(symbol)
    if bar is None:
        return {"fast_ret": 0, "break": False}

    open_ = bar["open"]
    close = bar["close"]

    fast_ret = (close - open_) / open_ * 100 if open_ else 0

    hist = global_data.latest_5s_history.get(symbol, [])
    if len(hist) >= 6:
        past_high = max(b["high"] for b in hist[-6:])
        is_break = close > past_high
    else:
        is_break = False

    return {"fast_ret": fast_ret, "break": is_break}


# ============================================================
# ▼ ランキング ENTRY
# ============================================================
def run_ranking_entry():

    df = global_data.get_latest_ranking_df()
    if df is None or df.empty:
        return

    df_push = global_data.get_push_df()

    for idx, row in df.iterrows():

        sym = str(row["symbol"])
        name = global_data.symbol_name_map.get(sym, "")
        price = row.get("price", None)

        vol_1m, val_1m = get_1min_volume_from_ranking(df, idx)
        avg3m = get_3min_avg_volume(df, idx)
        ret_1m = row.get("return_1m", 0)
        res_5s = analyze_5s(sym)

        # ===============================
        # BUY
        # ===============================
        score_buy, reasons_buy = calc_buy_ranking_score(sym)

        if score_buy >= 3:
            ng, _ = is_trade_restricted_pushdf(sym, "BUY", df_push)
            if not ng and can_entry_symbol(sym, "BUY"):
                reason_text = normalize_reasons(reasons_buy)

                place_entry_buy(
                    sym,
                    name,
                    None,
                    reason_text,
                    allow_no_tick=True   # ★ランキング由来：tick不要
                )

                logger.info(f"🔥 RANKING BUY ENTRY {sym} → {reason_text}")

        # ===============================
        # SELL
        # ===============================
        score_sell, reasons_sell = calc_sell_ranking_score(sym)

        if score_sell >= 3:
            ng, _ = is_trade_restricted_pushdf(sym, "SELL", df_push)
            if not ng and can_entry_symbol(sym, "SELL"):
                reason_text = normalize_reasons(reasons_sell)

                place_entry_sell(
                    sym,
                    name,
                    None,
                    reason_text,
                    allow_no_tick=True   # ★ランキング由来：tick不要
                )

                logger.info(f"🔥 RANKING SELL ENTRY {sym} → {reason_text}")
