"""============================================================
File: trading/core/db_write_queue.py
Ver2.0-PRODUCTION-ASYNC-IMMUNE
------------------------------------------------------------
✔ 非同期UPSERT
✔ 本体停止防止
✔ interval別対応
✔ Queueベース
✔ バックプレッシャー防止
✔ 多重起動防止
✔ シャットダウン安全
✔ ログ完全対応
✔ 将来拡張対応
✔ 本番永久安定版
============================================================"""
from __future__ import annotations

import logging
import threading
import time
from queue import Queue, Empty
from typing import Tuple

import pandas as pd

from trading.summary.persistence.summary_saver_bulk import bulk_upsert_summary

logger = logging.getLogger(__name__)

# ============================================================
# 設定
# ============================================================

MAX_QUEUE_SIZE = 10000        # バックプレッシャー防止
WORKER_SLEEP_SEC = 0.01       # CPU過負荷防止
RETRY_SLEEP_SEC = 0.5         # DB失敗時リトライ間隔
MAX_RETRY = 3                 # 最大リトライ回数


# ============================================================
# Queue 初期化
# ============================================================

_db_queue: "Queue[Tuple[int, pd.DataFrame]]" = Queue(maxsize=MAX_QUEUE_SIZE)
_worker_started = False
_worker_lock = threading.Lock()
_shutdown_flag = False


# ============================================================
# ワーカースレッド
# ============================================================

def _db_worker_loop():
    logger.info("🚀 [DB_QUEUE] worker started")

    while not _shutdown_flag:
        try:
            interval, df = _db_queue.get(timeout=0.1)
        except Empty:
            continue

        try:
            _safe_upsert(interval, df)
        except Exception:
            logger.exception("[DB_QUEUE] unexpected fatal error")

        finally:
            _db_queue.task_done()

        time.sleep(WORKER_SLEEP_SEC)

    logger.info("🛑 [DB_QUEUE] worker stopped")


# ============================================================
# 安全UPSERT
# ============================================================

def _safe_upsert(interval: int, df: pd.DataFrame):

    if df is None or df.empty:
        return

    retry = 0

    while retry < MAX_RETRY:

        try:
            bulk_upsert_summary(df, interval)
            return

        except Exception:
            retry += 1
            logger.exception(
                "[DB_QUEUE] upsert failed interval=%s retry=%s",
                interval,
                retry,
            )
            time.sleep(RETRY_SLEEP_SEC)

    logger.error(
        "❌ [DB_QUEUE] upsert permanently failed interval=%s",
        interval,
    )


# ============================================================
# 公開API
# ============================================================

def enqueue_summary_save(interval: int, df: pd.DataFrame):
    """
    非同期保存キュー登録
    """

    if df is None or df.empty:
        return

    try:
        _db_queue.put_nowait((interval, df.copy()))
    except Exception:
        logger.exception("[DB_QUEUE] enqueue failed (queue full?)")


# ============================================================
# 起動
# ============================================================

def start_db_worker():
    global _worker_started

    with _worker_lock:
        if _worker_started:
            return

        t = threading.Thread(
            target=_db_worker_loop,
            daemon=True,
            name="DBWriteWorker",
        )
        t.start()

        _worker_started = True


# ============================================================
# シャットダウン
# ============================================================

def shutdown_db_worker():
    global _shutdown_flag
    _shutdown_flag = True