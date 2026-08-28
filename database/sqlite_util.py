# -*- coding: utf-8 -*-
"""SQLite 接続のリソース管理ヘルパ (P0-1 / P0-2 / P0-6)。

なぜ必要か
----------
`with sqlite3.connect(path) as conn:` は **接続を閉じない**。
これはトランザクションの context manager であって、closing ではない。

実測 (2026-08-27):

    with sqlite3.connect(p) as conn:
        conn.execute("SELECT 1")
    conn.execute("SELECT 1")      # -> まだ使える = 閉じていない

    with 版を 300 回まわすと Connection オブジェクトが 300 個生存する。

本番ログではこの形が hot path で使われており、
`ats.ats_ranking.db_loader` だけで **28,803 回** 呼ばれていた。
例外パス (disk I/O error 444 件) では traceback がフレームを保持するため
解放がさらに遅れ、最終的に

    OSError: [Errno 24] Too many open files   (83 件、全て importlib)

でモジュールの import すら失敗していた。

読み取りが空 DB を作る問題
--------------------------
`sqlite3.connect(path)` は **パスが無ければ空の DB を作る**。
NAS が一時的に見えないときに reader がこれを呼ぶと、
0 テーブルの DB ファイルが出来てしまい、以後 `tables=[]` になる。
reader は必ず `file:...?mode=ro` を使い、作成しないこと。

使い方
------
    from database.sqlite_util import closing_connect, readonly_connect

    # 書き込み or 読み書き
    with closing_connect(path, timeout=5.0) as conn:
        conn.execute(...)

    # 読み取り専用 (存在しなければ FileNotFoundError。空 DB を作らない)
    with readonly_connect(path, timeout=5.0) as conn:
        conn.execute(...)
"""
from __future__ import annotations

import contextlib
import logging
import os
import sqlite3
import threading
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 診断用: 開いた/閉じた接続の累計と、現在開いている数
_STATS_LOCK = threading.Lock()
_OPENED = [0]
_CLOSED = [0]
_CLOSE_FAILED = [0]


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return default
    return str(v).strip().lower() in {"1", "true", "yes", "on"}


def stats() -> dict:
    """現在の接続統計。leak 監視に使う。"""
    with _STATS_LOCK:
        return {
            "opened": _OPENED[0],
            "closed": _CLOSED[0],
            "close_failed": _CLOSE_FAILED[0],
            "outstanding": _OPENED[0] - _CLOSED[0],
        }


def reset_stats() -> None:
    with _STATS_LOCK:
        _OPENED[0] = 0
        _CLOSED[0] = 0
        _CLOSE_FAILED[0] = 0


@contextlib.contextmanager
def closing_connect(path: Any, *, timeout: float = 5.0, uri: bool = False,
                    busy_timeout_ms: Optional[int] = None, **kwargs):
    """必ず close する sqlite3 接続。

    `with sqlite3.connect(...)` と違い、例外の有無にかかわらず finally で閉じる。
    """
    conn = sqlite3.connect(str(path), timeout=float(timeout), uri=bool(uri), **kwargs)
    with _STATS_LOCK:
        _OPENED[0] += 1
    try:
        if busy_timeout_ms is not None:
            try:
                conn.execute("PRAGMA busy_timeout=%d" % int(busy_timeout_ms))
            except Exception:
                # busy_timeout が設定できなくても接続自体は使える。
                # 握り潰さず debug に残す (ここで接続を捨てると再接続ループになる)。
                logger.debug("[SQLITE UTIL] busy_timeout set failed path=%s", path,
                             exc_info=True)
        yield conn
    finally:
        try:
            conn.close()
            with _STATS_LOCK:
                _CLOSED[0] += 1
        except Exception:
            with _STATS_LOCK:
                _CLOSE_FAILED[0] += 1
            logger.debug("[SQLITE UTIL] close failed path=%s", path, exc_info=True)


@contextlib.contextmanager
def readonly_connect(path: Any, *, timeout: float = 5.0,
                     busy_timeout_ms: Optional[int] = 3000,
                     must_exist: bool = True, **kwargs):
    """読み取り専用接続。**空 DB を新規作成しない。**

    must_exist=True (既定) でファイルが無ければ FileNotFoundError。
    NAS が見えないときに 0 テーブルの DB を作ってしまう事故を防ぐ。

    mode=ro が使えない環境では、明示的に許可された場合のみ通常接続へ落とす
    (既定では落とさない。黙って書き込み可能な接続にしない)。
    """
    p = str(path)
    if must_exist and not os.path.exists(p):
        raise FileNotFoundError(
            "readonly_connect: DB が存在しない (reader は作成しない): %s" % p)

    # 空 DB を作らない保証は上の os.path.exists() で既に得ている。
    # 活発に書かれている WAL DB では mode=ro のほうが遅いため
    # (実測 median 5.496s vs query_only 3.830s = 1.43 倍差)、
    # 既定は通常接続 + PRAGMA query_only=ON にする。
    if _env_bool("SQLITE_READONLY_USE_URI_MODE", False):
        uri = "file:%s?mode=ro" % p.replace("?", "%3f").replace("#", "%23")
        try:
            conn = sqlite3.connect(uri, timeout=float(timeout), uri=True, **kwargs)
        except Exception as e:
            if not _env_bool("SQLITE_READONLY_ALLOW_FALLBACK", False):
                logger.warning(
                    "[SQLITE UTIL] read-only(URI) 接続に失敗 path=%s err=%r "
                    "(SQLITE_READONLY_ALLOW_FALLBACK=1 で通常接続へ落とせるが既定では落とさない)",
                    p, e)
                raise
            logger.warning("[SQLITE UTIL] read-only 不可のため通常接続へフォールバック path=%s err=%r", p, e)
            conn = sqlite3.connect(p, timeout=float(timeout), **kwargs)
    else:
        conn = sqlite3.connect(p, timeout=float(timeout), **kwargs)
        try:
            # SQL レベルで書き込みを拒否する。reader としてはこれで十分。
            conn.execute("PRAGMA query_only=ON")
        except Exception:
            logger.warning(
                "[SQLITE UTIL] query_only を設定できなかった path=%s "
                "(書き込み可能な接続を reader に渡さないため接続を閉じる)", p)
            try:
                conn.close()
            except Exception:
                pass
            raise

    with _STATS_LOCK:
        _OPENED[0] += 1
    try:
        if busy_timeout_ms is not None:
            try:
                conn.execute("PRAGMA busy_timeout=%d" % int(busy_timeout_ms))
            except Exception:
                logger.debug("[SQLITE UTIL] busy_timeout set failed path=%s", p, exc_info=True)
        yield conn
    finally:
        try:
            conn.close()
            with _STATS_LOCK:
                _CLOSED[0] += 1
        except Exception:
            with _STATS_LOCK:
                _CLOSE_FAILED[0] += 1
            logger.debug("[SQLITE UTIL] close failed path=%s", p, exc_info=True)


__all__ = ["closing_connect", "readonly_connect", "stats", "reset_stats"]
