# ============================================================
# File   : trading/learning/reward_logger.py
# Version: FINAL-ROBUST-REWARD-LOGGER
# ------------------------------------------------------------
# ✔ SQLite保存
# ✔ index付与
# ✔ 自動初期化
# ✔ thread-safe
# ✔ 例外耐性
# ============================================================

from __future__ import annotations
import sqlite3
import datetime as dt
import threading
import logging

logger = logging.getLogger(__name__)


class RewardLogger:

    def __init__(self, db_path: str = "reward.db"):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS reward_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT,
                    symbol TEXT,
                    regime TEXT,
                    cluster TEXT,
                    reward REAL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_symbol ON reward_log(symbol)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_regime ON reward_log(regime)")

    def log(self, symbol: str, regime: str, cluster: str, reward: float):
        try:
            with self._lock:
                with sqlite3.connect(self.db_path) as conn:
                    conn.execute(
                        "INSERT INTO reward_log VALUES (NULL,?,?,?,?,?)",
                        (
                            dt.datetime.now().isoformat(),
                            symbol,
                            regime,
                            cluster,
                            float(reward),
                        )
                    )
        except Exception:
            logger.exception("[REWARD_LOGGER] log failed")