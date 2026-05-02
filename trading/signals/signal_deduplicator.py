# ============================================================
# trading/signals/signal_deduplicator.py
# Ver1.0-PRODUCTION-SIGNAL-DEDUPLICATOR
# ------------------------------------------------------------
# ✔ 同一シグナルの連続発火防止
# ✔ BUY / SHORT 別管理
# ✔ 銘柄単位 dedup
# ✔ TTL (time to live) 管理
# ✔ メモリリーク防止
# ✔ signals_engine / pipeline 互換
# ============================================================

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)


# ============================================================
# signal deduplicator
# ============================================================

class SignalDeduplicator:

    def __init__(self, ttl_sec: int = 60):

        # symbol -> signal_type -> {signal: timestamp}
        self._signals = {}

        # 同一シグナル再発火までの秒
        self.ttl_sec = ttl_sec


    # ========================================================
    # internal cleanup
    # ========================================================

    def _cleanup_symbol(self, symbol):

        now = time.time()

        symbol_map = self._signals.get(symbol)

        if not symbol_map:
            return

        for signal_type in ["buy", "short"]:

            signals = symbol_map.get(signal_type)

            if not signals:
                continue

            remove_keys = []

            for sig, ts in signals.items():

                if now - ts > self.ttl_sec:

                    remove_keys.append(sig)

            for sig in remove_keys:

                del signals[sig]


    # ========================================================
    # filter signals
    # ========================================================

    def filter_signals(
        self,
        symbol: str,
        buy_signals: list[str] | None = None,
        short_signals: list[str] | None = None
    ):

        now = time.time()

        if symbol not in self._signals:

            self._signals[symbol] = {
                "buy": {},
                "short": {}
            }

        self._cleanup_symbol(symbol)

        symbol_map = self._signals[symbol]

        new_buy = []
        new_short = []

        # BUY dedup
        if buy_signals:

            for sig in buy_signals:

                if sig not in symbol_map["buy"]:

                    new_buy.append(sig)

                    symbol_map["buy"][sig] = now

        # SHORT dedup
        if short_signals:

            for sig in short_signals:

                if sig not in symbol_map["short"]:

                    new_short.append(sig)

                    symbol_map["short"][sig] = now

        return new_buy, new_short


    # ========================================================
    # check duplicate
    # ========================================================

    def is_duplicate(
        self,
        symbol: str,
        signal: str,
        signal_type: str = "buy"
    ) -> bool:

        symbol_map = self._signals.get(symbol)

        if not symbol_map:

            return False

        signals = symbol_map.get(signal_type)

        if not signals:

            return False

        if signal in signals:

            return True

        return False


    # ========================================================
    # reset symbol
    # ========================================================

    def reset_symbol(self, symbol):

        if symbol in self._signals:

            del self._signals[symbol]


    # ========================================================
    # reset all
    # ========================================================

    def reset_all(self):

        self._signals.clear()


    # ========================================================
    # cleanup old symbols
    # ========================================================

    def cleanup(self):

        now = time.time()

        remove_symbols = []

        for symbol, symbol_map in self._signals.items():

            keep = False

            for signal_type in ["buy", "short"]:

                signals = symbol_map.get(signal_type)

                if not signals:
                    continue

                for ts in signals.values():

                    if now - ts <= self.ttl_sec:

                        keep = True
                        break

                if keep:
                    break

            if not keep:

                remove_symbols.append(symbol)

        for symbol in remove_symbols:

            del self._signals[symbol]

        if remove_symbols:

            logger.info(
                f"[SignalDeduplicator] cleaned {len(remove_symbols)} symbols"
            )


    # ========================================================
    # stats
    # ========================================================

    def stats(self):

        total_symbols = len(self._signals)

        total_signals = 0

        for symbol_map in self._signals.values():

            total_signals += len(symbol_map.get("buy", {}))
            total_signals += len(symbol_map.get("short", {}))

        return {
            "symbols": total_symbols,
            "signals": total_signals
        }