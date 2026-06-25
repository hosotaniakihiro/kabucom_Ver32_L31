# ============================================================
# File   : TaskSchedule/SubScript/run_night_yahoo_full_summary/main.py
# Version: V12-TASK-NIGHT-YAHOO-LIGHT-COMPUTE
# ------------------------------------------------------------
# Windowsタスクスケジューラ用入口。
#   1) Yahoo日足差分更新→daily_db/stock_analysis.db保存
#   2) 最新営業日のYahoo 1m全銘柄取得
#   3) 1m/3m/5m summary計算→summary DB保存
#
# V12:
#   - 外部の重い日足計算パイプラインを使わず、軽量固定208列計算を既定にする
#   - 銘柄ごとの fetch / compute / save stage ログを追加
#   - pandas PerformanceWarning の大量ログを抑止
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
os.environ.setdefault("NIGHT_YAHOO_DIRECT_TIMEOUT", "10")
# Chart API が失敗した銘柄で yfinance へ戻ると固まることがあるため、夜間は直接取得だけにする。
os.environ.setdefault("NIGHT_YAHOO_DIRECT_ONLY", "1")
os.environ.setdefault("NIGHT_YAHOO_DAILY_WARMUP_ROWS", "320")
os.environ.setdefault("NIGHT_YAHOO_DAILY_FORCE_FULL", "0")
os.environ.setdefault("NIGHT_YAHOO_DAILY_FULLCOL_REPAIR", "1")
os.environ.setdefault("NIGHT_YAHOO_DAILY_FULLCOL_REPAIR_ROWS", "320")
os.environ.setdefault("NIGHT_YAHOO_DAILY_LIGHT_COMPUTE", "1")
os.environ.setdefault("NIGHT_YAHOO_DAILY_SYMBOL_TIMEOUT_SEC", "30")
os.environ.setdefault("NIGHT_YAHOO_DAILY_TIMEOUT_WORKERS", "4")
os.environ.setdefault("NIGHT_YAHOO_DAILY_SAVE_CHUNK_SIZE", "500")
os.environ.setdefault("NIGHT_YAHOO_DAILY_SQLITE_TIMEOUT", "10")
os.environ.setdefault("NIGHT_YAHOO_DAILY_SQLITE_BUSY_TIMEOUT_MS", "10000")

# 夜間日足は sitecustomize の yfinance fail-cache 影響を受けることがあるため、
# daily module を先に読み込み、直接 chart API・差分更新・full columns・light compute・chunk保存・timeoutを差し込む。
from scripts import night_yahoo_daily_warning_filter_patch as _daily_warning_filter_patch
_daily_warning_filter_patch.install()

from scripts import night_yahoo_daily_update_batch as _daily_batch
from scripts import night_yahoo_daily_direct_chart_patch as _daily_chart_patch
from scripts import night_yahoo_daily_incremental_patch as _daily_incremental_patch
from scripts import night_yahoo_daily_full_columns_patch as _daily_full_columns_patch
from scripts import night_yahoo_daily_light_compute_patch as _daily_light_compute_patch
from scripts import night_yahoo_daily_save_chunk_patch as _daily_save_chunk_patch
from scripts import night_yahoo_daily_stage_log_patch as _daily_stage_log_patch
from scripts import night_yahoo_daily_timeout_patch as _daily_timeout_patch

_daily_chart_patch.install()
_daily_incremental_patch.install(_daily_batch)
_daily_full_columns_patch.install(_daily_batch)
_daily_light_compute_patch.install(_daily_batch)
_daily_save_chunk_patch.install(_daily_batch)
_daily_stage_log_patch.install(_daily_batch)
_daily_timeout_patch.install(_daily_batch)

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