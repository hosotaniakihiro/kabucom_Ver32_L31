#====================================================================================================
# scheduler_jobs/summary/runner_core.py
#====================================================================================================
# ============================================================
# File   : scheduler_jobs/summary/runner_core.py
# Version: PRODUCTION-STABLE-SUMMARY-RUNNER-CORE-V1.6-ASYNC-DISPLAY
# ------------------------------------------------------------
# 【概要】
#   PUSH / RANKING サマリーの実行本体。
#
# 【主な機能】
#   - job_summary
#   - job_ranking_summary
#   - job_1m / job_3m / job_5m
#   - job_ranking_1m / job_ranking_3m / job_ranking_5m
#   - run_push_summary_job / run_ranking_summary_job
#
# 【分離方針】
#   - 入力生成ルートは PUSH と RANKING で分離する
#   - 保存/表示/AI hook の出口パイプラインは同じ形に揃える
#   - PUSH は source="SUMMARY"
#   - RANKING は source="RANKING"
#
# REV1.5:
#   - AI entry hook を display より前に実行する
#
# REV1.6:
#   - 表示/Discord送信を非同期化する
#   - PUSH 1m/3m/5m を毎分表示対象にした場合、Discord表示が重く
#     SUMMARY PARALLEL timeout で親tickが90秒固まる問題を回避する
#   - AI entry は従来どおり表示より先に同期実行
#   - display=False の場合は従来どおり何もしない
#
# ENV:
#   SUMMARY_DISPLAY_ASYNC=1       # 既定ON
#   SUMMARY_DISPLAY_ASYNC_WORKERS=2
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import pandas as pd

from .closed_market_display import display_closed_market_push_summary
from .dependencies import (
    resolve_push_summary_runner,
    resolve_ranking_summary_runner,
)
from .display_prepare import normalize_df, latest_dt_str, clamp_future_rows
from .fallback_loader import (
    fallback_push_summary_df,
    fallback_ranking_summary_df,
    filter_push_like_rows,
)
from .output_normalizer import normalize_runner_output, log_job_result
from .quality_guards import looks_uncomputed_push_df, looks_uncomputed_ranking_df
from .runner_utils import call_runner_with_optional_now, log_df_state
from .safe_io import (
    save_summary_safe,
    display_push_summary_safe,
    display_ranking_summary_safe,
)
from .summary_ai_entry_hook_v20 import run_summary_ai_entry_safe
from .time_utils import (
    now_naive,
    floor_to_interval,
    is_market_session,
)

logger = logging.getLogger(__name__)

_DISPLAY_EXECUTOR: ThreadPoolExecutor | None = None


# ============================================================
# env helpers
# ============================================================

def _env_flag_value(name: str) -> str:
    try:
        return str(os.getenv(name, "")).strip().lower()
    except Exception:
        return ""


def _env_true(name: str) -> bool:
    raw = _env_flag_value(name)
    return raw in ("1", "true", "yes", "on", "enable", "enabled")


def _env_false(name: str) -> bool:
    raw = _env_flag_value(name)
    return raw in ("0", "false", "no", "off", "disable", "disabled")


def _env_bool(name: str, default: bool) -> bool:
    if _env_true(name):
        return True
    if _env_false(name):
        return False
    return bool(default)


def _env_int(name: str, default: int) -> int:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return int(default)
        return max(1, int(float(raw)))
    except Exception:
        return int(default)


def _display_async_enabled() -> bool:
    return _env_bool("SUMMARY_DISPLAY_ASYNC", True)


def _display_executor() -> ThreadPoolExecutor:
    global _DISPLAY_EXECUTOR
    if _DISPLAY_EXECUTOR is None:
        _DISPLAY_EXECUTOR = ThreadPoolExecutor(
            max_workers=_env_int("SUMMARY_DISPLAY_ASYNC_WORKERS", 2),
            thread_name_prefix="summary-display-async",
        )
    return _DISPLAY_EXECUTOR


# ============================================================
# save owner gate
# ============================================================

def _is_database_process_core() -> bool:
    return any(
        _env_true(name)
        for name in (
            "AUTOSTOCK_DATA_COLLECTORS_PROCESS",
            "AUTOSTOCK_SUMMARY_DB_WRITER",
            "AUTOSTOCK_MAIN_DATABASE_PROCESS",
        )
    )


def _summary_save_enabled_core() -> bool:
    mode = _env_flag_value("AUTOSTOCK_SUMMARY_SAVE_MODE")
    if mode in ("disabled", "disable", "calculate_only", "calc_only", "no_save", "skip", "off"):
        return False
    if mode in ("enabled", "enable", "save", "on"):
        return True

    owner = _env_flag_value("AUTOSTOCK_SUMMARY_SAVE_OWNER")
    if owner in ("database", "main_database", "data_collector", "data_collectors", "db"):
        return _is_database_process()
    if owner in ("main", "main.py", "realtime", "entry"):
        return not _is_database_process()
    if owner in ("both", "all", "any"):
        return True
    if owner in ("none", "off", "disabled", "no_save"):
        return False
    return True


def _main_light_tick_skip_sync_save(df: pd.DataFrame, interval: int, *, source: str) -> bool:
    """main (entry-only) では 1m summary の同期保存を飛ばす。

    旧 core/startup/summary_main_1m_light_tick_patch (現 .light_tick_1m) が
    _save_summary_if_owner をラップして実現していたものを、
    tier1/tier2 と同じ「呼び出し側から直接呼ぶ」形へ置き換えた。
    """
    try:
        from .light_tick_1m import skip_sync_save_in_main
    except Exception:
        logger.debug("[summary.runners] light tick save-skip import failed", exc_info=True)
        return False
    try:
        return bool(skip_sync_save_in_main(df, interval, source=source))
    except Exception:
        logger.exception("[summary.runners] light tick save-skip failed interval=%s", interval)
        return False


def _save_summary_if_owner(df: pd.DataFrame, interval: int, *, source: str) -> None:
    if _main_light_tick_skip_sync_save(df, interval, source=source):
        return

    if _summary_save_enabled():
        logger.info(
            "[summary.runners] DB save enabled interval=%s source=%s owner=%s db_process=%s",
            interval,
            source,
            os.getenv("AUTOSTOCK_SUMMARY_SAVE_OWNER", ""),
            _is_database_process(),
        )
        save_summary_safe(df, interval, source=source)
        return

    logger.info(
        "[summary.runners] DB save skipped interval=%s source=%s owner=%s mode=%s db_process=%s reason=summary_save_owner_gate",
        interval,
        source,
        os.getenv("AUTOSTOCK_SUMMARY_SAVE_OWNER", ""),
        os.getenv("AUTOSTOCK_SUMMARY_SAVE_MODE", ""),
        _is_database_process(),
    )


# ============================================================
# AI before display
# ============================================================

def _main_light_tick_async_ai(df: pd.DataFrame, interval: int, now: dt.datetime, run_entry: bool) -> bool:
    """main (entry-only) では PUSH AI hook を同期実行せず非同期 submit へ回す。

    旧 core/startup/summary_main_1m_light_tick_patch (現 .light_tick_1m) が
    _run_push_ai_entry_before_display を「置換」して実現していたもの
    (元実装は一切呼ばれなかった) を、tier1/tier2 と同じ形へ置き換えた。
    True を返したら同期 hook は実行しない = 旧構成と同じ。
    """
    try:
        from .light_tick_1m import handle_push_ai_entry_in_main
    except Exception:
        logger.debug("[summary.runners] light tick async-ai import failed", exc_info=True)
        return False
    try:
        return bool(handle_push_ai_entry_in_main(df, interval, now, run_entry))
    except Exception:
        logger.exception("[summary.runners] light tick async-ai failed interval=%s now=%s", interval, now)
        return False


def _run_push_ai_entry_before_display(df: pd.DataFrame, interval: int, now: dt.datetime, run_entry: bool) -> None:
    if _main_light_tick_async_ai(df, interval, now, run_entry):
        return

    if run_entry and interval in (1, 3, 5):
        logger.info("[summary.runners] push AI entry requested before display interval=%s now=%s source=SUMMARY hook=v20", interval, now)
        run_summary_ai_entry_safe(interval=interval, now=now, df=df, source="SUMMARY")
    else:
        logger.info(
            "[summary.runners] summary AI entry skipped interval=%s run_entry=%s reason=%s",
            interval,
            run_entry,
            "interval_not_enabled" if interval not in (1, 3, 5) else "run_entry_false",
        )


def _run_ranking_ai_entry_before_display(df: pd.DataFrame, interval: int, now: dt.datetime, run_entry: bool) -> None:
    if run_entry and interval in (1, 3, 5) and is_market_session(now):
        logger.info("[summary.runners] ranking AI entry requested before display interval=%s now=%s source=RANKING hook=v20", interval, now)
        run_summary_ai_entry_safe(interval=interval, now=now, df=df, source="RANKING")
    else:
        logger.info(
            "[summary.runners] ranking AI entry skipped interval=%s run_entry=%s in_session=%s reason=%s",
            interval,
            run_entry,
            is_market_session(now),
            "interval_not_enabled" if interval not in (1, 3, 5) else "run_entry_false_or_closed_market",
        )


# ============================================================
# async display helpers
# ============================================================

def _display_push_sync_or_async(df: pd.DataFrame, interval: int, now: dt.datetime, display: bool) -> None:
    if not display:
        logger.info("[summary.runners] display skipped interval=%s source=push reason=display_false", interval)
        return

    if not _display_async_enabled():
        display_push_summary_safe(df, interval, now=now)
        return

    def _task() -> None:
        try:
            logger.info("[summary.runners] async display start source=push interval=%s now=%s rows=%s", interval, now, len(df) if isinstance(df, pd.DataFrame) else 0)
            ok = display_push_summary_safe(df.copy() if isinstance(df, pd.DataFrame) else df, interval, now=now)
            logger.info("[summary.runners] async display done source=push interval=%s ok=%s", interval, ok)
        except Exception:
            logger.exception("[summary.runners] async display failed source=push interval=%s now=%s", interval, now)

    _display_executor().submit(_task)
    logger.info("[summary.runners] async display submitted source=push interval=%s now=%s", interval, now)


def _display_ranking_sync_or_async(df: pd.DataFrame, interval: int, now: dt.datetime, display: bool) -> None:
    if not display:
        logger.info("[summary.runners] display skipped interval=%s source=ranking reason=display_false", interval)
        return

    if not _display_async_enabled():
        display_ranking_summary_safe(df, interval, now=now)
        return

    def _task() -> None:
        try:
            logger.info("[summary.runners] async display start source=ranking interval=%s now=%s rows=%s", interval, now, len(df) if isinstance(df, pd.DataFrame) else 0)
            ok = display_ranking_summary_safe(df.copy() if isinstance(df, pd.DataFrame) else df, interval, now=now)
            logger.info("[summary.runners] async display done source=ranking interval=%s ok=%s", interval, ok)
        except Exception:
            logger.exception("[summary.runners] async display failed source=ranking interval=%s now=%s", interval, now)

    _display_executor().submit(_task)
    logger.info("[summary.runners] async display submitted source=ranking interval=%s now=%s", interval, now)


# ============================================================
# public shortcut jobs
# ============================================================

def job_1m(display: bool = True, now: Optional[dt.datetime] = None, run_entry: bool = True) -> pd.DataFrame:
    logger.info("[summary.runners] job_1m start display=%s now=%s run_entry=%s", display, now, run_entry)
    return job_summary(1, display=display, now=now, run_entry=run_entry)


def job_3m(display: bool = True, now: Optional[dt.datetime] = None, run_entry: bool = True) -> pd.DataFrame:
    logger.info("[summary.runners] job_3m start display=%s now=%s run_entry=%s", display, now, run_entry)
    return job_summary(3, display=display, now=now, run_entry=run_entry)


def job_5m(display: bool = True, now: Optional[dt.datetime] = None, run_entry: bool = True) -> pd.DataFrame:
    logger.info("[summary.runners] job_5m start display=%s now=%s run_entry=%s", display, now, run_entry)
    return job_summary(5, display=display, now=now, run_entry=run_entry)


def job_ranking_1m(display: bool = True, now: Optional[dt.datetime] = None, run_entry: bool = True) -> pd.DataFrame:
    logger.info("[summary.runners] job_ranking_1m start display=%s now=%s run_entry=%s", display, now, run_entry)
    return job_ranking_summary(1, display=display, now=now, run_entry=run_entry)


def job_ranking_3m(display: bool = True, now: Optional[dt.datetime] = None, run_entry: bool = True) -> pd.DataFrame:
    logger.info("[summary.runners] job_ranking_3m start display=%s now=%s run_entry=%s", display, now, run_entry)
    return job_ranking_summary(3, display=display, now=now, run_entry=run_entry)


def job_ranking_5m(display: bool = True, now: Optional[dt.datetime] = None, run_entry: bool = True) -> pd.DataFrame:
    logger.info("[summary.runners] job_ranking_5m start display=%s now=%s run_entry=%s", display, now, run_entry)
    return job_ranking_summary(5, display=display, now=now, run_entry=run_entry)


# ============================================================
# PUSH summary job
# ============================================================
#
# job_summary/job_1m/run_push_summary_job は、以前は core/startup/ の3本の
# runtime patch (summary_main_direct_push_force_patch.py
#  = 現 scheduler_jobs/summary/direct_push_1m.py /
# light_tick_1m.py / memory_latest_1m.py)
# がそれぞれ total-overwrite 方式で奪い合っていた ("3-way race")。
#
# 発見した不具合: summary_main_direct_push_force_patch.py は再適用用の
# 永続 watcher スレッド (起動後180秒間、2秒毎) を持ち、初回だけキャッシュした
# 古い "original" 参照を使い回していたため、他の2本が後から重ね書きしても
# 次の watcher tick (main.py 起動シーケンス中に確実に発生する) で古い原本を
# 土台に自分自身を再インストールし、他の2本のロジックを毎回確実に奪っていた。
# 結果、light_tick_1m.py の「本物のPUSH summary runner
# パイプラインを使う、より正確な高速経路」は事実上一度も選ばれなかった。
#
# 本文化により、tier1/tier2 として明示的に優先順位を固定した:
#   tier1: scheduler_jobs.summary.direct_push_1m.tier1_build_and_publish
#          (memory直接ビルド + タイムアウト保護 + 保存 + AI投入のhardening)
#   tier2: scheduler_jobs.summary.light_tick_1m.build_light_1m_summary
#          (本物のPUSH summary runnerパイプライン、保存/表示のみスキップ)
#   tier3: _job_summary_original_heavy (このファイルの元来の重い実装)
# いずれも interval==1 かつ各パッチ自身の実行コンテキスト判定
# (_is_main_py() 相当 / _is_entry_only_context()) を満たす場合のみ有効。


def _job_summary_tier1_direct_memory(now: dt.datetime, *, run_entry: bool) -> pd.DataFrame:
    try:
        from .direct_push_1m import (
            _is_main_py as _tier1_is_main_py,
            _env_bool as _tier1_env_bool,
            tier1_build_and_publish,
        )
    except Exception:
        logger.debug("[summary.runners] job_summary tier1 import failed", exc_info=True)
        return pd.DataFrame()
    try:
        if not (_tier1_is_main_py() and _tier1_env_bool("SUMMARY_FORCE_DIRECT_PATCH_ENABLED", True)):
            return pd.DataFrame()
        return tier1_build_and_publish(now, run_entry=run_entry)
    except Exception:
        logger.exception("[summary.runners] job_summary tier1 failed now=%s", now)
        return pd.DataFrame()


def _job_summary_tier2_light_runner(interval: int, now: dt.datetime, *, display: bool, run_entry: bool, **kwargs) -> pd.DataFrame:
    try:
        from .light_tick_1m import (
            _is_entry_only_context as _tier2_is_entry_only_context,
            _env_bool as _tier2_env_bool,
            build_light_1m_summary,
        )
    except Exception:
        logger.debug("[summary.runners] job_summary tier2 import failed", exc_info=True)
        return pd.DataFrame()
    try:
        if not (_tier2_is_entry_only_context() and _tier2_env_bool("SUMMARY_MAIN_EARLY_RETURN_AFTER_AI_SUBMIT", True)):
            return pd.DataFrame()
        return build_light_1m_summary(interval, now=now, display=display, run_entry=run_entry, **kwargs)
    except Exception:
        logger.exception("[summary.runners] job_summary tier2 failed now=%s", now)
        return pd.DataFrame()


def _stage():
    """P0-2: ステージ別 elapsed / 予算超過の協調停止。取得できなければ None。"""
    try:
        from scheduler_jobs.summary import stage_tracker as _st
        return _st
    except Exception:
        logger.debug("[summary.runners] stage_tracker import failed", exc_info=True)
        return None


# ============================================================
# P0-1: FAST_PATH_DECISION
# ------------------------------------------------------------
# 「なぜこの tick は高速経路に乗った / 外れたのか」を 1 行で分かるようにする。
# 09:46 hit / 09:47 miss の差を後から追えるようにするのが目的。
# hit 率も累積で出す。
# ============================================================

_FAST_PATH_HITS = 0
_FAST_PATH_MISSES = 0
_FAST_PATH_LOCK = threading.Lock()

_FAST_PATH_REQUIRED_COLS = ("symbol", "datetime", "close")


def fast_path_stats() -> dict:
    with _FAST_PATH_LOCK:
        total = _FAST_PATH_HITS + _FAST_PATH_MISSES
        return {
            "hits": _FAST_PATH_HITS,
            "misses": _FAST_PATH_MISSES,
            "total": total,
            "hit_rate": (_FAST_PATH_HITS / total) if total else None,
        }


def reset_fast_path_stats() -> None:
    global _FAST_PATH_HITS, _FAST_PATH_MISSES
    with _FAST_PATH_LOCK:
        _FAST_PATH_HITS = 0
        _FAST_PATH_MISSES = 0


def _log_fast_path_decision(now_i, *, hit: bool, rows: int, df, elapsed: float,
                            reject_reason: str = "", fallback_reason: str = "") -> None:
    global _FAST_PATH_HITS, _FAST_PATH_MISSES
    try:
        with _FAST_PATH_LOCK:
            if hit:
                _FAST_PATH_HITS += 1
            else:
                _FAST_PATH_MISSES += 1
            hits, misses = _FAST_PATH_HITS, _FAST_PATH_MISSES
        total = hits + misses
        rate = (hits / total) if total else 0.0

        symbols = -1
        latest = None
        age = None
        tech_ready = None
        missing = []
        if df is not None and hasattr(df, "columns") and not df.empty:
            try:
                symbols = int(df["symbol"].nunique()) if "symbol" in df.columns else -1
            except Exception:
                symbols = -1
            try:
                if "datetime" in df.columns:
                    ts = pd.to_datetime(df["datetime"], errors="coerce").max()
                    if not pd.isna(ts):
                        latest = ts
                        age = (pd.Timestamp(now_i) - pd.Timestamp(ts)).total_seconds()
            except Exception:
                logger.debug("[FAST_PATH_DECISION] latest calc failed", exc_info=True)
            try:
                if "technical_ready" in df.columns:
                    tech_ready = int(pd.to_numeric(df["technical_ready"], errors="coerce").fillna(0).astype(bool).sum())
            except Exception:
                tech_ready = None
            missing = [c for c in _FAST_PATH_REQUIRED_COLS if c not in df.columns]

        log = logger.warning if not hit else logger.info
        log(
            "[FAST_PATH_DECISION] tick=%s result=%s elapsed=%.3fs rows=%s symbols=%s "
            "freshest_latest=%s age=%s technical_ready=%s required_cols_missing=%s "
            "reject_reason=%s fallback_reason=%s hit=%d miss=%d hit_rate=%.2f",
            now_i, "HIT" if hit else "MISS", elapsed, rows, symbols,
            latest, None if age is None else round(age, 1), tech_ready, missing or "-",
            reject_reason or "-", fallback_reason or "-", hits, misses, rate,
        )
    except Exception:
        logger.debug("[FAST_PATH_DECISION] log failed", exc_info=True)


# ============================================================
# P0-2: main tick を heavy fallback で待たせない
# ------------------------------------------------------------
# 2026-08-28 の実測:
#   09:46  FAST LANE hit   rows=200 latest=09:45 tier1=2.247s  worker total=2.414s
#   09:47  FAST LANE miss  tier1=9.430s -> light_push_runner=123.077s
#          worker total=132.739s / cancel_latency=72.60s
#
# 高速経路そのものは完成している (09:46 が証拠)。
# miss の原因は判断ロジックではなく **同一プロセス内の CPU 競合**:
# 09:47 時点で ranking_summary_bg (21,764 行 / 151.4s)、
# light_push_runner (81.6s)、LONG TF 5m publish (25.3s)、
# RANKING ENTRY build x2 が同時に走り、threads=76 / rss=1.7GB。
# GIL を奪い合うので、09:46 に 2.2 秒で終わった同じ処理が
# 09:47 には数十倍の壁時計時間になる。
#
# したがって「miss したら main tick で heavy を回す」設計自体が
# 競合を増幅する。miss 時は:
#   - main tick は待たない (空を返す)
#   - 直近の fresh projection はそのまま維持される
#     (呼び出し側は空で既存 merged を潰さない)
#   - Entry は fresh が無いので fail-closed 側へ倒れる
#   - heavy rebuild が要るなら background で 1 本だけ走らせる
# とする。
# SUMMARY_MAIN_SYNC_HEAVY_FALLBACK=1 で従来動作に戻せる。
# ============================================================

_HEAVY_BG_LOCK = threading.Lock()
_HEAVY_BG_RUNNING = False
_HEAVY_BG_LAST_AT = 0.0

# P0-6: 「fresh input が絶対に無い」ことの安価な判定結果をキャッシュする
_FRESH_PROBE_LOCK = threading.Lock()
_FRESH_PROBE_AT = 0.0
_FRESH_PROBE_RESULT = ""


def _fresh_input_hopeless_max_age() -> float:
    """この秒数より古い入力しか無いなら、重い探索をしても無駄と判断する。"""
    try:
        return max(60.0, float(os.getenv("SUMMARY_FRESH_INPUT_HOPELESS_AGE_SEC", "300")))
    except Exception:
        return 300.0


def _fresh_input_hopeless(now_i) -> str:
    """fresh input が無いと安価に断定できるなら理由文字列を返す。

    PUSH owner (push_receiver_runner.py) が死ぬと push DB が止まる。
    2026-08-28 は 11:13:04 で止まり、12:15 時点で db_stale=3748.7s だった。
    その状態で毎 tick 9 秒かけて探すのは無駄なので、
    push DB の最新時刻だけを見て即 MISS にする。

    判定できないときは "" を返す (従来どおり探索する)。
    閾値は緩めない。あくまで「明らかに古い」ときだけ早期に降りる。
    """
    if not _env_bool_rc("SUMMARY_FRESH_INPUT_PROBE", True):
        return ""
    now_m = time.monotonic()
    with _FRESH_PROBE_LOCK:
        if _FRESH_PROBE_AT > 0.0 and (now_m - _FRESH_PROBE_AT) < 5.0:
            return _FRESH_PROBE_RESULT
    reason = ""
    try:
        from scheduler_jobs.summary.memory_latest_1m import _push_db_path

        path = _push_db_path()
        import sqlite3

        conn = sqlite3.connect(str(path), timeout=1.0)
        try:
            conn.execute("PRAGMA query_only=ON")
            # tick は追記専用なので rowid 最大 = 最新。
            # MAX(datetime) は datetime 単独の index が無く全表スキャンになる。
            # 実測 (543MB): MAX(datetime)=12,415ms / rowid DESC LIMIT 1=0.1ms
            row = conn.execute(
                "SELECT datetime FROM stream_data_raw ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        latest = row[0] if row else None
        if latest:
            ts = pd.to_datetime(latest, errors="coerce")
            if ts is not None and not pd.isna(ts):
                try:
                    ts = ts.tz_localize(None)
                except (TypeError, AttributeError):
                    pass
                age = (pd.Timestamp(now_i) - pd.Timestamp(ts)).total_seconds()
                limit = _fresh_input_hopeless_max_age()
                if age > limit:
                    reason = "push_db_stale age=%.0fs > %.0fs latest=%s" % (age, limit, ts)
    except Exception:
        # 判定できなければ従来どおり探索する (fail-open ではなく "分からない")
        logger.debug("[summary.runners] fresh input probe failed", exc_info=True)
        reason = ""
    with _FRESH_PROBE_LOCK:
        globals()["_FRESH_PROBE_AT"] = now_m
        globals()["_FRESH_PROBE_RESULT"] = reason
    return reason


def _env_bool_rc(name: str, default: bool) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}
    except Exception:
        return bool(default)


def _sync_heavy_fallback_allowed() -> bool:
    v = os.getenv("SUMMARY_MAIN_SYNC_HEAVY_FALLBACK")
    if v is None or str(v).strip() == "":
        return False
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}


def _heavy_bg_min_gap_sec() -> float:
    try:
        return max(30.0, float(os.getenv("SUMMARY_MAIN_HEAVY_BG_MIN_GAP_SEC", "60")))
    except Exception:
        return 60.0


def _submit_heavy_rebuild_bg(interval_i: int, now_i, display: bool, run_entry: bool, kwargs: dict) -> bool:
    """heavy rebuild を background で 1 本だけ走らせる。main tick は待たない。"""
    global _HEAVY_BG_RUNNING, _HEAVY_BG_LAST_AT

    with _HEAVY_BG_LOCK:
        if _HEAVY_BG_RUNNING:
            logger.warning(
                "[summary.runners] heavy rebuild bg skipped reason=already_running interval=%s now=%s",
                interval_i, now_i,
            )
            return False
        gap = time.monotonic() - _HEAVY_BG_LAST_AT
        if _HEAVY_BG_LAST_AT > 0.0 and gap < _heavy_bg_min_gap_sec():
            logger.warning(
                "[summary.runners] heavy rebuild bg skipped reason=min_gap interval=%s "
                "since_last=%.1fs min_gap=%.1fs",
                interval_i, gap, _heavy_bg_min_gap_sec(),
            )
            return False
        _HEAVY_BG_RUNNING = True
        _HEAVY_BG_LAST_AT = time.monotonic()

    def _task() -> None:
        global _HEAVY_BG_RUNNING
        t0 = time.perf_counter()
        try:
            logger.warning(
                "[summary.runners] heavy rebuild bg start interval=%s now=%s "
                "(main tick は待たない)", interval_i, now_i,
            )
            _job_summary_tier2_light_runner(
                interval_i, now_i, display=display, run_entry=run_entry, **kwargs
            )
        except BaseException:
            logger.exception("[summary.runners] heavy rebuild bg failed interval=%s", interval_i)
        finally:
            with _HEAVY_BG_LOCK:
                _HEAVY_BG_RUNNING = False
            logger.warning(
                "[summary.runners] heavy rebuild bg done interval=%s elapsed=%.3fs",
                interval_i, time.perf_counter() - t0,
            )

    threading.Thread(target=_task, name="summary-heavy-rebuild-bg", daemon=True).start()
    return True


def job_summary(interval: int, display: bool = True, now: Optional[dt.datetime] = None, run_entry: bool = True, **kwargs) -> pd.DataFrame:
    interval_i = int(interval)
    now_i = (now or now_naive()).replace(microsecond=0)
    _st = _stage()
    if _st is not None:
        try:
            _st.checkpoint("job_summary_begin")
        except Exception:
            logger.debug("[summary.runners] checkpoint failed", exc_info=True)

    if interval_i == 1:
        t_tier1 = time.perf_counter()

        # ------------------------------------------------------------
        # P0-6: fresh input が無いと分かっているなら 9 秒待たない。
        #
        # 2026-08-28 の実測では tier1 が MISS でも毎回 9.0〜9.4 秒
        # (build timeout 6s + fallback budget 9s) かかっていた。
        # だが PUSH owner が死んでいて push DB の最新が 56 分古い、
        # というのは **数百ミリ秒で分かる**。
        # その場合は重い探索をせず即 MISS にする。
        # 判定はあくまで「入力が古い」ことだけ。閾値は緩めない。
        # ------------------------------------------------------------
        # <between stages> 対策 (R17)
        # worker begin -> freshness check の区間が stage 化されておらず、
        # stack sampling で "<between stages>" としか出なかった。
        # probe 自体も stage にして、どこで待っているか名前で分かるようにする。
        if _st is not None:
            with _st.stage("fresh_input_probe"):
                stale_reason = _fresh_input_hopeless(now_i)
        else:
            stale_reason = _fresh_input_hopeless(now_i)
        if stale_reason:
            el_tier1 = time.perf_counter() - t_tier1
            _log_fast_path_decision(
                now_i, hit=False, rows=0, df=None, elapsed=el_tier1,
                reject_reason=stale_reason,
                fallback_reason="skipped_heavy_probe",
            )
            logger.warning(
                "[summary.runners] job_summary(PUSH) fresh input なし -> 即 MISS "
                "elapsed=%.3fs reason=%s (重い fallback 探索をしない。"
                "Entry は fresh 無しで fail-closed)",
                el_tier1, stale_reason,
            )
            return pd.DataFrame()

        if _st is not None:
            with _st.stage("tier1_direct_memory"):
                df = _job_summary_tier1_direct_memory(now_i, run_entry=run_entry)
        else:
            df = _job_summary_tier1_direct_memory(now_i, run_entry=run_entry)
        el_tier1 = time.perf_counter() - t_tier1

        if isinstance(df, pd.DataFrame) and not df.empty:
            _log_fast_path_decision(
                now_i, hit=True, rows=len(df), df=df, elapsed=el_tier1,
                reject_reason="", fallback_reason="",
            )
            logger.info("[summary.runners] job_summary(PUSH) tier1 direct-memory hit rows=%s now=%s", len(df), now_i)
            return df

        if _st is not None and _st.should_stop("after_tier1"):
            _log_fast_path_decision(now_i, hit=False, rows=0, df=None, elapsed=el_tier1,
                                    reject_reason="tier1_empty", fallback_reason="worker_stopped")
            return pd.DataFrame()

        if not _sync_heavy_fallback_allowed():
            submitted = _submit_heavy_rebuild_bg(interval_i, now_i, display, run_entry, kwargs)
            _log_fast_path_decision(
                now_i, hit=False, rows=0, df=None, elapsed=el_tier1,
                reject_reason="tier1_empty_or_timeout",
                fallback_reason="heavy_rebuild_bg_submitted" if submitted else "heavy_rebuild_bg_skipped",
            )
            logger.warning(
                "[summary.runners] job_summary(PUSH) tier1 miss -> main tick は heavy fallback を待たない "
                "(background=%s)。直近の fresh projection を維持し、Entry は fresh 無しで fail-closed。 "
                "SUMMARY_MAIN_SYNC_HEAVY_FALLBACK=1 で従来動作",
                submitted,
            )
            return pd.DataFrame()

        if _st is not None:
            with _st.stage("tier2_light_runner"):
                df = _job_summary_tier2_light_runner(interval_i, now_i, display=display, run_entry=run_entry, **kwargs)
        else:
            df = _job_summary_tier2_light_runner(interval_i, now_i, display=display, run_entry=run_entry, **kwargs)
        if isinstance(df, pd.DataFrame) and not df.empty:
            logger.info("[summary.runners] job_summary(PUSH) tier2 light-runner hit rows=%s now=%s", len(df), now_i)
            return df
        if _st is not None and _st.should_stop("after_tier2"):
            return pd.DataFrame()

    return _job_summary_original_heavy(interval_i, display=display, now=now_i, run_entry=run_entry, **kwargs)


def _job_summary_original_heavy(interval: int, display: bool = True, now: Optional[dt.datetime] = None, run_entry: bool = True, **kwargs) -> pd.DataFrame:
    interval = int(interval)
    now = (now or now_naive()).replace(microsecond=0)

    logger.info(
        "[summary.runners] job_summary(PUSH) start interval=%s display=%s run_entry=%s now=%s slot=%s in_session=%s extra_keys=%s save_enabled=%s db_process=%s display_async=%s",
        interval,
        display,
        run_entry,
        now,
        floor_to_interval(now, interval),
        is_market_session(now),
        sorted(list(kwargs.keys())),
        _summary_save_enabled(),
        _is_database_process(),
        _display_async_enabled(),
    )

    if not is_market_session(now):
        logger.info(
            "[summary.runners] market closed/lunch interval=%s now=%s slot=%s -> display latest persisted summary / fallback / rebuild if empty",
            interval,
            now,
            floor_to_interval(now, interval),
        )
        df = display_closed_market_push_summary(interval=interval, now=now)
        log_job_result("job_summary(PUSH-CLOSED)", interval, df, {})
        return df

    _st = _stage()

    runner = resolve_push_summary_runner()
    if not callable(runner):
        raise RuntimeError("push summary runner is not available")

    logger.info("[summary.runners] push runner resolved interval=%s runner=%s", interval, getattr(runner, "__name__", repr(runner)))
    if _st is not None:
        with _st.stage("push_runner"):
            result = call_runner_with_optional_now(runner, interval=interval, now=now, **kwargs)
    else:
        result = call_runner_with_optional_now(runner, interval=interval, now=now, **kwargs)

    df, meta = normalize_runner_output(result)
    log_df_state("push_after_normalize_runner_output", interval, df)

    if _st is not None:
        with _st.stage("normalize"):
            df = normalize_df(df)
    else:
        df = normalize_df(df)
    log_df_state("push_after_normalize_df", interval, df)

    before_filter_rows = len(df)
    if _st is not None:
        with _st.stage("filter_push_like"):
            df = filter_push_like_rows(df)
    else:
        df = filter_push_like_rows(df)
    logger.info("[summary.runners] filter_push_like_rows interval=%s before=%d after=%d", interval, before_filter_rows, len(df))
    log_df_state("push_after_filter_push_like_rows", interval, df)

    if not df.empty and looks_uncomputed_push_df(df):
        logger.warning("[summary.runners] runner returned uncomputed PUSH df interval=%s latest_dt=%s -> trying fallback", interval, latest_dt_str(df))
        df = pd.DataFrame()

    if df.empty:
        # DB fallback は NAS を触るので、予算が尽きていたらここで降りる。
        if _st is not None and _st.should_stop("before_db_fallback"):
            logger.warning(
                "[summary.runners] job_summary(PUSH) budget exhausted before db fallback interval=%s now=%s",
                interval, now,
            )
            return pd.DataFrame()
        logger.warning("[summary.runners] runner returned empty PUSH interval=%s -> trying push-only fallback from db/cache", interval)
        if _st is not None:
            with _st.stage("db_fallback"):
                df = fallback_push_summary_df(interval, now=now)
        else:
            df = fallback_push_summary_df(interval, now=now)
        log_df_state("push_after_fallback_push_summary_df", interval, df)

    df = normalize_df(df)
    log_df_state("push_after_second_normalize_df", interval, df)

    before_clamp_rows = len(df)
    df = clamp_future_rows(df, interval=interval, now=now)
    logger.info("[summary.runners] clamp_future_rows interval=%s source=push before=%d after=%d now=%s", interval, before_clamp_rows, len(df), now)
    log_df_state("push_after_clamp_future_rows", interval, df)

    if df.empty:
        logger.warning("[summary.runners] job_summary(PUSH) empty after runner/fallback/clamp interval=%s now=%s -> skip save/display/entry", interval, now)
        log_job_result("job_summary(PUSH-EMPTY)", interval, df, meta)
        return df

    if _st is not None:
        with _st.stage("db_write"):
            _save_summary_if_owner(df, interval, source="push")
    else:
        _save_summary_if_owner(df, interval, source="push")
    log_job_result("job_summary(PUSH)", interval, df, meta)
    # Entry dispatch は予算切れでも実行する。ここを飛ばすと発注が消えるため、
    # 降りるのは NAS I/O の手前まで。Entry 側は Entry 側の budget で守る。
    if _st is not None:
        with _st.stage("entry_dispatch"):
            _run_push_ai_entry_before_display(df, interval, now, run_entry)
    else:
        _run_push_ai_entry_before_display(df, interval, now, run_entry)
    if _st is not None:
        with _st.stage("display"):
            _display_push_sync_or_async(df, interval, now, display)
    else:
        _display_push_sync_or_async(df, interval, now, display)
    return df


# ============================================================
# RANKING summary job
# ============================================================

def job_ranking_summary(interval: int, display: bool = True, now: Optional[dt.datetime] = None, run_entry: bool = True, **kwargs) -> pd.DataFrame:
    interval = int(interval)
    now = (now or now_naive()).replace(microsecond=0)

    logger.info(
        "[summary.runners] job_ranking_summary(RANKING) start interval=%s display=%s run_entry=%s now=%s slot=%s in_session=%s extra_keys=%s save_enabled=%s db_process=%s display_async=%s",
        interval,
        display,
        run_entry,
        now,
        floor_to_interval(now, interval),
        is_market_session(now),
        sorted(list(kwargs.keys())),
        _summary_save_enabled(),
        _is_database_process(),
        _display_async_enabled(),
    )

    runner = resolve_ranking_summary_runner()
    if not callable(runner):
        raise RuntimeError("ranking summary runner is not available")

    logger.info("[summary.runners] ranking runner resolved interval=%s runner=%s", interval, getattr(runner, "__name__", repr(runner)))
    result = call_runner_with_optional_now(runner, interval=interval, now=now, **kwargs)

    df, meta = normalize_runner_output(result)
    log_df_state("ranking_after_normalize_runner_output", interval, df)

    df = normalize_df(df)
    log_df_state("ranking_after_normalize_df", interval, df)

    if not df.empty and looks_uncomputed_ranking_df(df):
        logger.warning("[summary.runners] ranking runner returned uncomputed RANKING df interval=%s latest_dt=%s -> trying fallback", interval, latest_dt_str(df))
        df = pd.DataFrame()

    if df.empty:
        logger.warning("[summary.runners] ranking runner returned empty interval=%s -> trying ranking-only fallback from cache/global_data", interval)
        df = fallback_ranking_summary_df(interval, now=now)
        log_df_state("ranking_after_fallback_ranking_summary_df", interval, df)

    df = normalize_df(df)
    log_df_state("ranking_after_second_normalize_df", interval, df)

    before_clamp_rows = len(df)
    df = clamp_future_rows(df, interval=interval, now=now)
    logger.info("[summary.runners] clamp_future_rows interval=%s source=ranking before=%d after=%d now=%s", interval, before_clamp_rows, len(df), now)
    log_df_state("ranking_after_clamp_future_rows", interval, df)

    if df.empty:
        logger.warning("[summary.runners] job_ranking_summary(RANKING) empty after runner/fallback/clamp interval=%s now=%s -> skip save/display/entry", interval, now)
        log_job_result("job_ranking_summary(RANKING-EMPTY)", interval, df, meta)
        return df

    _save_summary_if_owner(df, interval, source="ranking")
    log_job_result("job_ranking_summary(RANKING)", interval, df, meta)
    _run_ranking_ai_entry_before_display(df, interval, now, run_entry)
    _display_ranking_sync_or_async(df, interval, now, display)
    return df


# ============================================================
# compatibility aliases
# ============================================================

def run_push_summary_job(interval: int | str = 1, display: bool = True, now: Optional[dt.datetime] = None, run_entry: bool = True, **kwargs) -> pd.DataFrame:
    return job_summary(int(interval), display=display, now=now, run_entry=run_entry, **kwargs)


def run_ranking_summary_job(interval: int | str = 1, display: bool = True, now: Optional[dt.datetime] = None, run_entry: bool = True, **kwargs) -> pd.DataFrame:
    return job_ranking_summary(int(interval), display=display, now=now, run_entry=run_entry, **kwargs)


__all__ = [
    "job_1m",
    "job_3m",
    "job_5m",
    "job_summary",
    "job_ranking_1m",
    "job_ranking_3m",
    "job_ranking_5m",
    "job_ranking_summary",
    "run_push_summary_job",
    "run_ranking_summary_job",
]


# ============================================================
# DATABASE OWNER OVERRIDE
# (旧 core/startup/database_owner_runtime_patch._patch_summary_runner_core)
# ------------------------------------------------------------
# 旧パッチは install 時にこの 2 つを**完全置換**していた。
# 素の実装は *_core として残し、有効時だけ委譲する。
# ============================================================


def _dbo_owner():
    try:
        from data_collectors import database_owner as _m

        return _m if _m.owner_override_enabled() else None
    except Exception:
        return None


def _is_database_process() -> bool:
    _m = _dbo_owner()
    if _m is None:
        return _is_database_process_core()
    return _m.is_database_process_gate()


def _summary_save_enabled() -> bool:
    _m = _dbo_owner()
    if _m is None:
        return _summary_save_enabled_core()
    return _m.summary_save_enabled()
