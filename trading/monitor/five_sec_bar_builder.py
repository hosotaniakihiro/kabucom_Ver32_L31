# ============================================================
# File   : trading/monitor/five_sec_bar_builder.py
# Version: PRODUCTION-STABLE-REV1.0-5SEC-BAR-BUILDER
# ------------------------------------------------------------
# 【概要】
#   PUSH tick から symbol ごとの 5秒足 OHLCV を生成し、
#   GC.monitor.set_five_sec_bar(symbol, bar) に保存する。
#
# 【目的】
#   - EXIT監視で5秒足 close を現在値として使う
#   - 殿様イナゴEXITで5秒足急落 / 陰線連続 / VWAP割れ / 出来高急減を使う
#   - AI EXIT / collapse / 通常EXIT にも features として使えるようにする
#
# 【入力】
#   update_five_sec_bar_from_tick(symbol=symbol, tick=tick)
#
# 【出力 bar 例】
#   {
#       "symbol": "6857",
#       "datetime": "2026-04-26 09:00:05",
#       "start_time": "2026-04-26 09:00:00",
#       "end_time": "2026-04-26 09:00:05",
#       "open": 1000.0,
#       "high": 1005.0,
#       "low": 998.0,
#       "close": 1002.0,
#       "volume": 1200.0,
#       "tick_count": 8,
#       "drop_pct": -0.30,
#       "bar5s_drop_pct": -0.30,
#       "consecutive_down": 2,
#       "bar5s_consecutive_down": 2,
#       "volume_ratio": 0.42,
#       "bar5s_volume_ratio": 0.42,
#       "vwap": 1003.2,
#       "vwap_break": True,
#       "bar5s_vwap_break": True,
#       "is_complete": True,
#       "source": "push_5sec_builder",
#   }
#
# 【重要】
#   - このファイルだけでは動かない。
#   - PUSH受信側で update_five_sec_bar_from_tick() を呼ぶ必要がある。
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import math
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, Optional

from core.global_context.context import global_context as GC

logger = logging.getLogger(__name__)


# ============================================================
# constants
# ============================================================

BAR_SECONDS = 5
RECENT_VOLUME_WINDOW = 12  # 直近12本 = 約1分
DEFAULT_SOURCE = "push_5sec_builder"


# ============================================================
# safe helpers
# ============================================================

def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        x = float(v)
        if math.isnan(x) or math.isinf(x):
            return default
        return x
    except Exception:
        return default


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        if v is None:
            return default
        x = int(float(v))
        return x
    except Exception:
        return default


def _normalize_symbol(v: Any) -> str:
    try:
        if v is None:
            return ""
        s = str(v).strip()
        if not s:
            return ""
        if s.endswith(".0"):
            s2 = s[:-2]
            if s2.isdigit():
                return s2
        return s
    except Exception:
        return ""


def _dict_get_any(d: Any, *names: str, default: Any = None) -> Any:
    if not isinstance(d, dict):
        return default

    for name in names:
        try:
            if name in d:
                return d.get(name)
        except Exception:
            pass

    return default


def _parse_timestamp(v: Any = None) -> dt.datetime:
    try:
        if isinstance(v, dt.datetime):
            return v.replace(tzinfo=None)

        if v is None:
            return dt.datetime.now()

        # pandas.Timestamp にも対応
        if hasattr(v, "to_pydatetime"):
            return v.to_pydatetime().replace(tzinfo=None)

        s = str(v).strip()
        if not s:
            return dt.datetime.now()

        # ISO / 通常文字列をできるだけ吸収
        try:
            return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception:
            pass

        for fmt in (
            "%Y-%m-%d %H:%M:%S",
            "%Y/%m/%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y/%m/%d %H:%M",
            "%H:%M:%S",
            "%H:%M",
        ):
            try:
                parsed = dt.datetime.strptime(s, fmt)
                if fmt in ("%H:%M:%S", "%H:%M"):
                    today = dt.datetime.now()
                    parsed = parsed.replace(
                        year=today.year,
                        month=today.month,
                        day=today.day,
                    )
                return parsed
            except Exception:
                pass

    except Exception:
        pass

    return dt.datetime.now()


def _floor_to_5sec(ts: dt.datetime) -> dt.datetime:
    sec = int(ts.second // BAR_SECONDS * BAR_SECONDS)
    return ts.replace(second=sec, microsecond=0)


def _calc_pct(current: float, base: float) -> float:
    try:
        if base <= 0:
            return 0.0
        return (current / base - 1.0) * 100.0
    except Exception:
        return 0.0


def _ensure_monitor_state() -> bool:
    """
    GC.monitor が未設定の場合に MonitorState を作る。
    既に起動側で作られているなら何もしない。
    """
    try:
        if hasattr(GC, "monitor") and GC.monitor is not None:
            return True

        from core.global_context.monitor_state import MonitorState

        GC.monitor = MonitorState()
        logger.warning("[5SEC BUILDER] GC.monitor was missing. MonitorState created.")
        return True

    except Exception:
        logger.exception("[5SEC BUILDER] failed to ensure GC.monitor")
        return False


# ============================================================
# tick normalize
# ============================================================

def normalize_push_tick(
    *,
    symbol: Optional[str] = None,
    price: Optional[float] = None,
    volume: Optional[float] = None,
    timestamp: Any = None,
    tick: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """
    PUSH tick のキー揺れを吸収する。

    kabu Station / 独自PUSH / DB由来の代表キーに対応。
    """

    try:
        tick = tick if isinstance(tick, dict) else {}

        sym = _normalize_symbol(
            symbol
            or _dict_get_any(
                tick,
                "symbol",
                "Symbol",
                "code",
                "Code",
                "ticker",
                "Ticker",
                "銘柄コード",
                default="",
            )
        )

        px = _safe_float(
            price
            if price is not None
            else _dict_get_any(
                tick,
                "price",
                "Price",
                "current_price",
                "CurrentPrice",
                "last_price",
                "LastPrice",
                "last",
                "close",
                "Close",
                "現在値",
                default=0.0,
            ),
            0.0,
        )

        vol = _safe_float(
            volume
            if volume is not None
            else _dict_get_any(
                tick,
                "volume",
                "Volume",
                "trading_volume",
                "TradingVolume",
                "cum_volume",
                "CumulativeVolume",
                "出来高",
                default=0.0,
            ),
            0.0,
        )

        ts = _parse_timestamp(
            timestamp
            if timestamp is not None
            else _dict_get_any(
                tick,
                "datetime",
                "timestamp",
                "time",
                "CurrentPriceTime",
                "current_price_time",
                "ExchangeTime",
                "exchange_time",
                "時刻",
                default=None,
            )
        )

        if not sym or px <= 0:
            return None

        return {
            "symbol": sym,
            "price": px,
            "volume": vol,
            "timestamp": ts,
            "raw": tick,
        }

    except Exception:
        logger.exception("[5SEC BUILDER] normalize_push_tick failed")
        return None


# ============================================================
# state
# ============================================================

@dataclass
class _Symbol5SecState:
    symbol: str

    bucket_start: Optional[dt.datetime] = None
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    volume: float = 0.0
    tick_count: int = 0

    prev_close: float = 0.0
    consecutive_down_completed: int = 0

    recent_completed_volumes: Deque[float] = field(
        default_factory=lambda: deque(maxlen=RECENT_VOLUME_WINDOW)
    )

    # VWAP用。基本は当日内で累積。
    session_date: Optional[dt.date] = None
    cum_pv: float = 0.0
    cum_volume: float = 0.0

    # PUSHの出来高が累積出来高の場合に差分化するため
    last_cumulative_volume: float = 0.0


# ============================================================
# builder
# ============================================================

class FiveSecBarBuilder:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._states: Dict[str, _Symbol5SecState] = {}

    # --------------------------------------------------------
    # public
    # --------------------------------------------------------

    def update_tick(
        self,
        *,
        symbol: str,
        price: float,
        volume: float = 0.0,
        timestamp: Optional[dt.datetime] = None,
        raw_tick: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        tickを1件取り込み、最新5秒足をGC.monitorへ保存する。

        戻り値:
          最新bar dict
        """

        del raw_tick

        symbol = _normalize_symbol(symbol)
        price = _safe_float(price)
        volume = _safe_float(volume)
        ts = _parse_timestamp(timestamp)

        if not symbol or price <= 0:
            return None

        if not _ensure_monitor_state():
            return None

        try:
            with self._lock:
                st = self._states.get(symbol)
                if st is None:
                    st = _Symbol5SecState(symbol=symbol)
                    self._states[symbol] = st

                self._reset_session_if_needed(st, ts)

                bucket_start = _floor_to_5sec(ts)

                # 初回
                if st.bucket_start is None:
                    self._start_new_bucket(st, bucket_start, price, volume)
                    bar = self._make_bar(st, is_complete=False, now_ts=ts)
                    self._publish_bar(symbol, bar)
                    return bar

                # bucketが変わったら前barを確定して保存
                if bucket_start != st.bucket_start:
                    completed = self._make_bar(st, is_complete=True, now_ts=ts)
                    self._finalize_completed_bar(st, completed)
                    self._publish_bar(symbol, completed)

                    self._start_new_bucket(st, bucket_start, price, volume)
                    bar = self._make_bar(st, is_complete=False, now_ts=ts)
                    self._publish_bar(symbol, bar)
                    return bar

                # 同一bucket内更新
                self._update_current_bucket(st, price, volume)
                bar = self._make_bar(st, is_complete=False, now_ts=ts)
                self._publish_bar(symbol, bar)
                return bar

        except Exception:
            logger.exception("[5SEC BUILDER] update_tick failed symbol=%s", symbol)
            return None

    def snapshot_states(self) -> Dict[str, Dict[str, Any]]:
        try:
            with self._lock:
                out = {}
                for symbol, st in self._states.items():
                    out[symbol] = {
                        "symbol": st.symbol,
                        "bucket_start": st.bucket_start,
                        "open": st.open,
                        "high": st.high,
                        "low": st.low,
                        "close": st.close,
                        "volume": st.volume,
                        "tick_count": st.tick_count,
                        "prev_close": st.prev_close,
                        "consecutive_down_completed": st.consecutive_down_completed,
                        "recent_completed_volumes": list(st.recent_completed_volumes),
                        "session_date": st.session_date,
                        "cum_pv": st.cum_pv,
                        "cum_volume": st.cum_volume,
                        "last_cumulative_volume": st.last_cumulative_volume,
                    }
                return out
        except Exception:
            return {}

    def clear(self) -> None:
        try:
            with self._lock:
                self._states.clear()
        except Exception:
            logger.exception("[5SEC BUILDER] clear failed")

    # --------------------------------------------------------
    # internals
    # --------------------------------------------------------

    def _reset_session_if_needed(self, st: _Symbol5SecState, ts: dt.datetime) -> None:
        d = ts.date()
        if st.session_date is None:
            st.session_date = d
            return

        if st.session_date != d:
            st.session_date = d
            st.cum_pv = 0.0
            st.cum_volume = 0.0
            st.last_cumulative_volume = 0.0
            st.prev_close = 0.0
            st.consecutive_down_completed = 0
            st.recent_completed_volumes.clear()

    def _calc_delta_volume(self, st: _Symbol5SecState, volume: float) -> float:
        """
        PUSHのvolumeが累積出来高でも、差分出来高でも使えるようにする。

        - volume > last_cumulative_volume の場合:
            累積出来高とみなして差分
        - volume <= last_cumulative_volume の場合:
            セッション切替/リセット/差分出来高の可能性としてそのまま使う
        """

        volume = _safe_float(volume)
        if volume <= 0:
            return 0.0

        if st.last_cumulative_volume > 0 and volume >= st.last_cumulative_volume:
            delta = volume - st.last_cumulative_volume
            st.last_cumulative_volume = volume
            return max(0.0, delta)

        # 初回、または累積がリセットされた場合
        if st.last_cumulative_volume <= 0:
            st.last_cumulative_volume = volume
            return 0.0

        # volumeが差分値として来ている可能性
        return max(0.0, volume)

    def _start_new_bucket(
        self,
        st: _Symbol5SecState,
        bucket_start: dt.datetime,
        price: float,
        volume: float,
    ) -> None:
        delta_vol = self._calc_delta_volume(st, volume)

        st.bucket_start = bucket_start
        st.open = price
        st.high = price
        st.low = price
        st.close = price
        st.volume = delta_vol
        st.tick_count = 1

        self._update_vwap(st, price, delta_vol)

    def _update_current_bucket(
        self,
        st: _Symbol5SecState,
        price: float,
        volume: float,
    ) -> None:
        delta_vol = self._calc_delta_volume(st, volume)

        if st.open <= 0:
            st.open = price

        st.high = max(st.high or price, price)
        st.low = min(st.low or price, price)
        st.close = price
        st.volume += delta_vol
        st.tick_count += 1

        self._update_vwap(st, price, delta_vol)

    def _update_vwap(self, st: _Symbol5SecState, price: float, delta_volume: float) -> None:
        if delta_volume <= 0:
            return

        st.cum_pv += price * delta_volume
        st.cum_volume += delta_volume

    def _current_vwap(self, st: _Symbol5SecState) -> float:
        if st.cum_volume <= 0:
            return 0.0
        return st.cum_pv / st.cum_volume

    def _volume_ratio(self, st: _Symbol5SecState) -> float:
        vols = [float(v) for v in st.recent_completed_volumes if float(v) > 0]
        if not vols:
            return 1.0

        avg = sum(vols) / len(vols)
        if avg <= 0:
            return 1.0

        return st.volume / avg

    def _make_bar(
        self,
        st: _Symbol5SecState,
        *,
        is_complete: bool,
        now_ts: dt.datetime,
    ) -> Dict[str, Any]:
        bucket_start = st.bucket_start or _floor_to_5sec(now_ts)
        bucket_end = bucket_start + dt.timedelta(seconds=BAR_SECONDS)

        drop_pct = _calc_pct(st.close, st.open)

        current_down = 1 if st.close < st.open else 0
        if current_down:
            consecutive_down = int(st.consecutive_down_completed) + 1
        else:
            consecutive_down = 0

        vwap = self._current_vwap(st)
        vwap_break = bool(vwap > 0 and st.close < vwap)

        volume_ratio = self._volume_ratio(st)

        bar = {
            "symbol": st.symbol,
            "datetime": bucket_end.strftime("%Y-%m-%d %H:%M:%S"),
            "start_time": bucket_start.strftime("%Y-%m-%d %H:%M:%S"),
            "end_time": bucket_end.strftime("%Y-%m-%d %H:%M:%S"),
            "bucket_start": bucket_start,
            "bucket_end": bucket_end,

            "open": float(st.open),
            "high": float(st.high),
            "low": float(st.low),
            "close": float(st.close),
            "price": float(st.close),
            "current_price": float(st.close),
            "last_price": float(st.close),

            "volume": float(st.volume),
            "tick_count": int(st.tick_count),

            "drop_pct": float(drop_pct),
            "bar5s_drop_pct": float(drop_pct),

            "consecutive_down": int(consecutive_down),
            "bar5s_consecutive_down": int(consecutive_down),

            "volume_ratio": float(volume_ratio),
            "bar5s_volume_ratio": float(volume_ratio),

            "vwap": float(vwap),
            "vwap_break": bool(vwap_break),
            "bar5s_vwap_break": bool(vwap_break),

            # exit_features.py が optional で拾える互換キー
            # 注意: これは「5秒足bar内の高値」。entry後高値ではない。
            "bar5s_high": float(st.high),

            "is_complete": bool(is_complete),
            "updated_at": now_ts.strftime("%Y-%m-%d %H:%M:%S"),
            "source": DEFAULT_SOURCE,
        }

        return bar

    def _finalize_completed_bar(self, st: _Symbol5SecState, bar: Dict[str, Any]) -> None:
        try:
            st.prev_close = _safe_float(bar.get("close"))

            if _safe_float(bar.get("close")) < _safe_float(bar.get("open")):
                st.consecutive_down_completed = _safe_int(bar.get("consecutive_down"), 0)
            else:
                st.consecutive_down_completed = 0

            vol = _safe_float(bar.get("volume"))
            if vol > 0:
                st.recent_completed_volumes.append(vol)

        except Exception:
            logger.exception("[5SEC BUILDER] finalize completed bar failed symbol=%s", st.symbol)

    def _publish_bar(self, symbol: str, bar: Dict[str, Any]) -> None:
        try:
            if not _ensure_monitor_state():
                return

            if hasattr(GC.monitor, "set_five_sec_bar"):
                GC.monitor.set_five_sec_bar(symbol, bar)

            logger.debug(
                "[5SEC BAR] symbol=%s close=%.4f open=%.4f high=%.4f low=%.4f "
                "vol=%.0f drop=%.2f down=%s vwap=%.4f vwap_break=%s complete=%s",
                symbol,
                _safe_float(bar.get("close")),
                _safe_float(bar.get("open")),
                _safe_float(bar.get("high")),
                _safe_float(bar.get("low")),
                _safe_float(bar.get("volume")),
                _safe_float(bar.get("drop_pct")),
                bar.get("consecutive_down"),
                _safe_float(bar.get("vwap")),
                bar.get("vwap_break"),
                bar.get("is_complete"),
            )

        except Exception:
            logger.exception("[5SEC BUILDER] publish failed symbol=%s", symbol)


# ============================================================
# singleton API
# ============================================================

_GLOBAL_BUILDER: Optional[FiveSecBarBuilder] = None
_GLOBAL_LOCK = threading.RLock()


def get_five_sec_bar_builder() -> FiveSecBarBuilder:
    global _GLOBAL_BUILDER

    with _GLOBAL_LOCK:
        if _GLOBAL_BUILDER is None:
            _GLOBAL_BUILDER = FiveSecBarBuilder()
        return _GLOBAL_BUILDER


def update_five_sec_bar_from_tick(
    *,
    symbol: Optional[str] = None,
    price: Optional[float] = None,
    volume: Optional[float] = None,
    timestamp: Any = None,
    tick: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """
    PUSH受信側から呼ぶ入口。

    使用例:
        from trading.monitor.five_sec_bar_builder import update_five_sec_bar_from_tick

        update_five_sec_bar_from_tick(
            symbol=symbol,
            tick=tick_dict,
        )
    """

    norm = normalize_push_tick(
        symbol=symbol,
        price=price,
        volume=volume,
        timestamp=timestamp,
        tick=tick,
    )

    if not norm:
        return None

    builder = get_five_sec_bar_builder()

    return builder.update_tick(
        symbol=norm["symbol"],
        price=norm["price"],
        volume=norm["volume"],
        timestamp=norm["timestamp"],
        raw_tick=norm.get("raw"),
    )


def clear_five_sec_bar_builder() -> None:
    builder = get_five_sec_bar_builder()
    builder.clear()


def snapshot_five_sec_bar_builder_states() -> Dict[str, Dict[str, Any]]:
    builder = get_five_sec_bar_builder()
    return builder.snapshot_states()


__all__ = [
    "FiveSecBarBuilder",
    "get_five_sec_bar_builder",
    "update_five_sec_bar_from_tick",
    "clear_five_sec_bar_builder",
    "snapshot_five_sec_bar_builder_states",
    "normalize_push_tick",
]