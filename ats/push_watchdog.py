# ============================================================
# File   : ats/push_watchdog.py
# Version: Ver1.0-PRODUCTION-PUSH-WATCHDOG
# ------------------------------------------------------------
# ✔ PUSH停止銘柄自動除外
# ✔ ATSRotationManager連動
# ✔ 全滅防止
# ✔ websocket停止検知
# ✔ ranking / entry 安全化
# ✔ global_data互換
# ✔ 型完全統一（str）
# ============================================================

from __future__ import annotations

import logging
import datetime as dt
import threading
import time
from typing import List

from global_state import global_data

logger = logging.getLogger(__name__)

# ============================================================
# 設定
# ============================================================

# PUSH来ない銘柄除外秒
PUSH_TIMEOUT = 30

# websocket停止検知
STREAM_TIMEOUT = 10

# watchdog loop
WATCHDOG_INTERVAL = 5

# 全滅防止
MIN_SYMBOLS_KEEP = 5


# ============================================================
# PUSH更新
# ============================================================

def update_push_timestamp(symbol: str):

    symbol = str(symbol)

    now = dt.datetime.now()

    if not hasattr(global_data, "last_push_time"):
        global_data.last_push_time = {}

    global_data.last_push_time[symbol] = now

    global_data.last_push_stream_time = now


# ============================================================
# PUSH alive 判定
# ============================================================

def is_push_alive(symbol: str) -> bool:

    last_map = getattr(global_data, "last_push_time", None)

    if not last_map:
        return True

    last = last_map.get(str(symbol))

    if not last:
        return False

    diff = (dt.datetime.now() - last).total_seconds()

    return diff <= PUSH_TIMEOUT


# ============================================================
# websocket alive
# ============================================================

def is_stream_alive() -> bool:

    last = getattr(global_data, "last_push_stream_time", None)

    if not last:
        return True

    diff = (dt.datetime.now() - last).total_seconds()

    return diff <= STREAM_TIMEOUT


# ============================================================
# 銘柄フィルタ
# ============================================================

def filter_push_alive(symbols: List[str]) -> List[str]:

    if not symbols:
        return []

    alive = []

    last_map = getattr(global_data, "last_push_time", {})

    now = dt.datetime.now()

    for s in symbols:

        s = str(s)

        last = last_map.get(s)

        if not last:
            continue

        diff = (now - last).total_seconds()

        if diff <= PUSH_TIMEOUT:
            alive.append(s)

    # --------------------------------------------------------
    # 全滅防止
    # --------------------------------------------------------

    if not alive and symbols:

        logger.warning(
            "[PUSH WATCHDOG] all symbols stale → bypass (%d)",
            len(symbols),
        )

        return symbols[:MIN_SYMBOLS_KEEP]

    return alive


# ============================================================
# stale銘柄
# ============================================================

def get_stale_symbols() -> List[str]:

    last_map = getattr(global_data, "last_push_time", {})

    if not last_map:
        return []

    now = dt.datetime.now()

    stale = []

    for s, last in last_map.items():

        diff = (now - last).total_seconds()

        if diff > PUSH_TIMEOUT:
            stale.append(str(s))

    return stale


# ============================================================
# ATS連動 cleanup
# ============================================================

def cleanup_ats_symbols():

    ats = getattr(global_data, "ats_registered_symbols", None)

    if not ats:
        return

    stale = set(get_stale_symbols())

    if not stale:
        return

    new_list = []

    removed = []

    for s in ats:

        if str(s) in stale:
            removed.append(str(s))
            continue

        new_list.append(str(s))

    if removed:

        logger.info(
            "[PUSH WATCHDOG] remove stale ATS symbols: %s",
            ",".join(removed),
        )

        global_data.ats_registered_symbols = new_list


# ============================================================
# ranking連動
# ============================================================

def cleanup_ranking_cache():

    cache = getattr(global_data, "summary_cache", None)

    if not isinstance(cache, dict):
        return

    stale = set(get_stale_symbols())

    if not stale:
        return

    for tf in ("1min", "3min", "5min"):

        df = cache.get(tf)

        if df is None:
            continue

        if "symbol" not in df.columns:
            continue

        try:

            cache[tf] = df[~df["symbol"].astype(str).isin(stale)]

        except Exception:

            logger.exception("ranking cleanup failed")


# ============================================================
# watchdog main
# ============================================================

def push_watchdog_loop(interval: int = WATCHDOG_INTERVAL):

    if getattr(global_data, "_push_watchdog_running", False):

        logger.warning("push_watchdog already running")

        return

    global_data._push_watchdog_running = True

    logger.info("🚀 PUSH WATCHDOG START")

    while True:

        try:

            # ------------------------------------------------
            # websocket停止検知
            # ------------------------------------------------

            if not is_stream_alive():

                logger.warning(
                    "[PUSH WATCHDOG] stream stalled"
                )

            # ------------------------------------------------
            # stale
            # ------------------------------------------------

            stale = get_stale_symbols()

            if stale:

                logger.info(
                    "[PUSH WATCHDOG] stale symbols: %s",
                    ",".join(stale[:20]),
                )

            # ------------------------------------------------
            # cleanup
            # ------------------------------------------------

            cleanup_ats_symbols()

            cleanup_ranking_cache()

        except Exception:

            logger.exception("push_watchdog error")

        time.sleep(interval)


# ============================================================
# thread start
# ============================================================

def start_push_watchdog():

    t = threading.Thread(
        target=push_watchdog_loop,
        daemon=True,
        name="PushWatchdog",
    )

    t.start()

    return t