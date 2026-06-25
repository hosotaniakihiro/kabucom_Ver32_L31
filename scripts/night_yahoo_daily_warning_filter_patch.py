# ============================================================
# File   : scripts/night_yahoo_daily_warning_filter_patch.py
# Version: V1-NIGHT-YAHOO-DAILY-WARNING-FILTER
# ------------------------------------------------------------
# 夜間Yahoo日足バッチで、列追加が多いDataFrameに対して出る
# pandas PerformanceWarning を抑止する。
#
# これはDB保存失敗ではなく、ログ大量出力による遅延を防ぐためのもの。
# ============================================================

from __future__ import annotations

import logging
import warnings

LOG = logging.getLogger("night_yahoo_daily_warning_filter_patch")
VERSION = "V1-NIGHT-YAHOO-DAILY-WARNING-FILTER"
_INSTALLED = False


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        from pandas.errors import PerformanceWarning
        warnings.filterwarnings("ignore", category=PerformanceWarning)
        warnings.filterwarnings("ignore", message="DataFrame is highly fragmented.*")
    except Exception:
        warnings.filterwarnings("ignore", message="DataFrame is highly fragmented.*")
    _INSTALLED = True
    LOG.warning("[NIGHT YAHOO DAILY WARNING FILTER] installed version=%s", VERSION)
    return True
