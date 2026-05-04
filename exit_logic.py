import numpy as np
import pandas as pd

def should_exit_position(
    symbol,
    df_summary,
    entry_price,
    highest_price_since_entry=None,
    side="BUY_CREDIT",
    LOSS_CUT_RATIO=-0.03,          # -3%
    PROFIT_TAKE_RATIO=0.05,        # +5%
    TRAILING_STOP_RATIO=0.02,      # ±2%
    VOLUME_DROP_RATIO=0.5          # 半分以下
):
    """
    EXIT判定ロジック（BUY_CREDIT / SELL_CREDIT両対応）
    - 含み損 / 利確 / トレーリング / RSI反落 / ボリンジャーバンド / 出来高 等を総合判定
    """

    if df_summary is None or df_summary.empty or entry_price is None:
        return False, ["データ不足"]

    # ======================================================
    # 🔧 DataFrame安全コピー & 日付型統一
    # ======================================================
    df_summary = df_summary.copy()
    if "date" in df_summary.columns:
        df_summary.loc[:, "date"] = pd.to_datetime(df_summary["date"], errors="coerce")

    # ======================================================
    # 🔍 該当銘柄抽出
    # ======================================================
    df_symbol = df_summary[df_summary["symbol"] == symbol].sort_values(by="date", ascending=True)
    if df_symbol.empty:
        return False, ["該当銘柄データなし"]

    latest = df_symbol.iloc[-1]
    prev = df_symbol.iloc[-2] if len(df_symbol) >= 2 else None

    reasons = []
    is_short = side == "SELL_CREDIT"

    # ======================================================
    # 💰 価格安全取得
    # ======================================================
    current_price = latest.get("close_price", np.nan)
    if pd.isna(current_price) or entry_price <= 0:
        return False, ["価格データ不正"]

    current_price = float(current_price)

    # ======================================================
    # ① 即損切り
    # ======================================================
    pnl_ratio = (current_price - entry_price) / entry_price
    if is_short:
        pnl_ratio *= -1
    if pnl_ratio <= LOSS_CUT_RATIO:
        reasons.append(f"損切り {pnl_ratio*100:.2f}%")

    # ======================================================
    # ② 利確 (+5%)
    # ======================================================
    if pnl_ratio >= PROFIT_TAKE_RATIO:
        reasons.append(f"利確 {pnl_ratio*100:.2f}%")

    # ======================================================
    # ③ トレーリング反落（高値/安値から±2%）
    # ======================================================
    if highest_price_since_entry:
        if not is_short and current_price <= highest_price_since_entry * (1 - TRAILING_STOP_RATIO):
            reasons.append(f"トレーリング反落（最高値-{TRAILING_STOP_RATIO*100:.1f}%）")
        elif is_short and current_price >= highest_price_since_entry * (1 + TRAILING_STOP_RATIO):
            reasons.append(f"トレーリング反発（最安値+{TRAILING_STOP_RATIO*100:.1f}%）")

    # ======================================================
    # ④ RSI過熱反落
    # ======================================================
    if prev is not None and "rsi" in df_symbol.columns:
        prev_rsi = prev.get("rsi", np.nan)
        curr_rsi = latest.get("rsi", np.nan)
        if pd.notnull(prev_rsi) and pd.notnull(curr_rsi):
            if not is_short and prev_rsi > 80 and curr_rsi < prev_rsi:
                reasons.append("RSI過熱反落")
            if is_short and prev_rsi < 20 and curr_rsi > prev_rsi:
                reasons.append("RSI売られすぎ反発")

    # ======================================================
    # ⑤ ボリンジャーバンド反落
    # ======================================================
    if prev is not None and all(k in latest for k in ["bb_upper", "bb_lower", "close_price"]):
        prev_close, prev_upper, prev_lower = prev["close_price"], prev["bb_upper"], prev["bb_lower"]
        curr_close, curr_upper, curr_lower = latest["close_price"], latest["bb_upper"], latest["bb_lower"]

        if pd.notnull(prev_upper) and pd.notnull(curr_upper):
            if not is_short and prev_close > prev_upper and curr_close < curr_upper:
                reasons.append("ボリンジャーバンド+2σ反落")
            if is_short and prev_close < prev_lower and curr_close > curr_lower:
                reasons.append("ボリンジャーバンド-2σ反発")

    # ======================================================
    # ⑥ 出来高急減（直近3本平均の半分以下）
    # ======================================================
    if "volume" in df_symbol.columns and len(df_symbol) >= 4:
        recent_volumes = df_symbol["volume"].iloc[-4:-1]
        if recent_volumes.notna().any():
            vol_mean = recent_volumes.mean()
            if pd.notnull(latest["volume"]) and latest["volume"] < vol_mean * VOLUME_DROP_RATIO:
                reasons.append("出来高急減（平均の半分以下）")

    # ======================================================
    # ⑦ ヒゲパターン（上ヒゲ陰線 / 下ヒゲ陽線）
    # ======================================================
    if all(k in latest for k in ["high_price", "low_price", "open_price", "close_price"]):
        high, low, open_p, close_p = latest["high_price"], latest["low_price"], latest["open_price"], latest["close_price"]

        if not is_short:
            upper_wick = high - max(open_p, close_p)
            if upper_wick > close_p * 0.02 and close_p < open_p:
                reasons.append("上ヒゲ陰線（高値圏）")
        else:
            lower_wick = min(open_p, close_p) - low
            if lower_wick > close_p * 0.02 and close_p > open_p:
                reasons.append("下ヒゲ陽線（安値圏）")

    # ======================================================
    # ⑧ 5MA割れ・5MA戻し
    # ======================================================
    ma5 = latest.get("ma5", np.nan)
    close_price = latest.get("close_price", np.nan)
    if pd.notnull(ma5) and pd.notnull(close_price):
        if not is_short and close_price < ma5:
            reasons.append("5MA下抜け")
        if is_short and close_price > ma5:
            reasons.append("5MA上抜け")

    # ======================================================
    # 🚫 EXIT条件なし → False
    # ======================================================
    return (len(reasons) > 0), reasons
