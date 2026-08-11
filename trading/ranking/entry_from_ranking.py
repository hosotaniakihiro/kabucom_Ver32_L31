# ============================================================
# File   : trading/ranking/entry_from_ranking.py
# Ver    : RANKING-ONLY-ENTRY-v5.3.0-MIN-PENDING-ON-TIMEOUT-INLINE
# ------------------------------------------------------------
# ✔ ranking_snapshot / ranking_raw → pending 生成の唯一の入口
# ✔ ランキング表示情報と、そこから算出できる数値だけで ENTRY 判定
# ✔ Ver5.3.0:
#     - core/startup/ranking_entry_min_pending_on_timeout_patch.py の
#       budget/grace deadline ロジックを本体へインライン化。
#       スコアリング・pending追加の両ループにタイムアウト予算を設け、
#       強い候補が見つかっているのに created=0 で終わる事象を防止する。
# ✔ Ver5.2.0:
#     - latest_ranking_df が 1000件超の時、全件にテクニカル保存/attachを走らせて
#       20秒timeoutしていた問題を修正
#     - rank/price/volume/turnover/rank_typeで軽量prefilterしてから重い処理へ進む
#     - DROPログは大量出力せず、理由別集計 + sample に圧縮
#     - ENVで対象数を調整可能
# ============================================================

from __future__ import annotations

import logging
import datetime as dt
import os
import sqlite3
import sys
import time
from collections import Counter
from typing import Dict, Any, Optional, Tuple, List

import pandas as pd

from global_state import global_data
from trading.entry.pending_manager import add_pending
from trading.ranking.side_infer import infer_side_from_rank_type
from AI.entry_row_builder import build_entry_row as _build_entry_row_base
from trading.ranking import ranking_entry_high_low as _ranking_hl


def build_entry_row(row, *args, **kwargs):
    """ランキング由来のエントリー行を組み立てる。

    旧 core/startup/ranking_entry_high_low_from_snapshot_patch がこの
    モジュールの build_entry_row エイリアスを差し替えて、high/low が欠損した
    行を ranking_snapshot から補完していた。その処理を
    trading/ranking/ranking_entry_high_low.py へ移設し、ここから呼ぶ。

    AI.entry_row_builder.build_entry_row 自体は他モジュールからも使われるため
    そちらは変更せず、ランキング経路だけに補完がかかる形を保っている。
    """
    entry_row = _build_entry_row_base(row, *args, **kwargs)
    return _ranking_hl.fill_high_low_from_snapshot(entry_row, row)
from config.ranking_entry_config import RANKING_ENTRY_CONFIG, is_time_allowed

try:
    from trading.ranking.ranking_technical_store import (
        save_ranking_pseudo_technicals as _save_ranking_pseudo_technicals_core,
        attach_ranking_technicals,
    )
except Exception:  # pragma: no cover
    _save_ranking_pseudo_technicals_core = None
    attach_ranking_technicals = None

logger = logging.getLogger(__name__)

RANK_TYPE_WEIGHT = {
    "値上がり率": 1.15,
    "値下がり率": 1.15,
    "売買高上位": 0.85,
    "売買代金": 1.00,
    "売買代金上位": 1.00,
    "TICK回数": 0.90,
    "TICK回数上位": 0.90,
    "売買高急増": 1.10,
    "売買代金急増": 1.10,
}

EXCHANGE_WEIGHT = {
    "TP": 1.05,
    "TS": 1.00,
    "TG": 1.05,
    "ALL": 1.00,
}

PREFILTER_PRIORITY_TYPES = (
    "値上がり率",
    "値下がり率",
    "売買高上位",
    "売買代金",
    "売買代金上位",
    "TICK回数",
    "TICK回数上位",
    "売買高急増",
    "売買代金急増",
)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


def _env_bool(name: str, default: bool) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _now() -> dt.datetime:
    return dt.datetime.now()


def _is_ranking_pending_entry(entry: Any) -> bool:
    if not isinstance(entry, dict):
        return False
    src = str(entry.get("source") or "").strip().upper()
    et = str(entry.get("entry_type") or "").strip().upper()
    mode = str(entry.get("ranking_entry_mode") or "").strip().upper()
    return src == "RANKING" or et == "RANKING" or mode.startswith("RANKING")


def _ranking_pending_created_at(entry: Any) -> Optional[dt.datetime]:
    if not isinstance(entry, dict):
        return None
    v = entry.get("created_at") or entry.get("created") or entry.get("timestamp")
    if isinstance(v, dt.datetime):
        return v
    if isinstance(v, str) and v.strip():
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
            try:
                return dt.datetime.strptime(v.strip(), fmt)
            except Exception:
                pass
        try:
            return dt.datetime.fromisoformat(v.strip())
        except Exception:
            return None
    return None


def _cleanup_stale_ranking_pending(reason: str = "TTL") -> int:
    """古いRANKING pendingが残留してrolling-retryの枠を食い潰さないよう、
    起動時に一度だけ TTL 超過分を掃除する
    (旧 core/startup/entry_log_skip_reason_collision_patch.py)。
    """
    ttl = _env_float("RANKING_PENDING_TTL_SEC", 20.0)
    if ttl <= 0:
        return 0
    now = dt.datetime.now()
    try:
        from trading.entry.pending_manager import prune_entries, snapshot_root

        def _pred(_symbol: str, entry: Dict[str, Any]) -> bool:
            if not _is_ranking_pending_entry(entry):
                return False
            created = _ranking_pending_created_at(entry)
            if created is None:
                return False
            return (now - created).total_seconds() >= ttl

        removed = prune_entries(_pred, reason=f"RANKING_STALE:{reason}")
        if removed:
            logger.warning(
                "[RANKING PENDING STALE CLEANUP] removed=%s ttl=%.1fs reason=%s root=%s",
                removed, ttl, reason, snapshot_root(),
            )
        return int(removed or 0)
    except Exception:
        logger.exception("[RANKING PENDING STALE CLEANUP] failed reason=%s", reason)
        return 0


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        s = str(v).strip()
        if s == "" or s.lower() in ("nan", "none", "nat", "<na>"):
            return default
        s = s.replace(",", "").replace("%", "")
        return float(s)
    except Exception:
        return default


def _safe_int(v: Any, default: int = 999999) -> int:
    try:
        return int(_safe_float(v, float(default)))
    except Exception:
        return default


def _first(row: Dict[str, Any], keys: Tuple[str, ...], default: Any = None) -> Any:
    for k in keys:
        try:
            if k in row:
                v = row.get(k)
                if v is not None and str(v).strip() != "":
                    return v
        except Exception:
            pass
    return default


def _pct_change(now_price: float, prev_price: float) -> float:
    if prev_price <= 0:
        return 0.0
    return ((now_price - prev_price) / prev_price) * 100.0


def _clip(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _get_ranking_source_df_core() -> Optional[pd.DataFrame]:
    snapshot = getattr(global_data, "latest_ranking_snapshot", None)
    if isinstance(snapshot, list) and snapshot:
        logger.info("[RANKING ENTRY LOOP] source=latest_ranking_snapshot rows=%s", len(snapshot))
        return pd.DataFrame(snapshot)

    for name in ("latest_ranking_raw", "latest_ranking_df", "ranking_raw_df"):
        df = getattr(global_data, name, None)
        if isinstance(df, pd.DataFrame) and not df.empty:
            logger.info("[RANKING ENTRY LOOP] source=%s rows=%s", name, len(df))
            return df.copy()

    logger.warning("[RANKING ENTRY LOOP] ranking source dataframe not found")
    return None


# ============================================================
# ランキング入力の DB フォールバック
#   旧 core/startup/ranking_entry_source_db_fallback_patch.py (V4)
# ------------------------------------------------------------
# global_data.latest_ranking_* が空、または古いときに、当日の
# rankingYYYYMMDD.db を rowid DESC で安く読み直す。DB 自体が遅延して
# いても同日行があれば fail-open で返し、最終 entry/liquidity/board
# ガードに判断を委ねる。
#
# 旧パッチは _get_ranking_source_df を「完全置換」していた (元関数を
# 一度も呼ばない) ので、本文化でも置換のまま _rsrc_* 側へ移した。
# 元の実装は _get_ranking_source_df_core() として残してあり、
# パッチが install されないプロセス (DB / helper) ではそちらが動く。
# ============================================================
_RSRC_LAST_DB_DF: Optional[pd.DataFrame] = None
_RSRC_LAST_DB_TS: float = 0.0
_RSRC_LAST_DB_KEY: Optional[Tuple[str, str]] = None
_RSRC_ENV_APPLIED = False

# sitecustomize._is_database_process() 相当
_RSRC_DB_PROCESS_NAMES = (
    "main_database.py",
    "data_collectors_runner.py",
    "db_prepare_runner.py",
    "push_receiver_runner.py",
    "yahoo_complement_runner.py",
    "summary_database_runner.py",
    "ranking_collector_runner.py",
)
_RSRC_DB_PROCESS_ENVS = (
    "AUTOSTOCK_DATA_COLLECTORS_PROCESS",
    "AUTOSTOCK_SUMMARY_DB_WRITER",
    "AUTOSTOCK_MAIN_DATABASE_PROCESS",
    "AUTOSTOCK_RANKING_COLLECTOR_PROCESS",
)

_RSRC_DATETIME_COLS = ("datetime", "snapshot_time", "time", "created_at", "received_at", "inserted_at", "date")


def _rsrc_scope_enabled() -> bool:
    """このプロセスで DB フォールバックを使うか。

    旧パッチは sitecustomize の BACKGROUND_MAIN_PATCHES 経由で install
    されていた = main lite の background loop か
    SITECUSTOMIZE_ENABLE_FULL_NONMAIN=1 のときだけ。そのスコープと
    disabled_env ("1" 完全一致) をそのまま再現する。
    RANKING_ENTRY_SOURCE_DB_FALLBACK_SCOPE_ENABLED=1/0 で明示上書きできる。
    """
    try:
        raw = os.getenv("RANKING_ENTRY_SOURCE_DB_FALLBACK_SCOPE_ENABLED")
        if raw is not None and str(raw).strip() != "":
            return _env_bool("RANKING_ENTRY_SOURCE_DB_FALLBACK_SCOPE_ENABLED", False)
        if str(os.environ.get("DISABLE_RANKING_ENTRY_SOURCE_DB_FALLBACK_PATCH", "")).strip() == "1":
            return False
        argv_list = [str(x).replace("\\", "/").lower() for x in sys.argv]
        argv = " ".join(argv_list)
        if any(name in argv for name in _RSRC_DB_PROCESS_NAMES):
            return False
        if any(_env_bool(name, False) for name in _RSRC_DB_PROCESS_ENVS):
            return False
        if any(a.endswith("/main.py") or a == "main.py" for a in argv_list) and _env_bool("SITECUSTOMIZE_MAIN_LITE", True):
            return True
        return _env_bool("SITECUSTOMIZE_ENABLE_FULL_NONMAIN", False)
    except Exception:
        return False


def _rsrc_apply_env_defaults() -> None:
    """旧 install() の env setdefault 10 件を、有効なときだけ 1 度だけ再現する。

    これらのキーの読み手は本節と tonosama_history_failclose_strict_patch
    だけで、後者は os.environ[k]=v のハード代入なので順序に依存しない。
    """
    global _RSRC_ENV_APPLIED
    if _RSRC_ENV_APPLIED:
        return
    _RSRC_ENV_APPLIED = True
    try:
        os.environ.setdefault("RANKING_ENTRY_SOURCE_DB_FALLBACK_ENABLED", "1")
        os.environ.setdefault("RANKING_ENTRY_SOURCE_MAX_AGE_SEC", "300")
        os.environ.setdefault("RANKING_ENTRY_SOURCE_DB_MAX_AGE_SEC", "300")
        os.environ.setdefault("RANKING_ENTRY_SOURCE_DB_FAILOPEN_STALE", "1")
        os.environ.setdefault("RANKING_ENTRY_SOURCE_DB_FAILOPEN_MAX_AGE_SEC", "1800")
        os.environ.setdefault("RANKING_ENTRY_SOURCE_FAILOPEN_STALE_GLOBAL", "1")
        os.environ.setdefault("RANKING_ENTRY_SOURCE_FAILOPEN_GLOBAL_MAX_AGE_SEC", "1800")
        os.environ.setdefault("RANKING_ENTRY_SOURCE_DB_LOOKBACK_MIN", "30")
        os.environ.setdefault("RANKING_ENTRY_SOURCE_DB_MAX_ROWS", "900")
        os.environ.setdefault("RANKING_ENTRY_SOURCE_DB_SCAN_ROWS", "1500")
    except Exception:
        logger.warning("[RANKING ENTRY SOURCE DB FALLBACK] env defaults failed", exc_info=False)


def _rsrc_quote(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _rsrc_resolve_db_path() -> Optional[str]:
    try:
        from ats.ats_ranking.db_path import resolve_ranking_db_path
        p = resolve_ranking_db_path()
        if p:
            return str(p)
    except Exception as e:
        logger.debug("[RANKING ENTRY SOURCE DB FALLBACK] ats resolve failed: %s", e, exc_info=False)

    for key in ("RANKING_DB_PATH", "ATS_RANKING_DB_PATH", "KABU_RANKING_DB_PATH"):
        try:
            p = os.environ.get(key, "").strip()
            if p:
                return p
        except Exception:
            pass

    try:
        today = dt.datetime.now().strftime("%Y%m%d")
        p = rf"\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\ranking\ranking{today}.db"
        if os.path.exists(p):
            return p
    except Exception:
        pass
    return None


def _rsrc_table_columns(conn: sqlite3.Connection, table: str) -> List[str]:
    try:
        return [str(r[1]) for r in conn.execute(f"PRAGMA table_info({_rsrc_quote(table)})").fetchall()]
    except Exception:
        return []


def _rsrc_table_exists(conn: sqlite3.Connection, table: str) -> bool:
    try:
        return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1", (table,)).fetchone() is not None
    except Exception:
        return False


def _rsrc_find_col(cols: List[str], candidates: Tuple[str, ...]) -> Optional[str]:
    lower = {c.lower(): c for c in cols}
    for cand in candidates:
        if cand in cols:
            return cand
        if cand.lower() in lower:
            return lower[cand.lower()]
    return None


def _rsrc_first_datetime_col(df: pd.DataFrame) -> Optional[str]:
    for c in _RSRC_DATETIME_COLS:
        if c in df.columns:
            return c
    return None


def _rsrc_latest_info(df: pd.DataFrame) -> Tuple[Optional[dt.datetime], Optional[float], str]:
    try:
        if df is None or df.empty:
            return None, None, "empty"
        col = _rsrc_first_datetime_col(df)
        if not col:
            return None, None, "no_datetime_col"
        s = pd.to_datetime(df[col], errors="coerce").dropna()
        if s.empty:
            return None, None, f"bad_datetime_col={col}"
        latest = s.max().to_pydatetime().replace(tzinfo=None)
        age = max(0.0, (dt.datetime.now() - latest).total_seconds())
        return latest, age, col
    except Exception:
        return None, None, "latest_error"


def _rsrc_fresh_enough(df: pd.DataFrame, *, label: str) -> bool:
    latest, age, col = _rsrc_latest_info(df)
    max_age = max(30.0, _env_float("RANKING_ENTRY_SOURCE_MAX_AGE_SEC", 300.0))
    if age is not None and age <= max_age:
        logger.info("[RANKING ENTRY SOURCE DB FALLBACK] fresh source=%s rows=%s latest=%s age=%.1fs col=%s max_age=%.1fs", label, len(df), latest, age, col, max_age)
        return True
    logger.warning("[RANKING ENTRY SOURCE DB FALLBACK] stale/unknown source=%s rows=%s latest=%s age=%s col=%s max_age=%.1fs -> try DB refresh", label, 0 if df is None else len(df), latest, None if age is None else round(age, 1), col, max_age)
    return False


def _rsrc_read_global_data_source() -> pd.DataFrame:
    try:
        snapshot = getattr(global_data, "latest_ranking_snapshot", None)
        if isinstance(snapshot, list) and snapshot:
            df = pd.DataFrame(snapshot)
            if not df.empty:
                df.attrs["_ranking_source_name"] = "latest_ranking_snapshot"
                return df
    except Exception as e:
        logger.debug("[RANKING ENTRY SOURCE DB FALLBACK] direct snapshot read failed: %s", e, exc_info=False)

    for name in (
        "latest_ranking_raw",
        "latest_ranking_df",
        "ranking_raw_df",
        "ranking_snapshot_df",
        "ranking_df",
    ):
        try:
            df = getattr(global_data, name, None)
            if isinstance(df, pd.DataFrame) and not df.empty:
                out = df.copy()
                out.attrs["_ranking_source_name"] = name
                return out
        except Exception as e:
            logger.debug("[RANKING ENTRY SOURCE DB FALLBACK] direct attr read failed name=%s err=%s", name, e, exc_info=False)
    return pd.DataFrame()


def _rsrc_cache_get(db_path: str, table: str) -> pd.DataFrame:
    ttl = _env_float("RANKING_ENTRY_SOURCE_DB_CACHE_TTL_SEC", 10.0)
    if _RSRC_LAST_DB_DF is not None and _RSRC_LAST_DB_KEY == (db_path, table) and (time.time() - _RSRC_LAST_DB_TS) <= ttl:
        logger.warning("[RANKING ENTRY SOURCE DB FALLBACK] cache hit table=%s rows=%s age=%.1fs", table, len(_RSRC_LAST_DB_DF), time.time() - _RSRC_LAST_DB_TS)
        return _RSRC_LAST_DB_DF.copy()
    return pd.DataFrame()


def _rsrc_cache_put(db_path: str, table: str, df: pd.DataFrame) -> None:
    global _RSRC_LAST_DB_DF, _RSRC_LAST_DB_TS, _RSRC_LAST_DB_KEY
    try:
        if isinstance(df, pd.DataFrame) and not df.empty:
            _RSRC_LAST_DB_DF = df.copy()
            _RSRC_LAST_DB_TS = time.time()
            _RSRC_LAST_DB_KEY = (db_path, table)
    except Exception:
        pass


def _rsrc_trim_recent(df: pd.DataFrame, dt_col: Optional[str], lookback_min: int, max_rows: int) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    if dt_col and dt_col in df.columns:
        tmp = df.copy()
        tmp[dt_col] = pd.to_datetime(tmp[dt_col], errors="coerce")
        tmp = tmp.dropna(subset=[dt_col])
        if not tmp.empty:
            latest = tmp[dt_col].max()
            cutoff = latest - pd.Timedelta(minutes=max(1, lookback_min))
            recent = tmp[tmp[dt_col] >= cutoff]
            if not recent.empty:
                return recent.sort_values(dt_col, ascending=False, kind="stable").head(max_rows).copy()
            return tmp.sort_values(dt_col, ascending=False, kind="stable").head(max_rows).copy()
    return df.head(max_rows).copy()


def _rsrc_candidate_tables(conn: sqlite3.Connection) -> List[str]:
    configured = [x.strip() for x in os.environ.get("RANKING_ENTRY_SOURCE_DB_TABLES", "").split(",") if x.strip()]
    base = configured or [
        os.environ.get("RANKING_ENTRY_SOURCE_DB_TABLE", "ranking_snapshot_1min").strip() or "ranking_snapshot_1min",
        "ranking_snapshot",
        "ranking_raw_1min",
        "ranking_raw",
        "ranking_summary_1min",
        "ranking_summary_5min",
    ]
    out: List[str] = []
    for t in base:
        if t and t not in out and _rsrc_table_exists(conn, t):
            out.append(t)
    return out


def _rsrc_load_from_ranking_db() -> pd.DataFrame:
    if not _env_bool("RANKING_ENTRY_SOURCE_DB_FALLBACK_ENABLED", True):
        return pd.DataFrame()

    db_path = _rsrc_resolve_db_path()
    if not db_path or not os.path.exists(db_path):
        logger.warning("[RANKING ENTRY SOURCE DB FALLBACK] db missing path=%s", db_path)
        return pd.DataFrame()

    lookback_min = max(1, _env_int("RANKING_ENTRY_SOURCE_DB_LOOKBACK_MIN", 30))
    max_rows = max(100, _env_int("RANKING_ENTRY_SOURCE_DB_MAX_ROWS", 900))
    scan_rows = max(max_rows, _env_int("RANKING_ENTRY_SOURCE_DB_SCAN_ROWS", 1500))
    sqlite_timeout = max(0.2, _env_float("RANKING_ENTRY_SOURCE_DB_SQLITE_TIMEOUT_SEC", 1.2))
    busy_timeout_ms = max(200, int(_env_float("RANKING_ENTRY_SOURCE_DB_BUSY_TIMEOUT_MS", 900.0)))

    t0 = time.perf_counter()
    best = pd.DataFrame()
    best_table = ""
    try:
        with sqlite3.connect(str(db_path), timeout=sqlite_timeout) as conn:
            try:
                conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
                conn.execute("PRAGMA query_only=ON")
            except Exception:
                pass

            tables = _rsrc_candidate_tables(conn)
            if not tables:
                logger.warning("[RANKING ENTRY SOURCE DB FALLBACK] no candidate table db=%s", db_path)
                return pd.DataFrame()

            for table in tables:
                cached = _rsrc_cache_get(str(db_path), table)
                if isinstance(cached, pd.DataFrame) and not cached.empty:
                    return cached
                cols = _rsrc_table_columns(conn, table)
                if not cols:
                    continue
                dt_col = _rsrc_find_col(cols, _RSRC_DATETIME_COLS)
                try:
                    raw = pd.read_sql_query(f"SELECT * FROM {_rsrc_quote(table)} ORDER BY rowid DESC LIMIT ?", conn, params=(scan_rows,))
                except Exception as e:
                    logger.warning("[RANKING ENTRY SOURCE DB FALLBACK] table read failed table=%s err=%s", table, e, exc_info=False)
                    continue
                if raw is None or raw.empty:
                    continue
                df = _rsrc_trim_recent(raw, dt_col, lookback_min, max_rows)
                if df.empty:
                    continue
                latest, age, col = _rsrc_latest_info(df)
                logger.warning(
                    "[RANKING ENTRY SOURCE DB FALLBACK] table candidate db=%s table=%s raw_rows=%s rows=%s latest=%s age=%s col=%s lookback_min=%s scan_rows=%s",
                    db_path, table, len(raw), len(df), latest, None if age is None else round(age, 1), col, lookback_min, scan_rows,
                )
                if best.empty:
                    best = df
                    best_table = table
                else:
                    b_latest, _b_age, _ = _rsrc_latest_info(best)
                    if latest is not None and (b_latest is None or latest > b_latest):
                        best = df
                        best_table = table

        if best is None or best.empty:
            logger.warning("[RANKING ENTRY SOURCE DB FALLBACK] db read empty db=%s", db_path)
            return pd.DataFrame()

        latest, age, col = _rsrc_latest_info(best)
        elapsed = time.perf_counter() - t0
        max_age = max(30.0, _env_float("RANKING_ENTRY_SOURCE_DB_MAX_AGE_SEC", 300.0))
        failopen_max_age = max(max_age, _env_float("RANKING_ENTRY_SOURCE_DB_FAILOPEN_MAX_AGE_SEC", 1800.0))
        same_day = bool(latest is not None and latest.date() == dt.datetime.now().date())
        stale = bool(age is None or age > max_age)
        allow_stale = bool(stale and same_day and age is not None and age <= failopen_max_age and _env_bool("RANKING_ENTRY_SOURCE_DB_FAILOPEN_STALE", True))

        if stale and not allow_stale:
            logger.warning(
                "[RANKING ENTRY SOURCE DB FALLBACK] db latest stale and not allowed table=%s rows=%s latest=%s age=%s max_age=%.1fs failopen_max=%.1fs same_day=%s",
                best_table, len(best), latest, None if age is None else round(age, 1), max_age, failopen_max_age, same_day,
            )
            return pd.DataFrame()

        logger.warning(
            "[RANKING ENTRY SOURCE DB FALLBACK] loaded v4 db=%s table=%s rows=%s latest=%s age=%s stale=%s allow_stale=%s elapsed=%.3fs cols=%s",
            db_path, best_table, len(best), latest, None if age is None else round(age, 1), stale, allow_stale, elapsed, list(best.columns)[:20],
        )
        _rsrc_cache_put(str(db_path), best_table, best)
        return best
    except Exception as e:
        logger.warning("[RANKING ENTRY SOURCE DB FALLBACK] db load failed db=%s err=%s", db_path, e, exc_info=False)
        return pd.DataFrame()


def _rsrc_publish_to_global_data(df: pd.DataFrame, *, source: str) -> None:
    try:
        out = df.copy()
        setattr(global_data, "latest_ranking_df", out)
        setattr(global_data, "ranking_snapshot_df", out)
        try:
            setattr(global_data, "latest_ranking_snapshot", out.to_dict("records"))
        except Exception:
            pass
        logger.warning("[RANKING ENTRY SOURCE DB FALLBACK] published source=%s rows=%s latest=%s", source, len(out), _rsrc_latest_info(out)[0])
    except Exception:
        pass


def _get_ranking_source_df_with_db_fallback() -> Optional[pd.DataFrame]:
    direct = _rsrc_read_global_data_source()
    direct_name = str(getattr(direct, "attrs", {}).get("_ranking_source_name", "global_data")) if isinstance(direct, pd.DataFrame) else "global_data"

    if isinstance(direct, pd.DataFrame) and not direct.empty and _rsrc_fresh_enough(direct, label=direct_name):
        return direct

    fb = _rsrc_load_from_ranking_db()
    if isinstance(fb, pd.DataFrame) and not fb.empty:
        _rsrc_publish_to_global_data(fb, source="db_fallback_v4")
        return fb

    if isinstance(direct, pd.DataFrame) and not direct.empty and _env_bool("RANKING_ENTRY_SOURCE_FAILOPEN_STALE_GLOBAL", True):
        latest, age, col = _rsrc_latest_info(direct)
        max_age = _env_float("RANKING_ENTRY_SOURCE_FAILOPEN_GLOBAL_MAX_AGE_SEC", 1800.0)
        same_day = latest is not None and latest.date() == dt.datetime.now().date()
        if same_day and age is not None and age <= max_age:
            logger.warning(
                "[RANKING ENTRY SOURCE DB FALLBACK] fail-open stale global source=%s rows=%s latest=%s age=%.1fs col=%s max_age=%.1fs",
                direct_name, len(direct), latest, age, col, max_age,
            )
            return direct

    logger.warning("[RANKING ENTRY SOURCE DB FALLBACK] ranking source dataframe not found after direct+db fallback")
    return None


# ------------------------------------------------------------
# 入力行数の prefilter (旧 core/startup/ranking_entry_source_prefilter_patch_v2.py)
# ------------------------------------------------------------
# 旧構成では core/startup/__init__.py が _get_ranking_source_df をこの prefilter で
# 包んでいたが、その後 ranking_entry_source_db_fallback_patch が同じ属性を
# 「完全置換」していたため、main.py では prefilter は捨てられていた。
# 残っていたのは DB フォールバックが install されないプロセス
# (= 非DB かつ非main のヘルパー) だけ。その条件をそのまま再現する。
# ------------------------------------------------------------
_RSRC_PREFILTER_RANK_COLS = ("rank_position", "No", "Rank", "rank")
_RSRC_PREFILTER_PRICE_COLS = ("price", "current_price", "CurrentPrice", "close")
_RSRC_PREFILTER_SCORE_COLS = ("ranking_score_total", "score", "entry_score", "total_score", "final_score")
_RSRC_PREFILTER_VOLUME_COLS = ("volume", "trading_volume", "TradingVolume")
_RSRC_PREFILTER_TURNOVER_COLS = ("turnover", "trading_value", "TradingValue")
_RSRC_PREFILTER_SYMBOL_COLS = ("symbol", "Symbol", "code", "stock_code")


def _rsrc_prefilter_bool(name: str, default: bool = True) -> bool:
    """旧 prefilter の _b(): 真値集合が runner 側の _env_bool より狭い ({'1','true','yes','on'})。"""
    v = os.getenv(name)
    return default if v is None or str(v).strip() == "" else str(v).lower() in {"1", "true", "yes", "on"}


def _rsrc_prefilter_num(df: pd.DataFrame, names: Tuple[str, ...], default: float = 0.0) -> pd.Series:
    for n in names:
        if n in df.columns:
            return pd.to_numeric(df[n], errors="coerce").fillna(default)
    return pd.Series(default, index=df.index)


def _rsrc_prefilter_col(df: pd.DataFrame, names: Tuple[str, ...]) -> Optional[str]:
    for n in names:
        if n in df.columns:
            return n
    return None


def _rsrc_prefilter(df):
    if not isinstance(df, pd.DataFrame) or df.empty or not _rsrc_prefilter_bool("RANKING_ENTRY_SOURCE_PREFILTER_ENABLED", True):
        return df
    if len(df) <= _env_int("RANKING_ENTRY_SOURCE_PREFILTER_SKIP_UNDER_ROWS", 300):
        return df
    before = len(df)
    rank = _rsrc_prefilter_num(df, _RSRC_PREFILTER_RANK_COLS, 999999)
    price = _rsrc_prefilter_num(df, _RSRC_PREFILTER_PRICE_COLS, 0)
    score = _rsrc_prefilter_num(df, _RSRC_PREFILTER_SCORE_COLS, 0)
    vol = _rsrc_prefilter_num(df, _RSRC_PREFILTER_VOLUME_COLS, 0)
    turn = _rsrc_prefilter_num(df, _RSRC_PREFILTER_TURNOVER_COLS, 0)
    minv = _env_float("RANKING_ENTRY_PREFILTER_MIN_VOLUME", 15000)
    fixedv = vol.where(~((vol > 0) & (vol < minv)), vol * 1000.0)
    fixedt = turn.where(turn >= price * fixedv, price * fixedv)
    mask = (rank <= _env_int("RANKING_ENTRY_PREFILTER_MAX_RANK", 25)) | (score >= _env_float("RANKING_ENTRY_PREFILTER_MIN_SCORE", 55))
    mask &= (price >= _env_float("RANKING_ENTRY_PREFILTER_MIN_PRICE", 300)) & (price <= _env_float("RANKING_ENTRY_PREFILTER_MAX_PRICE", 12000))
    mask &= (fixedv >= minv) | (fixedt >= _env_float("RANKING_ENTRY_PREFILTER_MIN_TURNOVER", 5000000)) | (rank <= _env_int("RANKING_ENTRY_PREFILTER_KEEP_RANK", 20))
    out = df.loc[mask].copy()
    if out.empty:
        return df
    out["_pf_rank"] = rank.loc[out.index]
    out["_pf_score"] = score.loc[out.index]
    out = out.sort_values(["_pf_score", "_pf_rank"], ascending=[False, True])
    sc = _rsrc_prefilter_col(out, _RSRC_PREFILTER_SYMBOL_COLS)
    if sc:
        out = out.drop_duplicates(subset=[sc], keep="first")
    out = out.head(_env_int("RANKING_ENTRY_PREFILTER_MAX_ROWS", 250)).drop(columns=["_pf_rank", "_pf_score"], errors="ignore")
    logger.warning("[RANKING ENTRY PREFILTER] before=%s after=%s", before, len(out))
    return out.reset_index(drop=True)


def _rsrc_prefilter_scope_enabled() -> bool:
    """prefilter を適用するプロセスか。

    旧 prefilter は core/startup/__init__.py の
    _install_entry_runtime_init_patches() から install されていた
    (= DB コンテキストではスキップ)。さらに main.py では後から
    db_fallback に属性ごと置換されて無効化されていた。
    """
    try:
        if _rsrc_scope_enabled():
            return False
        if _env_bool("CORE_STARTUP_FORCE_FULL_INIT_PATCHES", False):
            return True
        argv = " ".join(str(x) for x in sys.argv).replace("\\", "/").lower()
        return not any(name in argv for name in _RSRC_DB_PROCESS_NAMES)
    except Exception:
        return False


def _get_ranking_source_df() -> Optional[pd.DataFrame]:
    if _rsrc_scope_enabled():
        _rsrc_apply_env_defaults()
        return _get_ranking_source_df_with_db_fallback()
    df = _get_ranking_source_df_core()
    if _rsrc_prefilter_scope_enabled():
        return _rsrc_prefilter(df)
    return df


def _normalize_rank_type(v: Any) -> str:
    s = str(v or "").strip()
    if not s:
        return ""
    s = s.replace("ランキング", "").strip()
    if "値上" in s or "上昇" in s:
        return "値上がり率"
    if "値下" in s or "下落" in s:
        return "値下がり率"
    if "売買代金" in s:
        return "売買代金"
    if "TICK" in s.upper() or "ティック" in s:
        return "TICK回数"
    if "出来高" in s or "売買高" in s:
        return "売買高上位"
    return s


def _extract_day_change_pct(row: Dict[str, Any]) -> float:
    v = _first(row, ("change_rate", "change_pct", "price_change_rate", "rate", "前日比率", "騰落率"), None)
    if v is not None:
        return _safe_float(v, 0.0)
    price = _safe_float(row.get("current_price") or row.get("price"), 0.0)
    prev_close = _safe_float(_first(row, ("prev_close", "previous_close", "base_price", "基準値", "前日終値"), 0.0), 0.0)
    if price > 0 and prev_close > 0:
        return _pct_change(price, prev_close)
    return 0.0


# ============================================================
# ランキング由来エントリーの volume / turnover 単位補正
# (旧 core/startup/ranking_entry_volume_unit_patch.py V4.5 から移設)
# ------------------------------------------------------------
# ランキングの出来高/売買代金は表示単位 (千株・百万円) のまま来ることがあり、
# そのままだと MIN_VOLUME / MIN_TURNOVER に届かず VOLUME_NG になる。
# 同一銘柄が type x market で多数出るため、同じ入力値はキャッシュして
# entry loop が重くならないようにする。
#
# 旧パッチは core/startup/__init__.py 経由でのみ install されていたため、
# DB専用子プロセスでは適用されなかった。本文化後も
# _ranking_unit_fix_enabled() で同じスコープを保つ。
# ============================================================

_UNIT_FIX_TRUE = {"1", "true", "yes", "y", "on", "enable", "enabled"}
_UNIT_FIX_FALSE = {"0", "false", "no", "n", "off", "disable", "disabled"}

_UNIT_FIX_DB_PROCESS_NAMES = (
    "main_database.py",
    "data_collectors_runner.py",
    "db_prepare_runner.py",
    "push_receiver_runner.py",
    "yahoo_complement_runner.py",
    "summary_database_runner.py",
    "ranking_collector_runner.py",
)

_UNIT_FIX_CACHE: Dict[Tuple[str, float, float, float], Dict[str, Any]] = {}
_UNIT_FIX_LOGGED_KEYS: set = set()
_UNIT_FIX_CACHE_MAX = 20000
_UNIT_FIX_LOG_FIRST_N = 0
_UNIT_FIX_LOG_COUNT = 0


def _unit_fix_env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        s = str(v).strip().lower()
        if s in _UNIT_FIX_TRUE:
            return True
        if s in _UNIT_FIX_FALSE:
            return False
    except Exception:
        pass
    return bool(default)


def _unit_fix_env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is not None and str(v).strip() != "":
            return float(v)
    except Exception:
        pass
    return float(default)


def _ranking_unit_fix_enabled() -> bool:
    """このプロセスで単位補正を適用するか。

    RANKING_ENTRY_UNIT_FIX_ENABLED=1/0 で明示的に上書きできる。
    CORE_STARTUP_FORCE_FULL_INIT_PATCHES=1 のときは DB プロセスでも適用する。
    """
    try:
        override = os.getenv("RANKING_ENTRY_UNIT_FIX_ENABLED")
        if override is not None and str(override).strip() != "":
            return _unit_fix_env_bool("RANKING_ENTRY_UNIT_FIX_ENABLED", True)
        if _unit_fix_env_bool("CORE_STARTUP_FORCE_FULL_INIT_PATCHES", False):
            return True
        import sys
        argv_text = " ".join(str(x) for x in sys.argv).replace("\\", "/").lower()
        return not any(name in argv_text for name in _UNIT_FIX_DB_PROCESS_NAMES)
    except Exception:
        return True


def _unit_fix_flag_on(v: Any) -> bool:
    try:
        return str(v).strip().lower() in _UNIT_FIX_TRUE
    except Exception:
        return False


def _unit_fix_key(symbol: Any, price: float, raw_volume: float, raw_turnover: float) -> Tuple[str, float, float, float]:
    return (
        str(symbol or "").strip(),
        round(float(price), 4),
        round(float(raw_volume), 4),
        round(float(raw_turnover), 4),
    )


def _unit_fix_should_log_once(base_key: Tuple[str, float, float, float], kind: str) -> bool:
    global _UNIT_FIX_LOG_COUNT
    if _unit_fix_env_bool("RANKING_ENTRY_UNIT_FIX_LOG_EACH", False):
        return True
    limit = int(_unit_fix_env_float("RANKING_ENTRY_UNIT_FIX_LOG_FIRST_N", _UNIT_FIX_LOG_FIRST_N))
    if limit <= 0:
        return False
    k = (base_key[0], base_key[1], base_key[2], base_key[3], kind)
    if k in _UNIT_FIX_LOGGED_KEYS:
        return False
    _UNIT_FIX_LOGGED_KEYS.add(k)
    _UNIT_FIX_LOG_COUNT += 1
    return _UNIT_FIX_LOG_COUNT <= limit


def _ranking_entry_volume_thresholds() -> Tuple[float, float]:
    """MIN_VOLUME / MIN_TURNOVER を config + env から解決する。"""
    try:
        from config.ranking_entry_config import RANKING_ENTRY_CONFIG

        vol_cfg = RANKING_ENTRY_CONFIG.get("VOLUME", {}) or {}
    except Exception:
        vol_cfg = {}
    min_volume = _unit_fix_env_float("RANKING_ENTRY_MIN_VOLUME", float(vol_cfg.get("MIN_VOLUME", 30000)))
    min_turnover = _unit_fix_env_float("RANKING_ENTRY_MIN_TURNOVER", float(vol_cfg.get("MIN_TURNOVER", 10000000)))
    return min_volume, min_turnover


def _normalize_ranking_entry_units(row: Dict[str, Any]) -> Dict[str, Any]:
    if not _unit_fix_env_bool("RANKING_ENTRY_NORMALIZE_VOLUME_UNITS", True):
        return row

    min_volume, min_turnover = _ranking_entry_volume_thresholds()

    try:
        if _unit_fix_flag_on(row.get("ranking_entry_units_finalized")):
            return row

        symbol = row.get("symbol")
        price = _safe_float(row.get("price") or row.get("current_price") or row.get("close_price") or row.get("close"), 0.0)
        raw_volume = _safe_float(row.get("volume") if row.get("volume") is not None else row.get("trading_volume"), 0.0)
        raw_turnover = _safe_float(row.get("turnover") if row.get("turnover") is not None else row.get("trading_value"), 0.0)
        base_key = _unit_fix_key(symbol, price, raw_volume, raw_turnover)

        cached = _UNIT_FIX_CACHE.get(base_key)
        if cached is not None:
            try:
                for k, v in cached.items():
                    row[k] = v
            except Exception:
                pass
            return row

        volume_multiplier = _unit_fix_env_float("RANKING_ENTRY_VOLUME_UNIT_MULTIPLIER", 1000.0)
        turnover_multiplier = _unit_fix_env_float("RANKING_ENTRY_TURNOVER_UNIT_MULTIPLIER", 1000000.0)
        implied_max_ratio = max(1.0, _unit_fix_env_float("RANKING_ENTRY_TURNOVER_IMPLIED_MAX_RATIO", 20.0))
        yen_floor = _unit_fix_env_float("RANKING_ENTRY_TURNOVER_YEN_FLOOR", 100000.0)

        volume = raw_volume
        turnover = raw_turnover
        volume_unit_fixed = False
        turnover_unit_fixed = False
        volume_unit_multiplier = 1.0
        turnover_unit_multiplier = 1.0
        zero_volume_turnover_ignored = False

        if 0.0 < raw_volume < min_volume and volume_multiplier > 1:
            candidate_volume = raw_volume * volume_multiplier
            if candidate_volume >= min_volume:
                volume = candidate_volume
                volume_unit_fixed = True
                volume_unit_multiplier = volume_multiplier

        implied_turnover = price * volume if price > 0 and volume > 0 else 0.0
        can_fix_turnover = volume > 0 and implied_turnover > 0

        if can_fix_turnover:
            if 0.0 < raw_turnover < min_turnover and raw_turnover < yen_floor and turnover_multiplier > 1:
                candidate_turnover = raw_turnover * turnover_multiplier
                upper = max(min_turnover, implied_turnover * implied_max_ratio)
                if candidate_turnover >= min_turnover and candidate_turnover <= upper:
                    turnover = candidate_turnover
                    turnover_unit_fixed = True
                    turnover_unit_multiplier = turnover_multiplier

            if turnover <= 0:
                turnover = implied_turnover
            elif turnover < min_turnover <= implied_turnover:
                turnover = implied_turnover
            elif turnover > implied_turnover * implied_max_ratio:
                if _unit_fix_should_log_once(base_key, "turnover_clamped"):
                    logger.warning(
                        "[RANKING ENTRY UNIT FIX] turnover clamped symbol=%s price=%s volume=%s turnover=%s implied=%s",
                        symbol, price, volume, turnover, implied_turnover,
                    )
                turnover = implied_turnover
        else:
            if raw_turnover > 0:
                zero_volume_turnover_ignored = True
                if _unit_fix_should_log_once(base_key, "zero_volume"):
                    logger.info(
                        "[RANKING ENTRY UNIT FIX] zero-volume turnover ignored symbol=%s price=%s raw_volume=%.3f raw_turnover=%.3f",
                        symbol, price, raw_volume, raw_turnover,
                    )
            turnover = 0.0

        updates = {
            "ranking_raw_volume": raw_volume,
            "ranking_raw_turnover": raw_turnover,
            "ranking_volume_unit_fixed": int(volume_unit_fixed),
            "ranking_turnover_unit_fixed": int(turnover_unit_fixed),
            "ranking_volume_unit_multiplier": volume_unit_multiplier,
            "ranking_turnover_unit_multiplier": turnover_unit_multiplier,
            "ranking_entry_units_finalized": 1,
            "ranking_zero_volume_turnover_ignored": int(zero_volume_turnover_ignored),
            "volume": volume,
            "trading_volume": volume,
            "turnover": turnover,
            "trading_value": turnover,
        }
        row.update(updates)

        if len(_UNIT_FIX_CACHE) >= int(_unit_fix_env_float("RANKING_ENTRY_UNIT_FIX_CACHE_MAX", _UNIT_FIX_CACHE_MAX)):
            _UNIT_FIX_CACHE.clear()
            _UNIT_FIX_LOGGED_KEYS.clear()
        _UNIT_FIX_CACHE[base_key] = dict(updates)

        changed = volume_unit_fixed or turnover_unit_fixed or raw_turnover != turnover or raw_volume != volume
        if changed and _unit_fix_should_log_once(base_key, "finalized"):
            logger.info(
                "[RANKING ENTRY UNIT FIX] finalized symbol=%s price=%s volume %.3f->%.3f turnover %.3f->%.3f "
                "vol_mul=%.0f turn_mul=%.0f implied=%.3f can_fix_turnover=%s zero_volume_ignored=%s",
                symbol, price, raw_volume, volume, raw_turnover, turnover,
                volume_unit_multiplier, turnover_unit_multiplier, implied_turnover,
                can_fix_turnover, zero_volume_turnover_ignored,
            )
    except Exception:
        logger.exception("[RANKING ENTRY UNIT FIX] normalize failed symbol=%s", row.get("symbol"))

    return row


def _normalize_ranking_row_for_entry_core(row: Dict[str, Any]) -> Dict[str, Any]:
    row = dict(row)
    symbol = str(_first(row, ("symbol", "Symbol", "code", "コード", "銘柄コード"), "")).strip()
    if symbol.endswith(".0") and symbol[:-2].isdigit():
        symbol = symbol[:-2]
    if symbol:
        row["symbol"] = symbol

    price = _safe_float(
        _first(row, ("close_price", "current_price", "CurrentPrice", "price", "close", "現在値"), 0.0),
        0.0,
    )
    volume = _safe_float(
        _first(row, ("volume", "trading_volume", "TradingVolume", "出来高", "売買高"), 0.0),
        0.0,
    )
    turnover = _safe_float(
        _first(row, ("turnover", "trading_value", "TradingValue", "売買代金", "売買代金上位", "value", "Value"), 0.0),
        0.0,
    )
    if turnover <= 0 and price > 0 and volume > 0:
        turnover = price * volume

    rank_position = _safe_int(_first(row, ("rank_position", "rank", "ranking", "順位", "Rank"), 999999), 999999)
    rank_type = _normalize_rank_type(_first(row, ("rank_type", "ranking_type", "type", "ランキング種別", "種別"), ""))
    day_change_pct = _extract_day_change_pct({**row, "current_price": price, "price": price})

    if price > 0:
        for k in ("close_price", "current_price", "price", "close"):
            row[k] = price
    if volume > 0:
        row["volume"] = volume
        row["trading_volume"] = volume
    if turnover > 0:
        row["turnover"] = turnover
        row["trading_value"] = turnover

    row["rank_position"] = rank_position
    row["rank_type"] = rank_type
    row["day_change_pct"] = day_change_pct
    if not row.get("datetime"):
        row["datetime"] = row.get("snapshot_time") or row.get("time") or row.get("created_at")

    # 旧 ranking_entry_volume_unit_patch から移設。本体の正規化の後に単位補正をかける。
    if _ranking_unit_fix_enabled():
        row = _normalize_ranking_entry_units(row)

    return row


# --- ranking_entry_runtime_fix_patch (native化, 元 core/startup/ranking_entry_runtime_fix_patch.py V1.3) ---
# ranking_technical attached symbols=0 や、出来高ランキング値が表示単位のままでMIN_VOLUMEに
# 届かずVOLUME_NGになる問題への対処。V1.3時点で残っていたのは (a) 単位補正が二重適用されない
# ようにする再補正抑止フラグと、(b) technical不足銘柄をsummary historyから補うfallbackの2点。
# (旧パッチが持っていた ranking_entry_flat_price_guard_patch 再installロジックは、
# 差し替え対象が既に entry_from_ranking.py へ直接本文化されており当時から no-op だった)
def _ranking_entry_fix_env_bool(name: str, default: bool = True) -> bool:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return bool(default)
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}


def _ranking_entry_fix_env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _ranking_entry_fix_flag_on(v: Any) -> bool:
    try:
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}
    except Exception:
        return False


def _ranking_entry_fix_looks_already_yen_turnover(turnover: float, *, min_turnover: float) -> bool:
    if turnover <= 0:
        return False
    yen_floor = _ranking_entry_fix_env_float("RANKING_ENTRY_TURNOVER_YEN_FLOOR", 100000.0)
    if turnover >= yen_floor:
        return True
    if turnover >= min_turnover:
        return True
    return False


def _normalize_ranking_row_for_entry(row: Dict[str, Any]) -> Dict[str, Any]:
    out = _normalize_ranking_row_for_entry_core(row)
    try:
        if not _ranking_entry_fix_env_bool("RANKING_ENTRY_NORMALIZE_VOLUME_UNITS", True):
            return out

        if _ranking_entry_fix_flag_on(out.get("ranking_entry_units_finalized")):
            return out

        # NOTE: _normalize_ranking_row_for_entry_core は _ranking_unit_fix_enabled()
        # (DBプロセス除外) を満たす限り常に ranking_entry_units_finalized=1 を設定して
        # 完了する (旧 ranking_entry_volume_unit_patch 由来、既に本文化済み) ため、
        # ここに到達するのは DBプロセス等でスコープ外だったケースのみ。
        # 旧パッチ自身のフォールバック補正ロジックをそのまま保持する。
        min_volume, min_turnover = _ranking_entry_volume_thresholds()
        volume_mul = _ranking_entry_fix_env_float("RANKING_ENTRY_VOLUME_UNIT_MULTIPLIER", 1000.0)
        turnover_mul = _ranking_entry_fix_env_float("RANKING_ENTRY_TURNOVER_UNIT_MULTIPLIER", 1000000.0)

        price = _safe_float(out.get("price") or out.get("current_price") or out.get("close_price"), 0.0)
        volume = _safe_float(out.get("volume") if out.get("volume") is not None else out.get("trading_volume"), 0.0)
        turnover = _safe_float(out.get("turnover") if out.get("turnover") is not None else out.get("trading_value"), 0.0)
        raw_volume = volume
        raw_turnover = turnover

        already_volume_fixed = _ranking_entry_fix_flag_on(out.get("ranking_volume_unit_fixed")) or _safe_float(out.get("ranking_volume_unit_multiplier"), 0.0) > 1
        already_turnover_fixed = _ranking_entry_fix_flag_on(out.get("ranking_turnover_unit_fixed")) or _safe_float(out.get("ranking_turnover_unit_multiplier"), 0.0) > 1

        if (not already_volume_fixed) and volume > 0 and volume < min_volume and volume_mul > 1:
            candidate_volume = volume * volume_mul
            if candidate_volume >= min_volume:
                volume = candidate_volume
                out["volume"] = volume
                out["trading_volume"] = volume
                out["ranking_volume_raw"] = raw_volume
                out["ranking_volume_unit_multiplier"] = volume_mul
                out["ranking_volume_unit_fixed"] = 1

        implied_turnover = price * volume if price > 0 and volume > 0 else 0.0

        if not already_turnover_fixed:
            if turnover > 0 and turnover < min_turnover and turnover_mul > 1:
                if (not _ranking_entry_fix_looks_already_yen_turnover(turnover, min_turnover=min_turnover)) and implied_turnover > 0:
                    candidate_turnover = turnover * turnover_mul
                    upper = implied_turnover * _ranking_entry_fix_env_float("RANKING_ENTRY_TURNOVER_IMPLIED_MAX_RATIO", 20.0)
                    if candidate_turnover >= min_turnover and candidate_turnover <= max(upper, min_turnover):
                        turnover = max(candidate_turnover, implied_turnover)
                        out["ranking_turnover_raw"] = raw_turnover
                        out["ranking_turnover_unit_multiplier"] = turnover_mul
                        out["ranking_turnover_unit_fixed"] = 1
                elif implied_turnover > turnover:
                    turnover = implied_turnover
            elif implied_turnover > turnover:
                turnover = implied_turnover

        if turnover > 0:
            out["turnover"] = turnover
            out["trading_value"] = turnover

        if raw_volume != volume or raw_turnover != turnover:
            logger.info(
                "[RANKING ENTRY FIX] normalized units symbol=%s price=%s volume %s->%s turnover %s->%s min_volume=%s min_turnover=%s already_vol=%s already_turn=%s",
                out.get("symbol"), price, raw_volume, volume, raw_turnover, turnover, min_volume, min_turnover,
                already_volume_fixed, already_turnover_fixed,
            )
    except Exception:
        logger.exception("[RANKING ENTRY FIX] normalize row wrapper failed")
    return out


def _ranking_entry_fix_get_gc_history(tf: int) -> pd.DataFrame:
    try:
        from core.global_context.context import global_context as GC
        try:
            df = GC.get_summary_history(tf, source="push")
        except TypeError:
            df = GC.get_summary_history(tf)
        if isinstance(df, pd.DataFrame):
            return df.copy()
    except Exception:
        pass
    return pd.DataFrame()


_RANKING_ENTRY_FIX_TECH_COLS = (
    "ma5", "ma25", "ma75", "rsi", "macd", "signal", "macd_hist", "hist",
    "atr", "slope", "slope_atr_scaled", "vwap", "score_buy", "score_sell",
    "score_total", "ranking_tech_score", "technical_ready", "symbol_hist_len",
)


def _ranking_entry_fix_nonzero(v: Any) -> bool:
    try:
        return abs(float(v)) > 1e-12
    except Exception:
        return False


def _ranking_entry_fix_latest_summary_tech_map(symbols: set) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    for tf in (1, 3, 5):
        df = _ranking_entry_fix_get_gc_history(tf)
        if df.empty or "symbol" not in df.columns:
            continue
        work = df.copy()
        work["symbol"] = work["symbol"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
        work = work[work["symbol"].isin(symbols)].copy()
        if work.empty:
            continue
        if "datetime" in work.columns:
            work["datetime"] = pd.to_datetime(work["datetime"], errors="coerce")
            work = work.sort_values(["symbol", "datetime"], kind="stable")
        latest = work.groupby("symbol", as_index=False).tail(1)
        for _, r in latest.iterrows():
            sym = str(r.get("symbol") or "").strip()
            if not sym or sym in result:
                continue
            tech: Dict[str, Any] = {}
            for c in _RANKING_ENTRY_FIX_TECH_COLS:
                if c in r.index:
                    tech[c] = r.get(c)
            if "hist" in tech and "macd_hist" not in tech:
                tech["macd_hist"] = tech.get("hist")
            if "ranking_tech_score" not in tech:
                tech["ranking_tech_score"] = _safe_float(tech.get("score_total"), 0.0)
            ready = any(_ranking_entry_fix_nonzero(tech.get(c)) for c in ("macd", "signal", "rsi", "slope", "slope_atr_scaled", "score_total"))
            tech["ranking_tech_ready"] = 1 if ready else 0
            tech["ranking_tech_reason"] = f"SUMMARY_HISTORY_FALLBACK_TF{tf}" if ready else f"SUMMARY_HISTORY_FALLBACK_WEAK_TF{tf}"
            tech["ranking_tech_datetime"] = r.get("datetime") if "datetime" in r.index else None
            tech["ranking_tech_db"] = "summary_history_cache"
            result[sym] = tech
    return result


def save_ranking_pseudo_technicals(rows: List[Dict[str, Any]], *args, **kwargs) -> Dict[str, Dict[str, Any]]:
    base: Dict[str, Dict[str, Any]] = {}
    try:
        if callable(_save_ranking_pseudo_technicals_core):
            base = _save_ranking_pseudo_technicals_core(rows, *args, **kwargs) or {}
    except Exception:
        logger.exception("[RANKING ENTRY FIX] original save_ranking_pseudo_technicals failed")
        base = {}

    try:
        symbols = {str(r.get("symbol") or "").strip() for r in rows or [] if str(r.get("symbol") or "").strip()}
        missing = {s for s in symbols if s not in base}
        fallback = _ranking_entry_fix_latest_summary_tech_map(missing)
        if fallback:
            merged = dict(base)
            merged.update(fallback)
            logger.warning(
                "[RANKING ENTRY FIX] technical fallback attached base=%s fallback=%s total=%s missing_symbols=%s",
                len(base), len(fallback), len(merged), len(missing),
            )
            return merged
        logger.warning(
            "[RANKING ENTRY FIX] technical fallback none base=%s symbols=%s missing=%s",
            len(base), len(symbols), len(missing),
        )
        return base
    except Exception:
        logger.exception("[RANKING ENTRY FIX] fallback tech map failed")
        return base


def _prepare_rows(ranking_df: pd.DataFrame) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for _, raw_row in ranking_df.iterrows():
        row = _normalize_ranking_row_for_entry(raw_row.to_dict())
        row["source"] = "RANKING"
        if not str(row.get("symbol") or "").strip():
            continue
        rows.append(row)
    return rows


def _get_history_store() -> Dict[str, Any]:
    store = getattr(global_data, "ranking_entry_history", None)
    if not isinstance(store, dict):
        store = {}
        setattr(global_data, "ranking_entry_history", store)
    return store


def _history_key(symbol: str, side: str) -> str:
    return f"{symbol}:{side}"


def _get_prev_history(symbol: str, side: str) -> Dict[str, Any]:
    store = _get_history_store()
    h = store.get(_history_key(symbol, side), {})
    return h if isinstance(h, dict) else {}


def _update_history(symbol: str, side: str, price: float, rank_position: int, rank_type: str, now: dt.datetime) -> Dict[str, Any]:
    store = _get_history_store()
    key = _history_key(symbol, side)
    old = store.get(key, {}) if isinstance(store.get(key, {}), dict) else {}
    prices = list(old.get("prices", []))
    prices.append(price)
    prices = prices[-10:]
    old_side_seen = bool(old)
    consecutive = int(old.get("consecutive", 0)) + 1 if old_side_seen else 1
    new_h = {
        "symbol": symbol,
        "prev_price": old.get("last_price"),
        "prev_rank_position": old.get("last_rank_position"),
        "last_price": price,
        "last_rank_position": rank_position,
        "last_rank_type": rank_type,
        "last_seen": now,
        "prices": prices,
        "consecutive": consecutive,
    }
    store[key] = new_h
    return new_h


def _reset_missing_histories(current_keys: set[str]) -> None:
    store = _get_history_store()
    for key, h in list(store.items()):
        if key not in current_keys and isinstance(h, dict):
            h["consecutive"] = 0
            store[key] = h


def _infer_side(row: Dict[str, Any]) -> str:
    rt = str(row.get("rank_type") or "")
    side = infer_side_from_rank_type(rt)
    if side in ("BUY", "SELL"):
        return side
    day_change = _safe_float(row.get("day_change_pct"), 0.0)
    return "SELL" if day_change < 0 else "BUY"


def _prefilter_rank_type_weight(rt: str) -> float:
    s = str(rt or "")
    if "値上" in s or "値下" in s:
        return 1.20
    if "売買代金" in s:
        return 1.10
    if "TICK" in s.upper() or "ティック" in s:
        return 1.00
    if "出来高" in s or "売買高" in s:
        return 0.95
    return 0.80


def _prefilter_row_priority(row: Dict[str, Any]) -> tuple:
    rank = _safe_int(row.get("rank_position") or row.get("rank"), 999999)
    turnover = _safe_float(row.get("turnover") or row.get("trading_value"), 0.0)
    volume = _safe_float(row.get("volume") or row.get("trading_volume"), 0.0)
    day_abs = abs(_safe_float(row.get("day_change_pct"), 0.0))
    rt_w = _prefilter_rank_type_weight(str(row.get("rank_type") or ""))
    return (rank, -rt_w, -turnover, -volume, -day_abs)


def _light_prefilter_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """重いtechnical保存/attach前に、明らかに対象外の行を削る。

    旧 core/startup/ranking_entry_fast_runtime_patch.py (V5) の_ultra_prefilter_rowsを
    インライン化した高速版。latest_ranking_df が1000件超の時に元の(全件ソート+全件走査の)
    実装が20秒以上timeoutしていたため、走査自体をRANKING_ENTRY_ULTRA_MAX_SOURCE_ROWS件で
    打ち切り、必要数(RANKING_ENTRY_FAST_MAX_PREFILTER_ROWS)集まった時点で止める。
    """
    if not _env_bool("RANKING_ENTRY_PREFILTER_ENABLED", True):
        return rows

    cfg_rank = RANKING_ENTRY_CONFIG["RANKING"]
    cfg_vol = RANKING_ENTRY_CONFIG["VOLUME"]
    cfg_price = RANKING_ENTRY_CONFIG["PRICE"]
    cfg_move = RANKING_ENTRY_CONFIG["PRICE_MOVE"]

    max_source = max(100, _env_int("RANKING_ENTRY_ULTRA_MAX_SOURCE_ROWS", 600))
    max_rows = max(5, _env_int("RANKING_ENTRY_FAST_MAX_PREFILTER_ROWS", 40))
    max_rank = _env_int("RANKING_ENTRY_PREFILTER_MAX_RANK", int(cfg_rank.get("MAX_RANK_POSITION", 30)))
    max_per_type = max(3, _env_int("RANKING_ENTRY_FAST_MAX_PER_TYPE", 10))
    max_per_side = max(3, _env_int("RANKING_ENTRY_FAST_MAX_PER_SIDE", 22))
    min_price = _safe_float(cfg_price.get("MIN", 300), 300)
    max_price = _safe_float(cfg_price.get("MAX", 7000), 7000)
    min_volume = _safe_float(cfg_vol.get("MIN_VOLUME", 30000), 30000)
    min_turnover = _safe_float(cfg_vol.get("MIN_TURNOVER", 10000000), 10000000)
    max_day = abs(_safe_float(cfg_move.get("MAX_DAY_CHANGE_PCT", 10.0), 10.0))
    buy_min_day = _safe_float(cfg_move.get("BUY_MIN_DAY_CHANGE_PCT", 0.0), 0.0)
    sell_max_day = _safe_float(cfg_move.get("SELL_MAX_DAY_CHANGE_PCT", 0.0), 0.0)

    ordered = sorted([dict(r) for r in rows[:max_source]], key=_prefilter_row_priority)
    kept: List[Dict[str, Any]] = []
    rejects = Counter()
    samples: List[Dict[str, Any]] = []
    per_type = Counter()
    per_side = Counter()
    seen: set[tuple[str, str, str]] = set()

    def _sample_reject(reason: str, row: Dict[str, Any]) -> None:
        rejects[reason] += 1
        if len(samples) < 10:
            samples.append({
                "symbol": row.get("symbol"),
                "rank_type": row.get("rank_type"),
                "rank": row.get("rank_position"),
                "price": row.get("price") or row.get("current_price"),
                "volume": row.get("volume"),
                "turnover": row.get("turnover"),
                "day_change_pct": row.get("day_change_pct"),
                "reason": reason,
            })

    for row in ordered:
        symbol = str(row.get("symbol") or "").strip()
        rank_type = str(row.get("rank_type") or "")
        side = _infer_side(row)
        row["side"] = side
        key = (symbol, side, rank_type)
        if not symbol:
            _sample_reject("NO_SYMBOL", row)
            continue
        if key in seen:
            _sample_reject("DUP_SYMBOL_SIDE_TYPE", row)
            continue
        seen.add(key)

        price = _safe_float(row.get("price") or row.get("current_price"), 0.0)
        volume = _safe_float(row.get("volume"), 0.0)
        turnover = _safe_float(row.get("turnover"), 0.0)
        rank = _safe_int(row.get("rank_position"), 999999)
        day = _safe_float(row.get("day_change_pct"), 0.0)

        if rank > max_rank:
            _sample_reject("PREFILTER_RANK", row)
            continue
        if price < min_price or price > max_price:
            _sample_reject("PREFILTER_PRICE", row)
            continue
        if volume < min_volume:
            _sample_reject("PREFILTER_VOLUME", row)
            continue
        if turnover < min_turnover:
            _sample_reject("PREFILTER_TURNOVER", row)
            continue
        if abs(day) > max_day:
            _sample_reject("PREFILTER_DAY_TOO_LARGE", row)
            continue
        if side == "BUY" and day <= buy_min_day:
            _sample_reject("PREFILTER_BUY_DAY", row)
            continue
        if side == "SELL" and day >= sell_max_day:
            _sample_reject("PREFILTER_SELL_DAY", row)
            continue
        if rank_type not in PREFILTER_PRIORITY_TYPES:
            _sample_reject("PREFILTER_TYPE", row)
            continue
        if per_type[rank_type] >= max_per_type:
            _sample_reject("PREFILTER_TYPE_LIMIT", row)
            continue
        if per_side[side] >= max_per_side:
            _sample_reject("PREFILTER_SIDE_LIMIT", row)
            continue

        kept.append(row)
        per_type[rank_type] += 1
        per_side[side] += 1
        if len(kept) >= max_rows:
            break

    logger.warning(
        "[RANKING ENTRY PREFILTER] before=%s scanned=%s after=%s max_rows=%s max_rank=%s per_type=%s per_side=%s rejects=%s samples=%s",
        len(rows), min(len(rows), max_source), len(kept), max_rows, max_rank, dict(per_type), dict(per_side), dict(rejects), samples,
    )
    return kept


def _ranking_technical_filter(row: Dict[str, Any], side: str) -> Tuple[bool, str, float]:
    cfg = RANKING_ENTRY_CONFIG.get("TECHNICAL", {}) or {}
    if not bool(cfg.get("ENABLED", True)):
        return True, "TECH_DISABLED", 0.0

    ready = _safe_int(row.get("ranking_tech_ready"), 0)
    if bool(cfg.get("REQUIRE_READY", False)) and ready <= 0:
        return False, f"RANKING_TECH_NOT_READY reason={row.get('ranking_tech_reason')}", 0.0

    if not bool(cfg.get("REQUIRE_DIRECTION", True)):
        return True, "TECH_DIRECTION_DISABLED", 0.0

    close = _safe_float(row.get("close") or row.get("current_price") or row.get("price"), 0.0)
    ma5 = _safe_float(row.get("ma5"), 0.0)
    ma25 = _safe_float(row.get("ma25"), 0.0)
    rsi = _safe_float(row.get("rsi"), 50.0)
    macd = _safe_float(row.get("macd"), 0.0)
    signal = _safe_float(row.get("signal"), 0.0)
    slope = _safe_float(row.get("slope"), 0.0)
    tech_score = _safe_float(row.get("ranking_tech_score") or row.get("score_total"), 0.0)
    min_slope = abs(_safe_float(cfg.get("MIN_SLOPE", 0.0001), 0.0001))

    if bool(cfg.get("REQUIRE_CLOSE_VS_MA5", True)) and close > 0 and ma5 > 0:
        if side == "BUY" and close < ma5:
            return False, f"RANKING_TECH_BUY_CLOSE_BELOW_MA5 close={close:.2f} ma5={ma5:.2f}", tech_score
        if side == "SELL" and close > ma5:
            return False, f"RANKING_TECH_SELL_CLOSE_ABOVE_MA5 close={close:.2f} ma5={ma5:.2f}", tech_score

    if bool(cfg.get("REQUIRE_MA5_MA25", True)) and ma5 > 0 and ma25 > 0:
        if side == "BUY" and ma5 < ma25:
            return False, f"RANKING_TECH_BUY_MA5_LT_MA25 ma5={ma5:.2f} ma25={ma25:.2f}", tech_score
        if side == "SELL" and ma5 > ma25:
            return False, f"RANKING_TECH_SELL_MA5_GT_MA25 ma5={ma5:.2f} ma25={ma25:.2f}", tech_score

    if bool(cfg.get("REQUIRE_SLOPE", True)):
        if side == "BUY" and slope < min_slope:
            return False, f"RANKING_TECH_BUY_SLOPE_NG slope={slope:.6f} min={min_slope:.6f}", tech_score
        if side == "SELL" and slope > -min_slope:
            return False, f"RANKING_TECH_SELL_SLOPE_NG slope={slope:.6f} min=-{min_slope:.6f}", tech_score

    if bool(cfg.get("REQUIRE_MACD_SIGNAL", False)):
        if side == "BUY" and macd < signal:
            return False, f"RANKING_TECH_BUY_MACD_NG macd={macd:.4f} signal={signal:.4f}", tech_score
        if side == "SELL" and macd > signal:
            return False, f"RANKING_TECH_SELL_MACD_NG macd={macd:.4f} signal={signal:.4f}", tech_score

    if side == "BUY" and rsi > _safe_float(cfg.get("BUY_RSI_MAX", 82.0), 82.0):
        return False, f"RANKING_TECH_BUY_RSI_TOO_HIGH rsi={rsi:.2f}", tech_score
    if side == "SELL" and rsi < _safe_float(cfg.get("SELL_RSI_MIN", 18.0), 18.0):
        return False, f"RANKING_TECH_SELL_RSI_TOO_LOW rsi={rsi:.2f}", tech_score
    return True, "OK", tech_score


def _calc_ranking_only_score(row: Dict[str, Any], side: str, prev_price: float, prev_rank: int, consecutive: int) -> Tuple[float, Dict[str, float]]:
    cfg_rank = RANKING_ENTRY_CONFIG["RANKING"]
    cfg_vol = RANKING_ENTRY_CONFIG["VOLUME"]
    cfg_tech = RANKING_ENTRY_CONFIG.get("TECHNICAL", {}) or {}
    price = _safe_float(row.get("current_price") or row.get("price"), 0.0)
    volume = _safe_float(row.get("volume"), 0.0)
    turnover = _safe_float(row.get("turnover"), 0.0)
    rank_position = _safe_int(row.get("rank_position"), 999999)
    day_change_pct = _safe_float(row.get("day_change_pct"), 0.0)
    max_rank = max(1, int(cfg_rank.get("MAX_RANK_POSITION", 30)))
    min_volume = max(1.0, _safe_float(cfg_vol.get("MIN_VOLUME", 30000), 30000))
    min_turnover = max(1.0, _safe_float(cfg_vol.get("MIN_TURNOVER", 10000000), 10000000))
    rank_score = _clip((max_rank - rank_position + 1) / max_rank, 0.0, 1.0) * 25.0
    turnover_score = _clip(turnover / (min_turnover * 5.0), 0.0, 1.0) * 20.0
    volume_score = _clip(volume / (min_volume * 5.0), 0.0, 1.0) * 15.0
    rank_improve = float(prev_rank - rank_position) if prev_rank and prev_rank < 999999 else 0.0
    rank_improve_score = _clip(rank_improve / 10.0, 0.0, 1.0) * 15.0
    step_pct = _pct_change(price, prev_price) if prev_price > 0 else 0.0
    if side == "BUY":
        price_momentum_score = _clip(step_pct / 0.7, 0.0, 1.0) * 15.0
        day_direction_score = _clip(day_change_pct / 5.0, 0.0, 1.0) * 5.0
    else:
        price_momentum_score = _clip((-step_pct) / 0.7, 0.0, 1.0) * 15.0
        day_direction_score = _clip((-day_change_pct) / 5.0, 0.0, 1.0) * 5.0
    consecutive_score = _clip((consecutive - 1) / 2.0, 0.0, 1.0) * 5.0
    rank_type_weight = RANK_TYPE_WEIGHT.get(str(row.get("rank_type") or ""), 1.0)
    market_weight = EXCHANGE_WEIGHT.get(str(row.get("market") or "ALL"), 1.0)
    tech_score_raw = _safe_float(row.get("ranking_tech_score"), 0.0)
    tech_weight = _safe_float(cfg_tech.get("SCORE_WEIGHT", 2.0), 2.0) if bool(cfg_tech.get("ENABLED", True)) else 0.0
    tech_bonus = max(0.0, tech_score_raw) * tech_weight if side == "BUY" else max(0.0, -tech_score_raw) * tech_weight
    raw_score = rank_score + turnover_score + volume_score + rank_improve_score + price_momentum_score + day_direction_score + consecutive_score + tech_bonus
    final_score = _clip(raw_score * rank_type_weight * market_weight, 0.0, 100.0)
    return final_score, {
        "rank_score": rank_score, "turnover_score": turnover_score, "volume_score": volume_score,
        "rank_improve_score": rank_improve_score, "price_momentum_score": price_momentum_score,
        "day_direction_score": day_direction_score, "consecutive_score": consecutive_score,
        "ranking_tech_score": tech_score_raw, "ranking_tech_bonus": tech_bonus,
        "rank_type_weight": rank_type_weight, "market_weight": market_weight,
        "rank_improve": rank_improve, "step_pct": step_pct,
    }


def _passes_ranking_only_filters(row: Dict[str, Any], side: str, prev_h: Dict[str, Any], score: float, parts: Dict[str, float]) -> Tuple[bool, str]:
    """ランキング単独条件のフィルタ。

    旧 core/startup/ranking_entry_flat_price_guard_patch V1.4 がこの関数を
    ラップして「volume/turnover 単位補修 + flat price 救済」を行っていた。
    その処理は trading/ranking/ranking_flat_price_guard.py へ移設済み。
    """
    from trading.ranking import ranking_flat_price_guard as _flat

    if _flat.flat_price_guard_enabled():
        return _flat.apply_flat_price_guard(
            _passes_ranking_only_filters_core, row, side, prev_h, score, parts
        )
    return _passes_ranking_only_filters_core(row, side, prev_h, score, parts)


def _passes_ranking_only_filters_core(row: Dict[str, Any], side: str, prev_h: Dict[str, Any], score: float, parts: Dict[str, float]) -> Tuple[bool, str]:
    cfg_rank = RANKING_ENTRY_CONFIG["RANKING"]
    cfg_vol = RANKING_ENTRY_CONFIG["VOLUME"]
    cfg_price = RANKING_ENTRY_CONFIG["PRICE"]
    cfg_move = RANKING_ENTRY_CONFIG["PRICE_MOVE"]
    cfg_score = RANKING_ENTRY_CONFIG["SCORE"]
    symbol = str(row.get("symbol") or "").strip()
    price = _safe_float(row.get("current_price") or row.get("price"), 0.0)
    volume = _safe_float(row.get("volume"), 0.0)
    turnover = _safe_float(row.get("turnover"), 0.0)
    rank_position = _safe_int(row.get("rank_position"), 999999)
    day_change_pct = _safe_float(row.get("day_change_pct"), 0.0)
    if not symbol:
        return False, "NO_SYMBOL"
    allow_type = cfg_rank.get("TYPE")
    if allow_type and str(allow_type) not in str(row.get("rank_type") or ""):
        return False, f"RANK_TYPE_NG rank_type={row.get('rank_type')} allow={allow_type}"
    if price < _safe_float(cfg_price.get("MIN", 300), 300) or price > _safe_float(cfg_price.get("MAX", 7000), 7000):
        return False, f"PRICE_RANGE_NG price={price}"
    if rank_position > int(cfg_rank.get("MAX_RANK_POSITION", 30)):
        return False, f"RANK_POSITION_NG rank={rank_position}"
    if volume < _safe_float(cfg_vol.get("MIN_VOLUME", 30000), 30000):
        return False, f"VOLUME_NG volume={volume}"
    if turnover < _safe_float(cfg_vol.get("MIN_TURNOVER", 10000000), 10000000):
        return False, f"TURNOVER_NG turnover={turnover}"
    prev_price = _safe_float(prev_h.get("last_price"), 0.0)
    prev_rank = _safe_int(prev_h.get("last_rank_position"), 999999)
    if bool(cfg_rank.get("REQUIRE_PREVIOUS_SNAPSHOT", True)) and prev_price <= 0:
        return False, "NO_PREVIOUS_RANKING_SNAPSHOT"
    consecutive = int(prev_h.get("consecutive", 0)) + 1 if prev_h else 1
    if consecutive < int(cfg_rank.get("MIN_CONSECUTIVE_APPEAR", 2)):
        return False, f"CONSECUTIVE_NG consecutive={consecutive}"
    if bool(cfg_rank.get("REQUIRE_RANK_NOT_WORSE", True)) and prev_rank < 999999 and rank_position > prev_rank:
        return False, f"RANK_WORSE prev={prev_rank} now={rank_position}"
    step_pct = _safe_float(parts.get("step_pct"), 0.0)
    max_step = abs(_safe_float(cfg_move.get("MAX_STEP_MOVE_PCT", 1.0), 1.0))
    if abs(step_pct) > max_step:
        return False, f"STEP_MOVE_TOO_LARGE step_pct={step_pct:.3f} max={max_step}"
    max_day = abs(_safe_float(cfg_move.get("MAX_DAY_CHANGE_PCT", 10.0), 10.0))
    if abs(day_change_pct) > max_day:
        return False, f"DAY_CHANGE_TOO_LARGE day_change_pct={day_change_pct:.3f} max={max_day}"
    if side == "BUY":
        if day_change_pct <= _safe_float(cfg_move.get("BUY_MIN_DAY_CHANGE_PCT", 0.0), 0.0):
            return False, f"BUY_DAY_DIRECTION_NG day_change_pct={day_change_pct:.3f}"
        if prev_price > 0 and price <= prev_price:
            return False, f"BUY_PRICE_NOT_UP prev={prev_price} now={price}"
    else:
        if day_change_pct >= _safe_float(cfg_move.get("SELL_MAX_DAY_CHANGE_PCT", 0.0), 0.0):
            return False, f"SELL_DAY_DIRECTION_NG day_change_pct={day_change_pct:.3f}"
        if prev_price > 0 and price >= prev_price:
            return False, f"SELL_PRICE_NOT_DOWN prev={prev_price} now={price}"
    if bool(cfg_rank.get("REQUIRE_PRICE_BREAKOUT", True)):
        prices = list(prev_h.get("prices", []))
        recent = prices[-max(1, int(cfg_rank.get("PRICE_BREAKOUT_WINDOW", 3))):]
        if recent:
            if side == "BUY" and price < max(recent):
                return False, f"BUY_NOT_RECENT_HIGH price={price} recent_high={max(recent)}"
            if side == "SELL" and price > min(recent):
                return False, f"SELL_NOT_RECENT_LOW price={price} recent_low={min(recent)}"
    tech_ok, tech_reason, _tech_score = _ranking_technical_filter(row, side)
    if not tech_ok:
        return False, tech_reason
    min_score = _safe_float(cfg_score.get("MIN_ENTRY_SCORE", 70.0), 70.0)
    if score < min_score:
        return False, f"SCORE_NG score={score:.2f} min={min_score:.2f}"
    return True, "OK"


def entry_from_ranking():
    _cleanup_stale_ranking_pending("before_ranking_entry")
    started = dt.datetime.now()
    started_perf = time.perf_counter()
    budget_sec = max(5.0, _env_float("RANKING_ENTRY_RUNTIME_BUDGET_SEC", 18.0))
    add_grace = max(0.0, min(_env_float("RANKING_ENTRY_MIN_PENDING_GRACE_SEC", 8.0), 15.0))
    deadline = started_perf + budget_sec
    add_deadline = deadline + add_grace
    max_pending = max(1, _env_int("RANKING_ENTRY_MAX_PENDING_PER_RUN", 5))
    min_force_score = _env_float("RANKING_ENTRY_MIN_PENDING_FORCE_SCORE", 70.0)

    logger.info(
        "[RANKING ENTRY LOOP] start at=%s mode=RANKING_ONLY budget_sec=%.1f add_grace=%.1f max_pending=%s",
        started.strftime("%Y-%m-%d %H:%M:%S"), budget_sec, add_grace, max_pending,
    )
    if not is_time_allowed(started):
        logger.info("[RANKING ENTRY LOOP] skip reason=TIME_GUARD now=%s", started.strftime("%H:%M:%S"))
        return 0
    ranking_df = _get_ranking_source_df()
    if ranking_df is None or ranking_df.empty:
        logger.info("[RANKING ENTRY LOOP] skip reason=no_ranking_df")
        return 0
    now = _now()
    rows_all = _prepare_rows(ranking_df)
    if not rows_all:
        logger.info("[RANKING ENTRY LOOP] skip reason=no_normalized_rows")
        return 0
    rows = _light_prefilter_rows(rows_all)
    if not rows:
        logger.info("[RANKING ENTRY LOOP] skip reason=no_prefilter_rows raw_rows=%s", len(rows_all))
        return 0

    tech_map: Dict[str, Dict[str, Any]] = {}
    cfg_tech = RANKING_ENTRY_CONFIG.get("TECHNICAL", {}) or {}
    if bool(cfg_tech.get("ENABLED", True)) and callable(save_ranking_pseudo_technicals):
        try:
            tech_map = save_ranking_pseudo_technicals(rows)
        except Exception:
            logger.exception("[RANKING ENTRY LOOP] technical attach failed -> continue without tech")
            tech_map = {}
        logger.info("[RANKING ENTRY LOOP] ranking_technical attached symbols=%s prefiltered_rows=%s raw_rows=%s", len(tech_map), len(rows), len(rows_all))

    created = 0
    build_reject = 0
    filter_reject = 0
    pending_reject = 0
    reject_samples: List[Dict[str, Any]] = []
    reject_counts = Counter()
    current_keys: set[str] = set()
    best_by_symbol_side: Dict[Tuple[str, str], Dict[str, Any]] = {}

    for row in rows:
        if time.perf_counter() >= deadline and best_by_symbol_side:
            logger.warning(
                "[RANKING ENTRY LOOP] stop scoring with candidates elapsed=%.3fs candidates=%s",
                time.perf_counter() - started_perf, len(best_by_symbol_side),
            )
            break
        if time.perf_counter() >= add_deadline:
            logger.warning(
                "[RANKING ENTRY LOOP] hard stop scoring elapsed=%.3fs candidates=%s",
                time.perf_counter() - started_perf, len(best_by_symbol_side),
            )
            break
        if callable(attach_ranking_technicals):
            row = attach_ranking_technicals(row, tech_map)
        symbol = str(row.get("symbol") or "").strip()
        if not symbol:
            continue
        side = _infer_side(row)
        row["side"] = side
        current_keys.add(_history_key(symbol, side))
        prev_h = _get_prev_history(symbol, side)
        prev_price = _safe_float(prev_h.get("last_price"), 0.0)
        prev_rank = _safe_int(prev_h.get("last_rank_position"), 999999)
        consecutive = int(prev_h.get("consecutive", 0)) + 1 if prev_h else 1
        score, parts = _calc_ranking_only_score(row, side, prev_price, prev_rank, consecutive)
        row["score"] = score
        row["score_total"] = score
        row["ranking_only_score"] = score
        row["ranking_score_parts"] = parts
        ok, reason = _passes_ranking_only_filters(row, side, prev_h, score, parts)
        _update_history(symbol, side, _safe_float(row.get("price") or row.get("current_price"), 0.0), _safe_int(row.get("rank_position"), 999999), str(row.get("rank_type") or ""), now)
        if not ok:
            filter_reject += 1
            reason_key = str(reason).split()[0].split("=")[0]
            reject_counts[reason_key] += 1
            if len(reject_samples) < 20:
                reject_samples.append({
                    "symbol": symbol, "side": side, "rank_type": row.get("rank_type"), "rank": row.get("rank_position"),
                    "price": row.get("price"), "prev_price": prev_price, "volume": row.get("volume"),
                    "turnover": row.get("turnover"), "day_change_pct": row.get("day_change_pct"),
                    "ranking_tech_score": row.get("ranking_tech_score"), "ma5": row.get("ma5"), "ma25": row.get("ma25"),
                    "rsi": row.get("rsi"), "macd": row.get("macd"), "signal": row.get("signal"),
                    "slope": row.get("slope"), "score": round(score, 2), "reason": reason,
                })
            continue
        key = (symbol, side)
        old = best_by_symbol_side.get(key)
        old_score = _safe_float(old["row"].get("score_total"), 0.0) if isinstance(old, dict) and isinstance(old.get("row"), dict) else 0.0
        if old is None or score > old_score:
            best_by_symbol_side[key] = {"row": row, "parts": parts, "prev_price": prev_price, "prev_rank": prev_rank, "consecutive": consecutive}

    _reset_missing_histories(current_keys)

    packs = list(best_by_symbol_side.items())
    packs.sort(key=lambda kv: _safe_float(kv[1]["row"].get("score_total"), 0.0), reverse=True)

    for (symbol, side), pack in packs:
        if created >= max_pending:
            break
        final_score = _safe_float(pack["row"].get("score_total"), 0.0)
        if time.perf_counter() >= deadline and created > 0:
            logger.warning(
                "[RANKING ENTRY LOOP] stop pending_add after created elapsed=%.3fs created=%s",
                time.perf_counter() - started_perf, created,
            )
            break
        if time.perf_counter() >= add_deadline and final_score < min_force_score:
            logger.warning(
                "[RANKING ENTRY LOOP] hard stop pending_add elapsed=%.3fs created=%s score=%.2f",
                time.perf_counter() - started_perf, created, final_score,
            )
            break

        row = pack["row"]
        parts = pack["parts"]
        prev_price = pack["prev_price"]
        prev_rank = pack["prev_rank"]
        consecutive = pack["consecutive"]
        entry_row = build_entry_row(row)
        if not entry_row:
            build_reject += 1
            continue
        entry_row["side"] = side
        entry_row["source"] = "RANKING"
        entry_row["symbol"] = symbol
        entry_row.setdefault("entry_type", "RANKING")
        entry_row.setdefault("interval", 1)
        entry_row["score"] = final_score
        entry_row["score_total"] = final_score
        entry_row["ranking_only_score"] = final_score
        entry_row["ranking_entry_mode"] = "RANKING_ONLY_WITH_TECH"
        entry_row["ranking_prev_price"] = prev_price
        entry_row["ranking_prev_rank"] = prev_rank
        entry_row["ranking_consecutive"] = consecutive
        entry_row["ranking_step_pct"] = parts.get("step_pct")
        entry_row["ranking_rank_improve"] = parts.get("rank_improve")
        entry_row["ranking_score_parts"] = parts
        entry_row["ranking_min_pending_timeout_rescue"] = bool(time.perf_counter() >= deadline)
        for k in ("ma5", "ma25", "ma75", "rsi", "macd", "signal", "macd_hist", "atr", "slope", "slope_atr_scaled", "vwap", "ranking_tech_score", "ranking_tech_ready", "ranking_tech_reason", "ranking_tech_datetime", "ranking_tech_db"):
            if k in row:
                entry_row[k] = row.get(k)
        pending_entry = {
            **entry_row,
            "source": "RANKING",
            "created_at": now,
            "ranking_fallback_used": False,
            "ranking_strength": final_score,
            "technical_score": _safe_float(row.get("ranking_tech_score"), 0.0),
            "snapshot_score": final_score,
        }
        if add_pending(pending_entry):
            created += 1
            logger.info(
                "[RANKING PENDING ADD] mode=RANKING_ONLY_WITH_TECH symbol=%s side=%s rank_type=%s rank=%s price=%.2f prev_price=%.2f step=%.3f%% day=%.3f%% volume=%.0f turnover=%.0f consecutive=%s rank_improve=%.1f score=%.2f tech=%.2f ma5=%.2f ma25=%.2f rsi=%.2f slope=%.6f timeout_rescue=%s",
                symbol, side, row.get("rank_type"), row.get("rank_position"),
                _safe_float(row.get("price") or row.get("current_price"), 0.0), prev_price,
                _safe_float(parts.get("step_pct"), 0.0), _safe_float(row.get("day_change_pct"), 0.0),
                _safe_float(row.get("volume"), 0.0), _safe_float(row.get("turnover"), 0.0), consecutive,
                _safe_float(parts.get("rank_improve"), 0.0), final_score, _safe_float(row.get("ranking_tech_score"), 0.0),
                _safe_float(row.get("ma5"), 0.0), _safe_float(row.get("ma25"), 0.0), _safe_float(row.get("rsi"), 0.0), _safe_float(row.get("slope"), 0.0),
                entry_row.get("ranking_min_pending_timeout_rescue"),
            )
        else:
            pending_reject += 1

    elapsed = time.perf_counter() - started_perf
    if reject_samples:
        logger.warning("[RANKING ENTRY LOOP] ranking_only_reject_counts=%s samples=%s", dict(reject_counts), reject_samples)
    logger.info(
        "[RANKING ENTRY LOOP] done mode=RANKING_ONLY_WITH_TECH created=%s raw_total=%s prefiltered=%s candidates=%s filter_reject=%s build_reject=%s pending_reject=%s budget_sec=%.1f add_grace=%.1f elapsed=%.3fs",
        created, len(rows_all), len(rows), len(best_by_symbol_side), filter_reject, build_reject, pending_reject, budget_sec, add_grace, elapsed,
    )
    return created


def run_ranking_entry_pipeline():
    return entry_from_ranking()
