# ============================================================
# ats_liquidity_monitor.py
# Ver1.1-PRODUCTION-LIQUIDITY-MONITOR
# ------------------------------------------------------------
# ✔ push / orderbook 更新銘柄記録
# ✔ 直近流動性銘柄検出
# ✔ 時間窓フィルター
# ✔ 重複除去
# ✔ thread-safe
# ✔ ATS統合
# ============================================================

import logging
import time
import threading
from collections import deque
from typing import List

logger = logging.getLogger(__name__)

# ============================================================
# 内部状態
# ============================================================

# 直近更新銘柄
_recent_symbols = deque(maxlen=1000)

# 銘柄最終更新時刻
_symbol_last_update = {}

# lock
_lock = threading.Lock()

# ============================================================
# push更新記録
# ============================================================

def record_symbol_update(symbol: str):

    if not symbol:
        return

    now = time.time()

    try:

        with _lock:

            _symbol_last_update[str(symbol)] = now

            _recent_symbols.append(str(symbol))

    except Exception:

        logger.exception("record_symbol_update failed")


# ============================================================
# 直近更新銘柄取得
# ============================================================

def get_recent_symbols(limit: int = 100) -> List[str]:

    try:

        with _lock:

            symbols = list(dict.fromkeys(_recent_symbols))

            return symbols[-limit:]

    except Exception:

        logger.exception("get_recent_symbols failed")

        return []


# ============================================================
# 活発銘柄取得
# ============================================================

def get_active_symbols(
    window_seconds: int = 30,
    limit: int = 40
) -> List[str]:

    now = time.time()

    active = []

    try:

        with _lock:

            for sym, ts in _symbol_last_update.items():

                if now - ts <= window_seconds:

                    active.append(sym)

        # order保持ユニーク
        active = list(dict.fromkeys(active))

        return active[:limit]

    except Exception:

        logger.exception("get_active_symbols failed")

        return []


# ============================================================
# 流動性スコア（更新頻度）
# ============================================================

def get_liquidity_scores(
    window_seconds: int = 60
):

    now = time.time()

    scores = {}

    try:

        with _lock:

            for sym, ts in _symbol_last_update.items():

                delta = now - ts

                if delta <= window_seconds:

                    score = 1 / max(delta, 0.001)

                    scores[sym] = score

        return scores

    except Exception:

        logger.exception("get_liquidity_scores failed")

        return {}


# ============================================================
# 上位流動性銘柄
# ============================================================

def get_top_liquidity_symbols(
    window_seconds: int = 30,
    limit: int = 30
) -> List[str]:

    scores = get_liquidity_scores(window_seconds)

    try:

        ranked = sorted(
            scores.items(),
            key=lambda x: x[1],
            reverse=True
        )

        symbols = [sym for sym, _ in ranked]

        return symbols[:limit]

    except Exception:

        logger.exception("get_top_liquidity_symbols failed")

        return []


# ============================================================
# クリーンアップ
# ============================================================

def cleanup_old_symbols(
    max_age_seconds: int = 300
):

    now = time.time()

    try:

        with _lock:

            remove = []

            for sym, ts in _symbol_last_update.items():

                if now - ts > max_age_seconds:

                    remove.append(sym)

            for sym in remove:

                _symbol_last_update.pop(sym, None)

    except Exception:

        logger.exception("cleanup_old_symbols failed")