# ============================================================
# File   : main_database.py
# Version: DATA-COLLECTORS-MAIN-DATABASE-ENTRY-V1
# ------------------------------------------------------------
# Purpose:
#   - DB作成 / ランキング取得 / PUSH銘柄登録 / PUSH受信 を起動する入口
#   - 既存 main.py とは分離する
#   - 実体は scripts/data_collectors_runner.py に委譲する
#
# Usage:
#   D:\Users\owner\anaconda3\python.exe F:\script\python\kabu\kabucom_Ver32_L31\main_database.py
#
# Notes:
#   - main.py は summary作成 / AI判定 / entry判定 / 表示 / 通知 を担当
#   - main_database.py は data collectors 系だけを担当
# ============================================================

from __future__ import annotations

import logging
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


logger = logging.getLogger(__name__)


def main() -> int:
    """
    data_collectors_runner.py の main() を呼び出す薄い入口。

    data_collectors_runner.py 側で、以下を実行する。
      1. db_prepare_runner.py
      2. ranking_collector_runner.py
      3. push_receiver_runner.py
      4. 子プロセス監視
    """
    try:
        from scripts.data_collectors_runner import main as data_collectors_main
    except Exception:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        )
        logger.exception("[MAIN DATABASE] failed to import scripts.data_collectors_runner.main")
        return 1

    return int(data_collectors_main())


if __name__ == "__main__":
    raise SystemExit(main())
