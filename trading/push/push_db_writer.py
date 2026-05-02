#====================================================================================================
# trading/push/push_db_writer.py
#====================================================================================================
# ============================================================
# File   : trading/push/push_db_writer.py
# Version: PRODUCTION-ABSOLUTE-FULL-STREAM-SAVER-REV23-RAW-ALWAYS-ON
#          -FUTURE-DATETIME-GUARD
#          -FLUSH-RUNTIME-METRICS
#          -PUSH-ONLY-LOG-RENAME
#          -NO-RANKING-SNAPSHOT-NAMING
# ------------------------------------------------------------
# ✔ REV21系互換ベース
# ✔ push_stream 新row形式に完全対応
# ✔ datetime文字列 / datetime型 両対応
# ✔ current_price / trading_volume / raw_json 両対応
# ✔ content / raw_json / Sell1..Buy10 両対応
# ✔ stream_data.datetime NOT NULL 対応
# ✔ 既存 stream_data は UNIQUE(symbol, datetime) を維持（最新値テーブルとして上書き）
# ✔ stream_data_raw 機能は維持（既定 ON / 全tick追記保存）
# ✔ flush成功時のみ True を返す
# ✔ 既存機能削除ゼロ思想
# ✔ flush失敗理由の詳細ログ
# ✔ cleaned件数・skip理由・buffer件数・DB例外可視化
# ✔ executemany / commit 経過時間ログ
# ✔ order_book_writer 二重書き込みなし
# ✔ NAS向けに buffer_size / flush_interval_sec を拡大
# ✔ current_price_time を datetime より優先
# ✔ 未来時間を検知して received_at / now にフォールバック
# ✔ flush runtime metrics を global_data に反映
# ✔ NEW: ranking snapshot を連想させる snapshot_* ログ名を廃止
# ✔ NEW: push writer は PUSH専用であることをログに明示
# ✔ REV23: stream_data_raw を既定ONにし、UNIQUE(symbol, datetime) 上書きでも全tickを残す
# ============================================================

from __future__ import annotations

import datetime as dt
import json
import logging
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from config.paths import get_path

try:
    from core.global_context.context import global_data  # type: ignore
except Exception:
    from global_state import global_data  # type: ignore

logger = logging.getLogger(__name__)


class StreamDBWriter:
    # stream_data: 21基本 + 40板 + 1json + date + time = 64
    EXPECTED_COLUMN_COUNT = 64
    TABLE_NAME = "stream_data"

    # stream_data_raw: 上記64 + received_at + ingest_id = 66
    RAW_EXPECTED_COLUMN_COUNT = 66
    RAW_TABLE_NAME = "stream_data_raw"

    def __init__(
        self,
        buffer_size: int = 500,
        flush_interval_sec: float = 2.0,
        enable_raw_save: bool = True,
    ):
        # NAS向け既定値
        self.buffer_size = int(buffer_size) if buffer_size else 500
        self.flush_interval_sec = float(flush_interval_sec) if flush_interval_sec else 2.0
        self.enable_raw_save = bool(enable_raw_save)

        self.buffer = []
        self.conn: Optional[sqlite3.Connection] = None
        self.cursor: Optional[sqlite3.Cursor] = None
        self.current_date: Optional[dt.date] = None

        self.lock = threading.RLock()
        self.last_saved_index = -1

        self._started = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    # ========================================================
    # utility
    # ========================================================

    def _safe_float(self, v: Any) -> Optional[float]:
        if v is None or v == "":
            return None
        try:
            return float(v)
        except Exception:
            return None

    def _safe_json_loads(self, v: Any) -> dict:
        if isinstance(v, dict):
            return v
        if isinstance(v, str):
            s = v.strip()
            if not s:
                return {}
            try:
                obj = json.loads(s)
                return obj if isinstance(obj, dict) else {}
            except Exception:
                return {}
        return {}

    def _safe_symbol(self, row: Any) -> str:
        try:
            if isinstance(row, dict):
                return str(row.get("symbol") or "").strip()
            if isinstance(row, (tuple, list)) and len(row) >= 1:
                return str(row[0] or "").strip()
        except Exception:
            pass
        return ""

    def _safe_dt_preview(self, row: Any) -> str:
        try:
            if isinstance(row, dict):
                return str(
                    row.get("datetime")
                    or row.get("current_price_time")
                    or row.get("received_at")
                    or ""
                ).strip()
            if isinstance(row, (tuple, list)) and len(row) >= 3:
                return str(row[2] or "").strip()
        except Exception:
            pass
        return ""

    def _now_local(self) -> dt.datetime:
        return dt.datetime.now(dt.timezone.utc).astimezone()

    def _now_iso(self) -> str:
        return self._now_local().isoformat()

    def _new_ingest_id(self) -> str:
        return uuid.uuid4().hex

    def _set_runtime_metric(self, name: str, value: Any) -> None:
        try:
            setattr(global_data, name, value)
        except Exception:
            pass

    def _add_runtime_counter(self, name: str, delta: int) -> None:
        try:
            current = getattr(global_data, name, 0)
            current = int(current) if current is not None else 0
            setattr(global_data, name, current + int(delta))
        except Exception:
            try:
                setattr(global_data, name, int(delta))
            except Exception:
                pass

    def _mark_flush_runtime(
        self,
        *,
        push_flushed: int,
        push_before: int,
        push_after: int,
        raw_flushed: int,
        raw_before: int,
        raw_after: int,
        db_path: Path,
        elapsed_sec: float,
    ) -> None:
        now_iso = self._now_iso()
        push_delta = int(push_after) - int(push_before)
        raw_delta = int(raw_after) - int(raw_before) if self.enable_raw_save else 0

        self._set_runtime_metric("last_push_db_flush_at", now_iso)
        self._set_runtime_metric("last_flush_at", now_iso)
        self._set_runtime_metric("last_push_db_path", str(db_path))
        self._set_runtime_metric("last_flush_rows", int(push_flushed))
        self._set_runtime_metric("last_flush_delta", int(push_delta))
        self._set_runtime_metric("last_raw_flush_rows", int(raw_flushed))
        self._set_runtime_metric("last_raw_flush_delta", int(raw_delta))
        self._set_runtime_metric("last_flush_elapsed_sec", float(elapsed_sec))

        self._add_runtime_counter("total_flushed", int(push_flushed))
        self._add_runtime_counter("total_flush_delta", int(push_delta))

        if self.enable_raw_save:
            self._add_runtime_counter("total_raw_flushed", int(raw_flushed))
            self._add_runtime_counter("total_raw_flush_delta", int(raw_delta))

    def _coerce_datetime_parts(
        self, value: Any
    ) -> tuple[Optional[str], Optional[str], Optional[str]]:
        if value is None or value == "":
            return None, None, None

        if isinstance(value, dt.datetime):
            dt_obj = value
            return (
                dt_obj.isoformat(),
                dt_obj.strftime("%Y-%m-%d"),
                dt_obj.strftime("%H:%M:%S"),
            )

        if isinstance(value, dt.date):
            dt_obj = dt.datetime.combine(value, dt.time())
            return (
                dt_obj.isoformat(),
                dt_obj.strftime("%Y-%m-%d"),
                dt_obj.strftime("%H:%M:%S"),
            )

        s = str(value).strip()
        if not s:
            return None, None, None

        try:
            normalized = s.replace("Z", "+00:00")
            dt_obj = dt.datetime.fromisoformat(normalized)
            return (
                dt_obj.isoformat(),
                dt_obj.strftime("%Y-%m-%d"),
                dt_obj.strftime("%H:%M:%S"),
            )
        except Exception:
            pass

        date_str = s[:10] if len(s) >= 10 else None
        time_str = None
        if "T" in s:
            try:
                time_str = s.split("T", 1)[1][:8]
            except Exception:
                time_str = None
        elif " " in s:
            try:
                time_str = s.split(" ", 1)[1][:8]
            except Exception:
                time_str = None

        return s, date_str, time_str

    def _coerce_datetime_obj(self, value: Any) -> Optional[dt.datetime]:
        if value is None or value == "":
            return None

        if isinstance(value, dt.datetime):
            return value

        if isinstance(value, dt.date):
            return dt.datetime.combine(value, dt.time())

        s = str(value).strip()
        if not s:
            return None

        try:
            normalized = s.replace("Z", "+00:00")
            return dt.datetime.fromisoformat(normalized)
        except Exception:
            return None

    def _resolve_push_datetime_value(self, row: dict) -> Any:
        """
        PUSH保存用の代表時刻を安全に決める。
        優先順位:
          1) current_price_time
          2) datetime
          3) received_at

        未来時刻は received_at / now へフォールバックする。
        """
        raw_candidates = [
            row.get("current_price_time"),
            row.get("datetime"),
            row.get("received_at"),
        ]

        now_local = self._now_local()
        future_allow_sec = 5.0

        for idx, raw in enumerate(raw_candidates, start=1):
            if raw is None or raw == "":
                continue

            dt_obj = self._coerce_datetime_obj(raw)
            if dt_obj is None:
                if idx == 1:
                    return raw
                continue

            try:
                if dt_obj.tzinfo is None:
                    dt_obj = dt_obj.replace(tzinfo=now_local.tzinfo)
            except Exception:
                pass

            try:
                delta_sec = (dt_obj - now_local).total_seconds()
                if delta_sec > future_allow_sec:
                    logger.warning(
                        "[PUSH DB WRITER] future datetime detected -> fallback symbol=%s candidate=%s now=%s delta_sec=%.3f source=%s",
                        row.get("symbol"),
                        dt_obj.isoformat(),
                        now_local.isoformat(),
                        delta_sec,
                        "current_price_time" if idx == 1 else "datetime" if idx == 2 else "received_at",
                    )
                    continue
            except Exception:
                pass

            return dt_obj

        fallback = row.get("received_at")
        if fallback:
            return fallback

        return now_local

    def _extract_content(self, row: dict) -> dict:
        content = row.get("content") or {}
        content = self._safe_json_loads(content) if not isinstance(content, dict) else content.copy()

        if not content:
            raw_json = row.get("raw_json")
            content = self._safe_json_loads(raw_json)

        direct_keys = [
            "MarketOrderBuyQty",
            "MarketOrderSellQty",
            "OverSellQty",
            "UnderBuyQty",
            "SecurityType",
            "IV",
            "Gamma",
            "Theta",
            "Vega",
            "Delta",
        ]
        for k in direct_keys:
            if k in row and row.get(k) is not None:
                content[k] = row.get(k)

        for side in ["Sell", "Buy"]:
            for i in range(1, 11):
                key = f"{side}{i}"
                if key in row and isinstance(row.get(key), dict):
                    content[key] = row.get(key)

        if row.get("over_sell_qty") is not None:
            content["OverSellQty"] = row.get("over_sell_qty")
        if row.get("under_buy_qty") is not None:
            content["UnderBuyQty"] = row.get("under_buy_qty")

        return content

    def _get_price(self, row: dict) -> Optional[float]:
        return (
            self._safe_float(row.get("price"))
            or self._safe_float(row.get("current_price"))
            or self._safe_float(row.get("ask_price"))
            or self._safe_float(row.get("bid_price"))
        )

    def _get_volume(self, row: dict) -> Optional[float]:
        return (
            self._safe_float(row.get("volume"))
            or self._safe_float(row.get("trading_volume"))
        )

    # ========================================================
    # DB PATH
    # ========================================================

    def _resolve_db_path(self) -> Path:
        today = dt.datetime.now().strftime("%Y%m%d")
        base_dir: Path = get_path("raw_push")
        base_dir.mkdir(parents=True, exist_ok=True)
        return base_dir / f"push{today}.db"

    # ========================================================
    # CONNECTION
    # ========================================================

    def _open_connection(self) -> None:
        self.current_date = dt.datetime.now().date()
        db_path = self._resolve_db_path()

        self.conn = sqlite3.connect(
            db_path,
            timeout=10,
            check_same_thread=False,
        )
        self.cursor = self.conn.cursor()

        self.cursor.execute("PRAGMA journal_mode=WAL;")
        self.cursor.execute("PRAGMA synchronous=NORMAL;")
        self.cursor.execute("PRAGMA wal_autocheckpoint=1000;")
        self.cursor.execute("PRAGMA busy_timeout=5000;")
        self.cursor.execute("PRAGMA temp_store=MEMORY;")
        self.cursor.execute("PRAGMA cache_size=-50000;")

        self._ensure_table()
        if self.enable_raw_save:
            self._ensure_raw_table()

        logger.info(
            "[PUSH DB WRITER] connected → %s raw_save=%s buffer_size=%d flush_interval=%.3f push_only=True",
            db_path,
            self.enable_raw_save,
            self.buffer_size,
            self.flush_interval_sec,
        )

    # ========================================================
    # TABLE SYNC
    # ========================================================

    def _get_existing_column_count(self, table_name: str) -> int:
        try:
            assert self.cursor is not None
            self.cursor.execute(f"PRAGMA table_info({table_name});")
            return len(self.cursor.fetchall())
        except Exception:
            return 0

    def _ensure_table(self) -> None:
        assert self.cursor is not None
        assert self.conn is not None

        existing_count = self._get_existing_column_count(self.TABLE_NAME)

        if existing_count and existing_count != self.EXPECTED_COLUMN_COUNT:
            logger.warning(
                "[PUSH DB WRITER] column mismatch -> recreating %s existing=%d expected=%d",
                self.TABLE_NAME,
                existing_count,
                self.EXPECTED_COLUMN_COUNT,
            )
            self.cursor.execute(f"DROP TABLE IF EXISTS {self.TABLE_NAME}")
            self.conn.commit()

        self.cursor.execute(f"""
        CREATE TABLE IF NOT EXISTS {self.TABLE_NAME} (
            symbol TEXT NOT NULL,
            symbolname TEXT,
            datetime TEXT NOT NULL,
            date TEXT NOT NULL,
            time TEXT NOT NULL,
            price REAL,
            volume REAL,
            trading_value REAL,
            vwap REAL,
            previousclose REAL,
            opening_price REAL,
            high_price REAL,
            low_price REAL,
            SecurityType TEXT,
            IV REAL,
            Gamma REAL,
            Theta REAL,
            Vega REAL,
            Delta REAL,
            MarketOrderBuyQty REAL,
            MarketOrderSellQty REAL,
            OverSellQty REAL,
            UnderBuyQty REAL,
            Sell1Price REAL, Sell1Qty REAL,
            Sell2Price REAL, Sell2Qty REAL,
            Sell3Price REAL, Sell3Qty REAL,
            Sell4Price REAL, Sell4Qty REAL,
            Sell5Price REAL, Sell5Qty REAL,
            Sell6Price REAL, Sell6Qty REAL,
            Sell7Price REAL, Sell7Qty REAL,
            Sell8Price REAL, Sell8Qty REAL,
            Sell9Price REAL, Sell9Qty REAL,
            Sell10Price REAL, Sell10Qty REAL,
            Buy1Price REAL, Buy1Qty REAL,
            Buy2Price REAL, Buy2Qty REAL,
            Buy3Price REAL, Buy3Qty REAL,
            Buy4Price REAL, Buy4Qty REAL,
            Buy5Price REAL, Buy5Qty REAL,
            Buy6Price REAL, Buy6Qty REAL,
            Buy7Price REAL, Buy7Qty REAL,
            Buy8Price REAL, Buy8Qty REAL,
            Buy9Price REAL, Buy9Qty REAL,
            Buy10Price REAL, Buy10Qty REAL,
            raw_json TEXT,
            UNIQUE(symbol, datetime)
        )
        """)
        self.conn.commit()

    def _ensure_raw_table(self) -> None:
        assert self.cursor is not None
        assert self.conn is not None

        existing_count = self._get_existing_column_count(self.RAW_TABLE_NAME)

        if existing_count and existing_count != self.RAW_EXPECTED_COLUMN_COUNT:
            logger.warning(
                "[PUSH DB WRITER] column mismatch -> recreating %s existing=%d expected=%d",
                self.RAW_TABLE_NAME,
                existing_count,
                self.RAW_EXPECTED_COLUMN_COUNT,
            )
            self.cursor.execute(f"DROP TABLE IF EXISTS {self.RAW_TABLE_NAME}")
            self.conn.commit()

        self.cursor.execute(f"""
        CREATE TABLE IF NOT EXISTS {self.RAW_TABLE_NAME} (
            symbol TEXT NOT NULL,
            symbolname TEXT,
            datetime TEXT NOT NULL,
            date TEXT NOT NULL,
            time TEXT NOT NULL,
            price REAL,
            volume REAL,
            trading_value REAL,
            vwap REAL,
            previousclose REAL,
            opening_price REAL,
            high_price REAL,
            low_price REAL,
            SecurityType TEXT,
            IV REAL,
            Gamma REAL,
            Theta REAL,
            Vega REAL,
            Delta REAL,
            MarketOrderBuyQty REAL,
            MarketOrderSellQty REAL,
            OverSellQty REAL,
            UnderBuyQty REAL,
            Sell1Price REAL, Sell1Qty REAL,
            Sell2Price REAL, Sell2Qty REAL,
            Sell3Price REAL, Sell3Qty REAL,
            Sell4Price REAL, Sell4Qty REAL,
            Sell5Price REAL, Sell5Qty REAL,
            Sell6Price REAL, Sell6Qty REAL,
            Sell7Price REAL, Sell7Qty REAL,
            Sell8Price REAL, Sell8Qty REAL,
            Sell9Price REAL, Sell9Qty REAL,
            Sell10Price REAL, Sell10Qty REAL,
            Buy1Price REAL, Buy1Qty REAL,
            Buy2Price REAL, Buy2Qty REAL,
            Buy3Price REAL, Buy3Qty REAL,
            Buy4Price REAL, Buy4Qty REAL,
            Buy5Price REAL, Buy5Qty REAL,
            Buy6Price REAL, Buy6Qty REAL,
            Buy7Price REAL, Buy7Qty REAL,
            Buy8Price REAL, Buy8Qty REAL,
            Buy9Price REAL, Buy9Qty REAL,
            Buy10Price REAL, Buy10Qty REAL,
            raw_json TEXT,
            received_at TEXT NOT NULL,
            ingest_id TEXT NOT NULL UNIQUE
        )
        """)
        self.cursor.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{self.RAW_TABLE_NAME}_symbol_datetime "
            f"ON {self.RAW_TABLE_NAME}(symbol, datetime)"
        )
        self.cursor.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{self.RAW_TABLE_NAME}_received_at "
            f"ON {self.RAW_TABLE_NAME}(received_at)"
        )
        self.conn.commit()

    # ========================================================
    # ROTATE
    # ========================================================

    def _flush_without_rotate_locked(self) -> bool:
        if not self.buffer:
            return True

        assert self.cursor is not None
        assert self.conn is not None

        push_placeholders = ",".join(["?"] * self.EXPECTED_COLUMN_COUNT)
        push_sql = f"INSERT OR REPLACE INTO {self.TABLE_NAME} VALUES ({push_placeholders})"

        raw_sql = None
        if self.enable_raw_save:
            raw_placeholders = ",".join(["?"] * self.RAW_EXPECTED_COLUMN_COUNT)
            raw_sql = f"INSERT OR IGNORE INTO {self.RAW_TABLE_NAME} VALUES ({raw_placeholders})"

        push_rows = []
        raw_rows = []

        for row in self.buffer:
            if self.enable_raw_save:
                push_rows.append(row[:self.EXPECTED_COLUMN_COUNT])
                raw_rows.append(row)
            else:
                push_rows.append(row)

        if push_rows:
            self.cursor.executemany(push_sql, push_rows)
        if raw_sql and raw_rows:
            self.cursor.executemany(raw_sql, raw_rows)

        self.conn.commit()

        self._mark_flush_runtime(
            push_flushed=len(push_rows),
            push_before=0,
            push_after=0,
            raw_flushed=len(raw_rows),
            raw_before=0,
            raw_after=0,
            db_path=self._resolve_db_path(),
            elapsed_sec=0.0,
        )

        self.buffer.clear()
        return True

    def _rotate_if_needed_locked(self) -> None:
        today = dt.datetime.now().date()
        if self.current_date == today:
            return

        logger.info("[PUSH DB WRITER] Rotating DB → %s", today)

        if self.conn:
            if self.buffer:
                self._flush_without_rotate_locked()
            try:
                self.conn.close()
            except Exception:
                logger.exception("[PUSH DB WRITER] close before rotate failed")

        self.conn = None
        self.cursor = None
        self.current_date = today
        self._open_connection()

    # ========================================================
    # DIRECT ADD
    # ========================================================

    def add_push_row(self, row: dict) -> None:
        if not isinstance(row, dict):
            logger.error("[PUSH DB WRITER] add_push_row skipped: row is not dict")
            return

        content = self._extract_content(row)

        dt_value = self._resolve_push_datetime_value(row)
        dt_str, date_str, time_str = self._coerce_datetime_parts(dt_value)

        if not dt_str:
            logger.error("[PUSH DB WRITER] add_push_row skipped: datetime unresolved symbol=%s", row.get("symbol"))
            return

        if not date_str:
            try:
                date_str = dt_str[:10]
            except Exception:
                date_str = None

        if not time_str:
            time_str = "00:00:00"

        raw_json = row.get("raw_json")
        if not raw_json:
            raw_json = json.dumps(content, ensure_ascii=False)

        push_data = [
            row.get("symbol"),
            row.get("symbolname"),
            dt_str,
            date_str,
            time_str,
            self._get_price(row),
            self._get_volume(row),
            self._safe_float(row.get("trading_value")),
            self._safe_float(row.get("vwap")),
            self._safe_float(row.get("previousclose")),
            self._safe_float(row.get("opening_price")),
            self._safe_float(row.get("high_price")),
            self._safe_float(row.get("low_price")),
            content.get("SecurityType"),
            self._safe_float(content.get("IV")),
            self._safe_float(content.get("Gamma")),
            self._safe_float(content.get("Theta")),
            self._safe_float(content.get("Vega")),
            self._safe_float(content.get("Delta")),
            self._safe_float(content.get("MarketOrderBuyQty")),
            self._safe_float(content.get("MarketOrderSellQty")),
            self._safe_float(content.get("OverSellQty")),
            self._safe_float(content.get("UnderBuyQty")),
        ]

        for side in ["Sell", "Buy"]:
            for i in range(1, 11):
                level = content.get(f"{side}{i}")
                if isinstance(level, dict):
                    push_data.append(self._safe_float(level.get("Price")))
                    push_data.append(self._safe_float(level.get("Qty")))
                else:
                    push_data.append(None)
                    push_data.append(None)

        push_data.append(raw_json)

        if len(push_data) != self.EXPECTED_COLUMN_COUNT:
            logger.error(
                "[PUSH DB WRITER] binding mismatch expected=%d got=%d symbol=%s dt=%s",
                self.EXPECTED_COLUMN_COUNT,
                len(push_data),
                row.get("symbol"),
                dt_str,
            )
            return

        if self.enable_raw_save:
            received_at = str(row.get("received_at") or self._now_iso())
            ingest_id = str(row.get("ingest_id") or self._new_ingest_id())
            raw_data = list(push_data) + [received_at, ingest_id]

            if len(raw_data) != self.RAW_EXPECTED_COLUMN_COUNT:
                logger.error(
                    "[PUSH DB WRITER] raw binding mismatch expected=%d got=%d symbol=%s dt=%s",
                    self.RAW_EXPECTED_COLUMN_COUNT,
                    len(raw_data),
                    row.get("symbol"),
                    dt_str,
                )
                return

            store_row = tuple(raw_data)
        else:
            store_row = tuple(push_data)

        with self.lock:
            try:
                if self.conn is None or self.cursor is None:
                    self._open_connection()

                self._rotate_if_needed_locked()
                self.buffer.append(store_row)
                current_buffer_len = len(self.buffer)

                logger.debug(
                    "[PUSH DB WRITER] buffer append symbol=%s dt=%s buffer_len=%d",
                    row.get("symbol"),
                    dt_str,
                    current_buffer_len,
                )
            except Exception:
                logger.exception("[PUSH DB WRITER] add_push_row buffer append failed")
                return

        if current_buffer_len >= self.buffer_size:
            logger.info(
                "[PUSH DB WRITER] buffer reached threshold buffer_len=%d threshold=%d -> flush",
                current_buffer_len,
                self.buffer_size,
            )
            self.flush()

    # ========================================================
    # 旧互換
    # ========================================================

    def add_latest_push(self) -> None:
        try:
            df = global_data.get_push_df()
        except Exception:
            df = None

        if df is None or df.empty:
            return

        new_rows = df.iloc[self.last_saved_index + 1:]
        if new_rows.empty:
            return

        for _, row in new_rows.iterrows():
            try:
                if hasattr(row, "to_dict"):
                    self.add_push_row(row.to_dict())
                else:
                    self.add_push_row(dict(row))
            except Exception:
                logger.exception("[PUSH DB WRITER] add_latest_push row add failed")

        self.last_saved_index = len(df) - 1

    # ========================================================
    # FLUSH
    # ========================================================

    def flush(self) -> bool:
        with self.lock:
            if not self.buffer:
                logger.debug("[PUSH DB WRITER] flush skipped: buffer empty")
                return True

            buffer_len_before = len(self.buffer)

            try:
                if self.conn is None or self.cursor is None:
                    self._open_connection()

                self._rotate_if_needed_locked()

                assert self.cursor is not None
                assert self.conn is not None

                push_placeholders = ",".join(["?"] * self.EXPECTED_COLUMN_COUNT)
                push_sql = f"INSERT OR REPLACE INTO {self.TABLE_NAME} VALUES ({push_placeholders})"

                raw_sql = None
                if self.enable_raw_save:
                    raw_placeholders = ",".join(["?"] * self.RAW_EXPECTED_COLUMN_COUNT)
                    raw_sql = f"INSERT OR IGNORE INTO {self.RAW_TABLE_NAME} VALUES ({raw_placeholders})"

                push_rows = []
                raw_rows = []

                skip_bad_len = 0
                skip_bad_dt = 0
                first_bad_len = None
                first_bad_dt = None

                for row in self.buffer:
                    if self.enable_raw_save:
                        if len(row) != self.RAW_EXPECTED_COLUMN_COUNT:
                            skip_bad_len += 1
                            if first_bad_len is None:
                                first_bad_len = {
                                    "symbol": self._safe_symbol(row),
                                    "dt": self._safe_dt_preview(row),
                                    "row_len": len(row),
                                }
                            logger.error(
                                "[PUSH DB WRITER] raw binding mismatch expected=%d got=%d symbol=%s dt=%s",
                                self.RAW_EXPECTED_COLUMN_COUNT,
                                len(row),
                                self._safe_symbol(row),
                                self._safe_dt_preview(row),
                            )
                            continue

                        push_row = row[:self.EXPECTED_COLUMN_COUNT]
                        raw_row = row
                    else:
                        if len(row) != self.EXPECTED_COLUMN_COUNT:
                            skip_bad_len += 1
                            if first_bad_len is None:
                                first_bad_len = {
                                    "symbol": self._safe_symbol(row),
                                    "dt": self._safe_dt_preview(row),
                                    "row_len": len(row),
                                }
                            logger.error(
                                "[PUSH DB WRITER] binding mismatch expected=%d got=%d symbol=%s dt=%s",
                                self.EXPECTED_COLUMN_COUNT,
                                len(row),
                                self._safe_symbol(row),
                                self._safe_dt_preview(row),
                            )
                            continue

                        push_row = row
                        raw_row = None

                    if not push_row[2] or not push_row[3] or not push_row[4]:
                        skip_bad_dt += 1
                        if first_bad_dt is None:
                            first_bad_dt = {
                                "symbol": push_row[0],
                                "datetime": push_row[2],
                                "date": push_row[3],
                                "time": push_row[4],
                            }
                        logger.error(
                            "[PUSH DB WRITER] skipped row due to null datetime fields symbol=%s datetime=%s date=%s time=%s",
                            push_row[0], push_row[2], push_row[3], push_row[4],
                        )
                        continue

                    push_rows.append(push_row)
                    if self.enable_raw_save and raw_row is not None:
                        raw_rows.append(raw_row)

                logger.info(
                    "[PUSH DB WRITER] flush prepare buffer=%d push_rows=%d raw_rows=%d raw_save=%s skip_bad_len=%d skip_bad_dt=%d",
                    buffer_len_before,
                    len(push_rows),
                    len(raw_rows),
                    self.enable_raw_save,
                    skip_bad_len,
                    skip_bad_dt,
                )

                if first_bad_len is not None:
                    logger.warning("[PUSH DB WRITER] flush first_bad_len=%r", first_bad_len)
                if first_bad_dt is not None:
                    logger.warning("[PUSH DB WRITER] flush first_bad_dt=%r", first_bad_dt)

                if not push_rows:
                    logger.error(
                        "[PUSH DB WRITER] flush aborted: valid rows empty buffer=%d push_rows=%d skip_bad_len=%d skip_bad_dt=%d",
                        buffer_len_before,
                        len(push_rows),
                        skip_bad_len,
                        skip_bad_dt,
                    )
                    self.buffer.clear()
                    return False

                self.cursor.execute(f"SELECT COUNT(*) FROM {self.TABLE_NAME}")
                before_push = self.cursor.fetchone()[0]

                if self.enable_raw_save:
                    self.cursor.execute(f"SELECT COUNT(*) FROM {self.RAW_TABLE_NAME}")
                    before_raw = self.cursor.fetchone()[0]
                else:
                    before_raw = 0

                t0 = time.time()
                db_path = self._resolve_db_path()

                logger.info(
                    "[PUSH DB WRITER] executemany start push_rows=%d raw_rows=%d",
                    len(push_rows),
                    len(raw_rows),
                )

                self.cursor.executemany(push_sql, push_rows)
                t1 = time.time()
                logger.info(
                    "[PUSH DB WRITER] executemany push done rows=%d elapsed=%.3fs",
                    len(push_rows),
                    t1 - t0,
                )

                if self.enable_raw_save and raw_sql and raw_rows:
                    self.cursor.executemany(raw_sql, raw_rows)
                    t2 = time.time()
                    logger.info(
                        "[PUSH DB WRITER] executemany raw done rows=%d elapsed=%.3fs",
                        len(raw_rows),
                        t2 - t1,
                    )
                else:
                    t2 = t1

                self.conn.commit()
                t3 = time.time()
                total_elapsed = t3 - t0

                logger.info(
                    "[PUSH DB WRITER] commit done elapsed=%.3fs total_elapsed=%.3fs",
                    t3 - t2,
                    total_elapsed,
                )

                self.cursor.execute(f"SELECT COUNT(*) FROM {self.TABLE_NAME}")
                after_push = self.cursor.fetchone()[0]

                if self.enable_raw_save:
                    self.cursor.execute(f"SELECT COUNT(*) FROM {self.RAW_TABLE_NAME}")
                    after_raw = self.cursor.fetchone()[0]
                else:
                    after_raw = 0

                self._mark_flush_runtime(
                    push_flushed=len(push_rows),
                    push_before=before_push,
                    push_after=after_push,
                    raw_flushed=len(raw_rows) if self.enable_raw_save else 0,
                    raw_before=before_raw,
                    raw_after=after_raw,
                    db_path=db_path,
                    elapsed_sec=total_elapsed,
                )

                logger.warning(
                    "[PUSH DB WRITER] db=%s push_flushed=%d push_before=%d push_after=%d push_delta=%d raw_save=%s raw_flushed=%d raw_before=%d raw_after=%d raw_delta=%d buffer_before=%d",
                    db_path,
                    len(push_rows),
                    before_push,
                    after_push,
                    after_push - before_push,
                    self.enable_raw_save,
                    len(raw_rows) if self.enable_raw_save else 0,
                    before_raw,
                    after_raw,
                    (after_raw - before_raw) if self.enable_raw_save else 0,
                    buffer_len_before,
                )

                self.buffer.clear()
                return True

            except sqlite3.IntegrityError:
                logger.exception(
                    "[PUSH DB WRITER] flush integrity failed buffer=%d table_push=%s raw_save=%s table_raw=%s",
                    buffer_len_before,
                    self.TABLE_NAME,
                    self.enable_raw_save,
                    self.RAW_TABLE_NAME,
                )
                self.buffer.clear()
                return False

            except sqlite3.OperationalError:
                logger.exception(
                    "[PUSH DB WRITER] flush operational failed buffer=%d table_push=%s raw_save=%s table_raw=%s",
                    buffer_len_before,
                    self.TABLE_NAME,
                    self.enable_raw_save,
                    self.RAW_TABLE_NAME,
                )
                self.buffer.clear()
                return False

            except Exception:
                logger.exception(
                    "[PUSH DB WRITER] flush failed buffer=%d table_push=%s raw_save=%s table_raw=%s",
                    buffer_len_before,
                    self.TABLE_NAME,
                    self.enable_raw_save,
                    self.RAW_TABLE_NAME,
                )
                self.buffer.clear()
                return False

    # ========================================================
    # START / STOP / LOOP
    # ========================================================

    def start(self) -> None:
        with self.lock:
            if self._started and self._thread and self._thread.is_alive():
                logger.info("[PUSH DB WRITER] start skipped: already running")
                return

            self._stop_event.clear()

            if self.conn is None or self.cursor is None:
                self._open_connection()

            th = threading.Thread(
                target=self._loop,
                daemon=True,
                name="StreamDBWriterLoop",
            )
            self._thread = th
            self._started = True
            th.start()

    def stop(self) -> None:
        with self.lock:
            self._stop_event.set()

        try:
            self.flush()
        except Exception:
            logger.exception("[PUSH DB WRITER] flush on stop failed")

        with self.lock:
            try:
                if self.conn:
                    self.conn.close()
            except Exception:
                logger.exception("[PUSH DB WRITER] close on stop failed")
            finally:
                self.conn = None
                self.cursor = None
                self._started = False

    def _loop(self) -> None:
        logger.info("[PUSH DB WRITER] writer loop started")

        while not self._stop_event.is_set():
            try:
                with self.lock:
                    has_buffer = bool(self.buffer)
                    current_len = len(self.buffer)

                if has_buffer:
                    logger.debug(
                        "[PUSH DB WRITER] loop flush trigger buffer_len=%d interval=%.3f",
                        current_len,
                        self.flush_interval_sec,
                    )
                    self.flush()

                time.sleep(self.flush_interval_sec)

            except Exception:
                logger.exception("[PUSH DB WRITER] loop error")
                time.sleep(1.0)


# ============================================================
# Singleton Instance
# ============================================================

stream_writer = StreamDBWriter(enable_raw_save=True)