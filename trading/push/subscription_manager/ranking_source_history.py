# -*- coding: utf-8 -*-
"""
File   : trading/push/subscription_manager/ranking_source_history.py
Version: V32-L24-PRODUCTION-SAFE-HISTORY-SEPARATE-DB
Date   : 2026-04-27

Purpose:
    PUSH購読対象シンボルの履歴を専用DBに保存する。

Important:
    以前は rankingYYYYMMDD.db に subscription_symbols_history を保存していたが、
    ranking_snapshot_1min / ranking_raw_1min の保存処理とロック競合しやすい。

    本版では保存先を以下へ分離する。

        \\192.168.0.22\\AutoStockBuyAndSell
          \\raw_data\\kabu_station\\push_subscription
          \\subscription_historyYYYYMMDD.db

Fix:
    - ranking DB への履歴書き込みを廃止
    - PUSH購読履歴専用DBへ保存
    - sqlite3.OperationalError: database is locked 対策
    - on_open 再接続連打時の履歴保存連打を抑制
    - target が変わっていない場合は短時間では保存しない
    - DBロック時は短い retry 後に諦める
    - 履歴保存失敗で PUSH 本体を止めない
    - NAS SQLite 向けに transaction を短くする
    - journal_mode 変更は原則しない
    - busy_timeout を設定
    - 既存呼び出し側の引数揺れに対応

Notes:
    このモジュールは「履歴保存」専用。
    失敗しても購読処理・PUSH受信・ランキング読込を止めてはいけない。
"""

from __future__ import annotations

import os
import time
import json
import sqlite3
import logging
import datetime as dt
from pathlib import Path
from typing import Any, Iterable, Optional, Union

logger = logging.getLogger(__name__)


# ============================================================
# Constants
# ============================================================

DEFAULT_NAS_ROOT = r"\\192.168.0.22\AutoStockBuyAndSell"

DEFAULT_TABLE_NAME = "subscription_symbols_history"
DEFAULT_META_TABLE_NAME = "subscription_symbols_history_meta"

# 保存先を ranking DB から分離
DEFAULT_HISTORY_SUBDIR = (
    "raw_data",
    "kabu_station",
    "push_subscription",
)

DEFAULT_HISTORY_DB_PREFIX = "subscription_history"

# on_open 連打時の履歴保存抑制秒数
DEFAULT_MIN_SAVE_INTERVAL_SEC = float(os.environ.get("SUB_HISTORY_MIN_SAVE_INTERVAL_SEC", "20"))

# sqlite retry
DEFAULT_RETRY_COUNT = int(os.environ.get("SUB_HISTORY_SQLITE_RETRY_COUNT", "5"))
DEFAULT_RETRY_BASE_SLEEP = float(os.environ.get("SUB_HISTORY_SQLITE_RETRY_BASE_SLEEP", "0.08"))
DEFAULT_BUSY_TIMEOUT_MS = int(os.environ.get("SUB_HISTORY_SQLITE_BUSY_TIMEOUT_MS", "3000"))

# 1回の executemany chunk
DEFAULT_CHUNK_SIZE = int(os.environ.get("SUB_HISTORY_INSERT_CHUNK_SIZE", "100"))

# 保存自体を止めたい場合
ENABLE_HISTORY_SAVE = os.environ.get("SUB_HISTORY_SAVE_ENABLED", "1").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
)


# ============================================================
# Module state
# ============================================================

_LAST_SAVE_AT: Optional[dt.datetime] = None
_LAST_SIGNATURE: Optional[str] = None


# ============================================================
# Path helpers
# ============================================================

def _today_yyyymmdd() -> str:
    return dt.datetime.now().strftime("%Y%m%d")


def _normalize_yyyymmdd(value: Optional[Union[str, int, dt.date, dt.datetime]] = None) -> str:
    if value is None:
        return _today_yyyymmdd()

    if isinstance(value, dt.datetime):
        return value.strftime("%Y%m%d")

    if isinstance(value, dt.date):
        return value.strftime("%Y%m%d")

    s = str(value).strip()
    if not s:
        return _today_yyyymmdd()

    s = s.replace("-", "").replace("/", "")
    if len(s) >= 8 and s[:8].isdigit():
        return s[:8]

    return _today_yyyymmdd()


def _get_nas_root() -> str:
    for key in ("NAS_ROOT", "AUTOSTOCK_NAS_ROOT", "KABU_NAS_ROOT"):
        v = os.environ.get(key, "").strip()
        if v:
            return v.rstrip("\\/")
    return DEFAULT_NAS_ROOT


def _default_subscription_history_db_path(
    yyyymmdd: Optional[Union[str, int, dt.date, dt.datetime]] = None,
) -> str:
    """
    PUSH購読履歴専用DBの既定パスを返す。

    例:
      \\192.168.0.22\\AutoStockBuyAndSell
        \\raw_data\\kabu_station\\push_subscription
        \\subscription_history20260427.db
    """
    d = _normalize_yyyymmdd(yyyymmdd)

    return str(
        Path(_get_nas_root())
        .joinpath(*DEFAULT_HISTORY_SUBDIR)
        .joinpath(f"{DEFAULT_HISTORY_DB_PREFIX}{d}.db")
    )


def _resolve_db_path(
    db_path: Optional[Union[str, os.PathLike]] = None,
    yyyymmdd: Optional[Union[str, int, dt.date, dt.datetime]] = None,
) -> str:
    """
    保存先DBを解決する。

    重要:
      ranking_db_path / ranking_path が渡されても、
      互換のため受け取るだけで、既定では ranking DB には保存しない。

    明示的に db_path / path が渡された場合のみ、それを尊重する。
    """
    # 明示 db_path / path は尊重
    if db_path:
        return str(db_path)

    # 環境変数で専用DBパスを上書き可能
    for key in (
        "SUB_HISTORY_DB_PATH",
        "PUSH_SUBSCRIPTION_HISTORY_DB_PATH",
    ):
        v = os.environ.get(key, "").strip()
        if v:
            return v

    # 旧コードから ranking_db_path / ranking_path が kwargs で渡されても、
    # save_subscription_symbols_history 側で db_path に入れない限りここには来ない。
    # 既定は必ず専用DB。
    return _default_subscription_history_db_path(yyyymmdd)


# ============================================================
# Data normalize
# ============================================================

def _symbol_to_str(value: Any) -> str:
    if value is None:
        return ""

    s = str(value).strip()
    if not s:
        return ""

    # 7203.0 のような float 文字列対策
    if s.endswith(".0") and s.replace(".0", "").isdigit():
        s = s[:-2]

    return s


def _extract_symbol_and_name(item: Any) -> tuple[str, str]:
    """
    item が str / dict / pandas row / object のどれでも吸収する。
    """
    if item is None:
        return "", ""

    if isinstance(item, str) or isinstance(item, int):
        return _symbol_to_str(item), ""

    # dict 系
    if isinstance(item, dict):
        symbol = (
            item.get("symbol")
            or item.get("Symbol")
            or item.get("code")
            or item.get("Code")
            or item.get("銘柄コード")
            or ""
        )
        name = (
            item.get("symbolname")
            or item.get("symbol_name")
            or item.get("name")
            or item.get("Name")
            or item.get("銘柄名")
            or ""
        )
        return _symbol_to_str(symbol), str(name or "").strip()

    # pandas Series 等
    get = getattr(item, "get", None)
    if callable(get):
        try:
            symbol = (
                get("symbol", None)
                or get("Symbol", None)
                or get("code", None)
                or get("Code", None)
                or get("銘柄コード", None)
                or ""
            )
            name = (
                get("symbolname", None)
                or get("symbol_name", None)
                or get("name", None)
                or get("Name", None)
                or get("銘柄名", None)
                or ""
            )
            return _symbol_to_str(symbol), str(name or "").strip()
        except Exception:
            pass

    # object attribute 系
    symbol = (
        getattr(item, "symbol", None)
        or getattr(item, "Symbol", None)
        or getattr(item, "code", None)
        or getattr(item, "Code", None)
        or ""
    )
    name = (
        getattr(item, "symbolname", None)
        or getattr(item, "symbol_name", None)
        or getattr(item, "name", None)
        or getattr(item, "Name", None)
        or ""
    )

    return _symbol_to_str(symbol), str(name or "").strip()


def _normalize_symbols(symbols: Any) -> list[tuple[str, str]]:
    """
    symbols を [(symbol, symbolname), ...] に正規化。
    """
    if symbols is None:
        return []

    # pandas DataFrame
    if hasattr(symbols, "iterrows"):
        out: list[tuple[str, str]] = []
        try:
            for _, row in symbols.iterrows():
                symbol, name = _extract_symbol_and_name(row)
                if symbol:
                    out.append((symbol, name))
            return _dedupe_symbol_pairs(out)
        except Exception:
            pass

    # dict: {symbol: name} 形式
    if isinstance(symbols, dict):
        out = []
        for k, v in symbols.items():
            symbol = _symbol_to_str(k)
            name = "" if v is None else str(v).strip()
            if symbol:
                out.append((symbol, name))
        return _dedupe_symbol_pairs(out)

    # iterable
    try:
        out = []
        for item in symbols:
            symbol, name = _extract_symbol_and_name(item)
            if symbol:
                out.append((symbol, name))
        return _dedupe_symbol_pairs(out)
    except TypeError:
        symbol, name = _extract_symbol_and_name(symbols)
        return [(symbol, name)] if symbol else []


def _dedupe_symbol_pairs(pairs: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    seen = set()
    out: list[tuple[str, str]] = []

    for symbol, name in pairs:
        symbol = _symbol_to_str(symbol)
        name = str(name or "").strip()

        if not symbol:
            continue

        if symbol in seen:
            continue

        seen.add(symbol)
        out.append((symbol, name))

    return out


def _make_signature(pairs: list[tuple[str, str]]) -> str:
    symbols = sorted([s for s, _ in pairs])
    return json.dumps(symbols, ensure_ascii=False, separators=(",", ":"))


# ============================================================
# SQLite helpers
# ============================================================

def _connect(path: str) -> sqlite3.Connection:
    parent = Path(path).parent
    parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(
        path,
        timeout=max(1.0, DEFAULT_BUSY_TIMEOUT_MS / 1000.0),
        isolation_level=None,
    )

    # NAS SQLite では WAL 変更が逆に詰まる場合があるため journal_mode は触らない
    try:
        conn.execute(f"PRAGMA busy_timeout={DEFAULT_BUSY_TIMEOUT_MS}")
    except Exception:
        pass

    try:
        conn.execute("PRAGMA synchronous=NORMAL")
    except Exception:
        pass

    return conn


def _safe_identifier(name: str) -> str:
    """
    SQLite identifier用の最低限の保護。
    """
    s = str(name or "").strip()
    if not s:
        return DEFAULT_TABLE_NAME
    return "".join(ch for ch in s if ch.isalnum() or ch == "_") or DEFAULT_TABLE_NAME


def _ensure_schema(conn: sqlite3.Connection, table_name: str = DEFAULT_TABLE_NAME) -> None:
    table_name = _safe_identifier(table_name)
    idx_saved_at = _safe_identifier(f"idx_{table_name}_saved_at")
    idx_symbol_saved_at = _safe_identifier(f"idx_{table_name}_symbol_saved_at")

    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS "{table_name}" (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            saved_at TEXT NOT NULL,
            yyyymmdd TEXT NOT NULL,
            reason TEXT,
            source TEXT,
            target_count INTEGER,
            symbol TEXT NOT NULL,
            symbolname TEXT,
            rank_no INTEGER,
            signature TEXT,
            created_at TEXT NOT NULL
        )
        """
    )

    conn.execute(
        f"""
        CREATE INDEX IF NOT EXISTS "{idx_saved_at}"
        ON "{table_name}" (saved_at)
        """
    )

    conn.execute(
        f"""
        CREATE INDEX IF NOT EXISTS "{idx_symbol_saved_at}"
        ON "{table_name}" (symbol, saved_at)
        """
    )

    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS "{DEFAULT_META_TABLE_NAME}" (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )


def _executemany_chunked(
    conn: sqlite3.Connection,
    sql: str,
    rows: list[tuple[Any, ...]],
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> int:
    if not rows:
        return 0

    done = 0
    for i in range(0, len(rows), max(1, chunk_size)):
        chunk = rows[i : i + chunk_size]
        conn.executemany(sql, chunk)
        done += len(chunk)

    return done


def _is_lock_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return "database is locked" in msg or "database table is locked" in msg or "busy" in msg


# ============================================================
# Save API
# ============================================================

def save_subscription_symbols_history(
    symbols: Any = None,
    *,
    db_path: Optional[Union[str, os.PathLike]] = None,
    yyyymmdd: Optional[Union[str, int, dt.date, dt.datetime]] = None,
    reason: str = "",
    source: str = "ranking",
    table_name: str = DEFAULT_TABLE_NAME,
    force: bool = False,
    min_save_interval_sec: float = DEFAULT_MIN_SAVE_INTERVAL_SEC,
    retry_count: int = DEFAULT_RETRY_COUNT,
    **kwargs,
) -> bool:
    """
    PUSH購読対象の履歴を保存する。

    既存コードの引数揺れ対策:
      - symbols
      - target_symbols
      - subscription_symbols
      - current_symbols
      - ranking_symbols
      - path
      - ranking_db_path
      - reason
      - source

    重要:
      - ranking_db_path / ranking_path が渡されても既定では使用しない
      - db_path / path が明示された場合のみ保存先として使う
      - 既定保存先は push_subscription/subscription_historyYYYYMMDD.db

    戻り値:
      True  : 保存した、または保存不要として正常扱い
      False : 保存失敗。ただし例外は外へ投げない
    """
    global _LAST_SAVE_AT, _LAST_SIGNATURE

    if not ENABLE_HISTORY_SAVE:
        logger.debug("[SUB MANAGER] subscription history save disabled by env")
        return True

    # kwargs 互換
    if symbols is None:
        symbols = (
            kwargs.get("target_symbols")
            or kwargs.get("subscription_symbols")
            or kwargs.get("current_symbols")
            or kwargs.get("ranking_symbols")
            or kwargs.get("symbols")
        )

    # 保存先の扱い:
    # - db_path / path は明示指定として尊重
    # - ranking_db_path / ranking_path は旧互換で受け取るが、ranking DB に書かないため無視
    if db_path is None:
        db_path = (
            kwargs.get("db_path")
            or kwargs.get("path")
        )

    ignored_ranking_path = kwargs.get("ranking_db_path") or kwargs.get("ranking_path")
    if ignored_ranking_path and db_path is None:
        logger.debug(
            "[SUB MANAGER] subscription history ignore ranking db path and use separate history db ranking_path=%s",
            ignored_ranking_path,
        )

    if not reason:
        reason = str(kwargs.get("reason") or kwargs.get("refresh_reason") or "")

    if not source:
        source = str(kwargs.get("source") or "ranking")

    pairs = _normalize_symbols(symbols)
    if not pairs:
        logger.debug("[SUB MANAGER] subscription history skip empty symbols")
        return True

    now = dt.datetime.now()
    saved_at = now.isoformat(timespec="seconds")
    date_s = _normalize_yyyymmdd(yyyymmdd)
    signature = _make_signature(pairs)

    # --------------------------------------------------------
    # 短時間に同じ構成なら保存しない
    # --------------------------------------------------------
    if not force and _LAST_SAVE_AT is not None and _LAST_SIGNATURE == signature:
        elapsed = (now - _LAST_SAVE_AT).total_seconds()
        if elapsed < float(min_save_interval_sec):
            logger.info(
                "[SUB MANAGER] subscription history skip unchanged elapsed=%.1fs count=%s reason=%s",
                elapsed,
                len(pairs),
                reason,
            )
            return True

    path = _resolve_db_path(db_path, yyyymmdd=date_s)
    table_name = _safe_identifier(table_name)

    rows: list[tuple[Any, ...]] = []
    created_at = saved_at
    target_count = len(pairs)

    for i, (symbol, symbolname) in enumerate(pairs, start=1):
        rows.append(
            (
                saved_at,
                date_s,
                reason,
                source,
                target_count,
                symbol,
                symbolname,
                i,
                signature,
                created_at,
            )
        )

    sql = f"""
        INSERT INTO "{table_name}" (
            saved_at,
            yyyymmdd,
            reason,
            source,
            target_count,
            symbol,
            symbolname,
            rank_no,
            signature,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    last_exc: Optional[BaseException] = None

    for attempt in range(1, max(1, retry_count) + 1):
        conn: Optional[sqlite3.Connection] = None
        try:
            conn = _connect(path)

            conn.execute("BEGIN IMMEDIATE")
            _ensure_schema(conn, table_name=table_name)

            inserted = _executemany_chunked(conn, sql, rows)

            conn.execute(
                f"""
                INSERT OR REPLACE INTO "{DEFAULT_META_TABLE_NAME}" (
                    key,
                    value,
                    updated_at
                )
                VALUES (?, ?, ?)
                """,
                (
                    "last_subscription_signature",
                    signature,
                    saved_at,
                ),
            )

            conn.execute(
                f"""
                INSERT OR REPLACE INTO "{DEFAULT_META_TABLE_NAME}" (
                    key,
                    value,
                    updated_at
                )
                VALUES (?, ?, ?)
                """,
                (
                    "last_subscription_count",
                    str(target_count),
                    saved_at,
                ),
            )

            conn.execute(
                f"""
                INSERT OR REPLACE INTO "{DEFAULT_META_TABLE_NAME}" (
                    key,
                    value,
                    updated_at
                )
                VALUES (?, ?, ?)
                """,
                (
                    "last_subscription_history_db_path",
                    str(path),
                    saved_at,
                ),
            )

            conn.commit()

            _LAST_SAVE_AT = now
            _LAST_SIGNATURE = signature

            logger.info(
                "[SUB MANAGER] subscription history saved path=%s table=%s rows=%s count=%s reason=%s source=%s",
                path,
                table_name,
                inserted,
                target_count,
                reason,
                source,
            )
            return True

        except sqlite3.OperationalError as e:
            last_exc = e

            try:
                if conn is not None:
                    conn.rollback()
            except Exception:
                pass

            if _is_lock_error(e):
                sleep_sec = DEFAULT_RETRY_BASE_SLEEP * attempt
                logger.warning(
                    "[SUB MANAGER] subscription history db locked attempt=%s/%s sleep=%.2fs path=%s reason=%s",
                    attempt,
                    retry_count,
                    sleep_sec,
                    path,
                    reason,
                )
                time.sleep(sleep_sec)
                continue

            logger.exception(
                "[SUB MANAGER] subscription history save failed operational path=%s reason=%s",
                path,
                reason,
            )
            return False

        except Exception as e:
            last_exc = e

            try:
                if conn is not None:
                    conn.rollback()
            except Exception:
                pass

            logger.exception(
                "[SUB MANAGER] subscription history save failed path=%s reason=%s",
                path,
                reason,
            )
            return False

        finally:
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass

    # --------------------------------------------------------
    # retry してもロックなら、履歴保存は諦めて本体は継続
    # --------------------------------------------------------
    logger.warning(
        "[SUB MANAGER] subscription history save skipped after retries path=%s count=%s reason=%s last_error=%s",
        path,
        target_count,
        reason,
        last_exc,
    )

    # ここで False にすると呼び出し側がエラー扱いする可能性があるため True にする。
    # 履歴保存は補助情報であり、PUSH本体を止めない。
    return True


# ============================================================
# Optional read helpers
# ============================================================

def load_latest_subscription_symbols_history(
    *,
    db_path: Optional[Union[str, os.PathLike]] = None,
    yyyymmdd: Optional[Union[str, int, dt.date, dt.datetime]] = None,
    table_name: str = DEFAULT_TABLE_NAME,
    limit: int = 200,
    **kwargs,
) -> list[dict[str, Any]]:
    """
    最新の購読履歴を読む。
    デバッグ・復元用。

    既定では push_subscription/subscription_historyYYYYMMDD.db を読む。
    """
    if db_path is None:
        db_path = kwargs.get("db_path") or kwargs.get("path")

    path = _resolve_db_path(db_path, yyyymmdd=yyyymmdd)
    table_name = _safe_identifier(table_name)

    if not os.path.exists(path):
        return []

    conn: Optional[sqlite3.Connection] = None
    try:
        conn = _connect(path)
        _ensure_schema(conn, table_name=table_name)

        rows = conn.execute(
            f"""
            SELECT
                saved_at,
                yyyymmdd,
                reason,
                source,
                target_count,
                symbol,
                symbolname,
                rank_no,
                signature,
                created_at
            FROM "{table_name}"
            ORDER BY saved_at DESC, rank_no ASC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()

        out: list[dict[str, Any]] = []
        for r in rows:
            out.append(
                {
                    "saved_at": r[0],
                    "yyyymmdd": r[1],
                    "reason": r[2],
                    "source": r[3],
                    "target_count": r[4],
                    "symbol": r[5],
                    "symbolname": r[6],
                    "rank_no": r[7],
                    "signature": r[8],
                    "created_at": r[9],
                }
            )

        return out

    except Exception:
        logger.exception("[SUB MANAGER] load subscription history failed path=%s", path)
        return []

    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass


def get_latest_subscription_symbols(
    *,
    db_path: Optional[Union[str, os.PathLike]] = None,
    yyyymmdd: Optional[Union[str, int, dt.date, dt.datetime]] = None,
    table_name: str = DEFAULT_TABLE_NAME,
    **kwargs,
) -> list[str]:
    """
    最新 saved_at の symbol 一覧を返す。

    既定では push_subscription/subscription_historyYYYYMMDD.db を読む。
    """
    if db_path is None:
        db_path = kwargs.get("db_path") or kwargs.get("path")

    path = _resolve_db_path(db_path, yyyymmdd=yyyymmdd)
    table_name = _safe_identifier(table_name)

    if not os.path.exists(path):
        return []

    conn: Optional[sqlite3.Connection] = None
    try:
        conn = _connect(path)
        _ensure_schema(conn, table_name=table_name)

        row = conn.execute(
            f"""
            SELECT saved_at
            FROM "{table_name}"
            ORDER BY saved_at DESC
            LIMIT 1
            """
        ).fetchone()

        if not row:
            return []

        saved_at = row[0]

        rows = conn.execute(
            f"""
            SELECT symbol
            FROM "{table_name}"
            WHERE saved_at = ?
            ORDER BY rank_no ASC
            """,
            (saved_at,),
        ).fetchall()

        return [_symbol_to_str(r[0]) for r in rows if _symbol_to_str(r[0])]

    except Exception:
        logger.exception("[SUB MANAGER] get latest subscription symbols failed path=%s", path)
        return []

    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass


def get_default_subscription_history_db_path(
    yyyymmdd: Optional[Union[str, int, dt.date, dt.datetime]] = None,
) -> str:
    """
    デバッグ用。
    既定の購読履歴DBパスを返す。
    """
    return _default_subscription_history_db_path(yyyymmdd)


# ============================================================
# Compatibility aliases
# ============================================================

def save_history(*args, **kwargs) -> bool:
    return save_subscription_symbols_history(*args, **kwargs)


def save_subscription_history(*args, **kwargs) -> bool:
    return save_subscription_symbols_history(*args, **kwargs)


def save_symbols_history(*args, **kwargs) -> bool:
    return save_subscription_symbols_history(*args, **kwargs)


def load_history(*args, **kwargs) -> list[dict[str, Any]]:
    return load_latest_subscription_symbols_history(*args, **kwargs)

# ============================================================
# Compatibility API
# ============================================================

def load_latest_subscription_symbols_from_history(
    *args,
    limit: int = 100,
    max_symbols: int | None = None,
    **kwargs,
) -> list[str]:
    """
    ranking_source.py 互換用。

    旧/新バージョン差分により ranking_source.py が
    load_latest_subscription_symbols_from_history を import するため、
    ranking_source_history.py 側で公開する。

    優先:
      1. 既存の load_subscription_symbols_history があれば使う
      2. 既存の load_latest_symbols_from_history があれば使う
      3. 既存の load_subscription_history があれば使う
      4. 何も無ければ空リスト

    Returns
    -------
    list[str]
        最新の登録履歴から復元した銘柄コード一覧
    """
    n = int(max_symbols or limit or 100)

    candidates = (
        "load_subscription_symbols_history",
        "load_latest_symbols_from_history",
        "load_subscription_history",
        "load_symbols_history",
        "read_latest_subscription_symbols",
        "read_subscription_symbols_history",
    )

    g = globals()

    for name in candidates:
        fn = g.get(name)
        if not callable(fn):
            continue

        call_patterns = (
            lambda: fn(limit=n),
            lambda: fn(max_symbols=n),
            lambda: fn(n),
            lambda: fn(),
        )

        for caller in call_patterns:
            try:
                result = caller()
            except TypeError:
                continue
            except Exception:
                try:
                    logger.exception(
                        "[SUB MANAGER HISTORY] compatibility load failed via %s",
                        name,
                    )
                except Exception:
                    pass
                result = None

            symbols = _extract_symbols_compat(result)
            if symbols:
                return symbols[:n]

    try:
        logger.warning(
            "[SUB MANAGER HISTORY] no compatible history loader found -> empty"
        )
    except Exception:
        pass

    return []


def _extract_symbols_compat(value) -> list[str]:
    """
    history loader の戻り値から symbol list を安全に取り出す。
    """
    if value is None:
        return []

    # pandas DataFrame 対応
    try:
        if hasattr(value, "columns"):
            for col in ("symbol", "Symbol", "code", "Code", "銘柄コード"):
                if col in value.columns:
                    return _dedupe_symbols_compat(value[col].tolist())
    except Exception:
        pass

    # dict 対応
    if isinstance(value, dict):
        for key in (
            "symbols",
            "codes",
            "items",
            "data",
            "subscription_symbols",
            "register_symbols",
            "monitor_symbols",
        ):
            if key in value:
                return _extract_symbols_compat(value.get(key))
        return _dedupe_symbols_compat(list(value.keys()))

    # str 対応
    if isinstance(value, str):
        return _dedupe_symbols_compat([value])

    # list/tuple/set 対応
    try:
        out = []
        for item in list(value):
            if isinstance(item, dict):
                s = (
                    item.get("symbol")
                    or item.get("Symbol")
                    or item.get("code")
                    or item.get("Code")
                    or item.get("銘柄コード")
                )
                if s:
                    out.append(s)
            else:
                out.append(item)
        return _dedupe_symbols_compat(out)
    except Exception:
        return []


def _dedupe_symbols_compat(values) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()

    try:
        seq = list(values or [])
    except Exception:
        return []

    for v in seq:
        if v is None:
            continue

        s = str(v).strip().upper()

        if not s:
            continue

        if s.endswith(".0") and s[:-2].isdigit():
            s = s[:-2]

        if s in {"NONE", "NULL", "NAN", "NA", "-", "0"}:
            continue

        if s in seen:
            continue

        seen.add(s)
        out.append(s)

    return out
# ============================================================
# Public exports
# ============================================================

__all__ = [
    "save_subscription_symbols_history",
    "save_subscription_history",
    "save_symbols_history",
    "save_history",
    "load_latest_subscription_symbols_history",
    "load_latest_subscription_symbols_from_history",
    "load_history",
    "get_latest_subscription_symbols",
    "get_default_subscription_history_db_path",
]