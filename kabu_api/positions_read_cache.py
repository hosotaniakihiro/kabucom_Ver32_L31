# ============================================================
# File   : kabu_api/positions_read_cache.py
# Version: V1.0-POSITIONS-READ-SINGLEFLIGHT
# ------------------------------------------------------------
# GET /kabusapi/positions の read owner を 1 本にする。
#
# 背景 (2026-09-01 12:53-12:55 のログ):
#   同一プロセス内に /positions を叩く経路が 2 系統あった。
#
#     trading.position.kabu_position_reader   urllib / product=2 / timeout 3s
#         -> PositionStateService から呼ばれる (キャッシュ有り)
#         -> exit_utils._read_broker_open_positions_safe からも直接呼ばれる
#            (キャッシュを通らない)
#     kabu_api.positions.get_positions        requests / product 指定なし
#         -> timeout 10s x 3 回リトライ = 1 呼び出しで最大 31 秒ブロック
#         -> core.position_sync / kabus_position_sync / trade_logic /
#            daytrade_credit から呼ばれる
#
#   localhost:18080 が無応答になったとき、この 2 系統が並行して
#   「読めない相手へ読みに行く」ため、
#     [KABU POSITION READER] request failed err=timed out   55 件
#     kabu_api.positions       get_positions timeout retry   多数
#   となり、待ち時間そのものが kabu Station の同時接続を占有していた。
#
# このモジュールの責務:
#   1. 同じクエリ (key) への同時要求を singleflight で 1 本にまとめる
#   2. 成功応答を TTL の間だけ共有する (重複 GET を消す)
#   3. 連続失敗中は指数バックオフして GET を打たない (即時 skip)
#      バックオフ明けの 1 回が復旧確認 probe を兼ねる
#   4. 呼び出し元 / キャッシュ利用 / 実リクエスト回数を診断ログに残す
#
# 安全規則 (絶対):
#   - ここは GET 専用。発注 / 取消 / 返済は通さない。
#   - 失敗時に「建玉 0 件」を返さない。ok=False で返し、
#     last-known-good は age 付きで別に渡す。
#     どちらを使うかは呼び出し側 (PositionStateService) が決める。
#   - 429 でトークンを取り直さない。429 は認証エラーではない。
#   - 間隔・TTL・timeout・backoff の数値は kabu_api.rest_limits が持つ。
#     このモジュールに数値を書かない。
# ============================================================

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Tuple

from kabu_api import rest_limits

logger = logging.getLogger(__name__)

SOURCE_FETCH = "fetch"
SOURCE_CACHE = "cache"
SOURCE_BACKOFF = "backoff_skip"
SOURCE_ERROR = "error"

# 既定のクエリキー
KEY_ALL = "all"          # product 指定なし (現物+信用)
KEY_CREDIT = "product2"  # product=2 (信用のみ)


@dataclass
class PositionsRead:
    """/positions の読み取り結果。

    ok=False は「建玉が無い」ではなく「読めていない」。
    caller はこれを 0 件と混同してはならない。
    """

    payload: Any = None
    ok: bool = False
    source: str = SOURCE_ERROR
    error: str = ""
    age_sec: float = 0.0
    # 失敗時でも「最後に読めた内容」を渡す。使うかは呼び出し側が決める。
    last_good: Any = None
    last_good_age_sec: float = float("inf")


@dataclass
class _Entry:
    lock: threading.RLock = field(default_factory=threading.RLock)
    inflight: threading.Lock = field(default_factory=threading.Lock)
    payload: Any = None
    payload_at: float = 0.0
    last_good: Any = None
    last_good_at: float = 0.0
    fail_streak: int = 0
    next_try_at: float = 0.0
    last_error: str = ""
    fetches: int = 0
    fetch_errors: int = 0
    cache_hits: int = 0
    backoff_skips: int = 0
    last_backoff_log_at: float = 0.0
    callers: Dict[str, int] = field(default_factory=dict)


class PositionsReadCache:
    """/positions の唯一の GET owner。"""

    def __init__(self) -> None:
        self._map_lock = threading.RLock()
        self._entries: Dict[str, _Entry] = {}
        self._last_stats_log_at: float = 0.0

    # --------------------------------------------------------

    def _entry(self, key: str) -> _Entry:
        with self._map_lock:
            e = self._entries.get(key)
            if e is None:
                e = _Entry()
                self._entries[key] = e
            return e

    @staticmethod
    def _refetch_window() -> float:
        """この秒数の間は実 GET を出さない。

        TTL と最短呼び出し間隔の大きい方。両方 rest_limits が持つ。
        """
        p = rest_limits.policy("positions")
        return max(float(p.cache_ttl_sec), float(p.min_interval_sec))

    @staticmethod
    def _backoff_schedule() -> Tuple[float, ...]:
        return rest_limits.policy("positions").backoff_sec

    # --------------------------------------------------------

    def invalidate(self, *, key: Optional[str] = None, reason: str = "") -> None:
        """建玉が変わった直後 (約定 / 返済) に呼ぶ。次回は必ず取り直す。"""
        with self._map_lock:
            keys = [key] if key else list(self._entries.keys())
        for k in keys:
            e = self._entry(k)
            with e.lock:
                e.payload = None
                e.payload_at = 0.0
        if reason:
            logger.debug("[POSITIONS READ] cache invalidated key=%s reason=%s", key or "*", reason)

    def reset_for_test(self) -> None:
        with self._map_lock:
            self._entries.clear()
            self._last_stats_log_at = 0.0

    def stats(self, *, key: Optional[str] = None) -> dict:
        with self._map_lock:
            keys = [key] if key else list(self._entries.keys())
        out: Dict[str, Any] = {}
        for k in keys:
            e = self._entry(k)
            with e.lock:
                out[k] = {
                    "fetches": e.fetches,
                    "fetch_errors": e.fetch_errors,
                    "cache_hits": e.cache_hits,
                    "backoff_skips": e.backoff_skips,
                    "fail_streak": e.fail_streak,
                    "last_error": e.last_error[:160],
                    "callers": dict(e.callers),
                }
        out["_policy"] = rest_limits.policy("positions").as_log_dict()
        return out

    def log_stats(self, *, min_interval_sec: float = 60.0) -> None:
        """呼び出し元別の実リクエスト数を定期的に 1 行で残す。"""
        now = time.monotonic()
        with self._map_lock:
            if (now - self._last_stats_log_at) < float(min_interval_sec):
                return
            self._last_stats_log_at = now
        logger.warning(
            "[POSITIONS READ STATS] %s "
            "(fetches=実 GET 回数 / cache_hits=共有キャッシュで消えた重複 / callers=呼び出し元)",
            self.stats(),
        )

    # --------------------------------------------------------

    def _cached(self, e: _Entry, now: float) -> Optional[PositionsRead]:
        if e.payload is None:
            return None
        age = now - e.payload_at
        if age >= self._refetch_window():
            return None
        e.cache_hits += 1
        return PositionsRead(
            payload=e.payload, ok=True, source=SOURCE_CACHE, age_sec=age,
            last_good=e.last_good,
            last_good_age_sec=(float("inf") if e.last_good_at <= 0 else now - e.last_good_at),
        )

    def _note_ok(self, e: _Entry, payload: Any, now: float) -> None:
        with e.lock:
            e.payload = payload
            e.payload_at = now
            e.last_good = payload
            e.last_good_at = now
            streak = e.fail_streak
            e.fail_streak = 0
            e.next_try_at = 0.0
            e.last_error = ""
        if streak:
            logger.warning("[POSITIONS READ] /positions recovered fail_streak=%d", streak)

    def _note_error(self, e: _Entry, err: str, now: float) -> None:
        schedule = self._backoff_schedule()
        with e.lock:
            e.fetch_errors += 1
            e.last_error = str(err or "")
            idx = min(e.fail_streak, len(schedule) - 1)
            wait = schedule[idx]
            e.fail_streak += 1
            e.next_try_at = now + wait
            # 失敗時に古い応答を「現在値」として配らない。
            # last_good は age 付きで別に渡す。
            e.payload = None
            e.payload_at = 0.0
            streak = e.fail_streak
            last_err = e.last_error
        logger.warning(
            "[POSITIONS READ] /positions read failed streak=%d next_retry_in=%.1fs err=%s "
            "(即時リトライしない: kabu Station REST への同時接続を増やさない)",
            streak, wait, last_err[:160],
        )

    def _backoff_result(self, e: _Entry, now: float) -> PositionsRead:
        with e.lock:
            e.backoff_skips += 1
            wait = max(0.0, e.next_try_at - now)
            should_log = (now - e.last_backoff_log_at) >= 5.0
            if should_log:
                e.last_backoff_log_at = now
            streak = e.fail_streak
            last_err = e.last_error
            last_good = e.last_good
            last_good_age = float("inf") if e.last_good_at <= 0 else now - e.last_good_at
        if should_log:
            logger.warning(
                "[POSITIONS READ] /positions backoff 中のため GET を skip retry_in=%.1fs streak=%d last_err=%s",
                wait, streak, last_err[:120],
            )
        return PositionsRead(
            payload=None, ok=False, source=SOURCE_BACKOFF,
            error="backoff (retry in %.1fs): %s" % (wait, last_err),
            last_good=last_good, last_good_age_sec=last_good_age,
        )

    def _read_allowed(self, e: _Entry, now: float) -> bool:
        with e.lock:
            return now >= e.next_try_at

    # --------------------------------------------------------

    def get(
        self,
        fetch: Callable[[], Tuple[Any, bool, str]],
        *,
        key: str = KEY_ALL,
        caller: str = "",
        force: bool = False,
    ) -> PositionsRead:
        """fetch() は (payload, ok, error) を返すこと。

        同じ key に同時に来た caller は 1 本の実 GET を共有する。
        """
        key = str(key or KEY_ALL)
        e = self._entry(key)
        with e.lock:
            e.callers[caller or "-"] = e.callers.get(caller or "-", 0) + 1

        now = time.monotonic()
        if not force:
            with e.lock:
                hit = self._cached(e, now)
            if hit is not None:
                return hit

        # singleflight: 先行 fetch が居るなら、その結果を待って共有する
        if not e.inflight.acquire(blocking=False):
            e.inflight.acquire()
            try:
                now = time.monotonic()
                with e.lock:
                    hit = self._cached(e, now)
                if hit is not None:
                    return hit
                if not self._read_allowed(e, now):
                    return self._backoff_result(e, now)
            finally:
                e.inflight.release()
            # 先行 fetch が失敗していた。同じ相手へ続けて GET しない。
            with e.lock:
                last_err = e.last_error or "concurrent read failed"
                last_good = e.last_good
                last_good_age = float("inf") if e.last_good_at <= 0 else time.monotonic() - e.last_good_at
            return PositionsRead(
                payload=None, ok=False, source=SOURCE_ERROR, error=last_err,
                last_good=last_good, last_good_age_sec=last_good_age,
            )

        try:
            now = time.monotonic()
            with e.lock:
                hit = self._cached(e, now)
            if hit is not None and not force:
                return hit

            if not self._read_allowed(e, now):
                return self._backoff_result(e, now)

            with e.lock:
                e.fetches += 1
            try:
                payload, ok, error = fetch()
            except Exception as ex:  # noqa: BLE001
                self._note_error(e, "%s: %s" % (type(ex).__name__, ex), time.monotonic())
                with e.lock:
                    last_good, lg_at = e.last_good, e.last_good_at
                return PositionsRead(
                    payload=None, ok=False, source=SOURCE_ERROR, error=str(ex),
                    last_good=last_good,
                    last_good_age_sec=(float("inf") if lg_at <= 0 else time.monotonic() - lg_at),
                )

            if not ok:
                self._note_error(e, str(error or "fetch returned ok=False"), time.monotonic())
                with e.lock:
                    last_good, lg_at = e.last_good, e.last_good_at
                return PositionsRead(
                    payload=None, ok=False, source=SOURCE_ERROR, error=str(error or ""),
                    last_good=last_good,
                    last_good_age_sec=(float("inf") if lg_at <= 0 else time.monotonic() - lg_at),
                )

            self._note_ok(e, payload, time.monotonic())
            logger.debug("[POSITIONS READ] /positions fetched key=%s caller=%s", key, caller or "-")
            return PositionsRead(payload=payload, ok=True, source=SOURCE_FETCH, age_sec=0.0,
                                 last_good=payload, last_good_age_sec=0.0)
        finally:
            try:
                e.inflight.release()
            except RuntimeError:
                logger.debug("[POSITIONS READ] inflight release skipped", exc_info=True)


# ============================================================
# MODULE-LEVEL API
# ============================================================

_cache = PositionsReadCache()


def get_positions_cached(
    fetch: Callable[[], Tuple[Any, bool, str]],
    *,
    key: str = KEY_ALL,
    caller: str = "",
    force: bool = False,
) -> PositionsRead:
    return _cache.get(fetch, key=key, caller=caller, force=force)


def invalidate(*, key: Optional[str] = None, reason: str = "") -> None:
    _cache.invalidate(key=key, reason=reason)


def stats(*, key: Optional[str] = None) -> dict:
    return _cache.stats(key=key)


def log_stats(*, min_interval_sec: float = 60.0) -> None:
    _cache.log_stats(min_interval_sec=min_interval_sec)


def reset_for_test() -> None:
    _cache.reset_for_test()


__all__ = [
    "PositionsRead",
    "PositionsReadCache",
    "get_positions_cached",
    "invalidate",
    "stats",
    "log_stats",
    "reset_for_test",
    "KEY_ALL",
    "KEY_CREDIT",
    "SOURCE_FETCH",
    "SOURCE_CACHE",
    "SOURCE_BACKOFF",
    "SOURCE_ERROR",
]
