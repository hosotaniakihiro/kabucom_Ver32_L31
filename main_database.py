# ============================================================
# File   : main_database.py
# Version: DATA-COLLECTORS-MAIN-DATABASE-ENTRY-V3-TOKEN-BOOTSTRAP
# ------------------------------------------------------------
# Purpose:
#   - DB作成 / ランキング取得 / PUSH銘柄登録 / PUSH受信 を起動する入口
#   - 既存 main.py とは分離する
#   - 実体は scripts/data_collectors_runner.py に委譲する
#
# V3 Fix:
#   ✔ 朝一に main_database.py 単独起動しても kabu Station token を取得する
#   ✔ token_manager.refresh_token() により settings.ini の token を更新する
#   ✔ 子プロセス ranking_collector / push_receiver が settings.ini から token を読める
#   ✔ cwd を PROJECT_ROOT に固定し、settings.ini の読み違いを防止する
#   ✔ main.py を先に起動しなくても data collectors が開始できる
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
import os
import sys
from pathlib import Path
from configparser import ConfigParser


PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    os.chdir(str(PROJECT_ROOT))
except Exception:
    pass

logger = logging.getLogger(__name__)


# ============================================================
# logging fallback
# ============================================================

def _ensure_basic_logging() -> None:
    try:
        root = logging.getLogger()
        if not root.handlers:
            logging.basicConfig(
                level=logging.INFO,
                format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            )
        root.setLevel(logging.INFO)
    except Exception:
        pass


# ============================================================
# token bootstrap
# ============================================================

def _read_api_password_from_settings() -> str:
    conf = ConfigParser()
    conf.read(str(PROJECT_ROOT / "settings.ini"), encoding="utf-8")

    if conf.has_section("aukabu"):
        return conf.get("aukabu", "apipassword", fallback="")

    if conf.has_section("kabusapi"):
        return conf.get("kabusapi", "apipassword", fallback="")

    return ""


def _bootstrap_kabu_token_for_data_collectors() -> bool:
    """
    main_database.py 単独起動用 token 初期化。

    main.py の system_startup() は呼ばない。
    理由:
      - system_startup() は summary / scheduler / push stack なども起動する
      - main_database.py は data collectors だけを起動したい

    ここでは最低限、kabu Station API token の refresh だけ行う。
    token_manager.refresh_token() は settings.ini に token を保存するため、
    その後に起動する子プロセスでも token を利用できる。
    """
    _ensure_basic_logging()

    try:
        api_password = _read_api_password_from_settings()

        if not api_password:
            logger.error(
                "[MAIN DATABASE] token bootstrap failed: settings.ini apipassword missing"
            )
            return False

        from token_manager import refresh_token, get_valid_token

        token = refresh_token(api_password)

        if not token:
            logger.error("[MAIN DATABASE] token bootstrap failed: empty token returned")
            return False

        # 念のため同一プロセス内でも読める状態にする
        try:
            _ = get_valid_token()
        except Exception:
            pass

        try:
            from global_state import global_data
            global_data.token_value = token
        except Exception:
            logger.debug("[MAIN DATABASE] global_data.token_value set skipped", exc_info=True)

        logger.info(
            "[MAIN DATABASE] kabu token refreshed for data collectors token_len=%s",
            len(str(token)),
        )
        return True

    except Exception:
        logger.exception("[MAIN DATABASE] token bootstrap failed")
        return False


# ============================================================
# main
# ============================================================

def main() -> int:
    """
    data_collectors_runner.py の main() を呼び出す入口。

    起動順序:
      1. cwd を PROJECT_ROOT に固定
      2. data collector process として mark
      3. kabu Station API token を refresh して settings.ini に保存
      4. data_collectors_runner.py を起動

    data_collectors_runner.py 側で、以下を実行する。
      1. db_prepare_runner.py
      2. ranking_collector_runner.py
      3. push_receiver_runner.py
      4. 子プロセス監視
    """
    _ensure_basic_logging()

    try:
        os.chdir(str(PROJECT_ROOT))
    except Exception:
        logger.exception("[MAIN DATABASE] chdir PROJECT_ROOT failed path=%s", PROJECT_ROOT)
        return 1

    logger.info("========== MAIN DATABASE BOOT START ==========")
    logger.info("[MAIN DATABASE] PROJECT_ROOT=%s", PROJECT_ROOT)
    logger.info("[MAIN DATABASE] cwd=%s", os.getcwd())

    try:
        from data_collectors.split_mode import mark_as_data_collector_process
        mark_as_data_collector_process()
    except Exception:
        logger.exception("[MAIN DATABASE] failed to mark data collector process")
        return 1

    if not _bootstrap_kabu_token_for_data_collectors():
        logger.error(
            "[MAIN DATABASE] abort because token bootstrap failed. "
            "Please confirm kabu Station is running and API password is correct."
        )
        return 1

    try:
        from scripts.data_collectors_runner import main as data_collectors_main
    except Exception:
        logger.exception("[MAIN DATABASE] failed to import scripts.data_collectors_runner.main")
        return 1

    return int(data_collectors_main())


if __name__ == "__main__":
    raise SystemExit(main())
