# ============================================================
# File   : trading/push/push_dispatcher.py
# Version: Ver1.2-PRODUCTION-PUSH-DISPATCHER-5SEC-BAR
# ------------------------------------------------------------
# ✔ push_stream処理分離
# ✔ ring_buffer
# ✔ push_state
# ✔ push_df
# ✔ incremental 1m engine
# ✔ stream_writer
# ✔ AI bridge
# ✔ ATS watchdog
# ✔ ATS liquidity monitor
# ✔ orderflow shock detector
# ✔ 5秒足生成をEXIT用に接続
# ✔ 例外完全防御
# ✔ HFT軽量設計
# ============================================================

from __future__ import annotations

import logging

import pandas as pd

from utils.alerts_util import send_discord_message
from trading.push.push_ring_buffer import push_ring_buffer
from trading.push.push_db_writer import stream_writer
from trading.ai.orderflow_detector import detect_orderflow_shock

from global_state import global_data

from trading.aggregation.incremental_1m_engine import get_incremental_1m_engine

from ats.push_watchdog import update_push_timestamp
from ats.ats_liquidity_monitor import record_symbol_update

from trading.ai.kabu_ai_integration import get_kabu_ai_bridge
from trading.push.push_event_engine import process_push_event

logger = logging.getLogger(__name__)

_ai = get_kabu_ai_bridge()

MAX_PUSH_DF_ROWS = 2000


# ============================================================
# 5秒足生成
# ============================================================

def _dispatch_5sec_bar(symbol: str, row: dict) -> None:
    """
    PUSH row から5秒足を生成する。

    重要:
      - 生成結果は GC.monitor.set_five_sec_bar() に保存される
      - ENTRY / SUMMARY / RANKING には使わない
      - trading/exit 配下の全EXITが利用する
    """
    try:
        if not isinstance(row, dict):
            return

        sym = symbol or row.get("symbol") or row.get("Symbol") or row.get("code")
        if not sym:
            return

        from trading.monitor.five_sec_bar_builder import update_five_sec_bar_from_tick

        update_five_sec_bar_from_tick(
            symbol=str(sym),
            tick=row,
        )

    except Exception:
        logger.exception("[push_dispatcher] 5sec bar update failed symbol=%s", symbol)


# ============================================================
# push_df append
# ============================================================

def _append_to_push_df(row: dict):
    try:
        df_current = global_data.get_push_df()

        df_new = pd.DataFrame([row])

        if df_current is None or df_current.empty:
            df_concat = df_new
        else:
            df_concat = pd.concat(
                [df_current, df_new],
                ignore_index=True
            )

        if len(df_concat) > MAX_PUSH_DF_ROWS:
            df_concat = (
                df_concat
                .tail(MAX_PUSH_DF_ROWS)
                .reset_index(drop=True)
            )

        global_data.set_push_df(df_concat)

        # ----------------------------------------------------
        # orderflow shock detector
        # ----------------------------------------------------
        try:
            shock = detect_orderflow_shock(df_concat)

            if shock is not None and len(shock) > 0:
                for _, r in shock.iterrows():
                    symbol = str(r.get("symbol", ""))
                    symbolname = str(r.get("symbolname", ""))

                    if not symbolname:
                        symbolname = symbol

                    embed = {
                        "title": "🚨 ORDERFLOW SHOCK",
                        "color": 15158332,
                        "fields": [
                            {
                                "name": "Symbol",
                                "value": f"{symbol} {symbolname}",
                                "inline": False
                            },
                            {
                                "name": "Price",
                                "value": str(r.get("price", "")),
                                "inline": True
                            },
                            {
                                "name": "Volume Spike",
                                "value": str(round(r.get("volume_ratio", 0), 2)),
                                "inline": True
                            },
                            {
                                "name": "Spread",
                                "value": str(round(r.get("spread", 0), 3)),
                                "inline": True
                            },
                        ],
                    }

                    send_discord_message(embeds=[embed])

        except Exception:
            logger.exception(
                "[push_dispatcher] orderflow shock detection failed"
            )

    except Exception:
        logger.exception("[push_dispatcher] push_df append failed")


# ============================================================
# AI ingest
# ============================================================

def _dispatch_ai(symbol: str, row: dict):
    try:
        _ai.ingest_tick(
            symbol,
            {
                "price": row.get("price") or row.get("current_price") or row.get("close"),
                "volume": row.get("volume") or row.get("trading_volume"),
                "bid": row.get("best_bid") or row.get("bid_price"),
                "ask": row.get("best_ask") or row.get("ask_price"),
                "spread": row.get("spread"),
                "vwap": row.get("vwap"),
                "timestamp": row.get("datetime"),
            }
        )

    except Exception:
        logger.exception("[push_dispatcher] AI ingest failed")


# ============================================================
# incremental engine
# ============================================================

def _dispatch_incremental(row: dict):
    try:
        engine = get_incremental_1m_engine()
        engine.process_row(row)

    except Exception:
        logger.exception("[push_dispatcher] incremental engine failed")


# ============================================================
# DB writer
# ============================================================

def _dispatch_db(row: dict):
    try:
        stream_writer.add_push_row(row)

    except Exception:
        logger.exception("[push_dispatcher] stream_writer failed")


# ============================================================
# ring buffer
# ============================================================

def _dispatch_ring_buffer(row: dict):
    try:
        push_ring_buffer.append(row)

    except Exception:
        logger.exception("[push_dispatcher] ring_buffer append failed")


# ============================================================
# push state
# ============================================================

def _dispatch_push_state(symbol: str, row: dict):
    try:
        global_data.push.set_tick(symbol, row)

    except Exception:
        logger.exception("[push_dispatcher] push_state update failed")


# ============================================================
# watchdog
# ============================================================

def _dispatch_watchdog(symbol: str):
    try:
        update_push_timestamp(symbol)

    except Exception:
        logger.exception("[push_dispatcher] watchdog update failed")


# ============================================================
# liquidity monitor
# ============================================================

def _dispatch_liquidity(symbol: str):
    try:
        record_symbol_update(symbol)

    except Exception:
        logger.exception("[push_dispatcher] liquidity monitor failed")


# ============================================================
# MAIN DISPATCH
# ============================================================

def dispatch_push_row(symbol: str, row: dict):
    try:
        # ----------------------------------------------------
        # 5秒足生成
        # EXIT用なので、PUSH受信直後に更新しておく。
        # ----------------------------------------------------
        _dispatch_5sec_bar(symbol, row)

        # ----------------------------------------------------
        # ATS監視
        # ----------------------------------------------------
        _dispatch_liquidity(symbol)
        _dispatch_watchdog(symbol)

        # ----------------------------------------------------
        # ring buffer
        # ----------------------------------------------------
        _dispatch_ring_buffer(row)

        # ----------------------------------------------------
        # push state
        # ----------------------------------------------------
        _dispatch_push_state(symbol, row)

        # ----------------------------------------------------
        # incremental engine（最優先）
        # ----------------------------------------------------
        _dispatch_incremental(row)

        # ----------------------------------------------------
        # DB
        # ----------------------------------------------------
        _dispatch_db(row)

        # ----------------------------------------------------
        # AI
        # ----------------------------------------------------
        _dispatch_ai(symbol, row)

        # ----------------------------------------------------
        # event engine
        # ----------------------------------------------------
        process_push_event(symbol, row)

        # ----------------------------------------------------
        # push_df（最後）
        # ----------------------------------------------------
        _append_to_push_df(row)

    except Exception:
        logger.exception("[push_dispatcher] dispatch_push_row failed")


__all__ = [
    "dispatch_push_row",
]