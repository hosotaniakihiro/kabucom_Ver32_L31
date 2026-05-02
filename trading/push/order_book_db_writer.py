# ============================================================
# File   : trading/push/order_book_db_writer.py
# Version: Ver31.2-PRODUCTION-ULTRA-STABLE-WRITER-SQLITE-BIND-FIX
#          -THREAD-STATE-FIX
#          -SAFE-STOP
#          -REQUEUE-ON-FAIL
# ------------------------------------------------------------
# 【概要】
#   PUSH由来の板情報 Sell1〜Sell10 / Buy1〜Buy10 を
#   orderbookYYYYMMDD.db の order_book テーブルへ保存する DB writer
#
# 【主な機能】
#   ✔ 全PUSH銘柄の板情報保存
#   ✔ グローバル sequence
#   ✔ 日付別DBローテーション
#   ✔ WAL最適化
#   ✔ executemany バルク保存
#   ✔ connection 常駐
#   ✔ 完全スレッド安全
#   ✔ 再入 flush 防止
#   ✔ cursor共有廃止
#   ✔ SQLITE LOCK RETRY
#   ✔ NAS耐性
#   ✔ writer loop 安全化
#   ✔ connection self-healing
#   ✔ pandas.Timestamp / NaT / numpy scalar を SQLite bind 可能型へ変換
#   ✔ flush失敗時に未保存データを buffer へ戻す
#   ✔ flush直前の型診断ログ
#   ✔ _thread を保持し外部から生存確認可能
#   ✔ stop() による安全停止
#
# 【修正ポイント】
#   - sqlite3.ProgrammingError:
#       Error binding parameter X: type 'Timestamp' is not supported
#     を防止
#
#   - pandas.Timestamp / datetime / date / NaT / NaN / numpy scalar を
#     SQLiteが受け取れる None / str / int / float / bytes へ変換
#
#   - flush失敗時に data を buffer 先頭へ戻し、データ消失を防止
#
# 【注意】
#   - order_book 側の保存失敗は PUSH stream_data 保存とは別経路
#   - ただし同一PUSHから派生するため、例外で push_stream 全体を止めない設計
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import math
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Optional

from config.paths import get_path

try:
    from global_state import global_data  # noqa: F401
except Exception:
    global_data = None  # type: ignore

try:
    import pandas as pd
except Exception:
    pd = None  # type: ignore

logger = logging.getLogger(__name__)


# ============================================================
# SQLite bind safe helpers
# ============================================================

def _is_null_like(v: Any) -> bool:
    """
    SQLite に None として渡すべき値を判定する。

    対応:
      - None
      - pandas.NaT
      - pandas.NA
      - numpy.nan
      - float("nan")
    """
    if v is None:
        return True

    try:
        if pd is not None and pd.isna(v):
            return True
    except Exception:
        pass

    try:
        if isinstance(v, float) and math.isnan(v):
            return True
    except Exception:
        pass

    return False


def _sqlite_safe_datetime(v: Any) -> Optional[str]:
    """
    sqlite3 が bind できる datetime 文字列へ変換する。

    対応:
      - pandas.Timestamp
      - datetime.datetime
      - datetime.date
      - str
      - NaT / NaN / None

    戻り値:
      - "YYYY-MM-DD HH:MM:SS.ffffff"
      - None
    """
    if _is_null_like(v):
        return None

    try:
        if pd is not None and isinstance(v, pd.Timestamp):
            if pd.isna(v):
                return None

            try:
                if v.tzinfo is not None:
                    v = v.tz_localize(None)
            except Exception:
                try:
                    v = v.replace(tzinfo=None)
                except Exception:
                    pass

            return v.to_pydatetime().replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S.%f")

        if isinstance(v, dt.datetime):
            if v.tzinfo is not None:
                v = v.replace(tzinfo=None)
            return v.strftime("%Y-%m-%d %H:%M:%S.%f")

        if isinstance(v, dt.date):
            return dt.datetime.combine(v, dt.time.min).strftime("%Y-%m-%d %H:%M:%S.%f")

        if isinstance(v, str):
            s = v.strip()
            return s or None

    except Exception:
        logger.debug("[OrderBookDB] datetime bind normalize failed value=%r", v, exc_info=True)

    try:
        s = str(v).strip()
        return s or None
    except Exception:
        return None


def _sqlite_safe_scalar(v: Any) -> Any:
    """
    sqlite3 executemany 用に Python 標準型へ変換する。

    sqlite3 が安全に受け取れる型:
      - None
      - str
      - int
      - float
      - bytes
    """
    if _is_null_like(v):
        return None

    if pd is not None and isinstance(v, pd.Timestamp):
        return _sqlite_safe_datetime(v)

    if isinstance(v, (dt.datetime, dt.date)):
        return _sqlite_safe_datetime(v)

    if isinstance(v, bool):
        return int(v)

    if isinstance(v, (str, int, float, bytes)):
        return v

    # numpy scalar / pandas scalar など
    try:
        if hasattr(v, "item"):
            x = v.item()
            if x is not v:
                return _sqlite_safe_scalar(x)
    except Exception:
        pass

    try:
        return str(v)
    except Exception:
        return None


def _sqlite_safe_row(row: Iterable[Any]) -> tuple[Any, ...]:
    """
    1行分の tuple/list を SQLite bind 可能型へ変換する。
    """
    return tuple(_sqlite_safe_scalar(v) for v in row)


def _normalize_symbol(symbol: Any) -> str:
    if symbol is None:
        return ""

    try:
        s = str(symbol).strip()
        if s.endswith(".0"):
            s = s[:-2]
        return s
    except Exception:
        return ""


def _normalize_side(side: Any) -> str:
    if side is None:
        return ""

    try:
        return str(side).strip().upper()
    except Exception:
        return ""


def _safe_int(v: Any, default: int = 0) -> int:
    if _is_null_like(v):
        return default

    try:
        return int(v)
    except Exception:
        try:
            return int(float(v))
        except Exception:
            return default


def _safe_float_or_none(v: Any) -> Optional[float]:
    if _is_null_like(v):
        return None

    try:
        return float(v)
    except Exception:
        return None


# ============================================================
# writer
# ============================================================

class OrderBookDBWriter:
    """
    PUSH板情報用 DB writer。

    保存対象:
      content["Sell1"] ... content["Sell10"]
      content["Buy1"]  ... content["Buy10"]

    保存テーブル:
      order_book(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          symbol TEXT NOT NULL,
          datetime TEXT NOT NULL,
          sequence INTEGER NOT NULL,
          side TEXT NOT NULL,
          level INTEGER NOT NULL,
          price REAL,
          qty REAL
      )
    """

    def __init__(self, buffer_size: int = 100):
        self.buffer_size = max(int(buffer_size), 1)
        self.buffer: list[tuple[Any, ...]] = []

        self.conn: sqlite3.Connection | None = None
        self.current_date: dt.date | None = None
        self.sequence = 0

        self.lock = threading.RLock()
        self._flushing = False
        self._started = False
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    # ========================================================
    # DB PATH
    # ========================================================

    def _resolve_db_path(self) -> Path:
        today = dt.datetime.now().strftime("%Y%m%d")

        base_dir: Path = get_path("raw_order_book")
        base_dir.mkdir(parents=True, exist_ok=True)

        return base_dir / f"orderbook{today}.db"

    # ========================================================
    # CONNECTION
    # ========================================================

    def _open_connection(self) -> None:
        db_path = self._resolve_db_path()
        self.current_date = dt.datetime.now().date()

        if self.conn:
            try:
                self.conn.close()
            except Exception:
                pass
            finally:
                self.conn = None

        self.conn = sqlite3.connect(
            db_path,
            timeout=30,
            check_same_thread=False,
        )

        # WAL optimization
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA synchronous=NORMAL;")
        self.conn.execute("PRAGMA busy_timeout=30000;")
        self.conn.execute("PRAGMA wal_autocheckpoint=1000;")

        self._ensure_table()

        logger.info("[StreamDB] connected → %s", db_path)

    # ========================================================
    # TABLE
    # ========================================================

    def _ensure_table(self) -> None:
        if not self.conn:
            return

        self.conn.execute("""
        CREATE TABLE IF NOT EXISTS order_book (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            datetime TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            side TEXT NOT NULL,
            level INTEGER NOT NULL,
            price REAL,
            qty REAL
        );
        """)

        self.conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_symbol_time
        ON order_book(symbol, datetime);
        """)

        self.conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_symbol_time_seq
        ON order_book(symbol, datetime, sequence);
        """)

        self.conn.commit()

    # ========================================================
    # STATE
    # ========================================================

    def is_running(self) -> bool:
        try:
            return bool(
                self._started
                and self._thread is not None
                and self._thread.is_alive()
                and not self._stop_event.is_set()
            )
        except Exception:
            return False

    # ========================================================
    # ROTATE
    # ========================================================

    def _rotate_if_needed(self) -> None:
        today = dt.datetime.now().date()

        if self.current_date is None:
            with self.lock:
                if not self.conn:
                    self._open_connection()
            return

        if today != self.current_date:
            logger.info("[OrderBookDB] Date changed → rotating DB")

            try:
                self.flush()
            except Exception:
                logger.exception("[OrderBookDB] flush before rotate failed")

            with self.lock:
                try:
                    if self.conn:
                        self.conn.close()
                except Exception:
                    pass
                finally:
                    self.conn = None

                self._open_connection()

    # ========================================================
    # ADD DATA
    # ========================================================

    def add_from_push_content(self, symbol: Any, datetime_str: Any, content: dict | None) -> None:
        """
        PUSH content 内の Sell1〜Sell10 / Buy1〜Buy10 を order_book DB へ保存する。

        datetime_str は pandas.Timestamp が渡ってくることがあるため、
        buffer投入時点でもSQLite安全文字列へ寄せる。
        flush直前でも再度safe変換する。
        """
        if not content:
            return

        symbol_s = _normalize_symbol(symbol)
        if not symbol_s:
            return

        datetime_s = _sqlite_safe_datetime(datetime_str)
        if not datetime_s:
            datetime_s = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")

        rows_to_add: list[tuple[Any, ...]] = []

        with self.lock:
            self.sequence += 1
            seq = self.sequence

            for side_key, side_name in (("Sell", "SELL"), ("Buy", "BUY")):
                for level in range(1, 11):
                    key = f"{side_key}{level}"
                    level_data = content.get(key)

                    if not level_data:
                        continue

                    if not isinstance(level_data, dict):
                        continue

                    price = _safe_float_or_none(level_data.get("Price"))
                    qty = _safe_float_or_none(level_data.get("Qty"))

                    # price / qty が両方 None の板は保存しない
                    if price is None and qty is None:
                        continue

                    rows_to_add.append(
                        (
                            symbol_s,
                            datetime_s,
                            int(seq),
                            side_name,
                            int(level),
                            price,
                            qty,
                        )
                    )

            if rows_to_add:
                self.buffer.extend(rows_to_add)

            should_flush = len(self.buffer) >= self.buffer_size

        if should_flush:
            self.flush()

    # 互換API
    def add_order_book_row(self, row: dict) -> None:
        """
        dict row 形式から order_book を追加する互換入口。

        想定キー:
          symbol
          datetime / current_price_time / received_at
          content
        """
        if not isinstance(row, dict):
            return

        symbol = row.get("symbol")
        dtv = row.get("datetime") or row.get("current_price_time") or row.get("received_at")
        content = row.get("content") if isinstance(row.get("content"), dict) else row

        self.add_from_push_content(symbol, dtv, content)

    def add_row(self, row: dict) -> None:
        """
        旧互換 add_row。
        """
        self.add_order_book_row(row)

    # ========================================================
    # FLUSH helpers
    # ========================================================

    def _make_safe_rows(self, data: list[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
        safe_rows: list[tuple[Any, ...]] = []

        for row in data:
            try:
                safe = _sqlite_safe_row(row)

                # order_book schema:
                # (symbol, datetime, sequence, side, level, price, qty)
                if len(safe) != 7:
                    logger.warning(
                        "[OrderBookDB] invalid row length skipped len=%s row=%r",
                        len(safe),
                        safe,
                    )
                    continue

                symbol, dtv, seq, side, level, price, qty = safe

                symbol = _normalize_symbol(symbol)
                dtv = _sqlite_safe_datetime(dtv)
                seq = _safe_int(seq, default=0)
                side = _normalize_side(side)
                level = _safe_int(level, default=0)
                price = _safe_float_or_none(price)
                qty = _safe_float_or_none(qty)

                if not symbol or not dtv or not side or level <= 0:
                    logger.debug(
                        "[OrderBookDB] invalid row skipped symbol=%r datetime=%r side=%r level=%r",
                        symbol,
                        dtv,
                        side,
                        level,
                    )
                    continue

                safe_rows.append(
                    (
                        symbol,
                        dtv,
                        seq,
                        side,
                        level,
                        price,
                        qty,
                    )
                )

            except Exception:
                logger.debug("[OrderBookDB] row normalize failed row=%r", row, exc_info=True)

        return safe_rows

    def _log_sample_types(self, rows: list[tuple[Any, ...]]) -> None:
        try:
            if not rows:
                return

            sample = rows[0]
            logger.debug(
                "[OrderBookDB] flush sample types=%s values=%s",
                [type(x).__name__ for x in sample],
                sample,
            )
        except Exception:
            logger.debug("[OrderBookDB] sample debug failed", exc_info=True)

    def _requeue_front(self, data: list[tuple[Any, ...]]) -> None:
        """
        flush失敗時、未保存データを buffer 先頭へ戻す。
        無限増殖を防ぐため、戻すのは元 data そのものだけ。
        """
        if not data:
            return

        with self.lock:
            self.buffer = list(data) + self.buffer

            logger.warning(
                "[OrderBookDB] requeued failed rows=%s buffer_now=%s",
                len(data),
                len(self.buffer),
            )

    # ========================================================
    # FLUSH
    # ========================================================

    def flush(self) -> None:
        with self.lock:
            if self._flushing:
                return

            if not self.buffer:
                return

            if not self.conn:
                try:
                    self._open_connection()
                except Exception:
                    logger.exception("[OrderBookDB] open connection before flush failed")
                    return

            self._flushing = True
            data = self.buffer[:]
            self.buffer.clear()

        safe_rows: list[tuple[Any, ...]] = []

        try:
            safe_rows = self._make_safe_rows(data)

            if not safe_rows:
                logger.warning(
                    "[OrderBookDB] flush skipped reason=no safe rows original_rows=%s",
                    len(data),
                )
                return

            self._log_sample_types(safe_rows)

            retry = 0

            while retry < 5:
                try:
                    if not self.conn:
                        self._open_connection()

                    if not self.conn:
                        raise RuntimeError("OrderBookDB connection unavailable")

                    cur = self.conn.cursor()

                    cur.executemany(
                        """
                        INSERT INTO order_book
                        (symbol, datetime, sequence, side, level, price, qty)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        safe_rows,
                    )

                    self.conn.commit()
                    cur.close()

                    logger.info(
                        "[OrderBookDB] flushed rows=%s original_rows=%s buffer_remaining=%s",
                        len(safe_rows),
                        len(data),
                        len(self.buffer),
                    )

                    return

                except sqlite3.OperationalError as e:
                    if "locked" in str(e).lower():
                        retry += 1
                        sleep_s = 0.2 * retry
                        time.sleep(sleep_s)

                        logger.warning(
                            "[OrderBookDB] retry flush retry=%s sleep=%.3fs rows=%s",
                            retry,
                            sleep_s,
                            len(safe_rows),
                        )
                        continue

                    raise

        except Exception:
            logger.exception("[OrderBookDB] flush failed")

            try:
                self._requeue_front(data)
            except Exception:
                logger.exception("[OrderBookDB] failed to requeue rows after flush failure")

            try:
                with self.lock:
                    try:
                        if self.conn:
                            self.conn.close()
                    except Exception:
                        pass
                    finally:
                        self.conn = None

                    self._open_connection()
            except Exception:
                logger.exception("[OrderBookDB] connection recovery failed")

        finally:
            with self.lock:
                self._flushing = False

    # ========================================================
    # START / STOP
    # ========================================================

    def start(self) -> None:
        with self.lock:
            if self._started and self._thread is not None and self._thread.is_alive():
                logger.info("[OrderBookDB] writer already started")
                return

            self._stop_event.clear()
            self._open_connection()
            self._started = True

            self._thread = threading.Thread(
                target=self._loop,
                name="OrderBookDBWriter",
                daemon=True,
            )
            self._thread.start()

    def stop(self, *, flush: bool = True, join_timeout: float = 3.0) -> None:
        logger.info("[OrderBookDB] stopping")

        try:
            self._stop_event.set()

            if flush:
                try:
                    self.flush()
                except Exception:
                    logger.exception("[OrderBookDB] flush during stop failed")

            th = self._thread
            if th is not None and th.is_alive():
                th.join(timeout=float(join_timeout))

            with self.lock:
                try:
                    if self.conn:
                        self.conn.close()
                except Exception:
                    pass
                finally:
                    self.conn = None

                self._started = False

            logger.info("[OrderBookDB] stopped")

        except Exception:
            logger.exception("[OrderBookDB] stop failed")

    # ========================================================
    # WRITER LOOP
    # ========================================================

    def _loop(self) -> None:
        logger.info("[OrderBookDB] writer loop started")

        while not self._stop_event.is_set():
            try:
                time.sleep(1)

                self._rotate_if_needed()
                self.flush()

            except Exception:
                logger.exception("[OrderBookDB] loop error")
                time.sleep(1)

        try:
            self.flush()
        except Exception:
            logger.exception("[OrderBookDB] final flush failed")

        logger.info("[OrderBookDB] writer loop stopped")


__all__ = [
    "OrderBookDBWriter",
]