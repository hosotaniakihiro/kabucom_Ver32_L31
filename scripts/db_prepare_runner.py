# ============================================================
# File   : scripts/db_prepare_runner.py
# Version: DATA-COLLECTORS-DB-PREPARE-RUNNER-V1
# ============================================================

from __future__ import annotations

# 2026-09-25: Norton が注入する SSLKEYLOGFILE を外す。
#   プロセス環境に SSLKEYLOGFILE=\\.\nllMonFltProxy\... が入っており
#   (Machine/User の環境変数には無い。Norton が各プロセスへ注入する)、
#   urllib3 は HTTPS 接続ごとにこれを読んで ssl context の
#   keylog_filename に設定する。その setter が Norton の名前付きパイプを
#   open() しようとして応答待ちになり、スレッドが永久に止まる。
#   socket より手前なので timeout= は効かない。
#   9/25 は ranking_summary_bg が Discord 通知の中でこれに掛かり、
#   generation=1 のまま 20 分以上完走しなかった。
#   env AUTOSTOCK_KEEP_SSLKEYLOGFILE=1 で従来どおり残す。
import os as _autostock_os

if _autostock_os.environ.get("SSLKEYLOGFILE") and str(
    _autostock_os.environ.get("AUTOSTOCK_KEEP_SSLKEYLOGFILE", "0")
).strip().lower() not in ("1", "true", "yes", "on"):
    _autostock_keylog = _autostock_os.environ.pop("SSLKEYLOGFILE", None)
    print(
        "[TLS ENV GUARD] SSLKEYLOGFILE を外した (Norton の TLS 監視が"
        " HTTPS 接続を固めるため): %s" % (_autostock_keylog,)
    )


import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from data_collectors.db_prepare import prepare_all_daily_dbs
from data_collectors.logging_setup import setup_logging


def main() -> int:
    logger = setup_logging("db_prepare_runner")
    logger.info("[DB PREPARE RUNNER] START")
    try:
        prepare_all_daily_dbs()
        logger.info("[DB PREPARE RUNNER] DONE")
        return 0
    except Exception:
        logger.exception("[DB PREPARE RUNNER] FAILED")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
