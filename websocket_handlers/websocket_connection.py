# ============================================================
# File   : websocket_connection.py
# Version: Ver28-WS-ATS-FIXED-TARGETS-DF-DB-IMMEDIATE-FLUSH-FINAL
# ------------------------------------------------------------
# ✔ Ver27 の PUSH受信 → DF更新 → DB即flush を維持
# ✔ 登録対象の正本を ATS 最終確定リストに固定
# ✔ ats_register_targets / ats_targets / should_register_symbols / push_symbols を優先順で解決
# ✔ list / tuple / set / dict 混在に安全対応
# ✔ symbol 正規化を追加
# ✔ writer.start() 自動保証
# ✔ 二重start防止
# ✔ reconnect通知維持
# ✔ 過剰処理なし
# ✔ 本番用シンプル版
# ============================================================

from __future__ import annotations

import json
import logging
import websocket
import datetime as dt
import threading
from typing import Any

from global_state import global_data
from websocket_handlers.dataframe_manager import append_push_tick
from websocket_handlers.websocket_reconnect import notify_ws_closed
from trading.push.push_db_writer import stream_writer

logger = logging.getLogger("websocket_connection")

# ============================================================
# Writer start guard
# ============================================================

_writer_started = False
_writer_lock = threading.Lock()


def _ensure_writer_started() -> None:
    global _writer_started

    if _writer_started:
        return

    with _writer_lock:
        if _writer_started:
            return

        try:
            stream_writer.start()
            logger.info("✅ stream_writer started")
            _writer_started = True
        except Exception:
            logger.exception("❌ stream_writer start failed")


# ============================================================
# util
# ============================================================

def _normalize_symbol(x: Any) -> str | None:
    if x is None:
        return None

    try:
        if isinstance(x, dict):
            for key in ("symbol", "Symbol", "code", "ticker", "security_code", "stock_code"):
                if key in x and x.get(key) is not None:
                    return _normalize_symbol(x.get(key))
            return None

        if isinstance(x, (list, tuple)):
            if not x:
                return None
            return _normalize_symbol(x[0])

        s = str(x).strip()
        if not s:
            return None

        if "." in s:
            s = s.split(".", 1)[0].strip()

        digits = "".join(ch for ch in s if ch.isdigit())

        # 4桁通常銘柄 / A付き銘柄にも一応対応
        if len(digits) >= 4:
            # 元文字列末尾に英字があれば 523A のような形式も拾う
            tail_alpha = ""
            if s and s[-1].isalpha():
                tail_alpha = s[-1].upper()
            base = digits[:4]
            return f"{base}{tail_alpha}" if tail_alpha else base

        return s
    except Exception:
        return None


def _unique_keep_order(items: list[str]) -> list[str]:
    seen = set()
    out: list[str] = []
    for x in items:
        if not x:
            continue
        if x in seen:
            continue
        seen.add(x)
        out.append(x)
    return out


def _normalize_target_source(src: Any) -> list[str]:
    if not src:
        return []

    raw: list[Any] = []

    try:
        if isinstance(src, dict):
            # 値もキーも候補になりうるため両方見る
            raw.extend(list(src.keys()))
            raw.extend(list(src.values()))
        elif isinstance(src, (list, tuple, set)):
            raw.extend(list(src))
        else:
            raw.append(src)
    except Exception:
        logger.exception("❌ target source flatten failed")
        return []

    targets: list[str] = []
    for item in raw:
        sym = _normalize_symbol(item)
        if sym:
            targets.append(sym)

    return _unique_keep_order(targets)


# ============================================================
# ATS fixed targets resolver
# ============================================================

def _resolve_register_targets_from_ats() -> list[str]:
    """
    登録対象の正本を ATS の最終確定リストに固定する。
    優先順:
      1. global_data.ats_register_targets
      2. global_data.ats_targets
      3. global_data.should_register_symbols
      4. global_data.push_symbols （最後のfallback）
    """
    candidates = [
        getattr(global_data, "ats_register_targets", None),
        getattr(global_data, "ats_targets", None),
        getattr(global_data, "should_register_symbols", None),
        getattr(global_data, "push_symbols", None),
    ]

    candidate_names = [
        "ats_register_targets",
        "ats_targets",
        "should_register_symbols",
        "push_symbols",
    ]

    for name, src in zip(candidate_names, candidates):
        if not src:
            continue

        try:
            targets = _normalize_target_source(src)
            if targets:
                targets = targets[:100]
                logger.info(
                    "✅ ATS target source resolved: %s count=%d head20=%s",
                    name,
                    len(targets),
                    targets[:20],
                )
                return targets
        except Exception:
            logger.exception("❌ ATS target normalize failed source=%s", name)

    logger.warning("⚠ no ATS target source resolved")
    return []


# ============================================================
# sender helpers
# ============================================================

def _install_ws_sender() -> None:
    try:
        global_data.push_ws_sender = send_message
    except Exception:
        pass

    try:
        global_data.ws_sender = send_message
    except Exception:
        pass

    try:
        global_data.push_register_sender = send_message
    except Exception:
        pass

    logger.info("✅ ws sender installed callable=True")


def _clear_ws_sender() -> None:
    try:
        global_data.push_ws_sender = None
    except Exception:
        pass

    try:
        global_data.ws_sender = None
    except Exception:
        pass

    try:
        global_data.push_register_sender = None
    except Exception:
        pass

    logger.info("✅ ws sender cleared")


# ============================================================
# PUSH row builder
# ============================================================

def build_row(d: dict, now: dt.datetime) -> dict:
    return {
        "symbol": d.get("Symbol"),
        "symbolname": d.get("SymbolName"),

        "price": d.get("CurrentPrice"),
        "volume": d.get("TradingVolume"),
        "trading_value": d.get("TradingValue"),
        "vwap": d.get("VWAP"),

        "high_price": d.get("HighPrice"),
        "high_price_time": d.get("HighPriceTime"),

        "low_price": d.get("LowPrice"),
        "low_price_time": d.get("LowPriceTime"),

        "previousclose": d.get("PreviousClose"),
        "previousclose_time": d.get("PreviousCloseTime"),

        "opening_price": d.get("OpeningPrice"),
        "opening_price_time": d.get("OpeningPriceTime"),

        "current_price_time": d.get("CurrentPriceTime"),

        "bid_price": d.get("BidPrice"),
        "bid_qty": d.get("BidQty"),
        "ask_price": d.get("AskPrice"),
        "ask_qty": d.get("AskQty"),

        "datetime": now,
        "time": now,
        "content": json.dumps(d, ensure_ascii=False),
    }


# ============================================================
# send
# ============================================================

def send_message(payload: Any) -> bool:
    try:
        raw = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        ws = getattr(global_data, "_current_ws", None)
        if ws is None:
            logger.warning("⚠ send skipped: ws not ready")
            return False

        ws.send(raw)
        return True
    except Exception:
        logger.exception("❌ send_message failed")
        return False


# ============================================================
# PUSH受信処理
# ============================================================

def on_message(ws, message):
    try:
        d = json.loads(message)
    except Exception as e:
        logger.warning("⚠ JSON decode failed: %s", e)
        return

    symbol = d.get("Symbol")
    if not symbol:
        return

    try:
        now = dt.datetime.now()
        row = build_row(d, now)

        # ----------------------------------------------------
        # ① メモリ更新
        # ----------------------------------------------------
        append_push_tick(row, now)

        # ----------------------------------------------------
        # ② DBへ直接保存して即flush
        #    まずは「確実に継続保存されること」を最優先
        # ----------------------------------------------------
        try:
            stream_writer.add_push_row(row)
            stream_writer.flush()
        except Exception:
            logger.exception("❌ stream_writer add_push_row / flush failed")

    except Exception:
        logger.exception("❌ on_message error")


def on_error(ws, error):
    logger.error("WebSocket ERROR: %s", error)


def on_close(ws, code=None, msg=None):
    logger.warning("WebSocket CLOSED code=%s msg=%s", code, msg)
    _clear_ws_sender()
    notify_ws_closed()


# ============================================================
# open / register
# ============================================================

def on_open(ws):
    logger.info("WebSocket CONNECTED")

    try:
        global_data._current_ws = ws
    except Exception:
        pass

    _ensure_writer_started()
    _install_ws_sender()

    try:
        # ----------------------------------------------------
        # 登録対象の正本は ATS 最終確定リスト
        # ----------------------------------------------------
        targets = _resolve_register_targets_from_ats()

        logger.info(
            "🟢 ATS REGISTER TARGETS count=%d head20=%s",
            len(targets),
            targets[:20],
        )

        if not targets:
            logger.warning("⚠ ATS register targets empty -> register skipped")
            return

        msg = {
            "type": "register",
            "sendInitialBoard": False,
            "symbols": [
                {"Symbol": sym, "Exchange": 1}
                for sym in targets
            ],
        }

        ws.send(json.dumps(msg, ensure_ascii=False))

        logger.info(
            "📡 Tick REGISTER sent for %d symbols (ATS fixed targets)",
            len(targets),
        )

    except Exception:
        logger.exception("❌ WebSocket REGISTER error")


# ============================================================
# connect
# ============================================================

def connect_websocket(ws_url: str):
    logger.info("[WebSocket] connect → %s", ws_url)
    websocket.enableTrace(False)

    ws = websocket.WebSocketApp(
        ws_url,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
    )

    try:
        global_data._current_ws = ws
    except Exception:
        pass

    ws.run_forever(
        ping_interval=10,
        ping_timeout=5,
    )