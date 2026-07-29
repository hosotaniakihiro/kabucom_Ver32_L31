# ============================================================
# File   : trading/ranking/ranking_db_writer.py
# Version: PRODUCTION-STABLE-RANKING-DB-WRITER-SQLITE3-DIRECT-REV6-SLIM
# ------------------------------------------------------------
# Purpose:
#   PUSH DB WRITER と同じ方式でランキング保存を行う。
#
# Design:
#   - 専用 sqlite3.Connection を保持
#   - PRAGMA journal_mode=WAL
#   - PRAGMA synchronous=NORMAL
#   - PRAGMA wal_autocheckpoint=1000
#   - buffer に積んで専用 thread が flush
#   - executemany -> commit
#
# REV6:
#   ✔ 移動済み責務を削除
#      - DBパス解決は database.paths.ranking_paths.get_ranking_db_path へ委譲
#      - raw schema は database.schema.ranking_raw_schema へ委譲
#      - snapshot schema は database.schema.ranking_snapshot_schema へ委譲
#      - raw normalize は database.upsert.ranking_raw_upsert.normalize_raw_row へ委譲
#      - snapshot normalize は database.upsert.ranking_snapshot_upsert.normalize_snapshot_row へ委譲
#      - legacy ranking type / market は database.bases の定義を利用
#   ✔ writer は PUSH方式の常駐保存処理に集中
#   ✔ 起動時に全DB作成済み前提
#   ✔ writer側の schema ensure は保険として最小限だけ実行
#   ✔ legacy table は保存時に必要なものだけ保険作成
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
import threading
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any, Optional

from database.paths.ranking_paths import get_ranking_db_path
from database.schema.ranking_raw_schema import (
    RAW_TABLE,
    ensure_ranking_raw_table,
)
from database.schema.ranking_snapshot_schema import (
    SNAPSHOT_TABLE,
    ensure_ranking_snapshot_table,
    patch_ranking_snapshot_schema,
)
from database.upsert.ranking_raw_upsert import normalize_raw_row
from database.upsert.ranking_snapshot_upsert import normalize_snapshot_row
from database.sqlite import quote_ident

try:
    from database.bases import TYPE_TO_TABLE, EXCHANGE_DIVISIONS
except Exception:
    TYPE_TO_TABLE = {
        1: "値上がり率",
        2: "値下がり率",
        3: "売買高上位",
        4: "売買代金",
        5: "TICK回数",
        6: "売買高急増",
        7: "売買代金急増",
    }
    EXCHANGE_DIVISIONS = {
        "ALL": "全市場",
        "TP": "東証プライム",
        "TS": "東証スタンダード",
        "TG": "東証グロース",
    }

logger = logging.getLogger(__name__)


# ============================================================
# global_data optional
# ============================================================

try:
    from core.global_context.context import global_data  # type: ignore
except Exception:
    try:
        from global_state import global_data  # type: ignore
    except Exception:
        global_data = None  # type: ignore


# ============================================================
# constants
# ============================================================

DEFAULT_BUFFER_SIZE = 1
DEFAULT_FLUSH_INTERVAL_SEC = 2.0
DEFAULT_BUSY_TIMEOUT_MS = 5000
DEFAULT_WAL_AUTOCHECKPOINT = 1000

LEGACY_RANKING_TYPES = tuple(dict.fromkeys(str(v) for v in TYPE_TO_TABLE.values()))
LEGACY_MARKETS = tuple(str(k) for k in EXCHANGE_DIVISIONS.keys())


# ============================================================
# env helpers
# ============================================================

def _env_int(name: str, default: int) -> int:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(v)
    except Exception:
        return int(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return bool(default)
    return str(v).strip().lower() in {"1", "true", "yes", "on", "y"}


def _resolve_ranking_db_path() -> Path:
    """
    ranking DB path を取得する。

    REV6:
      独自のNASパス組み立ては削除。
      database.paths.ranking_paths.get_ranking_db_path に一本化する。
    """
    return Path(get_ranking_db_path())


# ============================================================
# small helpers
# ============================================================

def _legacy_table_name(ranking_type: str, market: str) -> str:
    rt = str(ranking_type or "").strip() or "UNKNOWN"
    mk = str(market or "ALL").strip() or "ALL"
    return f"{rt}_{mk}"


def _copy_rows(rows: Any) -> list[dict]:
    out: list[dict] = []
    for r in rows or []:
        if isinstance(r, dict):
            out.append(dict(r))
        else:
            try:
                out.append(dict(r))
            except Exception:
                pass
    return out


def _ranking_type(row: dict) -> str:
    return str(
        row.get("ranking_type")
        or row.get("rank_type")
        or row.get("category")
        or row.get("type")
        or row.get("Type")
        or row.get("ランキング種別")
        or ""
    ).strip()


def _market(row: dict) -> str:
    return str(
        row.get("market")
        or row.get("exchange")
        or row.get("Market")
        or row.get("市場")
        or "ALL"
    ).strip() or "ALL"


def _type_counts(rows: list[dict]) -> dict[str, int]:
    try:
        return dict(Counter(_ranking_type(r) or "?" for r in rows))
    except Exception:
        return {}


def _market_counts(rows: list[dict]) -> dict[str, int]:
    try:
        return dict(Counter(_market(r) or "ALL" for r in rows))
    except Exception:
        return {}


def _minute_str(value: Any = None) -> str:
    """
    legacy用の inserted_at 補助。
    raw/snapshot の日時正規化は database.upsert 側に委譲する。
    """
    if value is None or value == "":
        return dt.datetime.now().replace(second=0, microsecond=0).strftime("%Y-%m-%d %H:%M:%S")

    if isinstance(value, dt.datetime):
        return value.replace(second=0, microsecond=0).strftime("%Y-%m-%d %H:%M:%S")

    if isinstance(value, dt.date):
        return dt.datetime.combine(value, dt.time.min).strftime("%Y-%m-%d %H:%M:%S")

    try:
        s = str(value).strip().replace("T", " ")
        if not s:
            return dt.datetime.now().replace(second=0, microsecond=0).strftime("%Y-%m-%d %H:%M:%S")

        if "+" in s:
            s = s.split("+", 1)[0].strip()
        if s.endswith("Z"):
            s = s[:-1].strip()
        if "." in s:
            s = s.split(".", 1)[0]

        for fmt in (
            "%Y-%m-%d %H:%M:%S",
            "%Y/%m/%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y/%m/%d %H:%M",
            "%Y%m%d %H:%M:%S",
            "%Y%m%d %H:%M",
            "%Y-%m-%d",
            "%Y/%m/%d",
            "%Y%m%d",
        ):
            try:
                d = dt.datetime.strptime(s, fmt).replace(second=0, microsecond=0)
                return d.strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                pass

        d = dt.datetime.fromisoformat(s).replace(second=0, microsecond=0)
        return d.strftime("%Y-%m-%d %H:%M:%S")

    except Exception:
        return dt.datetime.now().replace(second=0, microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def _to_float(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        if isinstance(v, str):
            s = (
                v.strip()
                .replace(",", "")
                .replace("%", "")
                .replace("％", "")
                .replace("円", "")
            )
            if not s or s in ("-", "－", "—", "None", "nan", "NaN"):
                return None
            return float(s)
        return float(v)
    except Exception:
        return None


def _to_int(v: Any) -> Optional[int]:
    if v is None or v == "":
        return None
    try:
        if isinstance(v, str):
            s = v.strip().replace(",", "")
            if not s or s in ("-", "－", "—", "None", "nan", "NaN"):
                return None
            return int(float(s))
        return int(float(v))
    except Exception:
        return None


def _symbol(v: Any) -> str:
    s = str(v or "").strip()
    if s.endswith(".0"):
        s2 = s[:-2]
        if s2.isdigit():
            s = s2
    return s


def _normalize_for_legacy(row: dict, now_dt: Any = None) -> dict:
    """
    旧カテゴリ別テーブル保存用の軽い正規化。

    raw/snapshot本体の正規化は database.upsert 側に移管済み。
    legacy table は過去互換用なので、ここでは最低限だけ行う。
    """
    minute = _minute_str(
        row.get("datetime")
        or row.get("snapshot_time")
        or row.get("inserted_at")
        or row.get("created_at")
        or now_dt
    )

    rt = _ranking_type(row) or "UNKNOWN"
    mk = _market(row) or "ALL"

    symbol = _symbol(
        row.get("symbol")
        or row.get("Symbol")
        or row.get("code")
        or row.get("Code")
        or row.get("銘柄コード")
    )

    return {
        "symbol": symbol,
        "symbolname": (
            row.get("symbolname")
            or row.get("SymbolName")
            or row.get("name")
            or row.get("Name")
            or row.get("銘柄名")
        ),
        "current_price": (
            _to_float(row.get("current_price"))
            or _to_float(row.get("price"))
            or _to_float(row.get("CurrentPrice"))
            or _to_float(row.get("現在値"))
        ),
        "change_percentage": (
            _to_float(row.get("change_percentage"))
            or _to_float(row.get("change_rate"))
            or _to_float(row.get("change_ratio"))
            or _to_float(row.get("ChangePercentage"))
            or _to_float(row.get("騰落率"))
            or _to_float(row.get("value"))
        ),
        "change_ratio": _to_float(row.get("change_ratio")),
        "trading_volume": (
            _to_float(row.get("trading_volume"))
            or _to_float(row.get("volume"))
            or _to_float(row.get("TradingVolume"))
            or _to_float(row.get("売買高"))
        ),
        "trading_value": (
            _to_float(row.get("trading_value"))
            or _to_float(row.get("turnover"))
            or _to_float(row.get("TradingValue"))
            or _to_float(row.get("売買代金"))
            or _to_float(row.get("value_amount"))
        ),
        "turnover": (
            _to_float(row.get("turnover"))
            or _to_float(row.get("trading_value"))
            or _to_float(row.get("TradingValue"))
            or _to_float(row.get("売買代金"))
            or _to_float(row.get("value_amount"))
        ),
        "tick_count": _to_int(
            row.get("tick_count")
            or row.get("TickCount")
            or row.get("TICK回数")
        ),
        "inserted_at": minute,
        "rank": _to_int(
            row.get("rank")
            or row.get("best_rank")
            or row.get("rank_position")
            or row.get("Rank")
            or row.get("順位")
        ),
        "ranking_type": rt,
        "market": mk,
    }


# ============================================================
# legacy table schema repair
# (旧 core/startup/sqlite_memory_pragmas_patch.py の
#  _install_ranking_legacy_schema_patch から移設)
#
# CREATE TABLE IF NOT EXISTS は既存テーブルの列不足を補修しないため、
# 古い ranking DB では INSERT rank で落ちる。CREATE 直後に不足列を
# ALTER TABLE で補う。
# ============================================================

LEGACY_RANKING_COLUMNS: tuple[tuple[str, str], ...] = (
    ("symbol", "TEXT"),
    ("symbolname", "TEXT"),
    ("current_price", "REAL"),
    ("change_percentage", "REAL"),
    ("change_ratio", "REAL"),
    ("trading_volume", "REAL"),
    ("trading_value", "REAL"),
    ("turnover", "REAL"),
    ("tick_count", "INTEGER"),
    ("inserted_at", "TEXT"),
    ("rank", "INTEGER"),
)


def _ensure_columns_with_cursor(cur: Any, table: str, columns: tuple[tuple[str, str], ...], label: str) -> list[str]:
    q = quote_ident(table)
    try:
        cur.execute(f"PRAGMA table_info({q})")
        existing = {str(row[1]) for row in cur.fetchall()}
    except Exception:
        logger.debug("[%s] table_info failed table=%s", label, table, exc_info=True)
        return []

    added: list[str] = []
    for col, decl in columns:
        if col in existing:
            continue
        try:
            cur.execute(f"ALTER TABLE {q} ADD COLUMN {quote_ident(col)} {decl}")
            added.append(col)
            existing.add(col)
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            if "duplicate column" in msg or "already exists" in msg:
                existing.add(col)
                continue
            raise
    if added:
        logger.warning("[%s] table=%s added_columns=%s", label, table, added)
    return added


# ============================================================
# writer
# ============================================================

class RankingDBWriter:
    def __init__(
        self,
        *,
        buffer_size: int | None = None,
        flush_interval_sec: float | None = None,
        enable_legacy_save: bool = True,
    ) -> None:
        self.buffer_size = int(buffer_size or _env_int("RANKING_WRITER_BUFFER_SIZE", DEFAULT_BUFFER_SIZE))
        self.flush_interval_sec = float(
            flush_interval_sec
            if flush_interval_sec is not None
            else _env_float("RANKING_WRITER_FLUSH_INTERVAL_SEC", DEFAULT_FLUSH_INTERVAL_SEC)
        )
        self.enable_legacy_save = bool(enable_legacy_save)

        self.lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._started = False

        self.conn: Optional[sqlite3.Connection] = None
        self.cursor: Optional[sqlite3.Cursor] = None
        self.current_date: Optional[dt.date] = None
        self.db_path: Optional[Path] = None

        self.raw_buffer: list[dict] = []
        self.snapshot_buffer: list[dict] = []
        self.legacy_buffer: list[dict] = []

        self.total_queued_raw = 0
        self.total_queued_snapshot = 0
        self.total_queued_legacy = 0
        self.total_flushed_raw = 0
        self.total_flushed_snapshot = 0
        self.total_flushed_legacy = 0
        self.total_flush_errors = 0

        self.last_flush_at: Optional[str] = None
        self.last_flush_elapsed_sec: float = 0.0
        self.last_error: Optional[str] = None

    # ========================================================
    # runtime metrics
    # ========================================================

    def _set_runtime_metric(self, name: str, value: Any) -> None:
        if global_data is None:
            return
        try:
            setattr(global_data, name, value)
        except Exception:
            pass

    def _mark_runtime(self) -> None:
        try:
            self._set_runtime_metric("ranking_writer_started", self._started)
            self._set_runtime_metric("ranking_writer_db_path", str(self.db_path) if self.db_path else "")
            self._set_runtime_metric("ranking_writer_buffer_raw", len(self.raw_buffer))
            self._set_runtime_metric("ranking_writer_buffer_snapshot", len(self.snapshot_buffer))
            self._set_runtime_metric("ranking_writer_buffer_legacy", len(self.legacy_buffer))
            self._set_runtime_metric("ranking_writer_total_queued_raw", self.total_queued_raw)
            self._set_runtime_metric("ranking_writer_total_queued_snapshot", self.total_queued_snapshot)
            self._set_runtime_metric("ranking_writer_total_queued_legacy", self.total_queued_legacy)
            self._set_runtime_metric("ranking_writer_total_flushed_raw", self.total_flushed_raw)
            self._set_runtime_metric("ranking_writer_total_flushed_snapshot", self.total_flushed_snapshot)
            self._set_runtime_metric("ranking_writer_total_flushed_legacy", self.total_flushed_legacy)
            self._set_runtime_metric("ranking_writer_total_flush_errors", self.total_flush_errors)
            self._set_runtime_metric("ranking_writer_last_flush_at", self.last_flush_at)
            self._set_runtime_metric("ranking_writer_last_flush_elapsed_sec", self.last_flush_elapsed_sec)
            self._set_runtime_metric("ranking_writer_last_error", self.last_error)
        except Exception:
            pass

    # ========================================================
    # connection
    # ========================================================

    def _open_connection(self) -> None:
        self.current_date = dt.datetime.now().date()
        db_path = _resolve_ranking_db_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)

        self.db_path = db_path

        self.conn = sqlite3.connect(
            str(db_path),
            timeout=10,
            check_same_thread=False,
        )
        self.cursor = self.conn.cursor()

        self.cursor.execute("PRAGMA journal_mode=WAL;")
        self.cursor.execute("PRAGMA synchronous=NORMAL;")
        self.cursor.execute(f"PRAGMA wal_autocheckpoint={DEFAULT_WAL_AUTOCHECKPOINT};")
        self.cursor.execute(f"PRAGMA busy_timeout={DEFAULT_BUSY_TIMEOUT_MS};")
        self.cursor.execute("PRAGMA temp_store=MEMORY;")
        self.cursor.execute("PRAGMA cache_size=-50000;")

        self._prepare_schema_safely()

        logger.info(
            "[RANKING DB WRITER] connected db=%s buffer_size=%d flush_interval=%.3f wal_autocheckpoint=%d",
            db_path,
            self.buffer_size,
            self.flush_interval_sec,
            DEFAULT_WAL_AUTOCHECKPOINT,
        )

    def _ensure_connection(self) -> None:
        if self.conn is None or self.cursor is None:
            self._open_connection()
            return

        today = dt.datetime.now().date()
        if self.current_date != today:
            logger.info("[RANKING DB WRITER] date changed -> rotate %s -> %s", self.current_date, today)
            try:
                self.flush()
            except Exception:
                logger.exception("[RANKING DB WRITER] flush before rotate failed")
            try:
                self.conn.close()
            except Exception:
                logger.exception("[RANKING DB WRITER] close before rotate failed")
            self.conn = None
            self.cursor = None
            self._open_connection()

    # ========================================================
    # schema
    # ========================================================

    def _prepare_schema_safely(self) -> None:
        """
        起動時DB bootstrapで全テーブル作成済みが原則。

        ここでは writer 起動時の保険として、
        raw/snapshot の schema ensure だけ最小限行う。
        legacy table は全件総作成せず、save_legacy時に必要なものだけ作成する。
        """
        assert self.conn is not None

        ensure_ranking_snapshot_table(self.conn)
        patch_ranking_snapshot_schema(self.conn)
        ensure_ranking_raw_table(self.conn)

        self.conn.commit()

    def _ensure_legacy_table(self, table: str) -> None:
        """
        legacy table は保存時だけ保険作成する。
        起動時に全legacyテーブルを総作成する責務は migrate/bootstrap 側へ移管。
        """
        assert self.cursor is not None

        q = quote_ident(table)

        self.cursor.execute(f"""
        CREATE TABLE IF NOT EXISTS {q} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT,
            symbolname TEXT,
            current_price REAL,
            change_percentage REAL,
            change_ratio REAL,
            trading_volume REAL,
            trading_value REAL,
            turnover REAL,
            tick_count INTEGER,
            inserted_at TEXT,
            rank INTEGER
        )
        """)

        self.cursor.execute(
            f"CREATE INDEX IF NOT EXISTS {quote_ident('idx_' + table + '_inserted_at')} "
            f"ON {q}(inserted_at)"
        )
        self.cursor.execute(
            f"CREATE INDEX IF NOT EXISTS {quote_ident('idx_' + table + '_symbol_inserted_at')} "
            f"ON {q}(symbol, inserted_at)"
        )

        if not _env_bool("DISABLE_RANKING_LEGACY_SCHEMA_REPAIR_PATCH", False):
            _ensure_columns_with_cursor(self.cursor, table, LEGACY_RANKING_COLUMNS, "RANKING LEGACY SCHEMA REPAIR")

    # ========================================================
    # lifecycle
    # ========================================================

    def start(self) -> None:
        with self.lock:
            if self._started and self._thread and self._thread.is_alive():
                logger.debug("[RANKING DB WRITER] start skipped already running")
                return

            if self.conn is None or self.cursor is None:
                self._open_connection()

            self._stop_event.clear()
            th = threading.Thread(
                target=self._loop,
                daemon=True,
                name="RankingDBWriterLoop",
            )
            self._thread = th
            self._started = True
            th.start()

            logger.info("[RANKING DB WRITER] writer thread started")
            self._mark_runtime()

    def stop(self) -> None:
        logger.info("[RANKING DB WRITER] stop requested")
        self._stop_event.set()

        try:
            self.flush()
        except Exception:
            logger.exception("[RANKING DB WRITER] flush on stop failed")

        with self.lock:
            try:
                if self.cursor is not None:
                    self.cursor.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                    logger.info("[RANKING DB WRITER] stop checkpoint TRUNCATE done")
            except Exception:
                logger.exception("[RANKING DB WRITER] stop checkpoint TRUNCATE failed")

            try:
                if self.conn is not None:
                    self.conn.close()
            except Exception:
                logger.exception("[RANKING DB WRITER] close failed")

            self.conn = None
            self.cursor = None
            self._started = False
            self._mark_runtime()

    def _loop(self) -> None:
        logger.info("[RANKING DB WRITER] loop started")

        while not self._stop_event.is_set():
            try:
                with self.lock:
                    has_buffer = bool(self.raw_buffer or self.snapshot_buffer or self.legacy_buffer)
                    raw_len = len(self.raw_buffer)
                    snapshot_len = len(self.snapshot_buffer)
                    legacy_len = len(self.legacy_buffer)

                if has_buffer:
                    logger.debug(
                        "[RANKING DB WRITER] loop flush trigger raw=%d snapshot=%d legacy=%d",
                        raw_len,
                        snapshot_len,
                        legacy_len,
                    )
                    self.flush()

                time.sleep(self.flush_interval_sec)

            except Exception:
                logger.exception("[RANKING DB WRITER] loop error")
                time.sleep(1.0)

    # ========================================================
    # add
    # ========================================================

    def add_ranking_rows(
        self,
        *,
        raw_rows: list[dict] | None = None,
        snapshot_rows: list[dict] | None = None,
        save_legacy: bool = False,
        now_dt: Any = None,
        source: str = "scheduler_core",
    ) -> dict[str, Any]:
        self.start()

        raw = _copy_rows(raw_rows)
        snapshot = _copy_rows(snapshot_rows)
        legacy = _copy_rows(raw_rows) if save_legacy and self.enable_legacy_save else []

        batch_id = uuid.uuid4().hex
        queued_at = dt.datetime.now().isoformat(timespec="seconds")
        target_minute = _minute_str(now_dt) if now_dt is not None else None

        for rows in (raw, snapshot, legacy):
            for r in rows:
                r.setdefault("_ranking_writer_batch_id", batch_id)
                r.setdefault("_ranking_writer_source", source)
                r.setdefault("_ranking_writer_queued_at", queued_at)

                if target_minute is not None:
                    r.setdefault("datetime", target_minute)
                    r.setdefault("snapshot_time", target_minute)

        with self.lock:
            self.raw_buffer.extend(raw)
            self.snapshot_buffer.extend(snapshot)
            self.legacy_buffer.extend(legacy)

            self.total_queued_raw += len(raw)
            self.total_queued_snapshot += len(snapshot)
            self.total_queued_legacy += len(legacy)

            raw_buffer_len = len(self.raw_buffer)
            snapshot_buffer_len = len(self.snapshot_buffer)
            legacy_buffer_len = len(self.legacy_buffer)

            self._mark_runtime()

        logger.info(
            "[RANKING DB WRITER] queued source=%s save_legacy=%s raw=%d snapshot=%d legacy=%d "
            "buffer_raw=%d buffer_snapshot=%d buffer_legacy=%d type_counts=%s market_counts=%s",
            source,
            save_legacy,
            len(raw),
            len(snapshot),
            len(legacy),
            raw_buffer_len,
            snapshot_buffer_len,
            legacy_buffer_len,
            _type_counts(raw or snapshot),
            _market_counts(raw or snapshot),
        )

        if (
            raw_buffer_len >= self.buffer_size
            or snapshot_buffer_len >= self.buffer_size
            or legacy_buffer_len >= self.buffer_size
        ):
            logger.info(
                "[RANKING DB WRITER] buffer reached threshold raw=%d snapshot=%d legacy=%d threshold=%d",
                raw_buffer_len,
                snapshot_buffer_len,
                legacy_buffer_len,
                self.buffer_size,
            )
            if _env_bool("RANKING_WRITER_FLUSH_ON_THRESHOLD", False):
                self.flush()

        return {
            "ok": True,
            "queued_raw": len(raw),
            "queued_snapshot": len(snapshot),
            "queued_legacy": len(legacy),
            "batch_id": batch_id,
            "buffer_raw": raw_buffer_len,
            "buffer_snapshot": snapshot_buffer_len,
            "buffer_legacy": legacy_buffer_len,
        }

    # ========================================================
    # tuple helpers
    # ========================================================

    @staticmethod
    def _snapshot_delete_key(row_tuple: tuple) -> tuple:
        """
        normalize_snapshot_row の tuple order に合わせる。

        想定:
          0  symbol
          1  datetime
          13 ranking_type
          16 market
        """
        return (
            row_tuple[0],
            row_tuple[1],
            row_tuple[13],
            row_tuple[16],
        )

    @staticmethod
    def _legacy_tuple(row: dict) -> tuple:
        x = _normalize_for_legacy(row)
        return (
            x.get("symbol"),
            x.get("symbolname"),
            x.get("current_price"),
            x.get("change_percentage"),
            x.get("change_ratio"),
            x.get("trading_volume"),
            x.get("trading_value"),
            x.get("turnover"),
            x.get("tick_count"),
            x.get("inserted_at"),
            x.get("rank"),
        )

    # ========================================================
    # flush
    # ========================================================

    def flush(self) -> bool:
        with self.lock:
            if not (self.raw_buffer or self.snapshot_buffer or self.legacy_buffer):
                logger.debug("[RANKING DB WRITER] flush skipped empty")
                return True

            raw_rows = self.raw_buffer
            snapshot_rows = self.snapshot_buffer
            legacy_rows = self.legacy_buffer

            self.raw_buffer = []
            self.snapshot_buffer = []
            self.legacy_buffer = []

        t0 = time.time()
        saved_snapshot = 0
        saved_raw = 0
        saved_legacy = 0

        try:
            with self.lock:
                self._ensure_connection()

                assert self.conn is not None
                assert self.cursor is not None

                logger.info(
                    "[RANKING DB WRITER] flush prepare raw=%d snapshot=%d legacy=%d snapshot_types=%s raw_types=%s",
                    len(raw_rows),
                    len(snapshot_rows),
                    len(legacy_rows),
                    _type_counts(snapshot_rows),
                    _type_counts(raw_rows),
                )

                snapshot_delete_sql = f"""
                DELETE FROM {quote_ident(SNAPSHOT_TABLE)}
                 WHERE symbol = ?
                   AND datetime = ?
                   AND ranking_type = ?
                   AND market = ?
                """

                snapshot_insert_sql = f"""
                INSERT INTO {quote_ident(SNAPSHOT_TABLE)} (
                    symbol,
                    datetime,
                    snapshot_time,
                    symbolname,
                    current_price,
                    price,
                    change_percentage,
                    change_rate,
                    trading_volume,
                    volume,
                    trading_value,
                    turnover,
                    tick_count,
                    ranking_type,
                    rank_type,
                    category,
                    market,
                    exchange,
                    source,
                    rank,
                    created_at,
                    inserted_at
                )
                VALUES ({",".join(["?"] * 22)})
                """

                raw_insert_sql = f"""
                INSERT OR IGNORE INTO {quote_ident(RAW_TABLE)} (
                    ingest_id,
                    symbol,
                    datetime,
                    snapshot_time,
                    symbolname,
                    current_price,
                    price,
                    change_percentage,
                    change_rate,
                    change_ratio,
                    trading_volume,
                    volume,
                    trading_value,
                    turnover,
                    tick_count,
                    ranking_type,
                    rank_type,
                    category,
                    market,
                    exchange,
                    source,
                    rank,
                    date,
                    time,
                    raw_json,
                    received_at,
                    created_at,
                    inserted_at,
                    updated_at
                )
                VALUES ({",".join(["?"] * 29)})
                """

                if snapshot_rows:
                    snapshot_params: list[tuple] = []
                    delete_keys: list[tuple] = []

                    for row in snapshot_rows:
                        try:
                            t = normalize_snapshot_row(row)
                            snapshot_params.append(t)
                            delete_keys.append(self._snapshot_delete_key(t))
                        except Exception:
                            logger.warning(
                                "[RANKING DB WRITER] snapshot normalize skipped row=%r",
                                row,
                                exc_info=True,
                            )

                    if snapshot_params:
                        self.cursor.executemany(snapshot_delete_sql, delete_keys)
                        self.cursor.executemany(snapshot_insert_sql, snapshot_params)
                        saved_snapshot = len(snapshot_params)

                if raw_rows:
                    raw_params: list[tuple] = []

                    for row in raw_rows:
                        try:
                            raw_params.append(normalize_raw_row(row))
                        except Exception:
                            logger.warning(
                                "[RANKING DB WRITER] raw normalize skipped row=%r",
                                row,
                                exc_info=True,
                            )

                    if raw_params:
                        self.cursor.executemany(raw_insert_sql, raw_params)
                        saved_raw = len(raw_params)

                if legacy_rows:
                    grouped: dict[tuple[str, str], list[dict]] = {}

                    for row in legacy_rows:
                        try:
                            x = _normalize_for_legacy(row)
                            key = (
                                str(x.get("ranking_type") or "UNKNOWN"),
                                str(x.get("market") or "ALL"),
                            )
                            grouped.setdefault(key, []).append(row)
                        except Exception:
                            logger.debug(
                                "[RANKING DB WRITER] legacy group skipped row=%r",
                                row,
                                exc_info=True,
                            )

                    for (ranking_type, market), rows in grouped.items():
                        table = _legacy_table_name(ranking_type, market)
                        self._ensure_legacy_table(table)
                        q = quote_ident(table)

                        legacy_sql = f"""
                        INSERT INTO {q} (
                            symbol,
                            symbolname,
                            current_price,
                            change_percentage,
                            change_ratio,
                            trading_volume,
                            trading_value,
                            turnover,
                            tick_count,
                            inserted_at,
                            rank
                        )
                        VALUES ({",".join(["?"] * 11)})
                        """

                        params = [self._legacy_tuple(r) for r in rows]
                        if params:
                            self.cursor.executemany(legacy_sql, params)
                            saved_legacy += len(params)

                self.conn.commit()

                try:
                    if _env_bool("RANKING_WRITER_PASSIVE_CHECKPOINT_AFTER_FLUSH", False):
                        self.cursor.execute("PRAGMA wal_checkpoint(PASSIVE);")
                except Exception:
                    logger.debug("[RANKING DB WRITER] passive checkpoint after flush failed", exc_info=True)

            elapsed = time.time() - t0
            now_iso = dt.datetime.now().isoformat(timespec="seconds")

            with self.lock:
                self.total_flushed_snapshot += saved_snapshot
                self.total_flushed_raw += saved_raw
                self.total_flushed_legacy += saved_legacy
                self.last_flush_at = now_iso
                self.last_flush_elapsed_sec = float(elapsed)
                self.last_error = None
                self._mark_runtime()

            logger.warning(
                "[RANKING DB WRITER] flush done ok=True db=%s snapshot_flushed=%d raw_flushed=%d legacy_flushed=%d "
                "input_snapshot=%d input_raw=%d input_legacy=%d elapsed=%.3fs",
                self.db_path,
                saved_snapshot,
                saved_raw,
                saved_legacy,
                len(snapshot_rows),
                len(raw_rows),
                len(legacy_rows),
                elapsed,
            )
            return True

        except Exception as e:
            elapsed = time.time() - t0

            with self.lock:
                self.snapshot_buffer = list(snapshot_rows) + self.snapshot_buffer
                self.raw_buffer = list(raw_rows) + self.raw_buffer
                self.legacy_buffer = list(legacy_rows) + self.legacy_buffer
                self.total_flush_errors += 1
                self.last_error = str(e)
                self.last_flush_elapsed_sec = float(elapsed)
                self._mark_runtime()

            logger.exception(
                "[RANKING DB WRITER] flush failed rows returned snapshot=%d raw=%d legacy=%d elapsed=%.3fs",
                len(snapshot_rows),
                len(raw_rows),
                len(legacy_rows),
                elapsed,
            )
            return False


# ============================================================
# singleton / public APIs
# ============================================================

ranking_writer = RankingDBWriter()


def ensure_ranking_writer_started() -> RankingDBWriter:
    ranking_writer.start()
    return ranking_writer


def stop_ranking_writer() -> None:
    ranking_writer.stop()


def flush_ranking_writer() -> bool:
    return ranking_writer.flush()


def add_ranking_rows_async(
    *,
    raw_rows: list[dict] | None = None,
    snapshot_rows: list[dict] | None = None,
    save_legacy: bool = False,
    now_dt: Any = None,
    source: str = "scheduler_core",
) -> dict[str, Any]:
    return ranking_writer.add_ranking_rows(
        raw_rows=raw_rows,
        snapshot_rows=snapshot_rows,
        save_legacy=save_legacy,
        now_dt=now_dt,
        source=source,
    )


__all__ = [
    "RankingDBWriter",
    "ranking_writer",
    "ensure_ranking_writer_started",
    "stop_ranking_writer",
    "flush_ranking_writer",
    "add_ranking_rows_async",
]