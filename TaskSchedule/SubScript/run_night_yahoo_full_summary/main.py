# ============================================================
# File   : TaskSchedule/SubScript/run_night_yahoo_full_summary/main.py
# Version: V13-TASK-NIGHT-YAHOO-PANDAS-SAFETY
# ------------------------------------------------------------
# Windowsタスクスケジューラ用入口。
#   1) Yahoo日足差分更新→daily_db/stock_analysis.db保存
#   2) 最新営業日のYahoo 1m全銘柄取得
#   3) 1m/3m/5m summary計算→summary DB保存
#
# V13:
#   - yfinance/pandas返却形揺れ対策を起動時に適用
#   - Series ambiguous / No objects to concatenate の連続失敗を抑止
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

# この環境では Norton の SSL/TLS スキャンが HTTPS を傍受しており、
# Norton のルートCAが certifi に無いため Yahoo へ到達できない。
# Windows 証明書ストアを足した CA バンドルを最初に用意する。
# (yfinance/curl_cffi はこれで通る。urllib 直叩きは通らない)
from scripts.yahoo_ca_bundle import install as _install_ca_bundle  # noqa: E402
_install_ca_bundle()

# 夜間は他プロセスと競合しない前提なので、Yahoo保存は少し待つ。
os.environ.setdefault("YAHOO_SUMMARY_LOCK_TIMEOUT_SEC", "30")
os.environ.setdefault("YAHOO_SUMMARY_SKIP_IF_BUSY", "0")

# summary_save_guards の lock pressure ガードは、save_reason が空だと
# 「リアルタイム保存」とみなして
#   latest_only=True (銘柄ごとに最新1本だけ) / skip_if_busy=True /
#   ロック待ち上限2秒
# に切り詰める。夜間バッチは過去分のバックフィルなので、これに当たると
# 取得した過去の足がほぼ保存されない (2026-08-08 に実測)。
# 夜間バッチのプロセス内でのみ無効化する。
os.environ.setdefault("SUMMARY_REALTIME_FORCE_LATEST_ONLY", "0")
os.environ.setdefault("SUMMARY_REALTIME_SKIP_IF_BUSY", "0")
os.environ.setdefault("SUMMARY_REALTIME_LOCK_TIMEOUT_SEC", "60")

# summary 保存は足の日付ごとの DB へ直接書く。
# 既定の recovery.persistence / bulk 経路は SQLAlchemy エンジン経由で、
# database/session.py が date.today() から DB を決めるため、過去日を埋める
# バックフィルでも「今日」の summaryYYYYMMDD.db を開いてしまう。
# 稼働中プロセスが同じ今日のDBを掴んでいると database is locked で止まる。
os.environ.setdefault("YAHOO_SUMMARY_DIRECT_ONLY", "1")
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

# 列追加が多い DataFrame に対して出る pandas PerformanceWarning を抑止する。
# (旧 scripts/night_yahoo_daily_warning_filter_patch.py を本文化。
#  DB 保存失敗の抑止ではなく、ログ大量出力による遅延を防ぐためのもの)
import warnings as _warnings

try:
    from pandas.errors import PerformanceWarning as _PerformanceWarning

    _warnings.filterwarnings("ignore", category=_PerformanceWarning)
    _warnings.filterwarnings("ignore", message="DataFrame is highly fragmented.*")
except Exception:
    _warnings.filterwarnings("ignore", message="DataFrame is highly fragmented.*")

# 夜間日足は sitecustomize の yfinance fail-cache 影響を受けることがあるため、
# daily module を先に読み込み、直接 chart API・差分更新・full columns・light compute・chunk保存・timeoutを差し込む。

# pandas/yfinance の返却形揺れで銘柄ごとに連続失敗する問題を先に抑止する。
# モジュール単位のラッパーは各本体へ本文化済みで、残るグローバル副作用
# (空リスト pd.concat の安全化) だけをここで入れる。
from scripts.night_yahoo_pandas_safety import install_concat_guard as _install_concat_guard
_install_concat_guard()

from scripts import night_yahoo_daily_update_batch as _daily_batch
from scripts import night_yahoo_daily_full_columns_patch as _daily_full_columns_patch
from scripts import night_yahoo_daily_light_compute_patch as _daily_light_compute_patch
from scripts import night_yahoo_daily_save_chunk_patch as _daily_save_chunk_patch
from scripts import night_yahoo_daily_stage_log_patch as _daily_stage_log_patch
from scripts import night_yahoo_daily_timeout_patch as _daily_timeout_patch

# module読み込み後に、対象関数にも安全ラッパーを当てる。
from scripts.night_yahoo_daily_direct_chart import apply_socket_timeout as _dc_apply_socket_timeout
_dc_apply_socket_timeout()
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
