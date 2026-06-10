from __future__ import annotations

import os


def install() -> bool:
    defaults = {
        # main_database.py はDB保存専用寄せ。表示/通知/entry系は main.py 側へ寄せる。
        "SUMMARY_DATABASE_RUNNER_DISPLAY": "0",
        "SUMMARY_DISCORD_EMPTY_FALLBACK_NOTIFY": "0",
        "ENABLE_SUMMARY_ENTRY_TICK": "0",
        "ENABLE_RANKING_SUMMARY_TICK": "0",

        # CPU高止まり対策: PUSH 1m/3m/5mを毎分すべて再計算しない。
        # 1mは毎分、3m/5mは時間境界だけにする。
        "SUMMARY_PUSH_DISPLAY_ALL_INTERVALS": "0",

        # spool flush は毎tick前後では重いので間引く。
        "SUMMARY_SAVE_SPOOL_FLUSH_MIN_INTERVAL_SEC": "120",
        "SUMMARY_SAVE_SPOOL_FLUSH_MAX_FILES": "10",

        # 1回のsummary tickが重すぎる時は次tickを1回休ませる。
        "SUMMARY_DATABASE_SLOW_TICK_SEC": "45",
        "SUMMARY_DATABASE_SKIP_NEXT_ON_SLOW_TICK": "1",

        # MA75 warmupの起動負荷を抑える。
        "PUSH_INCREMENTAL_MA75_SUMMARY_LOOKBACK_DAYS": "1",
        "PUSH_INCREMENTAL_MA75_TAIL_ROWS": "90",

        # メモリ余裕を使って SQLite の temp/cache をメモリ寄せする。
        "SQLITE_MEMORY_PRAGMAS_ENABLED": "1",
        "SQLITE_MEMORY_TEMP_STORE": "MEMORY",
        "SQLITE_MEMORY_CACHE_KB": "-65536",
        "SQLITE_BUSY_TIMEOUT_MS": "5000",
        "SQLITE_MMAP_SIZE_BYTES": "268435456",
        "SQLITE_CACHE_SPILL_OFF": "1",
        "RANKING_SQLITE_TEMP_STORE": "MEMORY",
        "SUMMARY_SQLITE_TEMP_STORE": "MEMORY",
        "PUSH_SQLITE_TEMP_STORE": "MEMORY",
        "YAHOO_SQLITE_TEMP_STORE": "MEMORY",
        "RANKING_SQLITE_CACHE_KB": "-65536",
        "SUMMARY_SQLITE_CACHE_KB": "-131072",
        "PUSH_SQLITE_CACHE_KB": "-65536",
        "YAHOO_SQLITE_CACHE_KB": "-65536",

        # summary履歴キャッシュ系。対応モジュールがある場合に有効化される。
        "SUMMARY_HISTORY_MEMORY_CACHE": "1",
        "SUMMARY_HISTORY_CACHE_TTL_SEC": "300",
        "SUMMARY_HISTORY_CACHE_MAX_SYMBOLS": "800",
        "SUMMARY_HISTORY_CACHE_TAIL_ROWS": "90",

        # NAS heartbeat / BLAS thread抑制。
        "AUTOSTOCK_COLLECTOR_PARENT_HEARTBEAT": "0",
        "AUTOSTOCK_DISABLE_NAS_HEARTBEAT": "1",
        "OMP_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "NUMEXPR_NUM_THREADS": "1",
    }
    for key, value in defaults.items():
        os.environ.setdefault(key, value)

    try:
        from core.startup.sqlite_memory_pragmas_patch import install as _install_sqlite_memory_pragmas
        _install_sqlite_memory_pragmas()
    except Exception:
        pass

    return True
