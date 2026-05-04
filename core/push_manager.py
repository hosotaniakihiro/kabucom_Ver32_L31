# ============================================================
# core/push_manager.py
# Ver19-FINAL-STREAMDB-ONLY-ARCH
# ------------------------------------------------------------
# ✔ WebSocket PUSH正規化
# ✔ append_push_tick → push_buffer
# ✔ flush → push_df
# ✔ ✅ 新StreamDBWriterのみ使用（旧websocket版完全排除）
# ✔ ranking ENTRY（既存）
# ✔ 5秒 tosama（加速検出）
# ✔ push_dfは軽量維持（展開はWriter側）
# ============================================================

import threading
import logging
import time
import datetime as dt
import pandas as pd

from config.paths import get_path

from websocket_handlers.dataframe_manager import (
    flush_push_buffer,
    append_push_tick,
)

# ❌ 旧版削除
# from websocket_handlers.push_db_writer import start_push_db_writer

# ✅ 新63列対応Writer
from trading.push.push_db_writer import start_stream_db_writer

# --- ranking ---
from trading.ranking.entry_executor_ranking import try_entry_from_push

# --- tosama 5秒 ---
from trading.tosama.tosama_buffer import update_5sec
from trading.tosama.tosama_detector import evaluate_tosama
from database.crud.crud_tosama_5sec import insert_tosama_5sec

from global_state import global_data

logger = logging.getLogger(__name__)


# ============================================================
# 🔧 PUSH データ正規化
# ============================================================
def normalize_push_json(d):
    """
    WebSocket PUSH → 内部共通形式（軽量版）
    """
    try:
        return {
            "symbol": str(d.get("Symbol")),
            "symbolname": d.get("SymbolName"),
            "price": d.get("CurrentPrice"),
            "volume": d.get("TradingVolume"),
            "vwap": d.get("VWAP"),
            "time": pd.to_datetime(d.get("CurrentPriceTime")).tz_localize(None),
            "bid_price": d.get("BidPrice"),
            "ask_price": d.get("AskPrice"),
            "raw": d,   # 🔥 Writer側で完全展開するため保持
        }
    except Exception:
        return None


# ============================================================
# 🔥 WebSocket → PUSH受信 の唯一の入口
# ============================================================
def on_push_message(content, now):
    try:
        normalized = normalize_push_json(content)
        if not normalized:
            return

        # ① push_bufferへ
        append_push_tick(normalized, now)

        # ② ranking ENTRY
        try_entry_from_push(normalized)

        # ③ tosama 5秒処理
        symbol = normalized["symbol"]
        price = normalized["price"]
        volume = normalized["volume"]
        symbolname = normalized.get("symbolname") or ""

        if price is not None:
            row, rows = update_5sec(symbol, price, volume)

            try:
                insert_tosama_5sec(row)
            except Exception:
                logger.debug("[TOSAMA] DB insert skipped", exc_info=True)

            evaluate_tosama(symbol, symbolname, rows)

    except Exception as e:
        logger.error(f"on_push_message error: {e}", exc_info=True)


# ============================================================
# 🔄 push_buffer → push_df 反映ループ（軽量）
# ============================================================
def _push_flush_loop():
    logger.info("push_flush_loop started (interval=0.2s)")

    while True:
        try:
            flush_push_buffer()
        except Exception as e:
            logger.error(f"flush_push_buffer error: {e}", exc_info=True)

        time.sleep(0.2)


# ============================================================
# 🗄 新 StreamDBWriter 起動（63列完全保存）
# ============================================================
def start_push_system():
    """
    PUSH受信機構：
      ① push_buffer flush loop
      ② ✅ 新StreamDBWriter起動（旧版完全排除）
    """

    # ---- flush loop ----
    threading.Thread(
        target=_push_flush_loop,
        daemon=True
    ).start()

    # ---- 新DB writer ----
    push_dir = get_path("raw_push")
    push_dir.mkdir(parents=True, exist_ok=True)

    logger.info("🚀 Starting StreamDBWriter (63 columns FULL PUSH)")

    # 🔥 新Writer（内部でパス解決）
    start_stream_db_writer(interval_sec=1)

    logger.info("✅ push_system started (StreamDBWriter active)")