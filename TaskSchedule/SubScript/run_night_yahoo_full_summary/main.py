# ============================================================
# File   : TaskSchedule/SubScript/run_night_yahoo_full_summary/main.py
# Version: V1-TASK-NIGHT-YAHOO-FULL-SUMMARY
# ------------------------------------------------------------
# Windowsタスクスケジューラ用入口。
# 最新営業日のYahoo 1m全銘柄取得→1m/3m/5m summary計算→summary DB保存。
# ============================================================

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 夜間は他プロセスと競合しない前提なので、Yahoo保存は少し待つ。
os.environ.setdefault("YAHOO_SUMMARY_LOCK_TIMEOUT_SEC", "30")
os.environ.setdefault("YAHOO_SUMMARY_SKIP_IF_BUSY", "0")
os.environ.setdefault("NIGHT_YAHOO_BATCH_SIZE", "80")
os.environ.setdefault("NIGHT_YAHOO_PAUSE_SEC", "1.0")
os.environ.setdefault("NIGHT_YAHOO_INTERVALS", "1,3,5")

from scripts.night_yahoo_full_summary_batch import main


if __name__ == "__main__":
    raise SystemExit(main())
