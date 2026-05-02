# ============================================================
# File   : trading/exit/rl_exit_agent.py
# Version: V1-FINAL-RL-EXIT-AGENT-QLEARNING
# ------------------------------------------------------------
# ✔ 軽量Q-Learning実装
# ✔ 状態: regime × cluster × inago × pnl_bucket
# ✔ 行動: HOLD / TAKE / EXIT
# ✔ ε-greedy
# ✔ SQLite永続化
# ✔ thread safe
# ✔ 例外安全
# ============================================================

from __future__ import annotations

import logging
import random
import sqlite3
import threading
from typing import Dict, Tuple

logger = logging.getLogger(__name__)


# ============================================================
# RLExitAgent
# ============================================================

class RLExitAgent:

    ACTIONS = ["HOLD", "TAKE", "EXIT"]

    def __init__(
        self,
        db_path: str,
        alpha: float = 0.1,
        gamma: float = 0.95,
        epsilon: float = 0.1,
    ):

        self.db_path = db_path
        self.alpha = alpha
        self.gamma = gamma
        self.epsilon = epsilon

        self._lock = threading.Lock()

        self._init_db()

    # ========================================================
    # DB INIT
    # ========================================================

    def _init_db(self):

        with sqlite3.connect(self.db_path) as conn:
            c = conn.cursor()

            c.execute("""
                CREATE TABLE IF NOT EXISTS rl_q_table (
                    state TEXT NOT NULL,
                    action TEXT NOT NULL,
                    value REAL NOT NULL,
                    PRIMARY KEY (state, action)
                )
            """)

            conn.commit()

    # ========================================================
    # STATE ENCODING
    # ========================================================

    def encode_state(
        self,
        regime: int,
        cluster: int,
        inago: int,
        pnl: float,
    ) -> str:

        pnl_bucket = self._bucket_pnl(pnl)

        return f"{regime}|{cluster}|{inago}|{pnl_bucket}"

    def _bucket_pnl(self, pnl: float) -> int:

        if pnl < -0.02:
            return -2
        elif pnl < -0.005:
            return -1
        elif pnl < 0.005:
            return 0
        elif pnl < 0.02:
            return 1
        else:
            return 2

    # ========================================================
    # ACTION SELECTION
    # ========================================================

    def select_action(self, state: str) -> str:

        try:
            # ε-greedy
            if random.random() < self.epsilon:
                return random.choice(self.ACTIONS)

            q_values = self._get_q_values(state)

            if not q_values:
                return random.choice(self.ACTIONS)

            return max(q_values, key=q_values.get)

        except Exception:
            logger.exception("RL select_action failed")
            return "HOLD"

    # ========================================================
    # UPDATE
    # ========================================================

    def update(
        self,
        state: str,
        action: str,
        reward: float,
        next_state: str,
    ):

        try:
            with self._lock:

                q_values = self._get_q_values(state)
                next_q = self._get_q_values(next_state)

                current_q = q_values.get(action, 0.0)
                max_next_q = max(next_q.values()) if next_q else 0.0

                new_q = current_q + self.alpha * (
                    reward + self.gamma * max_next_q - current_q
                )

                self._set_q_value(state, action, new_q)

        except Exception:
            logger.exception("RL update failed")

    # ========================================================
    # Q TABLE ACCESS
    # ========================================================

    def _get_q_values(self, state: str) -> Dict[str, float]:

        with sqlite3.connect(self.db_path) as conn:
            c = conn.cursor()
            c.execute(
                "SELECT action, value FROM rl_q_table WHERE state = ?",
                (state,),
            )
            rows = c.fetchall()

        return {a: v for a, v in rows}

    def _set_q_value(self, state: str, action: str, value: float):

        with sqlite3.connect(self.db_path) as conn:
            c = conn.cursor()

            c.execute("""
                INSERT INTO rl_q_table (state, action, value)
                VALUES (?, ?, ?)
                ON CONFLICT(state, action)
                DO UPDATE SET value=excluded.value
            """, (state, action, value))

            conn.commit()