from __future__ import annotations

import os


def install() -> bool:
    defaults = {
        "SUMMARY_DATABASE_RUNNER_DISPLAY": "0",
        "SUMMARY_DISCORD_EMPTY_FALLBACK_NOTIFY": "0",
        "ENABLE_SUMMARY_ENTRY_TICK": "0",
        "ENABLE_RANKING_SUMMARY_TICK": "0",
        "AUTOSTOCK_COLLECTOR_PARENT_HEARTBEAT": "0",
        "AUTOSTOCK_DISABLE_NAS_HEARTBEAT": "1",
        "OMP_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "NUMEXPR_NUM_THREADS": "1",
    }
    for key, value in defaults.items():
        os.environ.setdefault(key, value)
    return True
