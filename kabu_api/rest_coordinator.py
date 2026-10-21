# ============================================================
# File   : kabu_api/rest_coordinator.py
# Version: V1-KABU-REST-PRIORITY-COORDINATOR
# ------------------------------------------------------------
# kabu Station REST API の優先度つき同時実行制御。
#
# 背景 (2026-09-01 12:56-13:00 の実測):
#   ranking collector は 1 サイクル 28 呼び出しを行い、
#   1 呼び出しあたり 5.0s timeout x 2 回 = 約 10.5s かかっていた。
#   kabu Station が詰まると 28 呼び出し = 約 305 秒間、
#   低優先度の ranking がひたすら接続を張り続け、その裏で
#     PUSH register/unregister が timeout
#     -> 登録 0 件 -> WebSocket 切断 -> Summary 空 -> ranking 候補も空
#   という循環障害になっていた。
#
# ここでは全エンドポイントを 1 つのゲート経由にし、
# 逼迫時は「低優先度から先に止める」ことで循環を切る。
#
# 優先度 (小さいほど高い):
#   0 ORDER     発注・取消・ポジション確認   … 止めない
#   1 REGISTER  PUSH register/unregister    … 止めない
#   2 BOARD     板・価格取得
#   3 RANKING   ranking 取得                … 最初に止める
#
# 安全規則:
#   * ORDER と REGISTER は degraded でも決してブロックしない。
#     資金や建玉に関わる操作を、監視都合で止めてはいけない。
#   * ここは「待たせる／諦めさせる」だけで、リトライ判断は呼び出し側。
#   * 全体を止めないため、degraded にも必ず期限を設ける。
# ============================================================

from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import contextmanager
from typing import Optional

logger = logging.getLogger(__name__)

VERSION = "V1-KABU-REST-PRIORITY-COORDINATOR"

PRIO_ORDER = 0
PRIO_REGISTER = 1
PRIO_BOARD = 2
PRIO_RANKING = 3

_PRIO_NAMES = {
    PRIO_ORDER: "ORDER",
    PRIO_REGISTER: "REGISTER",
    PRIO_BOARD: "BOARD",
    PRIO_RANKING: "RANKING",
}

# degraded でも止めない優先度
_NEVER_BLOCKED = (PRIO_ORDER, PRIO_REGISTER)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).strip())
    except Exception:
        return float(default)


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env_float(name, float(default)))
    except Exception:
        return int(default)


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return bool(default)
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}


class RestBusy(Exception):
    """低優先度の呼び出しが、逼迫のため実行を許可されなかった。"""

    def __init__(self, priority: int, reason: str, retry_in_sec: float = 0.0):
        super().__init__(
            "kabu REST busy priority=%s reason=%s" % (_PRIO_NAMES.get(priority, priority), reason)
        )
        self.priority = priority
        self.reason = reason
        self.retry_in_sec = float(retry_in_sec)


class RestCoordinator:
    def __init__(self) -> None:
        self._cv = threading.Condition(threading.RLock())
        self._inflight: dict = {}
        self._degraded_until: float = 0.0
        self._degraded_reason: str = ""
        self._consecutive_timeouts: int = 0
        self._stats: dict = {}

    # --------------------------------------------------------
    # degraded 管理
    # --------------------------------------------------------

    def note_timeout(self, priority: int, reason: str = "") -> None:
        """timeout / 429 を記録する。閾値を超えたら低優先度を止める。"""
        with self._cv:
            self._consecutive_timeouts += 1
            threshold = max(1, _env_int("KABU_REST_DEGRADE_AFTER_TIMEOUTS", 2))
            if self._consecutive_timeouts < threshold:
                return
            hold = max(1.0, _env_float("KABU_REST_DEGRADE_HOLD_SEC", 60.0))
            self._degraded_until = time.monotonic() + hold
            self._degraded_reason = str(reason or "consecutive timeouts")[:160]
            logger.error(
                "[REST COORD] degraded: consecutive_timeouts=%d threshold=%d hold=%.0fs reason=%s "
                "(低優先度の RANKING/BOARD を止めて ORDER/REGISTER を通す)",
                self._consecutive_timeouts, threshold, hold, self._degraded_reason,
            )
            self._cv.notify_all()

    def note_success(self, priority: int) -> None:
        with self._cv:
            if not self._consecutive_timeouts and not self._degraded_until:
                return
            was_degraded = self._degraded_until > time.monotonic()
            self._consecutive_timeouts = 0
            self._degraded_until = 0.0
            self._degraded_reason = ""
            if was_degraded:
                logger.warning("[REST COORD] recovered: degraded 解除 (成功応答を確認)")
            self._cv.notify_all()

    def is_degraded(self) -> bool:
        with self._cv:
            return self._degraded_until > time.monotonic()

    def degraded_retry_in(self) -> float:
        with self._cv:
            return max(0.0, self._degraded_until - time.monotonic())

    # --------------------------------------------------------
    # ゲート
    # --------------------------------------------------------

    def _higher_inflight(self, priority: int) -> int:
        return sum(n for p, n in self._inflight.items() if p < priority and n > 0)

    @contextmanager
    def acquire(self, priority: int, *, label: str = "", timeout: Optional[float] = None):
        """優先度つきで REST 呼び出し区間に入る。

        ORDER / REGISTER は常に即時通す。
        BOARD / RANKING は
          - degraded 中なら RestBusy
          - 上位優先度が実行中なら待つ (待ち上限を超えたら RestBusy)
        """
        if not _env_bool("KABU_REST_COORDINATOR_ENABLED", True):
            yield
            return

        name = _PRIO_NAMES.get(priority, str(priority))
        waited = 0.0

        if priority not in _NEVER_BLOCKED:
            if self.is_degraded():
                retry_in = self.degraded_retry_in()
                self._bump(name, "refused_degraded")
                raise RestBusy(priority, self._degraded_reason or "degraded", retry_in)

            wait_limit = timeout if timeout is not None else _env_float("KABU_REST_LOW_PRIO_WAIT_SEC", 3.0)
            started = time.monotonic()
            with self._cv:
                while self._higher_inflight(priority) > 0:
                    remain = wait_limit - (time.monotonic() - started)
                    if remain <= 0:
                        waited = time.monotonic() - started
                        self._bump(name, "refused_busy", waited)
                        raise RestBusy(priority, "higher priority in flight", 0.0)
                    self._cv.wait(min(0.2, remain))
            waited = time.monotonic() - started

        with self._cv:
            self._inflight[priority] = self._inflight.get(priority, 0) + 1
        self._bump(name, "ran", waited)
        try:
            yield
        finally:
            with self._cv:
                self._inflight[priority] = max(0, self._inflight.get(priority, 0) - 1)
                self._cv.notify_all()

    # --------------------------------------------------------
    # 集計
    # --------------------------------------------------------

    def _bump(self, name: str, outcome: str, waited: float = 0.0) -> None:
        with self._cv:
            row = self._stats.setdefault(
                name, {"ran": 0, "refused_degraded": 0, "refused_busy": 0, "wait_sec": 0.0}
            )
            if outcome in row:
                row[outcome] += 1
            row["wait_sec"] += float(waited or 0.0)

    def stats(self) -> dict:
        with self._cv:
            return {
                "degraded": self._degraded_until > time.monotonic(),
                "degraded_reason": self._degraded_reason,
                "retry_in_sec": max(0.0, self._degraded_until - time.monotonic()),
                "consecutive_timeouts": self._consecutive_timeouts,
                "inflight": {_PRIO_NAMES.get(p, p): n for p, n in self._inflight.items() if n},
                "by_priority": {k: dict(v) for k, v in self._stats.items()},
            }

    def log_stats(self, context: str = "") -> dict:
        snap = self.stats()
        logger.warning(
            "[REST COORD STATS] context=%s degraded=%s retry_in=%.0fs consecutive_timeouts=%s "
            "inflight=%s by_priority=%s",
            context or "-", snap["degraded"], snap["retry_in_sec"],
            snap["consecutive_timeouts"], snap["inflight"], snap["by_priority"],
        )
        return snap

    def reset_for_test(self) -> None:
        with self._cv:
            self._inflight = {}
            self._degraded_until = 0.0
            self._degraded_reason = ""
            self._consecutive_timeouts = 0
            self._stats = {}


_coordinator = RestCoordinator()


def acquire(priority: int, *, label: str = "", timeout: Optional[float] = None):
    return _coordinator.acquire(priority, label=label, timeout=timeout)


def note_timeout(priority: int, reason: str = "") -> None:
    _coordinator.note_timeout(priority, reason)


def note_success(priority: int) -> None:
    _coordinator.note_success(priority)


def is_degraded() -> bool:
    return _coordinator.is_degraded()


def degraded_retry_in() -> float:
    return _coordinator.degraded_retry_in()


def stats() -> dict:
    return _coordinator.stats()


def log_stats(context: str = "") -> dict:
    return _coordinator.log_stats(context)


def reset_for_test() -> None:
    _coordinator.reset_for_test()


__all__ = [
    "VERSION",
    "PRIO_ORDER",
    "PRIO_REGISTER",
    "PRIO_BOARD",
    "PRIO_RANKING",
    "RestBusy",
    "RestCoordinator",
    "acquire",
    "note_timeout",
    "note_success",
    "is_degraded",
    "degraded_retry_in",
    "stats",
    "log_stats",
    "reset_for_test",
]
