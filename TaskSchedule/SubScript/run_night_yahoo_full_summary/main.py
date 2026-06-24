# ============================================================
# File   : TaskSchedule/SubScript/run_night_yahoo_full_summary/main.py
# Version: V3-TASK-NIGHT-YAHOO-DAILY-DIRECT-CHART
# ------------------------------------------------------------
# Windowsタスクスケジューラ用入口。
#   1) Yahoo日足全銘柄更新→daily_db/stock_analysis.db保存
#   2) 最新営業日のYahoo 1m全銘柄取得
#   3) 1m/3m/5m summary計算→summary DB保存
#
# V3:
#   - 日足更新は yfinance が空の場合に Yahoo chart API 直接取得へフォールバック
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

# 日足更新設定。重すぎる場合は環境変数で調整。
os.environ.setdefault("NIGHT_YAHOO_UPDATE_DAILY", "1")
os.environ.setdefault("NIGHT_YAHOO_DAILY_PERIOD", "3y")
os.environ.setdefault("NIGHT_YAHOO_DAILY_PAUSE_SEC", "0.05")
os.environ.setdefault("NIGHT_YAHOO_DIRECT_TIMEOUT", "20")

# 夜間日足は sitecustomize の yfinance fail-cache 影響を受けることがあるため、
# daily module を先に読み込み、直接 chart API フォールバックを差し込む。
from scripts import night_yahoo_daily_update_batch as _daily_batch
from scripts import night_yahoo_daily_direct_chart_patch as _daily_chart_patch

_daily_chart_patch.install()

daily_main = _daily_batch.main
from scripts.night_yahoo_full_summary_batch import main as summary_main


def main() -> int:
    daily_enabled = str(os.environ.get("NIGHT_YAHOO_UPDATE_DAILY", "1")).strip().lower() not in {"0", "false", "no", "off"}

    if daily_enabled:
        daily_ret = int(daily_main([]) or 0)
        if daily_ret != 0 and str(os.environ.get("NIGHT_YAHOO_DAILY_STRICT", "0")).strip().lower() in {"1", "true", "yes", "on"}:
            return daily_ret

    return int(summary_main([]) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
