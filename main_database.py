
# ============================================================
# File   : main_database.py
# Version: DATA-COLLECTORS-MAIN-DATABASE-ENTRY-V12-STARTUP-STALE-TOKEN-RECOVERY
# ------------------------------------------------------------
# Purpose:
#   - DB作成 / ランキング取得 / PUSH銘柄登録 / PUSH受信 を起動する入口
#   - 既存 main.py とは分離する
#   - 実体は scripts/data_collectors_runner.py に委譲する
#   - main_database.py 経由でも古い PUSH/ranking summary を候補に残さない
#   - main_database.py のコンソールログに時刻を付け、ファイルにも保存する
#   - summary DB の WAL を1分ごとに checkpoint して .db 本体へ反映する
#   - settings.ini の保存 token を read-only API で preflight し、認証NGなら子プロセスを起動しない
#   - 前日PUSH summary の global-lag/min_keep fallback を子プロセス既定で無効化する
#
# V12:
#   - 起動時の token 有効判定を TTL から read-only preflight へ移した。
#     旧: settings.ini の mtime が TTL(3600s)以内なら「有効」とみなして /token を呼ばない。
#         kabu Station 再起動でサーバ側 token が失効していても検知できず、
#         preflight 401 -> runtime refresh 禁止 -> 復旧不能 -> exit code 1 になっていた。
#     新: kabu_api/token_bootstrap.py のステートマシンが判定する。
#         cached -> preflight 200                  => そのまま使う (/token 0回)
#         cached -> preflight 401/403/4001009      => /token を 1回だけ -> 保存 -> preflight 1回
#         /token 自体が 401/4001009                => apipassword 不一致として明確に区別
#         再取得後も 401                            => retry せず abort
#         kabu Station へ到達不能                   => fail-safe abort (/token は消費しない)
#     startup stale token recovery = ON / runtime arbitrary refresh = OFF は維持。
#   - 起動順を変更: PUSH DB writer と WAL checkpoint ループは
#     token preflight 成功後にだけ起動する。
#
# V11:
#   - core/startup/push_summary_fallback_and_active_price_patch.py (REV5) を
#     trading/ranking/active_symbols/liquidity.py と
#     scheduler_jobs/summary/fallback_loader.py の本文へ完全インライン化し、
#     パッチファイル自体・sitecustomize.py の登録・本ファイルの install() 呼び出しを削除。
#   - core/startup/sqlite_memory_pragmas_patch.py は main.py / data_collectors_runner.py /
#     db_prepare_runner.py / push_receiver_runner.py / yahoo_complement_runner.py /
#     summary_database_runner.py の計7プロセス共通基盤のため元ファイル・
#     sitecustomize.py 側の登録はそのまま残しつつ、main_database.py には
#     ロジック全体を _sqlite_pragmas_* として複製・本文化した（二重管理は承知の上）。
#     sqlite3.connect の二重ラップを避けるため、sitecustomize側で既に
#     wrap済みの場合は自前のwrapをスキップするガードを入れている。
#
# V10:
#   - sitecustomize.py の DB_SYNC_PATCHES (argv判定による暗黙適用) のうち、
#     main_database.py 本文へ未移設だった5パッチを明示的にインライン化。
#     (indicator prevday warmup / yahoo direct upsert conflict /
#      push summary realtime / summary controller latest enrich /
#      active symbol target fill)
#   - db startup scope defaults (schema repair disable / mtf catchup disable)
#     も同様に明示化。sitecustomize.py 側のリストはフォールバックとして維持し、
#     他の子プロセス (data_collectors_runner.py 等) への適用は変更しない。
# ============================================================

from __future__ import annotations

# 2026-09-25: Norton が注入する SSLKEYLOGFILE を外す。
#   プロセス環境に SSLKEYLOGFILE=\\.\nllMonFltProxy\... が入っており
#   (Machine/User の環境変数には無い。Norton が各プロセスへ注入する)、
#   urllib3 は HTTPS 接続ごとにこれを読んで ssl context の
#   keylog_filename に設定する。その setter が Norton の名前付きパイプを
#   open() しようとして応答待ちになり、スレッドが永久に止まる。
#   socket より手前なので timeout= は効かない。
#   9/25 は ranking_summary_bg が Discord 通知の中でこれに掛かり、
#   generation=1 のまま 20 分以上完走しなかった。
#   env AUTOSTOCK_KEEP_SSLKEYLOGFILE=1 で従来どおり残す。
import os as _autostock_os

if _autostock_os.environ.get("SSLKEYLOGFILE") and str(
    _autostock_os.environ.get("AUTOSTOCK_KEEP_SSLKEYLOGFILE", "0")
).strip().lower() not in ("1", "true", "yes", "on"):
    _autostock_keylog = _autostock_os.environ.pop("SSLKEYLOGFILE", None)
    print(
        "[TLS ENV GUARD] SSLKEYLOGFILE を外した (Norton の TLS 監視が"
        " HTTPS 接続を固めるため): %s" % (_autostock_keylog,)
    )


import datetime as dt
import logging
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path
from configparser import ConfigParser
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    os.chdir(str(PROJECT_ROOT))
except Exception:
    pass

logger = logging.getLogger(__name__)
_MAIN_DATABASE_LOG_FILE_INSTALLED = False

KABU_API_BASE_URL = "http://localhost:18080/kabusapi"


def _ensure_basic_logging() -> None:
    """Configure timestamped console logging and save main_database logs to file.

    sitecustomize/usercustomize may create root handlers before main_database.py
    starts.  In that case logging.basicConfig() is ignored, so force the formatter
    on existing handlers as well.  The child collector output is captured/saved by
    scripts/data_collectors_runner.py.
    """
    global _MAIN_DATABASE_LOG_FILE_INSTALLED
    try:
        root = logging.getLogger()
        root.setLevel(logging.INFO)

        fmt = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        if not root.handlers:
            sh = logging.StreamHandler(sys.stdout)
            sh.setFormatter(fmt)
            root.addHandler(sh)
        else:
            for h in root.handlers:
                try:
                    h.setFormatter(fmt)
                except Exception:
                    pass

        if not _MAIN_DATABASE_LOG_FILE_INSTALLED:
            try:
                from data_collectors.config import LOG_DIR
                LOG_DIR.mkdir(parents=True, exist_ok=True)
                ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
                pid = os.getpid()
                log_path = LOG_DIR / f"main_database_{ts}_{pid}.log"
                fh = logging.FileHandler(log_path, encoding="utf-8")
                fh.setFormatter(fmt)
                root.addHandler(fh)
                _MAIN_DATABASE_LOG_FILE_INSTALLED = True
                logging.getLogger(__name__).warning("[MAIN DATABASE LOG] save to: %s", log_path)

                # 書き先はローカル。NAS へは差分を追記ミラーする
                # (NAS がハングしても logging を止めないため)。
                try:
                    from data_collectors import log_mirror

                    log_mirror.register(log_path)
                    log_mirror.start_background_mirror()
                except Exception:
                    logging.getLogger(__name__).debug("[MAIN DATABASE LOG] mirror 起動に失敗", exc_info=True)
            except Exception:
                logging.getLogger(__name__).exception("[MAIN DATABASE LOG] file handler install failed")
    except Exception:
        pass


# main_database.py プロセス専用の環境変数チューニング値。
# 旧 core/startup/main_database_cpu_guard_env.py から移設。
_CPU_GUARD_FORCE_DEFAULTS = {
    # main_database.py はDB保存専用寄せ。表示/通知/entry系は main.py 側へ寄せる。
    "SUMMARY_DATABASE_RUNNER_DISPLAY": "0",
    "SUMMARY_DISCORD_EMPTY_FALLBACK_NOTIFY": "0",
    "ENABLE_SUMMARY_ENTRY_TICK": "0",
    "ENABLE_RANKING_SUMMARY_TICK": "0",

    # CPU高止まり対策: PUSH 1m/3m/5mを毎分すべて再計算しない。
    # 1mは毎分、3m/5mは時間境界だけにする。
    # sitecustomize/summary_parallel が 1 を setdefault するため、ここは必ず上書きする。
    "SUMMARY_PUSH_DISPLAY_ALL_INTERVALS": "0",
    "SUMMARY_PARALLEL_FORCE_1_3_5": "0",
    "SUMMARY_PARALLEL_INTERVAL_WORKERS": "1",
    "SUMMARY_PUSH_BG_INTERVAL_WORKERS": "1",

    # spool flush は毎tick前後では重いので間引く。
    "SUMMARY_SAVE_SPOOL_FLUSH_MIN_INTERVAL_SEC": "120",
    "SUMMARY_SAVE_SPOOL_FLUSH_MAX_FILES": "10",

    # 1回のsummary tickが重すぎる時は次tickを1回休ませる。
    "SUMMARY_DATABASE_SLOW_TICK_SEC": "45",
    "SUMMARY_DATABASE_SKIP_NEXT_ON_SLOW_TICK": "1",

    # 1分足の差分キャッシュ (trading/summary/engine/incremental/bar_cache.py)。
    # 2026-08-19 の実測では tick データ量が日中に増えるほど tick が伸びる
    #   09:19 normalized_push 13,930 行 -> 41 秒
    #   10:35 normalized_push 20,000 行 -> 70 秒 (DB読みは上限 30,000 行)
    # 毎分 直近20分ぶんの tick を読み直して全再計算する構造が原因。
    # 差分キャッシュはこれを「前回の足以降だけ」にする。
    #
    # shadow で本番データを検証したうえで on にした (2026-08-19)。
    #   OHLC / volume / tick_count は完全一致
    #   差が出るのは ma5/ma25/ma75/rsi/macd などの指標のみで、
    #   これはキャッシュ側が履歴を多く持つことによる
    #   (cached 1,679〜1,839 本 vs full 969 本)。
    # 20 分窓から作る従来方式では ma25 も ma75 も最大 20 本しか使えておらず、
    # ma75 が名前どおりの期間で計算されていなかった。キャッシュ側の方が正しい。
    "SUMMARY_BAR_CACHE_MODE": "on",

    # MA75 warmupの起動負荷を抑える。
    "PUSH_INCREMENTAL_MA75_SUMMARY_LOOKBACK_DAYS": "1",
    "PUSH_INCREMENTAL_MA75_TAIL_ROWS": "90",

    # メモリ余裕を使って SQLite の temp/cache をメモリ寄せする。
    "SQLITE_MEMORY_PRAGMAS_ENABLED": "1",
    "SQLITE_MEMORY_TEMP_STORE": "MEMORY",
    "SQLITE_MEMORY_CACHE_KB": "-65536",
    "SQLITE_BUSY_TIMEOUT_MS": "5000",
    "SQLITE_MMAP_SIZE_BYTES": "268435456",
    "SQLITE_CACHE_SPILL_OFF": "1",
    "RANKING_SQLITE_TEMP_STORE": "MEMORY",
    "SUMMARY_SQLITE_TEMP_STORE": "MEMORY",
    "PUSH_SQLITE_TEMP_STORE": "MEMORY",
    "YAHOO_SQLITE_TEMP_STORE": "MEMORY",
    "RANKING_SQLITE_CACHE_KB": "-65536",
    "SUMMARY_SQLITE_CACHE_KB": "-131072",
    "PUSH_SQLITE_CACHE_KB": "-65536",
    "YAHOO_SQLITE_CACHE_KB": "-65536",

    # summary履歴キャッシュ系。対応モジュールがある場合に有効化される。
    "SUMMARY_HISTORY_MEMORY_CACHE": "1",
    "SUMMARY_HISTORY_CACHE_TTL_SEC": "300",
    "SUMMARY_HISTORY_CACHE_MAX_SYMBOLS": "800",
    "SUMMARY_HISTORY_CACHE_TAIL_ROWS": "90",

    # NAS heartbeat / BLAS thread抑制。
    "AUTOSTOCK_COLLECTOR_PARENT_HEARTBEAT": "0",
    "AUTOSTOCK_DISABLE_NAS_HEARTBEAT": "1",
    "OMP_NUM_THREADS": "1",
    "OPENBLAS_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1",
    "NUMEXPR_NUM_THREADS": "1",
}


def _install_cpu_guard_env() -> None:
    try:
        for key, value in _CPU_GUARD_FORCE_DEFAULTS.items():
            os.environ[key] = value

        try:
            _install_sqlite_memory_pragmas()
        except Exception:
            pass

        logger.info("[MAIN DATABASE] cpu guard env installed ok=True")
    except Exception:
        logger.exception("[MAIN DATABASE] cpu guard env install failed; continue")


# ============================================================
# sqlite memory pragmas (main_database.py 専用コピー)
# 旧 core/startup/sqlite_memory_pragmas_patch.py から丸ごとコピーして本文化した。
#
# NOTE: この patch は main.py / data_collectors_runner.py / db_prepare_runner.py /
# push_receiver_runner.py / yahoo_complement_runner.py / summary_database_runner.py の
# 計7プロセス共通基盤であり、他の6プロセスは引き続き sitecustomize.py の
# DB_SYNC_PATCHES / SYNC_MAIN_PATCHES 経由で core/startup 側のファイルに依存する
# （そちらは変更していない）。main_database.py だけ本文に複製したため、
# 元ファイルとロジックが乖離しないよう変更時は両方に反映すること。
#
# sitecustomize.py は Python 起動時に main_database.py よりも先に実行され、
# 通常は既に core.startup.sqlite_memory_pragmas_patch 側の sqlite3.connect ラップが
# 適用済みになっている。ここでの二重ラップ（性能劣化・PRAGMA二重適用）を避けるため、
# sqlite3.connect が既に同モジュールでラップ済みかどうかを __module__ で判定し、
# 済みならこちらのラップはスキップする。
# ============================================================
_SQLITE_PRAGMAS_ORIG_CONNECT = sqlite3.connect
_SQLITE_PRAGMAS_INSTALLED = False
_SQLITE_PRAGMAS_SPOOL_FLUSH_PATCHED = False
_SQLITE_PRAGMAS_TRUE = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
_SQLITE_PRAGMAS_FALSE = {"0", "false", "no", "n", "off", "disable", "disabled"}

_SQLITE_PRAGMAS_SUMMARY_RANKING_COLUMNS: tuple[tuple[str, str], ...] = (
    ("rank", "REAL"),
    ("rank_no", "REAL"),
    ("best_rank", "REAL"),
    ("avg_rank", "REAL"),
    ("rank_types_count", "INTEGER"),
    ("ranking_score", "REAL"),
    ("ranking_score_total", "REAL"),
    ("ranking_type", "TEXT"),
    ("rank_types", "TEXT"),
    ("type", "TEXT"),
    ("ranking", "TEXT"),
    ("market", "TEXT"),
    ("current_price", "REAL"),
    ("change_rate", "REAL"),
    ("chg", "REAL"),
    ("trading_volume", "REAL"),
    ("trading_value", "REAL"),
    ("turnover", "REAL"),
    ("turn", "REAL"),
)

_SQLITE_PRAGMAS_SUMMARY_TABLES: tuple[str, ...] = (
    "stock_summary_1min",
    "stock_summary_3min",
    "stock_summary_5min",
)


def _sqlite_pragmas_env_bool(name: str, default: bool = False) -> bool:
    try:
        raw = str(os.getenv(name, "")).strip().lower()
        if raw in _SQLITE_PRAGMAS_TRUE:
            return True
        if raw in _SQLITE_PRAGMAS_FALSE:
            return False
    except Exception:
        pass
    return bool(default)


def _sqlite_pragmas_env_int(name: str, default: int) -> int:
    try:
        return int(float(str(os.getenv(name, str(default))).strip()))
    except Exception:
        return int(default)


def _sqlite_pragmas_env_float(name: str, default: float) -> float:
    try:
        return float(str(os.getenv(name, str(default))).strip())
    except Exception:
        return float(default)


def _sqlite_pragmas_setdefault_env(name: str, value: str) -> None:
    try:
        if not str(os.getenv(name, "")).strip():
            os.environ[name] = value
    except Exception:
        pass


def _sqlite_pragmas_classify_db(database: Any) -> str:
    try:
        p = str(database).replace("\\", "/").lower()
    except Exception:
        return "default"
    if not p or p in {":memory:", "file::memory:"}:
        return "memory"
    name = Path(p).name.lower()
    if "ranking" in name or "/ranking/" in p:
        return "ranking"
    if "summary" in name or "/summary/" in p:
        return "summary"
    if "push" in name or "/push/" in p:
        return "push"
    if "yahoo" in name or "/yahoo/" in p:
        return "yahoo"
    return "default"


def _sqlite_pragmas_looks_like_summary_db_file(database: Any) -> bool:
    try:
        name = Path(str(database or "")).name.lower()
        return name.startswith("summary") and name.endswith(".db")
    except Exception:
        return False


def _sqlite_pragmas_quote_ident_fallback(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _sqlite_pragmas_quote_ident(name: str) -> str:
    try:
        from database.sqlite import quote_ident
        return quote_ident(str(name))
    except Exception:
        return _sqlite_pragmas_quote_ident_fallback(str(name))


def _sqlite_pragmas_cache_kb_for(kind: str) -> int:
    names = []
    if kind == "ranking":
        names.append("RANKING_SQLITE_CACHE_KB")
    elif kind == "summary":
        names.append("SUMMARY_SQLITE_CACHE_KB")
    elif kind == "push":
        names.append("PUSH_SQLITE_CACHE_KB")
    elif kind == "yahoo":
        names.append("YAHOO_SQLITE_CACHE_KB")
    names.append("SQLITE_MEMORY_CACHE_KB")

    for name in names:
        raw = os.getenv(name)
        if raw not in (None, ""):
            return _sqlite_pragmas_env_int(name, -65536)
    return -65536


def _sqlite_pragmas_temp_store_for(kind: str) -> str:
    names = []
    if kind == "ranking":
        names.append("RANKING_SQLITE_TEMP_STORE")
    elif kind == "summary":
        names.append("SUMMARY_SQLITE_TEMP_STORE")
    elif kind == "push":
        names.append("PUSH_SQLITE_TEMP_STORE")
    elif kind == "yahoo":
        names.append("YAHOO_SQLITE_TEMP_STORE")
    names.append("SQLITE_MEMORY_TEMP_STORE")

    for name in names:
        raw = os.getenv(name)
        if raw not in (None, ""):
            return str(raw).strip().upper()
    return "MEMORY"


def _sqlite_pragmas_table_exists(cur: Any, table: str) -> bool:
    try:
        row = cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (str(table),),
        ).fetchone()
        return row is not None
    except Exception:
        return False


def _sqlite_pragmas_ensure_columns_with_cursor(cur: Any, table: str, columns: tuple[tuple[str, str], ...], label: str) -> list[str]:
    q = _sqlite_pragmas_quote_ident(table)
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
            cur.execute(f"ALTER TABLE {q} ADD COLUMN {_sqlite_pragmas_quote_ident(col)} {decl}")
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


def _sqlite_pragmas_ensure_summary_ranking_columns_on_connection(conn: sqlite3.Connection, database: Any) -> None:
    if _sqlite_pragmas_env_bool("DISABLE_SUMMARY_RANKING_SCHEMA_REPAIR_PATCH", False):
        return
    if _sqlite_pragmas_classify_db(database) != "summary":
        return
    try:
        cur = conn.cursor()
        for table in _SQLITE_PRAGMAS_SUMMARY_TABLES:
            if not _sqlite_pragmas_table_exists(cur, table):
                continue
            _sqlite_pragmas_ensure_columns_with_cursor(cur, table, _SQLITE_PRAGMAS_SUMMARY_RANKING_COLUMNS, "SUMMARY RANKING SCHEMA REPAIR")
        try:
            conn.commit()
        except Exception:
            pass
    except Exception:
        logger.debug("[SUMMARY RANKING SCHEMA REPAIR] connection repair failed database=%s", database, exc_info=True)


def _sqlite_pragmas_force_summary_lock_env() -> None:
    _sqlite_pragmas_setdefault_env("SUMMARY_SQLITE_BUSY_TIMEOUT_MS", "60000")
    _sqlite_pragmas_setdefault_env("SUMMARY_SQLITE_TIMEOUT", "60")
    _sqlite_pragmas_setdefault_env("SUMMARY_SQLITE_CACHE_KB", "-131072")
    _sqlite_pragmas_setdefault_env("SUMMARY_SQLITE_TEMP_STORE", "MEMORY")

    _sqlite_pragmas_setdefault_env("SUMMARY_MTF_INDICATOR_SQLITE_TIMEOUT", "60")
    _sqlite_pragmas_setdefault_env("SUMMARY_MTF_INDICATOR_BUSY_TIMEOUT_MS", "60000")
    _sqlite_pragmas_setdefault_env("SUMMARY_MTF_INDICATOR_UPDATE_CHUNK_SIZE", "50")
    _sqlite_pragmas_setdefault_env("SUMMARY_MTF_INDICATOR_LOCK_RETRIES", "20")
    _sqlite_pragmas_setdefault_env("SUMMARY_MTF_INDICATOR_LOCK_SLEEP_BASE", "0.50")
    _sqlite_pragmas_setdefault_env("SUMMARY_MTF_INDICATOR_SKIP_IF_BUSY", "1")

    _sqlite_pragmas_setdefault_env("SUMMARY_MTF_CATCHUP_SQLITE_TIMEOUT", "60")
    _sqlite_pragmas_setdefault_env("SUMMARY_MTF_CATCHUP_BUSY_TIMEOUT_MS", "60000")
    _sqlite_pragmas_setdefault_env("SUMMARY_MTF_CATCHUP_LOCK_RETRIES", "20")
    _sqlite_pragmas_setdefault_env("SUMMARY_MTF_CATCHUP_RETRIES", "20")
    _sqlite_pragmas_setdefault_env("SUMMARY_MTF_CATCHUP_UPSERT_RETRIES", "20")
    _sqlite_pragmas_setdefault_env("SUMMARY_MTF_CATCHUP_BUSY_RETRIES", "20")
    _sqlite_pragmas_setdefault_env("SUMMARY_MTF_CATCHUP_LOCK_SLEEP_BASE", "0.50")
    _sqlite_pragmas_setdefault_env("SUMMARY_MTF_CATCHUP_CHUNK_SIZE", "100")
    _sqlite_pragmas_setdefault_env("SUMMARY_MTF_CATCHUP_UPSERT_CHUNK_SIZE", "100")


# ============================================================
# 2026-09-03: journal_mode は「読んでから、必要なときだけ」変える
# ------------------------------------------------------------
# 起動のたびに無条件で
#     PRAGMA journal_mode=WAL
# を投げると、SMB 上の DB では -shm のロック交渉に入るため、
# 他の writer/reader が居るだけで
#     sqlite3.OperationalError: locking protocol   (SQLITE_PROTOCOL)
# になりうる。実際 2026-09-03 に db_prepare がこれで異常終了し、
# main_database.py が exit code 1 で落ちた。
#
# journal_mode は DB 全体の永続設定なので、一度 WAL なら二度と設定しなくてよい。
# 読むだけ (PRAGMA journal_mode) は排他を必要としない。
# ============================================================


def _md_ensure_journal_mode_wal_if_needed(conn, label: str = "db") -> str:
    """既に WAL なら何もしない。戻り値は最終的な journal_mode。

    設定に失敗しても例外にしない (呼び出し側は続行してよい)。
    """
    current = ""
    try:
        row = conn.execute("PRAGMA journal_mode").fetchone()
        if row and row[0] is not None:
            current = str(row[0]).strip().lower()
    except Exception:
        logger.debug("[SQLITE PRAGMA] %s journal_mode read failed", label, exc_info=True)
        return ""

    if current == "wal":
        return current

    try:
        row = conn.execute("PRAGMA journal_mode=WAL").fetchone()
        after = str(row[0]).strip().lower() if row and row[0] is not None else ""
        logger.info("[SQLITE PRAGMA] %s journal_mode %s -> %s", label, current or "unknown", after or "?")
        return after or current
    except Exception as e:
        logger.warning(
            "[SQLITE PRAGMA] %s journal_mode=WAL skipped (current=%s) err=%s "
            "(設定失敗だけでは落とさない)",
            label, current or "unknown", e,
        )
        return current


def _sqlite_pragmas_configure_summary_connection_extra(conn: sqlite3.Connection) -> None:
    busy_ms = max(60000, _sqlite_pragmas_env_int("SUMMARY_SQLITE_BUSY_TIMEOUT_MS", 60000))
    # journal_mode は毎回投げない (既に WAL なら読むだけで終わる)。
    _md_ensure_journal_mode_wal_if_needed(conn, "summary")
    pragmas = [
        f"PRAGMA busy_timeout={busy_ms}",
        "PRAGMA synchronous=NORMAL",
        "PRAGMA wal_autocheckpoint=1000",
    ]
    for sql in pragmas:
        try:
            conn.execute(sql)
        except Exception:
            pass


def _sqlite_pragmas_apply_pragmas(conn: sqlite3.Connection, database: Any) -> None:
    if not _sqlite_pragmas_env_bool("SQLITE_MEMORY_PRAGMAS_ENABLED", True):
        return

    kind = _sqlite_pragmas_classify_db(database)
    if kind == "memory":
        return

    try:
        timeout_ms = max(0, _sqlite_pragmas_env_int("SQLITE_BUSY_TIMEOUT_MS", 5000))
        if timeout_ms:
            conn.execute(f"PRAGMA busy_timeout={timeout_ms}")
    except Exception:
        pass

    try:
        temp_store = _sqlite_pragmas_temp_store_for(kind)
        if temp_store in {"MEMORY", "2"}:
            conn.execute("PRAGMA temp_store=MEMORY")
        elif temp_store in {"FILE", "1"}:
            conn.execute("PRAGMA temp_store=FILE")
    except Exception:
        pass

    try:
        cache_kb = _sqlite_pragmas_cache_kb_for(kind)
        if cache_kb:
            conn.execute(f"PRAGMA cache_size={int(cache_kb)}")
    except Exception:
        pass

    try:
        mmap_bytes = max(0, _sqlite_pragmas_env_int("SQLITE_MMAP_SIZE_BYTES", 268435456))
        if mmap_bytes:
            # 2026-09-02 P0: NAS 上の DB では mmap を使わない
            # (SMB の mmap ページフォルトは EXCEPTION_IN_PAGE_ERROR で
            #  プロセスごと落ちる。core/startup/sqlite_memory_pragmas.py 参照)
            try:
                from core.startup.sqlite_memory_pragmas import resolve_mmap_size_bytes

                mmap_bytes = resolve_mmap_size_bytes(database, mmap_bytes)
            except Exception:
                pass
            conn.execute(f"PRAGMA mmap_size={mmap_bytes}")
    except Exception:
        pass

    try:
        if _sqlite_pragmas_env_bool("SQLITE_CACHE_SPILL_OFF", True):
            conn.execute("PRAGMA cache_spill=OFF")
    except Exception:
        pass

    if _sqlite_pragmas_looks_like_summary_db_file(database):
        try:
            _sqlite_pragmas_configure_summary_connection_extra(conn)
        except Exception:
            pass

    try:
        _sqlite_pragmas_ensure_summary_ranking_columns_on_connection(conn, database)
    except Exception:
        pass


def _sqlite_pragmas_patched_connect(database: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
    if _sqlite_pragmas_looks_like_summary_db_file(database):
        try:
            current_timeout = float(kwargs.get("timeout", 0) or 0)
        except Exception:
            current_timeout = 0.0
        kwargs["timeout"] = max(current_timeout, max(60.0, _sqlite_pragmas_env_float("SUMMARY_SQLITE_TIMEOUT", 60.0)))

    conn = _SQLITE_PRAGMAS_ORIG_CONNECT(database, *args, **kwargs)
    try:
        _sqlite_pragmas_apply_pragmas(conn, database)
    except Exception:
        try:
            logger.debug("[SQLITE MEMORY PRAGMAS] apply failed database=%s", database, exc_info=True)
        except Exception:
            pass
    return conn


def _sqlite_pragmas_install_summary_spool_flush_patch() -> bool:
    """spool flush の bulk-first 化は
    trading/summary/persistence/summary_save_spool._save_direct へ本文化済み。

    本体側が旧マーカー _summary_spool_flush_bulk_patch を関数属性として
    立てているため、仮にこのコピーが残っていても二重ラップにはならない。
    無効化スイッチ DISABLE_SUMMARY_SPOOL_FLUSH_BULK_PATCH も本体側が見る。
    """
    global _SQLITE_PRAGMAS_SPOOL_FLUSH_PATCHED
    _SQLITE_PRAGMAS_SPOOL_FLUSH_PATCHED = True
    return True


def _install_sqlite_memory_pragmas() -> bool:
    global _SQLITE_PRAGMAS_INSTALLED

    _sqlite_pragmas_force_summary_lock_env()
    spool_ok = _sqlite_pragmas_install_summary_spool_flush_patch()

    if _SQLITE_PRAGMAS_INSTALLED:
        return True
    if not _sqlite_pragmas_env_bool("SQLITE_MEMORY_PRAGMAS_ENABLED", True):
        return bool(spool_ok)

    if getattr(sqlite3.connect, "__module__", "") == "core.startup.sqlite_memory_pragmas":
        _SQLITE_PRAGMAS_INSTALLED = True
        logger.warning(
            "[SQLITE MEMORY PRAGMAS] skip own wrap: sqlite3.connect already wrapped by "
            "sitecustomize's core.startup.sqlite_memory_pragmas"
        )
        return True

    try:
        sqlite3.connect = _sqlite_pragmas_patched_connect  # type: ignore[assignment]
        _SQLITE_PRAGMAS_INSTALLED = True
        logger.warning(
            "[SQLITE MEMORY PRAGMAS] installed (main_database.py own copy) temp_store=%s cache_kb=%s mmap=%s spill_off=%s summary_busy_ms=%s spool_bulk=%s",
            os.getenv("SQLITE_MEMORY_TEMP_STORE", "MEMORY"),
            os.getenv("SQLITE_MEMORY_CACHE_KB", "-65536"),
            os.getenv("SQLITE_MMAP_SIZE_BYTES", "268435456"),
            os.getenv("SQLITE_CACHE_SPILL_OFF", "1"),
            os.getenv("SUMMARY_SQLITE_BUSY_TIMEOUT_MS", "60000"),
            spool_ok,
        )
        return True
    except Exception:
        logger.exception("[SQLITE MEMORY PRAGMAS] install failed (main_database.py own copy)")
        return bool(spool_ok)


# summary WAL checkpoint loop. 旧 core/startup/summary_wal_checkpoint_patch.py から移設。
_WAL_CHECKPOINT_TRUE = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
_WAL_CHECKPOINT_FALSE = {"0", "false", "no", "n", "off", "disable", "disabled"}
_WAL_CHECKPOINT_MODES = {"PASSIVE", "FULL", "RESTART", "TRUNCATE"}
_WAL_CHECKPOINT_INSTALLED = False
_WAL_CHECKPOINT_THREAD: threading.Thread | None = None


def _wal_checkpoint_env_bool(name: str, default: bool) -> bool:
    raw = str(os.getenv(name, "")).strip().lower()
    if raw in _WAL_CHECKPOINT_TRUE:
        return True
    if raw in _WAL_CHECKPOINT_FALSE:
        return False
    return bool(default)


def _wal_checkpoint_env_float(name: str, default: float) -> float:
    try:
        raw = str(os.getenv(name, "")).strip()
        return float(raw) if raw else float(default)
    except Exception:
        return float(default)


def _wal_checkpoint_env_int(name: str, default: int) -> int:
    try:
        raw = str(os.getenv(name, "")).strip()
        return int(float(raw)) if raw else int(default)
    except Exception:
        return int(default)


def _wal_checkpoint_mode() -> str:
    mode = str(os.getenv("SUMMARY_WAL_CHECKPOINT_MODE", "PASSIVE")).strip().upper()
    return mode if mode in _WAL_CHECKPOINT_MODES else "PASSIVE"


def _wal_checkpoint_summary_db_path() -> Path:
    from data_collectors.config import summary_db_path
    return Path(summary_db_path())


def _wal_checkpoint_targets() -> "list[tuple[str, Path]]":
    """WAL チェックポイントの対象 DB。

    当初は summary だけを見ていたが、2026-08-18 の後場に ranking DB の WAL が
    282MB まで伸び (本体 444MB)、SMB 越しの読み取りが 1 回 4 秒 -> 13〜20 秒に
    悪化して 1 tick が 333 秒になった。summary と同じ構造なので同じ扱いにする。

    SUMMARY_WAL_CHECKPOINT_TARGETS で対象を絞れる (既定 summary,ranking,push)。
    """
    raw = str(os.getenv("SUMMARY_WAL_CHECKPOINT_TARGETS", "summary,ranking,push"))
    want = [x.strip().lower() for x in raw.split(",") if x.strip()]

    out: list[tuple[str, Path]] = []
    for name in want:
        try:
            if name == "summary":
                out.append((name, _wal_checkpoint_summary_db_path()))
            elif name == "ranking":
                from data_collectors.config import ranking_db_path
                out.append((name, Path(ranking_db_path())))
            elif name == "push":
                from data_collectors.config import push_db_path
                out.append((name, Path(push_db_path())))
        except Exception:
            logger.debug("[WAL CHECKPOINT] target resolve failed name=%s", name, exc_info=True)
    return out


def _wal_checkpoint_once(force_mode: str | None = None, db_path: "Path | None" = None, label: str = "SUMMARY") -> bool:
    db_path = db_path if db_path is not None else _wal_checkpoint_summary_db_path()
    if not db_path.exists():
        logger.debug("[%s WAL CHECKPOINT] skip db missing path=%s", label, db_path)
        return False

    mode = force_mode if force_mode in _WAL_CHECKPOINT_MODES else _wal_checkpoint_mode()
    busy_timeout_ms = max(1000, _wal_checkpoint_env_int("SUMMARY_WAL_CHECKPOINT_BUSY_TIMEOUT_MS", 5000))
    wal_path = Path(str(db_path) + "-wal")
    try:
        wal_size = wal_path.stat().st_size if wal_path.exists() else 0
    except Exception:
        wal_size = 0

    try:
        conn = sqlite3.connect(str(db_path), timeout=max(1.0, busy_timeout_ms / 1000.0))
        try:
            conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
            # checkpoint 前に journal_mode を設定し直す必要はない。
            # 既に WAL ならこの行が locking protocol / malformed を拾うだけ損。
            _md_ensure_journal_mode_wal_if_needed(conn, "checkpoint")
            # checkpoint は WAL の中身を .db 本体へ書き戻す = 書き込み。
            # 引け後は他プロセスの summary 書き込みと重ねない (summary_write_lock.py)。
            import contextlib
            from database.sqlite.summary_write_lock import is_summary_target, summary_write_lock

            lock = summary_write_lock(f"{label} WAL CHECKPOINT") if is_summary_target(db_path) else contextlib.nullcontext()
            with lock:
                result = conn.execute(f"PRAGMA wal_checkpoint({mode})").fetchone()
                conn.commit()
        finally:
            conn.close()
        logger.info(
            "[%s WAL CHECKPOINT] done mode=%s db=%s wal_size=%s result=%s",
            label, mode, db_path, wal_size, result,
        )
        return True
    except sqlite3.OperationalError as exc:
        logger.warning("[%s WAL CHECKPOINT] skip busy mode=%s db=%s err=%s", label, mode, db_path, exc)
        return False
    except Exception:
        logger.exception("[%s WAL CHECKPOINT] failed db=%s", label, db_path)
        return False


def _wal_checkpoint_loop() -> None:
    """定期 WAL チェックポイント。

    PASSIVE はリーダー/ライターがいると何も回収せずに諦める。
    summary は毎分ジョブが走るため常時 busy になりがちで、
    2026-08-17 は "skip busy" を繰り返したまま WAL が 373MB まで伸び、
    SMB 越しの読み取りが "file is not a database" を 142 件出す原因になった。

    そこで PASSIVE が連続で空振りし、かつ WAL が閾値を超えたときだけ
    1 回だけ強いモード (既定 TRUNCATE) へ格上げする。
    既定モード自体は PASSIVE のままで、通常時のロック競合は増やさない。
    """
    initial_delay = max(0.0, _wal_checkpoint_env_float("SUMMARY_WAL_CHECKPOINT_INITIAL_DELAY_SEC", 20.0))
    interval = max(10.0, _wal_checkpoint_env_float("SUMMARY_WAL_CHECKPOINT_INTERVAL_SEC", 60.0))
    if initial_delay:
        time.sleep(initial_delay)

    miss_streak: dict[str, int] = {}
    while True:
        if _wal_checkpoint_env_bool("SUMMARY_WAL_CHECKPOINT_ENABLED", True):
            for name, db_path in _wal_checkpoint_targets():
                label = name.upper()
                ok = _wal_checkpoint_once(db_path=db_path, label=label)
                miss_streak[name] = 0 if ok else (miss_streak.get(name, 0) + 1)

                if not ok and _wal_checkpoint_should_escalate(miss_streak[name], db_path=db_path):
                    mode = str(os.getenv("SUMMARY_WAL_CHECKPOINT_ESCALATE_MODE", "TRUNCATE")).strip().upper()
                    if mode not in _WAL_CHECKPOINT_MODES:
                        mode = "TRUNCATE"
                    logger.warning(
                        "[%s WAL CHECKPOINT] escalate mode=%s miss_streak=%s wal_mb=%.0f",
                        label, mode, miss_streak[name], _wal_checkpoint_wal_size_mb(db_path),
                    )
                    if _wal_checkpoint_once(force_mode=mode, db_path=db_path, label=label):
                        miss_streak[name] = 0
        time.sleep(interval)


def _wal_checkpoint_wal_size_mb(db_path: "Path | None" = None) -> float:
    try:
        base = db_path if db_path is not None else _wal_checkpoint_summary_db_path()
        wal = Path(str(base) + "-wal")
        return wal.stat().st_size / (1024.0 * 1024.0) if wal.exists() else 0.0
    except Exception:
        return 0.0


def _wal_checkpoint_should_escalate(miss_streak: int, db_path: "Path | None" = None) -> bool:
    """PASSIVE が続けて空振りし、WAL も膨らんでいるときだけ True。"""
    try:
        if not _wal_checkpoint_env_bool("SUMMARY_WAL_CHECKPOINT_ESCALATE_ENABLED", True):
            return False
        need_miss = max(1, _wal_checkpoint_env_int("SUMMARY_WAL_CHECKPOINT_ESCALATE_AFTER_MISSES", 5))
        need_mb = max(1.0, _wal_checkpoint_env_float("SUMMARY_WAL_CHECKPOINT_ESCALATE_WAL_MB", 128.0))
        return miss_streak >= need_miss and _wal_checkpoint_wal_size_mb(db_path) >= need_mb
    except Exception:
        return False


def _install_summary_wal_checkpoint() -> None:
    """Install a 1-minute WAL checkpoint loop for summaryYYYYMMDD.db.

    This copies committed frames from summaryYYYYMMDD.db-wal into the .db file
    without changing the writer logic.  PASSIVE mode is the default to avoid
    increasing writer/reader lock contention on NAS SQLite.
    """
    global _WAL_CHECKPOINT_INSTALLED, _WAL_CHECKPOINT_THREAD
    try:
        os.environ.setdefault("SUMMARY_WAL_CHECKPOINT_ENABLED", "1")
        os.environ.setdefault("SUMMARY_WAL_CHECKPOINT_INTERVAL_SEC", "60")
        os.environ.setdefault("SUMMARY_WAL_CHECKPOINT_MODE", "PASSIVE")
        os.environ.setdefault("SUMMARY_WAL_CHECKPOINT_BUSY_TIMEOUT_MS", "5000")
        os.environ.setdefault("SUMMARY_WAL_CHECKPOINT_INITIAL_DELAY_SEC", "20")

        if _WAL_CHECKPOINT_INSTALLED:
            ok = True
        elif not _wal_checkpoint_env_bool("SUMMARY_WAL_CHECKPOINT_ENABLED", True):
            logger.warning("[SUMMARY WAL CHECKPOINT] disabled by env")
            ok = False
        else:
            _WAL_CHECKPOINT_THREAD = threading.Thread(
                target=_wal_checkpoint_loop, name="summary-wal-checkpoint-loop", daemon=True
            )
            _WAL_CHECKPOINT_THREAD.start()
            _WAL_CHECKPOINT_INSTALLED = True
            ok = True

        logger.warning("[MAIN DATABASE] summary wal checkpoint installed ok=%s", ok)
    except Exception:
        logger.exception("[MAIN DATABASE] summary wal checkpoint install failed; continue")


def _install_summary_stale_guard() -> None:
    """Install stale summary guard for main_database.py / child collectors.

    main.py has its own runtime patch bootstrap, but main_database.py is a
    separate entrypoint.  When data collectors publish merged summary through
    core.global_context.context, this guard prevents old PUSH/ranking rows from
    staying alive as fresh candidates.
    """
    try:
        # Defaults are inherited by any child collector processes.
        # Important: default must be hard-drop.  Keeping latest-per-symbol on
        # global lag caused 2026-06-30 15:19 rows to remain alive on 2026-07-01
        # during market session.
        defaults = {
            "SUMMARY_STALE_GUARD_ENABLED": "1",
            "SUMMARY_STALE_STRICT_TODAY_ONLY": "1",
            "SUMMARY_STALE_KEEP_LATEST_PER_SYMBOL_ON_GLOBAL_LAG": "0",
            "SUMMARY_STALE_GLOBAL_LAG_GRACE_SEC": "30",
            "PUSH_SUMMARY_1MIN_MAX_AGE_SEC": "120",
            "PUSH_SUMMARY_3MIN_MAX_AGE_SEC": "240",
            "PUSH_SUMMARY_5MIN_MAX_AGE_SEC": "420",
            "PUSH_SUMMARY_1MIN_KEEP_ROWS": "0",
            "PUSH_SUMMARY_3MIN_KEEP_ROWS": "0",
            "PUSH_SUMMARY_5MIN_KEEP_ROWS": "0",
            "RANKING_SUMMARY_1MIN_MAX_AGE_SEC": "180",
            "RANKING_SUMMARY_3MIN_MAX_AGE_SEC": "300",
            "RANKING_SUMMARY_5MIN_MAX_AGE_SEC": "480",
            "RANKING_SUMMARY_1MIN_KEEP_ROWS": "0",
            "RANKING_SUMMARY_3MIN_KEEP_ROWS": "0",
            "RANKING_SUMMARY_5MIN_KEEP_ROWS": "0",
            # User rule: recent 5-bar turnover target is 1,000,000 yen.
            # The old 5,000,000 default can delete all candidates around open.
            "LIQUIDITY_MIN_TURNOVER": "1000000",
            "SUMMARY_ENTRY_ALLOW_UNREADY_5MIN_AT_OPEN": "1",
        }
        for key, value in defaults.items():
            os.environ.setdefault(key, value)

        # drop_stale_summary_rows 本体は core/global_context/context.py へ移設済みのため、
        # ここでは呼び出し先プロセス全体で読まれる環境変数デフォルトの設定だけを行う。
        logger.warning(
            "[MAIN DATABASE] summary stale guard env defaults set strict_today=%s global_lag_keep=%s liquidity_min_turnover=%s",
            os.getenv("SUMMARY_STALE_STRICT_TODAY_ONLY"),
            os.getenv("SUMMARY_STALE_KEEP_LATEST_PER_SYMBOL_ON_GLOBAL_LAG"),
            os.getenv("LIQUIDITY_MIN_TURNOVER"),
        )
    except Exception:
        logger.exception("[MAIN DATABASE] summary stale guard env defaults setup failed; continue")


def _install_db_startup_scope_defaults() -> None:
    """Set the main_database.py-side resolution of sitecustomize's DB startup scope.

    Moved-in equivalent of sitecustomize.py's _configure_db_startup_scope_defaults()
    for the non-db_prepare branch (main_database.py is never db_prepare_runner.py,
    so that branch can never apply here). sitecustomize.py already sets these via
    setdefault before main_database.py's own code runs; this call makes the same
    resolution explicit and independent of sys.argv sniffing.
    """
    try:
        os.environ.setdefault("DISABLE_SUMMARY_RANKING_SCHEMA_REPAIR_PATCH", "1")
        os.environ.setdefault("SUMMARY_RANKING_SCHEMA_REPAIR_SCOPE", "db_prepare_only")
        os.environ.setdefault("DISABLE_SUMMARY_MTF_CATCHUP", "1")
        logger.warning(
            "[MAIN DATABASE] db startup scope defaults installed schema_repair_disabled=%s mtf_catchup_disabled=%s",
            os.getenv("DISABLE_SUMMARY_RANKING_SCHEMA_REPAIR_PATCH"),
            os.getenv("DISABLE_SUMMARY_MTF_CATCHUP"),
        )
    except Exception:
        logger.exception("[MAIN DATABASE] db startup scope defaults install failed; continue")


def _install_push_summary_realtime() -> None:
    """Force PUSH DB writer env, start the writer singleton, and schedule a bootstrap rebuild.

    The function-wrap parts of the old core/startup/push_summary_realtime_patch.py
    (StreamDBWriter.flush trigger, start_push_stream forced writer injection, 401
    refresh suppression) are now baked directly into their real target modules
    (trading/push/push_db_writer.py, trading/push/push_stream/__init__.py,
    force_cancel_loop.py, trading/position/kabu_position_reader.py,
    kabu_api/positions.py), gated at call time by
    data_collectors.split_mode.is_data_collector_process(). Only the startup-time
    side effects (not tied to any single function) remain here.
    """
    try:
        from trading.push.push_stream import (
            _force_push_writer_env_if_database_process,
            _ensure_stream_writer_singleton_started,
        )
        _force_push_writer_env_if_database_process()
        writer = _ensure_stream_writer_singleton_started()
        logger.warning("[MAIN DATABASE] push db writer singleton ensured ok=%s", writer is not None)
    except Exception:
        logger.exception("[MAIN DATABASE] push db writer singleton ensure failed; continue")

    try:
        from trading.push.push_summary_rebuild_trigger import schedule_bootstrap_rebuild
        schedule_bootstrap_rebuild()
    except Exception:
        logger.exception("[MAIN DATABASE] push summary bootstrap rebuild schedule failed; continue")


class SettingsIniCorrupted(RuntimeError):
    """settings.ini exists but ConfigParser cannot parse it.

    Kept distinct from "apipassword is empty": a broken file is never a valid
    configuration, so it must not be reported as a password/token problem.
    """


def _read_api_password_from_settings() -> str:
    conf = ConfigParser()
    path = PROJECT_ROOT / "settings.ini"
    try:
        # utf-8-sig: a BOM in front of [aukabu] breaks the first section header the
        # same way a stray character does.
        conf.read(str(path), encoding="utf-8-sig")
    except Exception as e:
        from token_manager import describe_parsing_error

        raise SettingsIniCorrupted(
            "settings.ini cannot be parsed path=%s detail=%s" % (path, describe_parsing_error(e))
        ) from e

    if conf.has_section("aukabu"):
        return conf.get("aukabu", "apipassword", fallback="")

    if conf.has_section("kabusapi"):
        return conf.get("kabusapi", "apipassword", fallback="")

    return ""


def _safe_content_text(value: Any, limit: int = 240) -> str:
    """Compatibility shim; single implementation lives in kabu_api.token_bootstrap."""
    from kabu_api import token_bootstrap as _tb

    return _tb.safe_content_text(value, limit)


def _is_api_key_mismatch(content: Any) -> bool:
    """Compatibility shim; single implementation lives in kabu_api.token_bootstrap."""
    from kabu_api import token_bootstrap as _tb

    return _tb.is_api_key_mismatch(content)


def _kabu_preflight_request(token: str, endpoint: str, timeout: float = 5.0) -> tuple[bool, int | None, Any]:
    """Compatibility shim; the implementation lives in kabu_api.token_bootstrap."""
    from kabu_api import token_bootstrap as _tb

    return _tb.http_get_json(f"{KABU_API_BASE_URL}{endpoint}", token, timeout)


def _preflight_kabu_token(token: str) -> bool:
    """Validate a token with real read-only kabu API calls before spawning children.

    /token can succeed while subsequent APIs still reject X-API-KEY, and a cached
    token can be dead on the server while settings.ini still looks fresh.  This
    guard catches both and prevents push_receiver/ranking_collector from starting
    with an unusable token.

    Kept as a thin wrapper so existing callers and log expectations still work; the
    rules now live in kabu_api.token_bootstrap.preflight_token().
    """
    from kabu_api import token_bootstrap as _tb

    result = _tb.preflight_token(token, base_url=KABU_API_BASE_URL, label="main_database")
    if result.ok:
        os.environ["KABU_TOKEN_PREFLIGHT_OK"] = "1"
        os.environ["KABU_TOKEN_PREFLIGHT_ENDPOINT"] = str(result.endpoint or "")
        return True
    os.environ["KABU_TOKEN_PREFLIGHT_OK"] = "0"
    return False


def _bootstrap_kabu_token_for_data_collectors() -> bool:
    """Startup token gate: preflight decides validity, one /token recovers a stale cache.

    Old behaviour: refresh_token() was asked for a token, but its TTL layer returned
    the cached settings.ini token whenever the file was younger than
    KABU_TOKEN_PARENT_REFRESH_TTL_SEC (3600s).  After a kabu Station restart that
    cached token was already dead server-side, preflight got 401 Code=4001009, and
    runtime refresh is forbidden by policy -> unrecoverable abort.

    New behaviour (kabu_api.token_bootstrap):
      cached token -> preflight -> 200                       => use it, 0 /token calls
      cached token -> preflight -> 401/403/4001009           => one /token, save,
                                                                publish, one more
                                                                preflight
      /token itself -> 401/4001009                           => API password mismatch
      fresh token still rejected                             => abort, no retry loop
      kabu Station unreachable                               => fail-safe abort
    """
    _ensure_basic_logging()

    try:
        from kabu_api import token_bootstrap as _tb
    except Exception:
        logger.exception("[MAIN DATABASE] token bootstrap module import failed")
        return False

    # Explicit password source, so an apipassword problem is never mistaken for a
    # stale token.  The value itself is never logged.
    api_password = ""
    try:
        api_password = _read_api_password_from_settings()
    except SettingsIniCorrupted as e:
        # 破損したファイルで先へ進むと password_present=False → apipassword 不一致に
        # 見えてしまう。壊れた行を名指しして、ここで止める。
        logger.error(
            "[MAIN DATABASE] abort: settings.ini is corrupted, so nothing could be read from it. "
            "This is NOT a stale token and NOT an API password mismatch. "
            "Fix the reported line (a settings.ini.*.bak next to it may hold the previous copy). %s",
            e,
        )
        return False
    except Exception:
        logger.exception("[MAIN DATABASE] settings.ini apipassword read failed")
    logger.warning(
        "[MAIN DATABASE] api_password_source=settings.ini password_present=%s password_len=%d",
        bool(api_password),
        len(str(api_password or "")),
    )

    try:
        result = _tb.bootstrap_startup_token(
            apipassword=api_password or None,
            label="main_database",
        )
    except Exception:
        logger.exception("[MAIN DATABASE] token bootstrap raised")
        return False

    if not result.ok:
        if result.failure_kind == _tb.FAIL_API_PASSWORD_MISMATCH:
            logger.error(
                "[MAIN DATABASE] TOKEN REQUEST FAILED: POST /token rejected the API password "
                "(Code=4001009 on /token itself). This is NOT a stale cached token. "
                "Fix settings.ini [aukabu] apipassword so it matches kabu Station's API password. %s",
                result.summary(),
            )
        elif result.failure_kind == _tb.FAIL_FRESH_TOKEN_REJECTED:
            logger.error(
                "[MAIN DATABASE] abort: a freshly issued token was still rejected by kabu API. "
                "Restart kabu Station and confirm the API is enabled. Not retrying. %s",
                result.summary(),
            )
        elif result.failure_kind in (_tb.FAIL_PREFLIGHT_TRANSPORT, _tb.FAIL_TOKEN_API_TRANSPORT):
            logger.error(
                "[MAIN DATABASE] abort: kabu Station API could not be reached on %s. %s",
                KABU_API_BASE_URL,
                result.summary(),
            )
        else:
            logger.error("[MAIN DATABASE] abort: token bootstrap failed. %s", result.summary())
        return False

    token = result.token
    os.environ["KABU_TOKEN_PREFLIGHT_OK"] = "1"
    if result.preflight is not None and result.preflight.endpoint:
        os.environ["KABU_TOKEN_PREFLIGHT_ENDPOINT"] = str(result.preflight.endpoint)

    # Keep the token holders that predate token_bootstrap in sync.
    try:
        from token_manager import get_valid_token

        _ = get_valid_token()
    except Exception:
        logger.debug("[MAIN DATABASE] get_valid_token sync skipped", exc_info=True)

    try:
        from global_state import global_data

        global_data.token_value = token
    except Exception:
        logger.debug("[MAIN DATABASE] global_data.token_value set skipped", exc_info=True)

    logger.warning(
        "[MAIN DATABASE] token bootstrap OK for data collectors %s", result.summary()
    )
    return True


def _acquire_single_instance_or_report(role: str) -> bool:
    """このマシンで role が唯一のインスタンスかを確定する。

    OS ファイルロックを取れなかった場合は False を返す。
    ロックが取れない = 別の生きたプロセスがハンドルを握っている、なので
    holder meta の鮮度に関わらず起動しない (安全側)。
    ガード自体が使えない場合 (モジュール import 失敗・例外) だけ
    True を返して従来どおり起動する = 誤検知で本番を落とさない。
    無関係な python プロセスには一切触らない (kill もしない)。
    """
    try:
        from core.process_lock import acquire_single_instance, describe_holder
    except Exception:
        logger.exception("[MAIN DATABASE] single instance guard unavailable -> 多重起動チェックなしで続行")
        return True

    try:
        ok, holder = acquire_single_instance(role)
    except Exception:
        logger.exception("[MAIN DATABASE] single instance guard failed -> 多重起動チェックなしで続行")
        return True

    if ok:
        return True

    state = str(holder.get("holder_state", "unknown"))
    if state == "alive":
        logger.error(
            "[MAIN DATABASE][DUPLICATE START BLOCKED] 既に %s が起動しているので "
            "このプロセスは子プロセスを一切起動せずに終了する。holder: %s "
            "(kabu Station の PUSH は 1 セッション 1 接続。2 ツリー走らせると "
            "後発の push_receiver が ws_owner lock を取れず永久に受信できない)",
            role, describe_holder(holder),
        )
        logger.error(
            "[MAIN DATABASE][DUPLICATE START BLOCKED] 意図的に 2 本目を起動したい場合は "
            "AUTOSTOCK_SINGLE_INSTANCE_LOCK=0 を設定する (非推奨)"
        )
        return False

    # meta は古いが OS ロックは誰かが握っている、という矛盾した状態。
    # 起動は止めるが、状態をそのまま出して原因を追えるようにする。
    logger.error(
        "[MAIN DATABASE][DUPLICATE START BLOCKED] %s のロックを取得できなかった "
        "(holder meta は最新ではない state=%s)。holder: %s",
        role, state, describe_holder(holder),
    )
    return False


def main() -> int:
    _ensure_basic_logging()

    try:
        os.chdir(str(PROJECT_ROOT))
    except Exception:
        logger.exception("[MAIN DATABASE] chdir PROJECT_ROOT failed path=%s", PROJECT_ROOT)
        return 1

    logger.info("========== MAIN DATABASE BOOT START ==========")
    logger.info("[MAIN DATABASE] PROJECT_ROOT=%s", PROJECT_ROOT)
    logger.info("[MAIN DATABASE] cwd=%s", os.getcwd())

    # ------------------------------------------------------------
    # 多重起動ガード (2026-09-03)
    # ------------------------------------------------------------
    # main_database.py を 2 回起動しても誰も止めていなかった。
    # 実測: tree A 10:47:06 (main_database 12608) と
    #       tree B 11:08:56 (main_database 19972) が同時に走り、
    #       それぞれが push_receiver / summary / yahoo / ranking を
    #       1 セットずつ持っていた。
    #
    # kabu Station の PUSH は 1 セッション 1 接続なので、
    # ws_owner の single-owner lock が 2 本目の WebSocket を拒否する。
    # その結果 tree B の push_receiver は永久に受信できない。
    # ロックを緩めるのではなく、ツリーごと二重に立ち上げるのをここで止める。
    #
    # AUTOSTOCK_SINGLE_INSTANCE_LOCK=0 で従来どおり多重起動できる。
    if not _acquire_single_instance_or_report("main_database"):
        return 0

    # Order matters.  Everything above the token gate must be env-only setup with
    # no threads, no sockets and no DB traffic; everything that costs something is
    # started only after preflight proves the token works.  The old order started
    # the PUSH DB writer singleton and the WAL checkpoint loop first, so an auth
    # failure meant we had already spun up background work we then threw away.
    _install_db_startup_scope_defaults()
    _install_cpu_guard_env()
    _install_summary_stale_guard()

    try:
        from data_collectors.split_mode import mark_as_data_collector_process
        mark_as_data_collector_process()
    except Exception:
        logger.exception("[MAIN DATABASE] failed to mark data collector process")
        return 1

    # --- auth gate: no worker and no child process starts before this passes ---
    if not _bootstrap_kabu_token_for_data_collectors():
        logger.error(
            "[MAIN DATABASE] abort because token bootstrap/preflight failed. "
            "Please confirm kabu Station is running, API is enabled, and API password is correct."
        )
        return 1

    _install_summary_wal_checkpoint()
    _install_push_summary_realtime()

    try:
        from scripts.data_collectors_runner import main as data_collectors_main
    except Exception:
        logger.exception("[MAIN DATABASE] failed to import scripts.data_collectors_runner.main")
        return 1

    return int(data_collectors_main())


if __name__ == "__main__":
    raise SystemExit(main())
