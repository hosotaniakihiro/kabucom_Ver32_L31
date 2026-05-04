# ============================================================
# ranking_entry.py（Ver24-RANKING-INDEPENDENT / FINAL-REV6）
# ------------------------------------------------------------
# ✔ ランキングENTRY専用（テクニカルとは独立）
# ✔ 買い対象・売り対象の事前定義なし
# ✔ スコア優勢方向を1つだけ採用
# ✔ 同点は見送り（両建て構造的に不可）
# ✔ 成行禁止 → PUSH直前価格の指値
# ============================================================

import logging
import pandas as pd

from global_state import global_data
from trading.summary.position_filter import can_entry_symbol
from trading.handlers.entry_handler import (
    place_entry_buy,
    place_entry_sell,
)
from utils_common import safe_float

logger = logging.getLogger("ranking_entry")

# ============================================================
# パラメータ
# ============================================================
MIN_ENTRY_PRICE = 150.0

MIN_VOL_60S = 5_000
MIN_AMOUNT_5M = 10_000_000
MIN_TICK_30S = 15


# ============================================================
# datetime 正規化
# ============================================================
def _normalize_datetime(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df

    d = df.copy()

    if "datetime" not in d.columns:
        if "time" in d.columns:
            d["datetime"] = d["time"]
        else:
            return pd.DataFrame()

    d["datetime"] = pd.to_datetime(d["datetime"], errors="coerce")
    d = d.dropna(subset=["datetime"])
    if d.empty:
        return d

    try:
        if d["datetime"].dt.tz is not None:
            d["datetime"] = d["datetime"].dt.tz_localize(None)
    except Exception:
        d["datetime"] = d["datetime"].dt.tz_localize(None)

    return d


# ============================================================
# ランキングスコア計算
# ============================================================
def calc_ranking_factors(symbol: str, df_push: pd.DataFrame):

    d = df_push[df_push["symbol"] == symbol]
    d = _normalize_datetime(d)
    if len(d) < 10:
        return 0, 0, [], []

    last = d.iloc[-1]
    price = safe_float(last.get("price"))
    now = last["datetime"]

    d60 = d[d["datetime"] >= now - pd.Timedelta(seconds=60)]
    d5m = d[d["datetime"] >= now - pd.Timedelta(minutes=5)]

    # --- 出来高 ---
    v60 = d60["volume"].sum()
    vavg = d5m["volume"].mean() if not d5m.empty else 0
    score_vol = (
        3 if vavg > 0 and v60 / vavg >= 5 else
        2 if vavg > 0 and v60 / vavg >= 3 else
        1 if vavg > 0 and v60 / vavg >= 2 else 0
    )

    # --- tick ---
    tick_30 = len(d[d["datetime"] >= now - pd.Timedelta(seconds=30)])
    score_tick = 2 if tick_30 >= 20 else 1 if tick_30 >= 10 else 0

    # --- 上下率 ---
    low5 = d5m["price"].min()
    high5 = d5m["price"].max()

    pct_up = (price - low5) / low5 if low5 > 0 else 0
    pct_down = (high5 - price) / high5 if high5 > 0 else 0

    score_up = 3 if pct_up >= 0.04 else 2 if pct_up >= 0.025 else 1 if pct_up >= 0.015 else 0
    score_down = 3 if pct_down >= 0.04 else 2 if pct_down >= 0.025 else 1 if pct_down >= 0.015 else 0

    # --- ボラ ---
    vola = d60["price"].max() - d60["price"].min()
    score_vola = 2 if vola >= price * 0.01 else 1 if vola >= price * 0.005 else 0

    buy_score = score_vol + score_tick + score_up + score_vola
    sell_score = score_vol + score_tick + score_down + score_vola

    r_buy, r_sell = [], []

    if score_vol:
        r_buy.append(f"出来高急増 +{score_vol}")
        r_sell.append(f"出来高急増 +{score_vol}")
    if score_tick:
        r_buy.append(f"tick急増 +{score_tick}")
        r_sell.append(f"tick急増 +{score_tick}")
    if score_up:
        r_buy.append(f"値上がり率 +{score_up}")
    if score_down:
        r_sell.append(f"値下がり率 +{score_down}")
    if score_vola:
        r_buy.append(f"ボラ急増 +{score_vola}")
        r_sell.append(f"ボラ急増 +{score_vola}")

    return buy_score, sell_score, r_buy, r_sell


# ============================================================
# ランキング ENTRY
# ============================================================
def run_ranking_entry():

    df_push = global_data.get_push_df()
    if df_push is None or df_push.empty:
        return

    for sym in df_push["symbol"].unique():

        d = df_push[df_push["symbol"] == sym]
        d = _normalize_datetime(d)
        if d.empty:
            continue

        last = d.iloc[-1]
        price = safe_float(last.get("price"))
        if price is None or price < MIN_ENTRY_PRICE:
            continue

        now = last["datetime"]

        # --- 流動性・絶対量 ---
        d60 = d[d["datetime"] >= now - pd.Timedelta(seconds=60)]
        d5m = d[d["datetime"] >= now - pd.Timedelta(minutes=5)]

        if d60["volume"].sum() < MIN_VOL_60S:
            continue
        if (d5m["price"] * d5m["volume"]).sum() < MIN_AMOUNT_5M:
            continue
        if len(d[d["datetime"] >= now - pd.Timedelta(seconds=30)]) < MIN_TICK_30S:
            continue

        # --- スコア ---
        b, s, r_b, r_s = calc_ranking_factors(sym, df_push)

        if b < 4 and s < 4:
            continue

        # ====================================================
        # ★ 方向は1つだけ決定
        # ====================================================
        if b > s:
            side = "BUY"
            reasons = r_b
        elif s > b:
            side = "SELL"
            reasons = r_s
        else:
            continue  # 同点は見送り

        # --- エントリー可否 ---
        if not can_entry_symbol(sym, side):
            continue

        name = global_data.symbol_name_map.get(sym, "")
        reason = ", ".join(reasons)

        logger.info(
            f"🔥 RANKING {side} LIMIT {sym} @{price} ({reason})"
        )

        # --- 指値（PUSH価格） ---
        if side == "BUY":
            place_entry_buy(sym, name, price, reason)
        else:
            place_entry_sell(sym, name, price, reason)
