# ============================================================
# File: core/state/last_state_manager.py
# Ver : Ver3.3-PRODUCTION-FULL-COMPAT-LAST-STATE-MANAGER-FINAL
# ------------------------------------------------------------
# ✔ Ver3.2 完全保持（削除ゼロ）
# ✔ load_from_db 既存維持
# ✔ load_last_state / restore_from_db 互換追加
# ✔ save_to_db 互換追加（現状メモリ同期用no-op安全実装）
# ✔ get_state_dict 追加
# ✔ global_data 同期補助追加
# ✔ session.get_* 経由維持
# ✔ SQLAlchemy 2.0 execute完全対応
# ✔ SQLite 安全
# ✔ テーブル未存在安全
# ✔ datetimeカラム未存在安全
# ✔ NULL安全
# ✔ 型安全
# ✔ engine診断ログ安全化
# ✔ stream_data診断安全化
# ✔ 例外完全耐性
# ============================================================

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

import pandas as pd
from sqlalchemy import text

from database import session

logger = logging.getLogger(__name__)


class LastStateManager:
    def __init__(self):
        self.last_1m: datetime | None = None
        self.last_3m: datetime | None = None
        self.last_5m: datetime | None = None
        self.last_push: datetime | None = None

    # ========================================================
    # 初期ロード
    # ========================================================

    def load_from_db(self):
        logger.info("🔄 [STATE] loading last timestamps from DB")

        try:
            summary_engine = session.get_summary_engine()
            push_engine = session.get_push_engine()

            try:
                logger.info(f"[STATE] SUMMARY ENGINE: {summary_engine.url}")
            except Exception:
                logger.info("[STATE] SUMMARY ENGINE url unavailable")

            try:
                logger.info(f"[STATE] PUSH ENGINE: {push_engine.url}")
            except Exception:
                logger.info("[STATE] PUSH ENGINE url unavailable")

        except Exception:
            logger.info("[STATE] Engine unavailable")
            return self

        self.last_1m = self._get_last_dt(summary_engine, "stock_summary_1min")
        self.last_3m = self._get_last_dt(summary_engine, "stock_summary_3min")
        self.last_5m = self._get_last_dt(summary_engine, "stock_summary_5min")

        # pushはリアルタイム専用
        self.last_push = None

        # ----------------------------------------------------
        # stream_data診断
        # ----------------------------------------------------
        try:
            with push_engine.connect() as conn:
                exists = self._table_exists(conn, "stream_data")

                if exists:
                    count = conn.execute(
                        text("SELECT COUNT(*) FROM stream_data")
                    ).scalar_one_or_none() or 0

                    logger.info(f"[STATE][stream_data rows] {count}")
                else:
                    logger.warning("[STATE] stream_data table not found")

        except Exception:
            logger.exception("[STATE] stream_data diagnostic failed")

        self._sync_to_global_data()

        logger.info(
            f"[STATE] "
            f"1m={self.last_1m} "
            f"3m={self.last_3m} "
            f"5m={self.last_5m} "
            f"push={self.last_push}"
        )
        return self

    # ========================================================
    # 互換API
    # ========================================================

    def load_last_state(self):
        """
        旧名/別名互換
        """
        return self.load_from_db()

    def restore_from_db(self):
        """
        旧名/別名互換
        """
        return self.load_from_db()

    def save_to_db(self):
        """
        互換用の安全 no-op。
        現行設計では最終時刻は summary/push DB の実データから復元するため、
        専用stateテーブル未使用でも安全に動くようにする。
        """
        try:
            self._sync_to_global_data()
            logger.info("[STATE] save_to_db noop-compatible success")
            return True
        except Exception:
            logger.exception("[STATE] save_to_db noop-compatible failed")
            return False

    # ========================================================
    # テーブル存在確認
    # ========================================================

    def _table_exists(self, conn, table: str) -> bool:
        try:
            result = conn.execute(
                text("""
                    SELECT COUNT(*)
                    FROM sqlite_master
                    WHERE type='table'
                      AND name=:table
                """),
                {"table": table}
            ).scalar_one_or_none()

            if result:
                return True

        except Exception:
            pass

        return False

    # ========================================================
    # DBから最終datetime取得
    # ========================================================

    def _get_last_dt(self, engine, table: str):
        try:
            with engine.connect() as conn:
                if not self._table_exists(conn, table):
                    logger.debug(f"[STATE] table not found: {table}")
                    return None

                cols = conn.execute(
                    text(f"PRAGMA table_info({table})")
                ).fetchall()

                colnames = {c[1] for c in cols if len(c) > 1}

                if "datetime" not in colnames:
                    logger.warning(f"[STATE] datetime column missing: {table}")
                    return None

                result = conn.execute(
                    text(f"SELECT MAX(datetime) FROM {table}")
                ).scalar_one_or_none()

            if result:
                dt = pd.to_datetime(result, errors="coerce")

                if pd.isna(dt):
                    return None

                return dt.to_pydatetime()

            return None

        except Exception:
            logger.exception(f"[STATE] unexpected error on {table}")
            return None

    # ========================================================
    # global_data 同期
    # ========================================================

    def _sync_to_global_data(self):
        try:
            from global_state import global_data

            setattr(global_data, "last_summary_1m_dt", self.last_1m)
            setattr(global_data, "last_summary_3m_dt", self.last_3m)
            setattr(global_data, "last_summary_5m_dt", self.last_5m)
            setattr(global_data, "last_push_dt", self.last_push)

            setattr(global_data, "last_1m", self.last_1m)
            setattr(global_data, "last_3m", self.last_3m)
            setattr(global_data, "last_5m", self.last_5m)
            setattr(global_data, "last_push", self.last_push)

            return True
        except Exception:
            logger.debug("[STATE] global_data sync skipped", exc_info=True)
            return False

    # ========================================================
    # 更新系
    # ========================================================

    def update_1m(self, dt: datetime):
        if dt and (not self.last_1m or dt > self.last_1m):
            self.last_1m = dt
            self._sync_to_global_data()

    def update_3m(self, dt: datetime):
        if dt and (not self.last_3m or dt > self.last_3m):
            self.last_3m = dt
            self._sync_to_global_data()

    def update_5m(self, dt: datetime):
        if dt and (not self.last_5m or dt > self.last_5m):
            self.last_5m = dt
            self._sync_to_global_data()

    def update_push(self, dt: datetime):
        if dt and (not self.last_push or dt > self.last_push):
            self.last_push = dt
            self._sync_to_global_data()

    # ========================================================
    # 一括更新
    # ========================================================

    def update(
        self,
        last_1m: Optional[datetime] = None,
        last_3m: Optional[datetime] = None,
        last_5m: Optional[datetime] = None,
        last_push: Optional[datetime] = None,
    ):
        if last_1m:
            self.update_1m(last_1m)
        if last_3m:
            self.update_3m(last_3m)
        if last_5m:
            self.update_5m(last_5m)
        if last_push:
            self.update_push(last_push)
        return self

    # ========================================================
    # 取得系
    # ========================================================

    def get_last_1m(self):
        return self.last_1m

    def get_last_3m(self):
        return self.last_3m

    def get_last_5m(self):
        return self.last_5m

    def get_last_push(self):
        return self.last_push

    def get_state_dict(self) -> dict[str, Any]:
        return {
            "last_1m": self.last_1m,
            "last_3m": self.last_3m,
            "last_5m": self.last_5m,
            "last_push": self.last_push,
        }


# ============================================================
# シングルトン
# ============================================================

last_state = LastStateManager()


# ============================================================
# モジュール関数互換
# ============================================================

def load_from_db():
    return last_state.load_from_db()


def load_last_state():
    return last_state.load_last_state()


def restore_from_db():
    return last_state.restore_from_db()


def save_to_db():
    return last_state.save_to_db()


def get_last_1m():
    return last_state.get_last_1m()


def get_last_3m():
    return last_state.get_last_3m()


def get_last_5m():
    return last_state.get_last_5m()


def get_last_push():
    return last_state.get_last_push()


def get_state_dict():
    return last_state.get_state_dict()