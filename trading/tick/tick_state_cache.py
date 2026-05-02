# ============================================================
# File   : trading/tick/tick_state_cache.py
# Version: V1.0-FINAL-TICK-STATE-CACHE-LOWLATENCY
# ------------------------------------------------------------
# ✔ スレッドセーフ
# ✔ symbol毎deque管理
# ✔ 時間窓抽出API
# ✔ 最大保持数制御
# ✔ メモリ暴走防止
# ✔ volume累積補助
# ✔ 安全例外防御
# ✔ exit_loop高頻度呼び出し対応
# ============================================================

from collections import deque
from threading import Lock
import time
import logging

logger = logging.getLogger(__name__)


class TickStateCache:
    """
    超低遅延 tick キャッシュ

    目的:
        - collapse検知
        - tick特徴量生成
        - 瞬間ボラ検出
        - 板急変監視

    想定:
        1銘柄あたり 100〜300tick保持
    """

    def __init__(self, maxlen: int = 300):
        self._cache = {}
        self._locks = {}
        self.maxlen = maxlen

    # ============================================================
    # 内部: ロック取得
    # ============================================================

    def _get_lock(self, symbol: str) -> Lock:
        if symbol not in self._locks:
            self._locks[symbol] = Lock()
        return self._locks[symbol]

    # ============================================================
    # 更新
    # ============================================================

    def update(self, symbol: str, tick: dict):
        """
        tick形式:
        {
            "price": float,
            "bid": float | None,
            "ask": float | None,
            "volume": float | int,
            "ts": optional (epoch秒)
        }
        """

        try:
            lock = self._get_lock(symbol)

            with lock:
                if symbol not in self._cache:
                    self._cache[symbol] = deque(maxlen=self.maxlen)

                self._cache[symbol].append({
                    "price": float(tick.get("price", 0)),
                    "bid": tick.get("bid"),
                    "ask": tick.get("ask"),
                    "volume": tick.get("volume", 0),
                    "ts": tick.get("ts", time.time()),
                })

        except Exception:
            logger.exception(f"[TickStateCache] update failed: {symbol}")

    # ============================================================
    # 全tick取得
    # ============================================================

    def get_all(self, symbol: str):
        lock = self._get_lock(symbol)
        with lock:
            return list(self._cache.get(symbol, []))

    # ============================================================
    # 直近N秒取得
    # ============================================================

    def get_last_seconds(self, symbol: str, seconds: float):
        """
        直近 N秒 のtickを返す
        collapse 1秒/3秒計算用
        """
        now = time.time()
        lock = self._get_lock(symbol)

        with lock:
            ticks = self._cache.get(symbol, [])
            return [
                t for t in ticks
                if now - t["ts"] <= seconds
            ]

    # ============================================================
    # 最新tick取得
    # ============================================================

    def get_latest(self, symbol: str):
        lock = self._get_lock(symbol)
        with lock:
            ticks = self._cache.get(symbol)
            if not ticks:
                return None
            return ticks[-1]

    # ============================================================
    # tick数取得
    # ============================================================

    def size(self, symbol: str) -> int:
        lock = self._get_lock(symbol)
        with lock:
            return len(self._cache.get(symbol, []))

    # ============================================================
    # volume合計（直近N秒）
    # ============================================================

    def get_volume_last_seconds(self, symbol: str, seconds: float):
        ticks = self.get_last_seconds(symbol, seconds)
        return sum(t.get("volume", 0) for t in ticks)

    # ============================================================
    # symbol削除
    # ============================================================

    def clear_symbol(self, symbol: str):
        lock = self._get_lock(symbol)
        with lock:
            if symbol in self._cache:
                del self._cache[symbol]
            if symbol in self._locks:
                del self._locks[symbol]

    # ============================================================
    # 全削除（市場終了時）
    # ============================================================

    def clear_all(self):
        self._cache.clear()
        self._locks.clear()

    # ============================================================
    # デバッグ情報
    # ============================================================

    def debug_info(self):
        return {
            "symbols": len(self._cache),
            "total_ticks": sum(len(v) for v in self._cache.values()),
            "maxlen": self.maxlen,
        }


# ============================================================
# グローバルインスタンス（推奨）
# ============================================================

tick_state_cache = TickStateCache()