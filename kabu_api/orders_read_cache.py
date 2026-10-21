# ============================================================
# File   : kabu_api/orders_read_cache.py
# Version: V1.0-ORDERS-READ-SINGLEFLIGHT
# ------------------------------------------------------------
# GET /kabusapi/orders の read owner を 1 本にする。
#
# 背景 (2026-09-01 のログ):
#   localhost:18080 の読み取り API が終日応答しなくなり、
#     trading.handlers.pending_monitor.get_orders   5 秒 timeout を反復
#     force_cancel_loop.get_orders                  5 秒 timeout を反復
#   が同一プロセス内から重複して /orders を叩き続けていた。
#   本ログだけで /orders 失敗が 1000 件超。読めない相手に読みに行く
#   こと自体が kabu Station の同時接続を占有し、障害を増幅していた。
#
# このモジュールの責務:
#   1. 成功した /orders 応答を短い TTL で共有する (重複 GET を消す)
#   2. singleflight: 同時に来た caller は 1 本の実 GET を共有する
#   3. 連続失敗中は指数バックオフして GET を打たない (即時 skip)
#      バックオフ明けの 1 回が復旧確認 probe を兼ねる
#
# 安全規則 (絶対):
#   - ここは GET 専用。発注 / 取消 / 返済の POST・PUT・DELETE は通さない。
#     注文送信の優先経路をこのバックオフで塞いではならない。
#   - 失敗時に古いキャッシュを返さない。
#     古い注文状態でキャンセル判断をすると、約定済みの注文を
#     取消しに行くなどの誤動作になる。失敗は失敗として ok=False を返す。
#   - 注文を出した / 取り消した直後は invalidate() でキャッシュを捨てる。
# ============================================================

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, List, Tuple

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


def _cache_ttl() -> float:
    """この秒数の間は /orders を叩かず、共有キャッシュを返す。

    TTL と「最短呼び出し間隔」の大きい方を実効値にする。
    どちらの数値も kabu_api.rest_limits が唯一の持ち主で、
    このモジュールにも force_cancel_loop にも数値を書かない。
    """
    from kabu_api import rest_limits

    p = rest_limits.policy("orders")
    return max(0.2, float(p.cache_ttl_sec), float(p.min_interval_sec))


def _backoff_schedule() -> Tuple[float, ...]:
    from kabu_api import rest_limits

    return rest_limits.policy("orders").backoff_sec


# ============================================================
# RESULT
# ============================================================

SOURCE_FETCH = "fetch"
SOURCE_CACHE = "cache"
SOURCE_BACKOFF = "backoff_skip"
SOURCE_ERROR = "error"


@dataclass
class OrdersRead:
    """/orders の読み取り結果。

    ok=False は「注文が無い」ではなく「読めていない」。
    caller はこれを 0 件と混同してはならない。
    """

    orders: List[Any] = field(default_factory=list)
    ok: bool = False
    source: str = SOURCE_ERROR
    error: str = ""
    age_sec: float = 0.0


# ============================================================
# SERVICE
# ============================================================

class OrdersReadCache:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._inflight = threading.Lock()

        self._orders: List[Any] | None = None
        self._orders_at: float = 0.0

        self._fail_streak: int = 0
        self._next_try_at: float = 0.0
        self._last_error: str = ""

        self._fetches = 0
        self._fetch_errors = 0
        self._cache_hits = 0
        self._backoff_skips = 0
        self._last_backoff_log_at: float = 0.0
        self._last_stats_log_at: float = 0.0
        self._callers: dict = {}

    # --------------------------------------------------------

    def invalidate(self, *, reason: str = "") -> None:
        """注文状態を変えた直後に呼ぶ。次回は必ず取り直す。"""
        with self._lock:
            self._orders = None
            self._orders_at = 0.0
        if reason:
            logger.debug("[ORDERS READ] cache invalidated reason=%s", reason)

    def reset_for_test(self) -> None:
        with self._lock:
            self._orders = None
            self._orders_at = 0.0
            self._fail_streak = 0
            self._next_try_at = 0.0
            self._last_error = ""
            self._fetches = 0
            self._fetch_errors = 0
            self._cache_hits = 0
            self._backoff_skips = 0
            self._last_backoff_log_at = 0.0
            self._last_stats_log_at = 0.0
            self._callers = {}

    def stats(self) -> dict:
        with self._lock:
            return {
                "fetches": self._fetches,
                "fetch_errors": self._fetch_errors,
                "cache_hits": self._cache_hits,
                "backoff_skips": self._backoff_skips,
                "fail_streak": self._fail_streak,
                "last_error": self._last_error,
                "cache_ttl_sec": _cache_ttl(),
                "callers": dict(self._callers),
            }

    def log_stats(self, *, min_interval_sec: float = 60.0) -> None:
        """呼び出し元別の実リクエスト数を定期的に 1 行で残す。"""
        now = time.monotonic()
        with self._lock:
            if (now - self._last_stats_log_at) < float(min_interval_sec):
                return
            self._last_stats_log_at = now
        logger.warning(
            "[ORDERS READ STATS] %s "
            "(fetches=実 GET 回数 / cache_hits=共有キャッシュで消えた重複 / callers=呼び出し元)",
            self.stats(),
        )

    # --------------------------------------------------------

    def _cached(self, now: float) -> OrdersRead | None:
        if self._orders is None:
            return None
        age = now - self._orders_at
        if age >= _cache_ttl():
            return None
        self._cache_hits += 1
        return OrdersRead(orders=list(self._orders), ok=True, source=SOURCE_CACHE, age_sec=age)

    def _note_ok(self, orders: List[Any], now: float) -> None:
        with self._lock:
            self._orders = list(orders)
            self._orders_at = now
        if self._fail_streak:
            logger.warning("[ORDERS READ] /orders recovered fail_streak=%d", self._fail_streak)
        self._fail_streak = 0
        self._next_try_at = 0.0
        self._last_error = ""

    def _note_error(self, err: str, now: float) -> None:
        self._fetch_errors += 1
        self._last_error = str(err or "")
        schedule = _backoff_schedule()
        idx = min(self._fail_streak, len(schedule) - 1)
        wait = schedule[idx]
        self._fail_streak += 1
        self._next_try_at = now + wait
        # 失敗時は古い応答を配らない。誤ったキャンセル判断を防ぐ。
        with self._lock:
            self._orders = None
            self._orders_at = 0.0
        logger.warning(
            "[ORDERS READ] /orders read failed streak=%d next_retry_in=%.1fs err=%s "
            "(即時リトライしない: kabu Station REST への同時接続を増やさない)",
            self._fail_streak, wait, self._last_error[:160],
        )

    def _backoff_result(self, now: float) -> OrdersRead:
        self._backoff_skips += 1
        wait = max(0.0, self._next_try_at - now)
        if now - self._last_backoff_log_at >= 5.0:
            self._last_backoff_log_at = now
            logger.warning(
                "[ORDERS READ] /orders backoff 中のため GET を skip retry_in=%.1fs streak=%d last_err=%s",
                wait, self._fail_streak, self._last_error[:120],
            )
        return OrdersRead(
            orders=[], ok=False, source=SOURCE_BACKOFF,
            error=f"backoff (retry in {wait:.1f}s): {self._last_error}",
        )

    # --------------------------------------------------------

    def get(
        self,
        fetch: Callable[[], Tuple[List[Any], bool, str]],
        *,
        caller: str = "",
        force: bool = False,
    ) -> OrdersRead:
        """fetch() は (orders, ok, error) を返すこと。

        ok=False のときの orders は無視される。
        例外を投げた場合も失敗として扱う。
        """
        now = time.monotonic()
        with self._lock:
            key = caller or "-"
            self._callers[key] = self._callers.get(key, 0) + 1

        if not force:
            with self._lock:
                hit = self._cached(now)
            if hit is not None:
                return hit

        # singleflight: 同時に来た caller は 1 本の実 GET を共有する
        if not self._inflight.acquire(blocking=False):
            self._inflight.acquire()
            try:
                with self._lock:
                    hit = self._cached(time.monotonic())
                if hit is not None:
                    return hit
                if not self._read_allowed(time.monotonic()):
                    return self._backoff_result(time.monotonic())
            finally:
                self._inflight.release()
            # 先行 fetch が失敗していたら、この caller はここで諦める
            # (同じ相手に続けて GET しない)
            return OrdersRead(
                orders=[], ok=False, source=SOURCE_ERROR,
                error=self._last_error or "concurrent read failed",
            )

        try:
            now = time.monotonic()
            with self._lock:
                hit = self._cached(now)
            if hit is not None and not force:
                return hit

            if not self._read_allowed(now):
                return self._backoff_result(now)

            self._fetches += 1
            try:
                orders, ok, error = fetch()
            except Exception as e:  # noqa: BLE001
                self._note_error(f"{type(e).__name__}: {e}", time.monotonic())
                return OrdersRead(orders=[], ok=False, source=SOURCE_ERROR, error=str(e))

            if not ok:
                self._note_error(str(error or "fetch returned ok=False"), time.monotonic())
                return OrdersRead(orders=[], ok=False, source=SOURCE_ERROR, error=str(error or ""))

            orders = list(orders or [])
            self._note_ok(orders, time.monotonic())
            logger.debug("[ORDERS READ] /orders fetched count=%d caller=%s", len(orders), caller or "-")
            return OrdersRead(orders=orders, ok=True, source=SOURCE_FETCH, age_sec=0.0)
        finally:
            try:
                self._inflight.release()
            except RuntimeError:
                logger.debug("[ORDERS READ] inflight release skipped", exc_info=True)

    def _read_allowed(self, now: float) -> bool:
        return now >= self._next_try_at


# ============================================================
# MODULE-LEVEL API
# ============================================================

_cache = OrdersReadCache()


def get_orders_cached(
    fetch: Callable[[], Tuple[List[Any], bool, str]],
    *,
    caller: str = "",
    force: bool = False,
) -> OrdersRead:
    return _cache.get(fetch, caller=caller, force=force)


def invalidate(*, reason: str = "") -> None:
    _cache.invalidate(reason=reason)


def stats() -> dict:
    return _cache.stats()


def log_stats(*, min_interval_sec: float = 60.0) -> None:
    _cache.log_stats(min_interval_sec=min_interval_sec)


def reset_for_test() -> None:
    _cache.reset_for_test()


__all__ = [
    "OrdersRead",
    "OrdersReadCache",
    "get_orders_cached",
    "invalidate",
    "stats",
    "log_stats",
    "reset_for_test",
    "SOURCE_FETCH",
    "SOURCE_CACHE",
    "SOURCE_BACKOFF",
    "SOURCE_ERROR",
]
