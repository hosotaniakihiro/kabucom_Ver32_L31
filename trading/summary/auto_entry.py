# ============================================================
# trading/summary/auto_entry.py
# Ver17.3（1分 + 3分 + 5分完全対応ENTRY）
# ============================================================

import logging
import pandas as pd

from trading.handlers.entry_handler import place_entry_order
from utils.alerts_util import send_discord_notify_embed_entry

logger = logging.getLogger(__name__)

MAX_ENTRY_PER_RUN = 5


# ============================================================
# 🔹 自動エントリー（BUY/SELL）
# ============================================================
def auto_entry(df: pd.DataFrame, tf_name: str = ""):
    if df is None or df.empty:
        logger.info(f"[auto_entry] {tf_name}: 対象なし")
        return

    df_buy = df[df["entry_side"] == "BUY_CREDIT"].copy()
    df_sell = df[df["entry_side"] == "SELL_CREDIT"].copy()

    # スコア順
    if "total_score" in df_buy:
        df_buy.sort_values("total_score", ascending=False, inplace=True)
    if "total_score" in df_sell:
        df_sell.sort_values("total_score", ascending=False, inplace=True)

    # BUY
    for _, row in df_buy.head(MAX_ENTRY_PER_RUN).iterrows():
        _process_auto_entry_row(row, "BUY_CREDIT", tf_name)

    # SELL
    for _, row in df_sell.head(MAX_ENTRY_PER_RUN).iterrows():
        _process_auto_entry_row(row, "SELL_CREDIT", tf_name)

    logger.info(
        f"[auto_entry] {tf_name}: 完了 (BUY={len(df_buy[:MAX_ENTRY_PER_RUN])} / "
        f"SELL={len(df_sell[:MAX_ENTRY_PER_RUN])})"
    )


# ============================================================
# 🔹 ENTRY 1銘柄処理
# ============================================================
def _process_auto_entry_row(row, side: str, tf_name: str):
    symbol = str(row.get("symbol"))
    symbolname = row.get("symbolname", "")
    score = row.get("total_score", 0)

    if score < 3:
        logger.info(f"⚪ {symbol}: score={score} < 3 → skip")
        return

    reason = row.get("entry_reason") or f"{tf_name} ENTRY score={score:.1f}"

    logger.info(f"➡️ auto_entry {tf_name}: {symbol} {side} score={score}")

    order_ids = place_entry_order(
        symbol=symbol,
        symbolname=symbolname,
        side=side,
        total_budget=500_000,
        reason=reason,

        # Ver17 ENTRY 必須パラメータ
        trigger_price=row.get("trigger_price"),
        atr=row.get("atr"),
        summary_1=row.get("summary_1"),
        summary_3=row.get("summary_3"),
        summary_5=row.get("summary_5"),
    )

    if order_ids:
        logger.info(f"🟢 ENTRY成功: {symbol} order_ids={order_ids}")
        _notify_discord(row, side, tf_name, score, reason)
    else:
        logger.warning(f"⚠ ENTRY失敗: {symbol}")


# ============================================================
# 🔹 Discord通知
# ============================================================
def _notify_discord(row, side, tf_name, score, reason):
    try:
        send_discord_notify_embed_entry(
            symbol=row.get("symbol"),
            symbolname=row.get("symbolname", ""),
            side=side,
            price=row.get("trigger_price"),
            qty=row.get("volume", 0),
            score=float(score),
            reason=reason,
            timeframe=tf_name,
            ma5=row.get("ma5"),
            ma25=row.get("ma25"),
            ma75=row.get("ma75"),
            vwap=row.get("vwap"),
            rsi=row.get("rsi"),
            macd=row.get("macd"),
            volume=row.get("volume"),
        )
    except Exception as e:
        logger.warning(f"⚠ Discord ENTRY通知失敗: {e}")
