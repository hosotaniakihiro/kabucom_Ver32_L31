# ============================================================
# File   : trading/push/push_stall_guard.py
# Version: V1.0-PUSH-STALL-GUARD
# ------------------------------------------------------------
# PUSH の新規 tick が止まったことを検知し、
#   1. 市場時間内かを見て
#   2. 購読銘柄数と PUSH 接続状態を確認し
#   3. 安全な再接続を **1 回だけ** 要求し
#   4. 連打を防ぎ
#   5. その間 Summary-AI / Tonosama へ stale を渡さない
# ための状態機械。
#
# 【背景】(2026-09-01 12:53:05 - 12:54:46 のログ)
#   [MAIN PRICE DB FEED LATENCY] ... rows=0 rowid_delta=0
#   が 50 サイクル (約 101 秒) 続き、
#   [MAIN PRICE DB FEED] no price update for 30 cycles
#   の警告だけ出て、その後ひとりでに再開した。
#   その 101 秒の間、Summary-AI と Tonosama は
#   「更新の止まった最後の 1 分足」をそのまま新しい足として扱っていた。
#   (12:53 台の候補はすべて datetime=12:52:00 のまま)
#
#   止まったこと自体は上流 (push_receiver_runner の WS) の問題だが、
#   下流が「古い」と分からないまま判定を続けるのが危険。
#
# 【設計】
#   - 時計は注入できる (テストで実時間を待たない)
#   - 再接続は「要求」を出すだけ。実際に WS を張り直すのは所有プロセス。
#     main.py 側は PUSH を所有しないので、ここから WS を触ってはならない
#     (kabu Station の PUSH は 1 セッションのみ。二重接続は互いを切る)
#   - 再接続要求は cool-down 内に 1 回だけ (連打しない)
#   - 市場時間外は stall と呼ばない (単に配信が無いだけ)
#   - 購読 0 件のときは「再接続」ではなく「購読が空」と報告する
#     (再接続しても直らないため)
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

VERSION = "V1.0-PUSH-STALL-GUARD"

STATE_OK = "ok"
STATE_STALL = "stall"
STATE_MARKET_CLOSED = "market_closed"
STATE_NO_SUBSCRIPTION = "no_subscription"


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).strip())
    except Exception:
        return float(default)


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return bool(default)
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}


def stall_threshold_sec() -> float:
    """この秒数、新規 tick が無ければ stall とみなす。"""
    return max(5.0, _env_float("PUSH_STALL_THRESHOLD_SEC", 60.0))


def stale_data_max_age_sec() -> float:
    """この秒数を超えた PUSH データは Summary-AI / Tonosama へ渡さない。"""
    return max(5.0, _env_float("PUSH_STALE_DATA_MAX_AGE_SEC", 90.0))


def reconnect_cooldown_sec() -> float:
    """再接続要求の最短間隔 (連打防止)。"""
    return max(30.0, _env_float("PUSH_STALL_RECONNECT_COOLDOWN_SEC", 180.0))


def is_market_time_now(now: Optional[dt.datetime] = None) -> bool:
    """東証の通常立会時間帯か。判定できなければ True 側 (= 監視する) に倒す。"""
    try:
        from core.startup.scheduler.market_guard import is_market_time_for_exit

        return bool(is_market_time_for_exit())
    except Exception:
        logger.debug("[PUSH STALL] market_guard 参照に失敗 -> 時刻で判定する", exc_info=True)

    now = now or dt.datetime.now()
    if now.weekday() >= 5:
        return False
    hhmm = now.hour * 100 + now.minute
    return (900 <= hhmm <= 1130) or (1230 <= hhmm <= 1530)


@dataclass
class StallStatus:
    state: str = STATE_OK
    since_last_tick_sec: float = 0.0
    subscribed_symbols: Optional[int] = None
    ws_connected: Optional[bool] = None
    reconnect_requested: bool = False
    reconnect_suppressed_reason: str = ""
    detail: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_stalled(self) -> bool:
        return self.state in (STATE_STALL, STATE_NO_SUBSCRIPTION)


class PushStallGuard:
    """PUSH 停滞の検知と、1 回だけの再接続要求。"""

    def __init__(self, *, clock: Optional[Callable[[], float]] = None) -> None:
        self._lock = threading.RLock()
        self._clock = clock or self._default_clock
        self._last_tick_at: Optional[float] = None
        self._last_reconnect_at: float = -1e9
        self._reconnect_requests = 0
        self._suppressed_requests = 0
        self._stall_episodes = 0
        self._in_stall = False

    @staticmethod
    def _default_clock() -> float:
        import time

        return time.monotonic()

    # --------------------------------------------------------

    def note_tick(self, count: int = 1) -> None:
        """新規行を受け取ったときに呼ぶ。"""
        if count <= 0:
            return
        with self._lock:
            was_stall = self._in_stall
            self._last_tick_at = self._clock()
            self._in_stall = False
        if was_stall:
            logger.warning(
                "[PUSH STALL] PUSH の新規行が再開した (stall 解除) episodes=%d version=%s",
                self._stall_episodes, VERSION,
            )

    def seconds_since_last_tick(self) -> float:
        with self._lock:
            if self._last_tick_at is None:
                return 0.0
            return max(0.0, self._clock() - self._last_tick_at)

    def mark_started(self) -> None:
        with self._lock:
            if self._last_tick_at is None:
                self._last_tick_at = self._clock()

    # --------------------------------------------------------

    def _subscribed_symbol_count(self) -> Optional[int]:
        try:
            from global_state import global_data

            for name in ("push_registered_symbols", "register_symbols", "push_symbols"):
                syms = getattr(global_data, name, None)
                if syms:
                    return len(list(syms))
            return 0
        except Exception:
            logger.debug("[PUSH STALL] 購読銘柄数を取れない", exc_info=True)
            return None

    def _ws_connected(self) -> Optional[bool]:
        try:
            from trading.push.push_stream import monitor as push_monitor

            snap = push_monitor.ws_snapshot()
            if isinstance(snap, dict):
                return bool(snap.get("connected")) and bool(snap.get("ws_alive"))
        except Exception:
            logger.debug("[PUSH STALL] WS 状態を取れない", exc_info=True)
        return None

    # --------------------------------------------------------

    def check(
        self,
        *,
        now: Optional[dt.datetime] = None,
        request_reconnect: Optional[Callable[[], bool]] = None,
    ) -> StallStatus:
        """停滞判定を 1 回行う。必要なら再接続を 1 回だけ要求する。"""
        idle = self.seconds_since_last_tick()
        st = StallStatus(since_last_tick_sec=idle)

        if idle < stall_threshold_sec():
            st.state = STATE_OK
            return st

        if not is_market_time_now(now):
            st.state = STATE_MARKET_CLOSED
            st.detail["reason"] = "市場時間外なので配信が無いのは正常"
            return st

        subscribed = self._subscribed_symbol_count()
        connected = self._ws_connected()
        st.subscribed_symbols = subscribed
        st.ws_connected = connected

        with self._lock:
            if not self._in_stall:
                self._in_stall = True
                self._stall_episodes += 1

        if subscribed == 0:
            # 再接続しても直らない。購読を作り直す側の問題。
            st.state = STATE_NO_SUBSCRIPTION
            st.reconnect_suppressed_reason = "subscribed_symbols=0 (再接続では直らない)"
            logger.error(
                "[PUSH STALL] 新規 tick が %.0fs 無く、購読銘柄が 0 件 "
                "(再接続ではなく購読の作り直しが要る) ws_connected=%s version=%s",
                idle, connected, VERSION,
            )
            return st

        st.state = STATE_STALL

        now_mono = self._clock()
        with self._lock:
            since_reconnect = now_mono - self._last_reconnect_at
            allowed = since_reconnect >= reconnect_cooldown_sec()
            if allowed:
                self._last_reconnect_at = now_mono
                self._reconnect_requests += 1
            else:
                self._suppressed_requests += 1

        if not allowed:
            st.reconnect_suppressed_reason = (
                "cooldown %.0fs 中 (連打しない。残り %.0fs)"
                % (reconnect_cooldown_sec(), max(0.0, reconnect_cooldown_sec() - since_reconnect))
            )
            logger.warning(
                "[PUSH STALL] 停滞中だが再接続要求は出さない idle=%.0fs %s version=%s",
                idle, st.reconnect_suppressed_reason, VERSION,
            )
            return st

        if request_reconnect is None:
            st.reconnect_suppressed_reason = "この プロセスは PUSH を所有していない (再接続要求先が無い)"
            logger.error(
                "[PUSH STALL] PUSH の新規 tick が %.0fs 止まっている "
                "subscribed=%s ws_connected=%s -> %s version=%s",
                idle, subscribed, connected, st.reconnect_suppressed_reason, VERSION,
            )
            return st

        logger.error(
            "[PUSH STALL] PUSH の新規 tick が %.0fs 止まっている subscribed=%s ws_connected=%s "
            "-> 安全な再接続を 1 回だけ要求する (cooldown=%.0fs) version=%s",
            idle, subscribed, connected, reconnect_cooldown_sec(), VERSION,
        )
        try:
            ok = bool(request_reconnect())
        except Exception:
            logger.exception("[PUSH STALL] 再接続要求が失敗した")
            ok = False
        st.reconnect_requested = ok
        if not ok:
            st.reconnect_suppressed_reason = "再接続要求が失敗した"
        return st

    # --------------------------------------------------------

    def data_is_stale(self, data_time: Any, *, now: Optional[dt.datetime] = None) -> bool:
        """その足を Summary-AI / Tonosama へ渡してよいか。

        True なら stale。判定できないときは False (従来どおり通す) にせず、
        「時刻が読めない」= 判断材料が無いので True (渡さない) に倒す。
        """
        if not _env_bool("PUSH_STALE_GATE_ENABLED", True):
            return False
        try:
            import pandas as pd

            t = pd.to_datetime(data_time, errors="coerce")
            if t is None or pd.isna(t):
                return True
            if getattr(t, "tzinfo", None) is not None:
                t = t.tz_localize(None) if t.tz is None else t.tz_convert(None).tz_localize(None)
            ref = now or dt.datetime.now()
            age = (ref - t.to_pydatetime()).total_seconds()
        except Exception:
            logger.debug("[PUSH STALL] stale 判定に失敗 -> stale 扱い", exc_info=True)
            return True
        return age > stale_data_max_age_sec()

    def stats(self) -> dict:
        with self._lock:
            return {
                "since_last_tick_sec": self.seconds_since_last_tick(),
                "stall_episodes": self._stall_episodes,
                "reconnect_requests": self._reconnect_requests,
                "suppressed_requests": self._suppressed_requests,
                "in_stall": self._in_stall,
                "stall_threshold_sec": stall_threshold_sec(),
                "reconnect_cooldown_sec": reconnect_cooldown_sec(),
                "stale_data_max_age_sec": stale_data_max_age_sec(),
            }

    def reset_for_test(self, *, clock: Optional[Callable[[], float]] = None) -> None:
        with self._lock:
            if clock is not None:
                self._clock = clock
            self._last_tick_at = None
            self._last_reconnect_at = -1e9
            self._reconnect_requests = 0
            self._suppressed_requests = 0
            self._stall_episodes = 0
            self._in_stall = False


# ============================================================
# MODULE-LEVEL
# ============================================================

_guard = PushStallGuard()


def note_tick(count: int = 1) -> None:
    _guard.note_tick(count)


def mark_started() -> None:
    _guard.mark_started()


def check(**kwargs) -> StallStatus:
    return _guard.check(**kwargs)


def data_is_stale(data_time: Any, *, now: Optional[dt.datetime] = None) -> bool:
    return _guard.data_is_stale(data_time, now=now)


def seconds_since_last_tick() -> float:
    return _guard.seconds_since_last_tick()


def stats() -> dict:
    return _guard.stats()


def reset_for_test(*, clock: Optional[Callable[[], float]] = None) -> None:
    _guard.reset_for_test(clock=clock)


__all__ = [
    "VERSION",
    "STATE_OK", "STATE_STALL", "STATE_MARKET_CLOSED", "STATE_NO_SUBSCRIPTION",
    "StallStatus", "PushStallGuard",
    "note_tick", "mark_started", "check", "data_is_stale",
    "seconds_since_last_tick", "stats", "reset_for_test",
    "stall_threshold_sec", "stale_data_max_age_sec", "reconnect_cooldown_sec",
    "is_market_time_now",
]
