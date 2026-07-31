# -*- coding: utf-8 -*-
# ============================================================
# File   : core/startup/summary_main_direct_push_force_patch.py
# Version: V10-JOB-SUMMARY-WRAP-REMOVED
# ------------------------------------------------------------
# main.py の1分PUSH summary tickを軽量経路に固定する。
#
# V10:
#   - job_summary/run_push_summary_job/job_1m への再代入 + 180秒間2秒毎に
#     再適用し続ける watcher スレッドを撤去した。
#
#   発見した不具合: _patch_once() は _ORIGINAL_JOB_SUMMARY をモジュール変数として
#   初回成功時に1度だけキャッシュし、以降の watcher 呼び出しではそのまま
#   古いキャッシュを使い回していた。このパッチは sitecustomize.py の
#   SYNC_MAIN_PATCHES で最も早く (main.py 起動前に) install されるため、
#   summary_main_1m_light_tick_patch / summary_main_memory_latest_1m_patch が
#   後から重ね書きしても、次の watcher tick (最大2秒後、確実に main.py の
#   起動シーケンス中に発生する) で「まだ自分の目印が付いていない」と判定し、
#   古いキャッシュ原本を土台に自分自身を re-install していた。結果、
#   後続2パッチの job_summary ロジックは毎回確実に奪われ、
#   summary_main_1m_light_tick_patch の「本物のrunnerパイプラインを使う、
#   より正確な高速経路」は事実上一度も選ばれなかった。
#
#   本文化にあたり、_build_direct / _store / _submit_ai 等はライブラリ関数として
#   維持し、scheduler_jobs/summary/runner_core.py の job_summary から
#   tier1 (memory直接+このファイルのhardening) として直接呼ぶ形にした。
#   tier2 として summary_main_1m_light_tick_patch.build_light_1m_summary
#   (本物のrunnerパイプライン) を明示的に重ね、tier1が空/失敗の場合だけ
#   フォールバックするようにして、3本の奪い合いを解消した。
#
# V9:
#   - V8 fallback が global_data.summary_1m_df をPUSHメモリとして拾い、
#     11:02に10:59の古いsummaryを再利用してAIを回していた。
#   - fallback対象を raw/current PUSH系フレームだけに限定する。
#   - robust/process memory の出力も latest_dt freshness で検証し、古い1分足は
#     AIへ渡さない。
#   - timeout/empty/stale時は raw count / original fallback を呼ばず即returnする。
#   - ガード/発注条件は緩和しない。
# ============================================================
from __future__ import annotations

import datetime as dt
import logging
import os
import sys
import threading
import time
from typing import Any, Optional

import pandas as pd

logger = logging.getLogger(__name__)
VERSION = "V10-JOB-SUMMARY-WRAP-REMOVED"
_PATCHED = False
_AI_LOCK = threading.RLock()
_AI_RUNNING: dict[str, float] = {}

_TRUE = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
_FALSE = {"0", "false", "no", "n", "off", "ng", "disable", "disabled"}


def _env_bool(name: str, default: bool = False) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        s = str(v).strip().lower()
        if s in _TRUE:
            return True
        if s in _FALSE:
            return False
    except Exception:
        pass
    return bool(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        return int(default) if v is None or str(v).strip() == "" else int(float(v))
    except Exception:
        return int(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        return float(default) if v is None or str(v).strip() == "" else float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _is_main_py() -> bool:
    try:
        argv = " ".join(str(x).replace("\\", "/").lower() for x in sys.argv)
        if any(x in argv for x in ("main_database.py", "data_collectors_runner.py", "summary_database_runner.py", "push_receiver_runner.py")):
            return False
        return "main.py" in argv
    except Exception:
        return False


def _dt_key(now: Any) -> str:
    return now.strftime("%Y%m%d%H%M%S") if isinstance(now, dt.datetime) else str(now)


def _latest_dt(df: Any) -> pd.Timestamp | None:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty:
            return None
        for c in ("datetime", "received_at", "event_time", "time", "timestamp", "created_at", "updated_at", "inserted_at"):
            if c in df.columns:
                s = pd.to_datetime(df[c], errors="coerce")
                try:
                    s = s.dt.tz_localize(None)
                except Exception:
                    pass
                mx = s.max()
                if pd.notna(mx):
                    ts = pd.Timestamp(mx)
                    return ts.tz_localize(None) if getattr(ts, "tzinfo", None) else ts
    except Exception:
        pass
    return None


def _is_fresh_df(df: Any, now: dt.datetime, *, label: str) -> bool:
    latest = _latest_dt(df)
    if latest is None:
        return False
    max_age = max(30.0, _env_float("SUMMARY_FORCE_DIRECT_MAX_OUTPUT_AGE_SEC", 90.0))
    age = (pd.Timestamp(now) - latest).total_seconds()
    if age > max_age:
        try:
            logger.warning(
                "[SUMMARY FORCE DIRECT 1M] reject stale frame label=%s latest_dt=%s age=%.1fs max_age=%.1fs rows=%s version=%s",
                label, latest, age, max_age, len(df) if isinstance(df, pd.DataFrame) else None, VERSION,
            )
        except Exception:
            pass
        return False
    return True


def _candidate_push_frames() -> list[tuple[str, pd.DataFrame]]:
    frames: list[tuple[str, pd.DataFrame]] = []
    # V9: summary_1m_df / push_summary_1m_df は完成済みsummaryなので除外する。
    # fallbackでは、現在受信中のraw PUSH/streamフレームだけを見る。
    attr_names = (
        "push_df", "push_memory_df", "push_stream_df", "stream_data_df", "stream_data_raw",
        "raw_push_df", "push_history_df", "push_data_df", "latest_push_df", "realtime_push_df",
    )
    targets: list[tuple[str, Any]] = []
    try:
        from global_state import global_data
        targets.append(("global_data", global_data))
    except Exception:
        pass
    try:
        from core.global_context.context import global_context
        targets.append(("global_context", global_context))
    except Exception:
        pass

    seen: set[int] = set()
    for prefix, obj in targets:
        if obj is None:
            continue
        for name in attr_names:
            try:
                v = getattr(obj, name, None)
            except Exception:
                continue
            if isinstance(v, pd.DataFrame) and not v.empty and id(v) not in seen:
                seen.add(id(v))
                frames.append((f"{prefix}.{name}", v))
        # summary getters are intentionally excluded to avoid stale self-feeding.
        for meth in ("get_push_df", "get_push_memory_df", "get_stream_data_df"):
            fn = getattr(obj, meth, None)
            if not callable(fn):
                continue
            for call in (lambda fn=fn: fn(), lambda fn=fn: fn(1)):
                try:
                    v = call()
                    if isinstance(v, pd.DataFrame) and not v.empty and id(v) not in seen:
                        seen.add(id(v))
                        frames.append((f"{prefix}.{meth}", v))
                        break
                except TypeError:
                    continue
                except Exception:
                    break
    return frames


def _normalize_push_ticks(df: pd.DataFrame, now: dt.datetime, *, source_name: str) -> pd.DataFrame:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty:
            return pd.DataFrame()
        x = df.copy(deep=False)
        x = x.loc[:, ~pd.Index(x.columns).duplicated()].copy()
        sym_col = next((c for c in ("symbol", "Symbol", "code", "銘柄コード", "stock_code") if c in x.columns), None)
        price_col = next((c for c in ("price", "current_price", "CurrentPrice", "現在値", "close", "close_price") if c in x.columns), None)
        dt_col = next((c for c in ("datetime", "received_at", "event_time", "time", "timestamp", "created_at", "updated_at", "inserted_at") if c in x.columns), None)
        vol_col = next((c for c in ("volume", "trading_volume", "vol", "Volume", "出来高") if c in x.columns), None)
        if sym_col is None or price_col is None or dt_col is None:
            return pd.DataFrame()
        out = pd.DataFrame()
        out["symbol"] = x[sym_col].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
        out["price"] = pd.to_numeric(x[price_col], errors="coerce")
        out["datetime"] = pd.to_datetime(x[dt_col], errors="coerce")
        try:
            out["datetime"] = out["datetime"].dt.tz_localize(None)
        except Exception:
            pass
        if vol_col is not None:
            out["volume"] = pd.to_numeric(x[vol_col], errors="coerce").fillna(0.0)
        else:
            out["volume"] = 0.0
        out = out.dropna(subset=["symbol", "price", "datetime"])
        out = out[(out["symbol"] != "") & (out["price"] > 0)]
        if out.empty:
            return pd.DataFrame()
        latest = out["datetime"].max()
        max_age = max(30, _env_int("SUMMARY_FORCE_DIRECT_MEMORY_LOOKBACK_SEC", 90))
        age = (pd.Timestamp(now) - pd.Timestamp(latest)).total_seconds()
        if age > max_age:
            logger.warning(
                "[SUMMARY FORCE DIRECT 1M] reject stale push memory source=%s latest=%s age=%.1fs max_age=%ss rows=%s version=%s",
                source_name, latest, age, max_age, len(out), VERSION,
            )
            return pd.DataFrame()
        min_dt = pd.Timestamp(now) - pd.Timedelta(seconds=max_age)
        out = out[out["datetime"] >= min_dt]
        if out.empty:
            return pd.DataFrame()
        out["minute"] = out["datetime"].dt.floor("min")
        return out.reset_index(drop=True)
    except Exception:
        logger.debug("[SUMMARY FORCE DIRECT 1M] normalize push ticks failed source=%s", source_name, exc_info=True)
        return pd.DataFrame()


def _build_from_process_memory(now: dt.datetime) -> pd.DataFrame:
    t0 = time.perf_counter()
    best_name = ""
    best_ticks = pd.DataFrame()
    try:
        for name, df in _candidate_push_frames():
            ticks = _normalize_push_ticks(df, now, source_name=name)
            if len(ticks) > len(best_ticks):
                best_name = name
                best_ticks = ticks
        if best_ticks.empty:
            return pd.DataFrame()
        rows = []
        for (sym, minute), g in best_ticks.sort_values("datetime").groupby(["symbol", "minute"], sort=False):
            prices = pd.to_numeric(g["price"], errors="coerce").dropna()
            if prices.empty:
                continue
            vol = float(pd.to_numeric(g.get("volume", 0.0), errors="coerce").fillna(0.0).sum()) if "volume" in g.columns else 0.0
            close = float(prices.iloc[-1])
            rows.append({
                "symbol": str(sym),
                "datetime": pd.Timestamp(minute).to_pydatetime().replace(tzinfo=None),
                "interval": 1,
                "open": float(prices.iloc[0]),
                "high": float(prices.max()),
                "low": float(prices.min()),
                "close": close,
                "price": close,
                "current_price": close,
                "close_price": close,
                "volume": vol,
                "turnover": vol * close if vol > 0 else 0.0,
                "source": "SUMMARY",
            })
        df = pd.DataFrame(rows)
        if df.empty:
            return pd.DataFrame()
        latest = df.sort_values(["symbol", "datetime"]).groupby("symbol", as_index=False).tail(1).reset_index(drop=True)
        if not _is_fresh_df(latest, now, label=f"process_memory:{best_name}"):
            return pd.DataFrame()
        logger.warning(
            "[SUMMARY FORCE DIRECT 1M] built via fresh raw push memory fallback source=%s tick_rows=%s summary_rows=%s latest_dt=%s elapsed=%.3fs version=%s",
            best_name, len(best_ticks), len(latest), latest["datetime"].max() if "datetime" in latest.columns else None,
            time.perf_counter() - t0, VERSION,
        )
        return latest
    except Exception:
        logger.debug("[SUMMARY FORCE DIRECT 1M] process memory fallback failed", exc_info=True)
        return pd.DataFrame()


def _build_direct_inner(now: dt.datetime) -> pd.DataFrame:
    try:
        from core.startup.summary_main_memory_latest_1m_patch import _build_memory_1m_summary
        df = _build_memory_1m_summary(now=now)
        if isinstance(df, pd.DataFrame) and not df.empty:
            return df.reset_index(drop=True)
    except Exception:
        logger.debug("[SUMMARY FORCE DIRECT 1M] robust memory builder failed", exc_info=True)
    return pd.DataFrame()


def _build_direct(now: dt.datetime) -> pd.DataFrame:
    t0 = time.perf_counter()
    timeout = max(0.0, _env_float("SUMMARY_FORCE_DIRECT_BUILD_TIMEOUT_SEC", 6.0))
    df = pd.DataFrame()
    if timeout <= 0:
        df = _build_direct_inner(now)
    else:
        box: dict[str, Any] = {"df": pd.DataFrame()}

        def _target() -> None:
            box["df"] = _build_direct_inner(now)

        th = threading.Thread(target=_target, name="summary-force-direct-build", daemon=True)
        th.start()
        th.join(timeout)
        if th.is_alive():
            logger.error(
                "[SUMMARY FORCE DIRECT 1M] build timeout timeout=%.3fs elapsed=%.3fs now=%s version=%s note=skip_raw_count_and_try_fresh_raw_push_memory",
                timeout, time.perf_counter() - t0, now, VERSION,
            )
            df = pd.DataFrame()
        else:
            df = box.get("df")
    if isinstance(df, pd.DataFrame) and not df.empty and not _is_fresh_df(df, now, label="robust_builder"):
        df = pd.DataFrame()
    if not isinstance(df, pd.DataFrame) or df.empty:
        df = _build_from_process_memory(now)
    if isinstance(df, pd.DataFrame) and not df.empty:
        logger.warning(
            "[SUMMARY FORCE DIRECT 1M] built via fresh robust/process memory rows=%s symbols=%s latest_dt=%s elapsed=%.3fs timeout=%.3fs version=%s",
            len(df), int(df["symbol"].nunique()) if "symbol" in df.columns else 0,
            df["datetime"].max() if "datetime" in df.columns else None,
            time.perf_counter() - t0, timeout, VERSION,
        )
        return df.reset_index(drop=True)
    return pd.DataFrame()


def _normalize_1m_df(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    try:
        out = df.copy()
        out = out.loc[:, ~pd.Index(out.columns).duplicated()].copy()
        if "symbol" in out.columns:
            out["symbol"] = out["symbol"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
        if "datetime" in out.columns:
            out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
            try:
                out["datetime"] = out["datetime"].dt.tz_localize(None)
            except Exception:
                pass
            out = out.dropna(subset=["datetime"])
        if "close" not in out.columns:
            for c in ("close_price", "current_price", "price"):
                if c in out.columns:
                    out["close"] = out[c]
                    break
        if "close" not in out.columns:
            return pd.DataFrame()
        for c in ("open", "high", "low"):
            if c not in out.columns:
                out[c] = out["close"]
        if "volume" not in out.columns:
            for c in ("trading_volume", "vol", "Volume", "出来高"):
                if c in out.columns:
                    out["volume"] = out[c]
                    break
        if "volume" not in out.columns:
            out["volume"] = 0.0
        for c in ("open", "high", "low", "close", "volume"):
            out[c] = pd.to_numeric(out[c], errors="coerce")
        out = out.dropna(subset=["symbol", "datetime", "close"])
        out["interval"] = 1
        return out.reset_index(drop=True)
    except Exception:
        logger.debug("[SUMMARY FORCE DIRECT 1M] normalize 1m df failed", exc_info=True)
        return pd.DataFrame()


def _assign_attrs(obj: Any, hist: pd.DataFrame, latest: pd.DataFrame) -> None:
    for name, value in (
        ("summary_1m_df", hist), ("latest_summary_1m_df", latest), ("summary_1m_latest_df", latest),
        ("push_summary_1", latest), ("push_summary_1min", latest), ("push_summary_1m_df", hist),
        ("push_merged_summary_1", latest), ("push_merged_summary_1min", latest), ("push_merged_summary_1m_df", latest),
        ("merged_summary_1", latest), ("merged_summary_1min", latest),
    ):
        try:
            setattr(obj, name, value.copy(deep=False))
        except Exception:
            pass


def _store(df: pd.DataFrame) -> None:
    hist = _normalize_1m_df(df)
    if hist.empty:
        return
    try:
        latest = hist.sort_values(["symbol", "datetime"], kind="stable").groupby("symbol", as_index=False).tail(1).reset_index(drop=True)
    except Exception:
        latest = hist.copy()
    t0 = time.perf_counter()
    try:
        from global_state import global_data
        _assign_attrs(global_data, hist, latest)
    except Exception:
        pass
    try:
        from core.global_context.context import global_context as GC
        _assign_attrs(GC, hist, latest)
    except Exception:
        pass
    logger.warning(
        "[SUMMARY FORCE DIRECT 1M] fast attrs only tf=1 hist_rows=%s latest_rows=%s latest_dt=%s elapsed=%.3fs version=%s",
        len(hist), len(latest), hist["datetime"].max() if "datetime" in hist.columns else None, time.perf_counter() - t0, VERSION,
    )


def _cleanup_ai_running(now_ts: float) -> None:
    max_age = max(30.0, _env_float("SUMMARY_FORCE_DIRECT_AI_RUNNING_MAX_AGE_SEC", 90.0))
    for k, ts in list(_AI_RUNNING.items()):
        age = now_ts - float(ts or 0.0)
        if age > max_age:
            _AI_RUNNING.pop(k, None)
            logger.warning("[SUMMARY FORCE DIRECT 1M] async AI stale running key cleared key=%s age_sec=%.1f version=%s", k, age, VERSION)


def _submit_ai(df: pd.DataFrame, now: dt.datetime, run_entry: bool) -> None:
    if not run_entry or df is None or df.empty or not _env_bool("SUMMARY_FORCE_DIRECT_ASYNC_AI", True):
        return
    key = "force-summary-ai:1:" + _dt_key(now)
    now_ts = time.time()
    with _AI_LOCK:
        _cleanup_ai_running(now_ts)
        if key in _AI_RUNNING:
            logger.warning("[SUMMARY FORCE DIRECT 1M] async AI skipped already_running key=%s rows=%s", key, len(df))
            return
        active_cap = max(1, _env_int("SUMMARY_FORCE_DIRECT_AI_MAX_ACTIVE", 3))
        if len(_AI_RUNNING) >= active_cap:
            oldest = sorted(_AI_RUNNING.items(), key=lambda kv: kv[1])[:3]
            logger.warning("[SUMMARY FORCE DIRECT 1M] async AI skipped active_cap=%s active=%s oldest=%s key=%s", active_cap, len(_AI_RUNNING), oldest, key)
            return
        _AI_RUNNING[key] = now_ts
    df_copy = df.copy(deep=False)

    def _task() -> None:
        started = time.time()
        try:
            logger.warning("[SUMMARY FORCE DIRECT 1M] async AI start key=%s rows=%s version=%s", key, len(df_copy), VERSION)
            try:
                from scheduler_jobs.summary.summary_ai_entry_hook_v20 import run_summary_ai_entry_safe
                run_summary_ai_entry_safe(interval=1, now=now, df=df_copy, source="SUMMARY")
            except Exception:
                logger.exception("[SUMMARY FORCE DIRECT 1M] async AI failed key=%s", key)
        finally:
            with _AI_LOCK:
                _AI_RUNNING.pop(key, None)
            logger.warning("[SUMMARY FORCE DIRECT 1M] async AI done key=%s elapsed=%.3fs version=%s", key, time.time() - started, VERSION)

    try:
        threading.Thread(target=_task, name=f"summary-force-direct-ai-{_dt_key(now)}", daemon=True).start()
        logger.warning("[SUMMARY FORCE DIRECT 1M] async AI thread started key=%s rows=%s active=%s version=%s", key, len(df_copy), len(_AI_RUNNING), VERSION)
    except Exception:
        with _AI_LOCK:
            _AI_RUNNING.pop(key, None)
        logger.exception("[SUMMARY FORCE DIRECT 1M] async AI thread start failed key=%s", key)


def tier1_build_and_publish(now: dt.datetime, *, run_entry: bool) -> pd.DataFrame:
    """scheduler_jobs/summary/runner_core.py の job_summary (tier1) から直接呼ばれる。
    memory直接ビルド (_build_direct、内部で summary_main_memory_latest_1m_patch を使う) を
    タイムアウト付きで試し、成功時のみ _store/_submit_ai を行う。呼び出し元の事前条件
    (_is_main_py() 相当・interval==1) は runner_core.py 側で確認済みの前提。"""
    if not _env_bool("SUMMARY_FORCE_DIRECT_PATCH_ENABLED", True):
        return pd.DataFrame()
    now_i = (now or dt.datetime.now()).replace(microsecond=0)
    t0 = time.perf_counter()
    try:
        df = _build_direct(now_i)
        if df is not None and not df.empty:
            _store(df)
            _submit_ai(df, now_i, run_entry)
            logger.warning("[SUMMARY FORCE DIRECT 1M] tier1 hit rows=%s elapsed=%.3fs version=%s", len(df), time.perf_counter() - t0, VERSION)
            return df
        logger.warning("[SUMMARY FORCE DIRECT 1M] tier1 direct empty/timeout/stale elapsed=%.3fs version=%s", time.perf_counter() - t0, VERSION)
    except Exception:
        logger.exception("[SUMMARY FORCE DIRECT 1M] tier1 build failed now=%s", now_i)
    return pd.DataFrame()


def install() -> bool:
    global _PATCHED
    os.environ.setdefault("SUMMARY_FORCE_DIRECT_BUILD_TIMEOUT_SEC", "6")
    os.environ.setdefault("SUMMARY_FORCE_DIRECT_AI_MAX_ACTIVE", "3")
    os.environ.setdefault("SUMMARY_FORCE_DIRECT_MEMORY_LOOKBACK_SEC", "90")
    os.environ.setdefault("SUMMARY_FORCE_DIRECT_MAX_OUTPUT_AGE_SEC", "90")
    os.environ.setdefault("SUMMARY_FORCE_DIRECT_NO_ORIGINAL_FALLBACK_WHEN_RAW_EXISTS", "1")
    _PATCHED = True
    logger.warning("[SUMMARY FORCE DIRECT 1M] installed version=%s main=%s (job_summary wrap removed; called as tier1 from runner_core.job_summary)", VERSION, _is_main_py())
    return True


try:
    install()
except Exception:
    logger.exception("[SUMMARY FORCE DIRECT 1M] auto install failed")

__all__ = ["VERSION", "install", "tier1_build_and_publish", "_is_main_py", "_env_bool"]
