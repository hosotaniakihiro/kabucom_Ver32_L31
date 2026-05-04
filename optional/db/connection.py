# ============================================================
# optional/db/connection.py
# ------------------------------------------------------------
# ✔ OPTIONAL 系 SQLite 接続を完全統一
# ✔ database is locked 対策（timeout / busy_timeout）
# ✔ WAL / NORMAL 同期設定
# ✔ sqlite3.connect 直書き禁止（この関数のみ使用）
# ✔ migration / ingest / batch 共通
# ✔ 親ディレクトリ保証
# ✔ 絶対パス正規化
# ✔ WAL失敗時フォールバック
# ✔ 診断ログ強化
# ============================================================

import sqlite3
import logging
from pathlib import Path
from typing import Union

logger = logging.getLogger(__name__)


# ============================================================
def connect_sqlite(db_path: Union[str, Path]) -> sqlite3.Connection:
    """
    OPTIONAL 系 SQLite DB への安全な接続を返す

    Args:
        db_path: DB ファイルパス（str / Path）

    Returns:
        sqlite3.Connection
    """

    # --------------------------------------------------------
    # Path 正規化（★重要）
    # --------------------------------------------------------
    db_path = Path(db_path).expanduser().resolve()

    logger.info(f"[OPTIONAL][DB CONNECT] {db_path}")

    # --------------------------------------------------------
    # 親ディレクトリ保証
    # --------------------------------------------------------
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        logger.exception("[OPTIONAL][DB] Failed to ensure parent directory")
        raise

    # --------------------------------------------------------
    # 接続（★ timeout が最重要）
    # --------------------------------------------------------
    try:
        con = sqlite3.connect(
            db_path.as_posix(),
            timeout=30,            # ★ database is locked 対策
            isolation_level=None,  # autocommit（長時間トランザクション防止）
            detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES,
        )
    except Exception:
        logger.exception(f"[OPTIONAL][DB] Connection failed: {db_path}")
        raise

    # --------------------------------------------------------
    # PRAGMA（全接続共通）
    # --------------------------------------------------------
    cur = con.cursor()
    try:
        try:
            cur.execute("PRAGMA journal_mode=WAL;")      # 並列書き込み安定化
        except Exception:
            logger.warning("[OPTIONAL][DB] WAL mode failed, fallback to DELETE")

        cur.execute("PRAGMA synchronous=NORMAL;")        # 性能と安全のバランス
        cur.execute("PRAGMA busy_timeout=30000;")        # 30秒待機
        cur.execute("PRAGMA foreign_keys=ON;")           # FK 有効化

    except Exception:
        logger.exception("[OPTIONAL][DB] PRAGMA setup failed")
    finally:
        cur.close()

    return con