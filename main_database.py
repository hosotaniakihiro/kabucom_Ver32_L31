# ============================================================
# File   : main_database.py
# Version: DATA-COLLECTORS-MAIN-DATABASE-ENTRY-V9-STRICT-LIVE-SUMMARY
# ------------------------------------------------------------
# Purpose:
#   - DB作成 / ランキング取得 / PUSH銘柄登録 / PUSH受信 を起動する入口
#   - 既存 main.py とは分離する
#   - 実体は scripts/data_collectors_runner.py に委譲する
#   - main_database.py 経由でも古い PUSH/ranking summary を候補に残さない
#   - main_database.py のコンソールログに時刻を付け、ファイルにも保存する
#   - summary DB の WAL を1分ごとに checkpoint して .db 本体へ反映する
#   - /token 取得直後に実APIで token preflight を行い、認証NGなら子プロセスを起動しない
#   - 前日PUSH summary の global-lag/min_keep fallback を子プロセス既定で無効化する
# ============================================================

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import sys
import urllib.error
import urllib.request
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
            from core.startup.sqlite_memory_pragmas_patch import install as _install_sqlite_memory_pragmas
            _install_sqlite_memory_pragmas()
        except Exception:
            pass

        logger.info("[MAIN DATABASE] cpu guard env installed ok=True")
    except Exception:
        logger.exception("[MAIN DATABASE] cpu guard env install failed; continue")


def _install_summary_sqlite_lock_tolerance() -> None:
    """Install summary DB lock tolerance before spawning child collectors.

    The actual summary/Yahoo/MTF work runs in child processes. Installing this
    patch here is still useful because it sets environment defaults that are
    inherited by those child processes. The sqlite3.connect monkey patch also
    protects any summary DB access done directly in main_database.py.
    """
    try:
        from core.startup.summary_sqlite_lock_tolerance_patch import install
        ok = install()
        logger.warning("[MAIN DATABASE] summary sqlite lock tolerance installed ok=%s", ok)
    except Exception:
        logger.exception("[MAIN DATABASE] summary sqlite lock tolerance install failed; continue")


def _install_summary_wal_checkpoint() -> None:
    """Install a 1-minute WAL checkpoint loop for summaryYYYYMMDD.db.

    This copies committed frames from summaryYYYYMMDD.db-wal into the .db file
    without changing the writer logic.  PASSIVE mode is the default to avoid
    increasing writer/reader lock contention on NAS SQLite.
    """
    try:
        os.environ.setdefault("SUMMARY_WAL_CHECKPOINT_ENABLED", "1")
        os.environ.setdefault("SUMMARY_WAL_CHECKPOINT_INTERVAL_SEC", "60")
        os.environ.setdefault("SUMMARY_WAL_CHECKPOINT_MODE", "PASSIVE")
        os.environ.setdefault("SUMMARY_WAL_CHECKPOINT_BUSY_TIMEOUT_MS", "5000")
        from core.startup.summary_wal_checkpoint_patch import install
        ok = install()
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

        from core.startup.summary_stale_guard_patch import install
        ok = install()
        logger.warning(
            "[MAIN DATABASE] summary stale guard installed ok=%s strict_today=%s global_lag_keep=%s liquidity_min_turnover=%s",
            ok,
            os.getenv("SUMMARY_STALE_STRICT_TODAY_ONLY"),
            os.getenv("SUMMARY_STALE_KEEP_LATEST_PER_SYMBOL_ON_GLOBAL_LAG"),
            os.getenv("LIQUIDITY_MIN_TURNOVER"),
        )
    except Exception:
        logger.exception("[MAIN DATABASE] summary stale guard install failed; continue")


def _read_api_password_from_settings() -> str:
    conf = ConfigParser()
    conf.read(str(PROJECT_ROOT / "settings.ini"), encoding="utf-8")

    if conf.has_section("aukabu"):
        return conf.get("aukabu", "apipassword", fallback="")

    if conf.has_section("kabusapi"):
        return conf.get("kabusapi", "apipassword", fallback="")

    return ""


def _safe_content_text(value: Any, limit: int = 240) -> str:
    try:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    except Exception:
        text = str(value)
    text = text.replace("\r", " ").replace("\n", " ")
    return text[:limit]


def _is_api_key_mismatch(content: Any) -> bool:
    try:
        if isinstance(content, dict):
            code = str(content.get("Code") or "")
            msg = str(content.get("Message") or "")
            return code == "4001009" or "APIキー不一致" in msg
        s = str(content)
        return "4001009" in s or "APIキー不一致" in s
    except Exception:
        return False


def _kabu_preflight_request(token: str, endpoint: str, timeout: float = 5.0) -> tuple[bool, int | None, Any]:
    url = f"{KABU_API_BASE_URL}{endpoint}"
    req = urllib.request.Request(
        url,
        headers={
            "Content-Type": "application/json",
            "X-API-KEY": str(token or ""),
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            raw = res.read().decode("utf-8", errors="ignore")
            try:
                content = json.loads(raw) if raw else {}
            except Exception:
                content = raw
            return True, int(getattr(res, "status", 200) or 200), content
    except urllib.error.HTTPError as e:
        raw = ""
        try:
            raw = e.read().decode("utf-8", errors="ignore")
        except Exception:
            raw = ""
        try:
            content = json.loads(raw) if raw else str(e)
        except Exception:
            content = raw or str(e)
        status = int(getattr(e, "code", 0) or 0)
        return False, status, content
    except Exception as e:
        return False, None, str(e)


def _preflight_kabu_token(token: str) -> bool:
    """Validate the freshly acquired token with real kabu API calls before spawning children.

    /token can succeed while subsequent APIs still reject X-API-KEY.  This guard
    catches that condition early and prevents push_receiver/ranking_collector from
    starting with an unusable token.
    """
    token = str(token or "").strip()
    if not token:
        logger.error("[MAIN DATABASE] kabu token preflight failed: empty token")
        return False

    endpoints = [
        "/positions",
        "/wallet/cash",
    ]

    auth_failures: list[tuple[str, int | None, Any]] = []
    transport_failures: list[tuple[str, int | None, Any]] = []

    for endpoint in endpoints:
        ok, status, content = _kabu_preflight_request(token, endpoint)
        if ok:
            logger.warning(
                "[MAIN DATABASE] kabu token preflight ok endpoint=%s status=%s token_len=%d",
                endpoint,
                status,
                len(token),
            )
            os.environ["KABU_TOKEN_PREFLIGHT_OK"] = "1"
            os.environ["KABU_TOKEN_PREFLIGHT_ENDPOINT"] = endpoint
            return True

        if status in (401, 403) or _is_api_key_mismatch(content):
            auth_failures.append((endpoint, status, content))
            logger.error(
                "[MAIN DATABASE] kabu token preflight auth failed endpoint=%s status=%s content=%s token_len=%d",
                endpoint,
                status,
                _safe_content_text(content),
                len(token),
            )
            continue

        # 400/404 etc. means the request reached kabu Station and was not rejected
        # by API key.  For authentication preflight, this is enough to prove token
        # was accepted; log it as accepted-with-endpoint-error.
        if status is not None:
            logger.warning(
                "[MAIN DATABASE] kabu token preflight accepted endpoint=%s status=%s content=%s token_len=%d",
                endpoint,
                status,
                _safe_content_text(content),
                len(token),
            )
            os.environ["KABU_TOKEN_PREFLIGHT_OK"] = "1"
            os.environ["KABU_TOKEN_PREFLIGHT_ENDPOINT"] = endpoint
            return True

        transport_failures.append((endpoint, status, content))
        logger.error(
            "[MAIN DATABASE] kabu token preflight transport failed endpoint=%s error=%s",
            endpoint,
            _safe_content_text(content),
        )

    if auth_failures:
        endpoint, status, content = auth_failures[-1]
        logger.error(
            "[MAIN DATABASE] abort: token was obtained but rejected by kabu API endpoint=%s status=%s content=%s. "
            "Please restart kabu Station, enable API, and confirm settings.ini apipassword matches kabu Station.",
            endpoint,
            status,
            _safe_content_text(content),
        )
    elif transport_failures:
        endpoint, _status, content = transport_failures[-1]
        logger.error(
            "[MAIN DATABASE] abort: kabu API preflight could not connect endpoint=%s error=%s. "
            "Please confirm kabu Station API is running on localhost:18080.",
            endpoint,
            _safe_content_text(content),
        )
    else:
        logger.error("[MAIN DATABASE] abort: kabu token preflight failed for unknown reason")
    return False


def _bootstrap_kabu_token_for_data_collectors() -> bool:
    _ensure_basic_logging()

    try:
        api_password = _read_api_password_from_settings()

        if not api_password:
            logger.error(
                "[MAIN DATABASE] token bootstrap failed: settings.ini apipassword missing"
            )
            return False
        from token_manager import refresh_token, get_valid_token

        token = refresh_token(api_password)

        if not token:
            logger.error("[MAIN DATABASE] token bootstrap failed: empty token returned")
            return False

        try:
            _ = get_valid_token()
        except Exception:
            pass

        if not _preflight_kabu_token(str(token)):
            logger.error(
                "[MAIN DATABASE] token bootstrap failed: preflight rejected token; children will not start"
            )
            return False

        try:
            from global_state import global_data
            global_data.token_value = token
        except Exception:
            logger.debug("[MAIN DATABASE] global_data.token_value set skipped", exc_info=True)

        logger.info(
            "[MAIN DATABASE] kabu token refreshed and preflight passed for data collectors token_len=%s",
            len(str(token)),
        )
        return True

    except Exception:
        logger.exception("[MAIN DATABASE] token bootstrap failed")
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

    _install_cpu_guard_env()
    _install_summary_sqlite_lock_tolerance()
    _install_summary_wal_checkpoint()
    _install_summary_stale_guard()

    try:
        from data_collectors.split_mode import mark_as_data_collector_process
        mark_as_data_collector_process()
    except Exception:
        logger.exception("[MAIN DATABASE] failed to mark data collector process")
        return 1

    if not _bootstrap_kabu_token_for_data_collectors():
        logger.error(
            "[MAIN DATABASE] abort because token bootstrap/preflight failed. "
            "Please confirm kabu Station is running, API is enabled, and API password is correct."
        )
        return 1

    try:
        from scripts.data_collectors_runner import main as data_collectors_main
    except Exception:
        logger.exception("[MAIN DATABASE] failed to import scripts.data_collectors_runner.main")
        return 1

    return int(data_collectors_main())


if __name__ == "__main__":
    raise SystemExit(main())
