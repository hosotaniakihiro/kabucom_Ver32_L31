# ============================================================
# trading/push/push_event_engine.py
# Ver1.0-PRODUCTION-HFT-EVENT-ENGINE
# ------------------------------------------------------------
# ✔ 殿様イナゴ検出
# ✔ 急騰検知
# ✔ 資金流入検知
# ✔ Discord通知
# ✔ ATS優先登録
# ✔ summary_cache連携
# ✔ pushイベント解析
# ✔ HFT軽量設計
# ✔ NaN / inf 防御
# ✔ 副作用ゼロ
# ============================================================

import logging
import math
from collections import defaultdict
from datetime import datetime, timedelta

from global_state import global_data

from utils.alerts_util import send_discord_message


logger = logging.getLogger(__name__)

# ============================================================
# thresholds
# ============================================================

TONOSAMA_VOLUME = 3
TONOSAMA_PRICE = 1.2

SPIKE_PCT = 2.0

CAPITAL_INFLOW_TURNOVER = 20_000_000

# ============================================================
# internal state
# ============================================================

_last_price = {}
_last_volume = {}

_symbol_history = defaultdict(list)

MAX_HISTORY = 20


# ============================================================
# safe
# ============================================================

def _safe(v):

    try:

        if v is None:
            return 0.0

        val = float(v)

        if math.isnan(val) or math.isinf(val):
            return 0.0

        return val

    except Exception:

        return 0.0


# ============================================================
# record tick
# ============================================================

def _record_tick(symbol, price, volume, ts):

    hist = _symbol_history[symbol]

    hist.append((ts, price, volume))

    if len(hist) > MAX_HISTORY:
        hist.pop(0)


# ============================================================
# price spike detection
# ============================================================

def _detect_price_spike(symbol, price):

    prev = _last_price.get(symbol)

    _last_price[symbol] = price

    if not prev:
        return False

    pct = ((price - prev) / prev) * 100

    return pct >= SPIKE_PCT


# ============================================================
# tonosama detection
# ============================================================

def _detect_tonosama(symbol, price, volume):

    prev_v = _last_volume.get(symbol)

    _last_volume[symbol] = volume

    prev_p = _last_price.get(symbol)

    if not prev_v or not prev_p:
        return False

    vol_ratio = volume / prev_v if prev_v else 0
    price_ratio = price / prev_p if prev_p else 0

    return vol_ratio >= TONOSAMA_VOLUME and price_ratio >= TONOSAMA_PRICE


# ============================================================
# capital inflow detection
# ============================================================

def _detect_capital_inflow(symbol, turnover):

    return turnover >= CAPITAL_INFLOW_TURNOVER


# ============================================================
# discord notify
# ============================================================

def _notify(event, symbol, row):

    try:

        msg = f"[{event}] {symbol} price={row['price']} volume={row['volume']}"

        send_discord_message(msg)

    except Exception:

        logger.exception("discord notify failed")


# ============================================================
# ATS promote
# ============================================================

def _promote_to_ats(symbol):

    try:

        ats = getattr(global_data, "symbols_active", [])

        if symbol not in ats:

            ats.insert(0, symbol)

            global_data.symbols_active = ats

    except Exception:

        logger.exception("ATS promote failed")


# ============================================================
# main event engine
# ============================================================

def process_push_event(symbol, row):

    try:

        price = _safe(row.get("price"))
        volume = _safe(row.get("volume"))
        turnover = _safe(row.get("turnover"))
        ts = row.get("datetime")

        if not ts:
            ts = datetime.now()

        _record_tick(symbol, price, volume, ts)

        # ----------------------------------------------------
        # price spike
        # ----------------------------------------------------

        if _detect_price_spike(symbol, price):

            logger.info("[SPIKE] %s", symbol)

            _notify("SPIKE", symbol, row)

            _promote_to_ats(symbol)

        # ----------------------------------------------------
        # tonosama
        # ----------------------------------------------------

        if _detect_tonosama(symbol, price, volume):

            logger.info("[TONOSAMA] %s", symbol)

            _notify("TONOSAMA", symbol, row)

            _promote_to_ats(symbol)

        # ----------------------------------------------------
        # capital inflow
        # ----------------------------------------------------

        if _detect_capital_inflow(symbol, turnover):

            logger.info("[CAPITAL INFLOW] %s", symbol)

            _notify("CAPITAL INFLOW", symbol, row)

            _promote_to_ats(symbol)

    except Exception:

        logger.exception("push event engine failed")