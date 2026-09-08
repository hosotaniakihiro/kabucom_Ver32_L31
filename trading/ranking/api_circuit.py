# ============================================================
# File   : trading/ranking/api_circuit.py
# Version: V1.0-RANKING-API-CIRCUIT-BREAKER
# ------------------------------------------------------------
# ranking API (GET /kabusapi/ranking) のサーキットブレーカー。
#
# 背景 (2026-09-01 のログ):
#   localhost:18080 が応答しなくなった後も、ranking collector は
#   1 サイクル 28 呼び出しを最後まで実行し続けていた。
#     1 区分 = 5.0s timeout x 2 回 + retry sleep = 約 10.5s
#     28 呼び出し = 約 305s
#   これが数分おきに繰り返され、api_success=0 のまま延々と
#   kabu Station へ接続を張り続けて障害を増幅していた。
#
# このモジュールの責務:
#   1. サイクル内で連続失敗が閾値に達したら、そのサイクルの残りを
#      fail-fast で打ち切る (allow_call() が False を返す)
#   2. 打ち切ったら指数バックオフでブレーカーを開く。
#      開いている間、次サイクルは即時 skip される
#   3. バックオフ期限が来たら「軽い probe」を 1 回だけ許可する。
#      probe が通ればブレーカーを閉じて通常サイクルへ戻る
#
# 安全規則:
#   - これは ranking の読み取り専用。発注経路には一切関与しない。
#   - 復旧確認 probe を必ず残す (永久に止まらないこと)。
# ============================================================

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Tuple

logger = logging.getLogger(__name__)


# ============================================================
# ENV
# ============================================================

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


def _fail_fast_threshold() -> int:
    """サイクル内でこの回数だけ連続失敗したら残りを打ち切る。"""
    return max(1, _env_int("RANKING_API_FAIL_FAST_STREAK", 3))


def _backoff_schedule() -> Tuple[float, ...]:
    raw = os.getenv("RANKING_API_BREAKER_BACKOFF_SEC", "60,120,300,600")
    out = []
    for part in str(raw).split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(max(1.0, float(part)))
        except Exception:
            continue
    return tuple(out) if out else (60.0, 120.0, 300.0, 600.0)


def _probe_timeout_sec() -> float:
    """復旧確認 probe の timeout。通常サイクルより短くする。"""
    return max(0.5, _env_float("RANKING_API_PROBE_TIMEOUT_SEC", 2.0))


# ============================================================
# STATE
# ============================================================

DECISION_RUN = "run"              # 通常サイクルを実行してよい
DECISION_PROBE = "probe"          # 復旧確認の 1 呼び出しだけ許可
DECISION_SKIP = "skip"            # ブレーカーが開いている。即時 skip


@dataclass
class CycleDecision:
    decision: str
    reason: str = ""
    retry_in_sec: float = 0.0
    probe_timeout_sec: float = 0.0

    @property
    def should_skip(self) -> bool:
        return self.decision == DECISION_SKIP

    @property
    def is_probe(self) -> bool:
        return self.decision == DECISION_PROBE


def _rate_limit_cooldown_sec() -> float:
    """429 を受けたときの待機。指数バックオフとは別枠の短い待ち。"""
    return max(1.0, _env_float("RANKING_API_RATE_LIMIT_COOLDOWN_SEC", 10.0))


class RankingApiCircuit:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._open_until: float = 0.0
        self._open_streak: int = 0
        self._last_error: str = ""
        self._cycle_fail_streak: int = 0
        self._cycle_aborted: bool = False
        self._skips: int = 0
        self._probes: int = 0
        self._opens: int = 0

    # --------------------------------------------------------
    # cycle lifecycle
    # --------------------------------------------------------

    def begin_cycle(self) -> CycleDecision:
        """サイクル開始時に呼ぶ。"""
        with self._lock:
            self._cycle_fail_streak = 0
            self._cycle_aborted = False

            now = time.monotonic()
            if self._open_until <= 0.0:
                return CycleDecision(DECISION_RUN)

            if now < self._open_until:
                self._skips += 1
                return CycleDecision(
                    DECISION_SKIP,
                    reason=self._last_error or "breaker open",
                    retry_in_sec=self._open_until - now,
                )

            # バックオフ期限到来 -> probe を 1 回だけ許可する
            self._probes += 1
            return CycleDecision(
                DECISION_PROBE,
                reason=self._last_error or "breaker probe",
                probe_timeout_sec=_probe_timeout_sec(),
            )

    def allow_call(self) -> bool:
        """サイクル内の次の API 呼び出しを実行してよいか。"""
        with self._lock:
            return not self._cycle_aborted

    def note_success(self) -> None:
        with self._lock:
            self._cycle_fail_streak = 0
            if self._open_until > 0.0 or self._open_streak:
                logger.warning(
                    "[RANKING API BREAKER] closed (recovered) opens=%d skips=%d probes=%d",
                    self._opens, self._skips, self._probes,
                )
            self._open_until = 0.0
            self._open_streak = 0
            self._last_error = ""

    def note_failure(self, error: str = "") -> bool:
        """失敗を記録する。サイクルを打ち切るべきなら True。"""
        with self._lock:
            self._cycle_fail_streak += 1
            self._last_error = str(error or "")[:160]
            threshold = _fail_fast_threshold()
            if self._cycle_fail_streak < threshold or self._cycle_aborted:
                return self._cycle_aborted

            self._cycle_aborted = True
            schedule = _backoff_schedule()
            idx = min(self._open_streak, len(schedule) - 1)
            wait = schedule[idx]
            self._open_streak += 1
            self._opens += 1
            self._open_until = time.monotonic() + wait
            logger.error(
                "[RANKING API BREAKER] open fail_streak=%d threshold=%d backoff=%.0fs "
                "err=%s (このサイクルの残り呼び出しを打ち切る)",
                self._cycle_fail_streak, threshold, wait, self._last_error or "-",
            )
            return True

    def note_rate_limited(self, retry_after: float = 0.0, error: str = "") -> None:
        """429 を受けたとき。障害ではないので指数バックオフを進めない。

        2026-09-01 実測: /ranking は 0.02s で 50 行を返すが、連打すると 429 を返す。
        これを「invalid response」として note_failure/note_probe_failure に流すと
        backoff が 120 -> 300 -> 600s と伸び、レート制限だけでランキングが
        恒久停止していた。ここでは短いクールダウンだけ置く。
        """
        with self._lock:
            wait = retry_after if retry_after and retry_after > 0 else _rate_limit_cooldown_sec()
            self._last_error = ("rate limited (429) " + str(error or ""))[:160]
            # open_streak は進めない = 次の probe も通常どおり行う
            self._open_until = max(self._open_until, time.monotonic() + wait)
            self._cycle_aborted = True
            logger.warning(
                "[RANKING API BREAKER] rate limited (429) -> cooldown %.1fs のみ "
                "(障害ではないので backoff は伸ばさない) err=%s",
                wait, self._last_error,
            )

    def note_probe_failure(self, error: str = "") -> None:
        """復旧確認 probe が失敗したときに呼ぶ。

        probe は 1 呼び出しなので通常の連続失敗閾値には届かない。
        ここで必ずブレーカーを開き直さないと、期限切れのまま毎サイクル
        probe を打ち続けてしまう。バックオフは 1 段深くする。
        """
        with self._lock:
            self._last_error = str(error or "")[:160]
            self._cycle_aborted = True
            schedule = _backoff_schedule()
            idx = min(self._open_streak, len(schedule) - 1)
            wait = schedule[idx]
            self._open_streak += 1
            self._opens += 1
            self._open_until = time.monotonic() + wait
            logger.error(
                "[RANKING API BREAKER] probe failed -> reopen backoff=%.0fs err=%s",
                wait, self._last_error or "-",
            )

    # --------------------------------------------------------

    def stats(self) -> dict:
        with self._lock:
            now = time.monotonic()
            return {
                "open": self._open_until > now,
                "retry_in_sec": max(0.0, self._open_until - now),
                "open_streak": self._open_streak,
                "opens": self._opens,
                "skips": self._skips,
                "probes": self._probes,
                "cycle_fail_streak": self._cycle_fail_streak,
                "cycle_aborted": self._cycle_aborted,
                "last_error": self._last_error,
            }

    def reset_for_test(self) -> None:
        with self._lock:
            self._open_until = 0.0
            self._open_streak = 0
            self._last_error = ""
            self._cycle_fail_streak = 0
            self._cycle_aborted = False
            self._skips = 0
            self._probes = 0
            self._opens = 0


_circuit = RankingApiCircuit()


def begin_cycle() -> CycleDecision:
    return _circuit.begin_cycle()


def allow_call() -> bool:
    return _circuit.allow_call()


def note_success() -> None:
    _circuit.note_success()


def note_failure(error: str = "") -> bool:
    return _circuit.note_failure(error)


def note_rate_limited(retry_after: float = 0.0, error: str = "") -> None:
    _circuit.note_rate_limited(retry_after, error)


def note_probe_failure(error: str = "") -> None:
    _circuit.note_probe_failure(error)


def stats() -> dict:
    return _circuit.stats()


def reset_for_test() -> None:
    _circuit.reset_for_test()


__all__ = [
    "CycleDecision",
    "RankingApiCircuit",
    "DECISION_RUN",
    "DECISION_PROBE",
    "DECISION_SKIP",
    "begin_cycle",
    "allow_call",
    "note_success",
    "note_failure",
    "note_probe_failure",
    "stats",
    "reset_for_test",
]
