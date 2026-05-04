# ============================================================
# optional/db/reset_db.py
# ------------------------------------------------------------
# ⚠ 手動実行専用
# ✔ OPTIONAL DB を完全初期化（DROP）
# ✔ migrate.py とは完全分離
# ✔ runtime / scheduler からは絶対に呼ばない
# ============================================================

import logging
from pathlib import Path

from config.paths import get_path
from optional.db.connection import connect_sqlite

logger = logging.getLogger(__name__)


# ============================================================
def reset_optional_db(confirm: bool = False):
    """
    OPTIONAL DB を完全リセットする（DROP）

    Args:
        confirm (bool): True のときのみ実行
    """

    if not confirm:
        logger.error(
            "❌ reset_optional_db aborted. "
            "confirm=True を指定してください。"
        )
        return

    db_path: Path = get_path("optional_db")

    logger.warning("⚠ OPTIONAL DB RESET START: %s", db_path)

    con = connect_sqlite(db_path)
    cur = con.cursor()

    try:
        # ----------------------------------------------------
        # DROP（存在しなくても安全）
        # ----------------------------------------------------
        cur.execute("DROP TABLE IF EXISTS news_events")
        cur.execute("DROP TABLE IF EXISTS margin_master")
        cur.execute("DROP TABLE IF EXISTS daily_watchlist")

        logger.warning("🗑 OPTIONAL DB all tables dropped")

    except Exception:
        logger.exception("❌ OPTIONAL DB reset failed")
        raise

    finally:
        try:
            cur.close()
            con.close()
        except Exception:
            pass


# ============================================================
# 単体実行用（明示的確認必須）
# ============================================================
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    print("⚠⚠⚠ OPTIONAL DB を完全削除します ⚠⚠⚠")
    ans = input("本当に実行しますか？ [yes/no]: ").strip().lower()

    if ans == "yes":
        reset_optional_db(confirm=True)
        print("✅ OPTIONAL DB RESET 完了")
    else:
        print("⛔ キャンセルしました")
