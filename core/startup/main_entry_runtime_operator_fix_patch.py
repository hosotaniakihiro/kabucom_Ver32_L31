# -*- coding: utf-8 -*-
# ============================================================
# File   : core/startup/main_entry_runtime_operator_fix_patch.py
# Version: V1-MAIN-ENTRY-LIGHT-TONOSAMA-MEMORY-SUMMARYAI
# ------------------------------------------------------------
# Purpose:
#   Operator-requested runtime fix for main.py:
#   1) Do not require PUSH DB writer readiness for SUMMARY_AI.
#   2) Keep main.py summary tick light: wait only for PUSH 1m and skip ranking.
#   3) When Tonosama 3m/5m surge ratio is no-ratio, allow recovery from
#      in-memory PUSH frames as well as pushYYYYMMDD.db.
# ============================================================
from __future__ import annotations

import datetime as dt
import logging
import os
import sys
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V1-MAIN-ENTRY-LIGHT-TONOSAMA-MEMORY-SUMMARYAI"
_INSTALLED = False
_WATCHER_STARTED = False
_PATCHED_TONOSAMA_MEMORY = False
_TRUE = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}


def _env_on(name: str, default: bool = False) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in _TRUE
    except Exception:
        return bool(default)


def _force_env(name: str, value: str, changed: dict[str, tuple[str | None, str]] | None = None) -> None:
    try:
        old = os.getenv(name)
        os.environ[name] = str(value)
        if changed is not None and str(old or "") != str(value):
            changed[name] = (old, str(value))
    except Exception:
        pass


def _set_env_defaults() -> None:
    changed: dict[str, tuple[str | None, str]] = {}

    # 2) main.py should not wait for 3m/5m PUSH or ranking summary jobs.
    #    3m/5m judgement uses cache/raw fallback/previous DB values instead.
    for name, value in {
        "SUMMARY_MAIN_WAIT_PUSH_1M_ONLY": "1",
        "SUMMARY_MAIN_BG_LONG_PUSH_ENABLED": "0",
        "SUMMARY_PUSH_BG_ALL_INTERVALS": "0",
        "SUMMARY_PUSH_BG_LONG_INTERVALS": "0",
        "SUMMARY_PUSH_DISPLAY_ALL_INTERVALS": "0",
        "SUMMARY_PUSH_FORCE_1_3_5": "0",
        "SUMMARY_PARALLEL_FORCE_1_3_5": "0",
        "SUMMARY_PARALLEL_MAIN_ENTRY_ONLY": "1",
        "SUMMARY_RUN_ENTRY_ON_1M_ONLY": "1",
        # Correct key used by summary_parallel_intervals_runtime_patch.py.
        "SUMMARY_PARALLEL_RANKING_ENABLED": "0",
        # Backward-compatible old key used by older patches/logs.
        "SUMMARY_RANKING_PARALLEL_ENABLED": "0",
        # Correct timeout keys used by summary_parallel_intervals_runtime_patch.py.
        "SUMMARY_PARALLEL_INTERVAL_TIMEOUT_SEC": "18",
        "SUMMARY_PARALLEL_TIMEOUT_MIN_SEC": "18",
        # Older timeout keys still used by scheduler/relief wrappers.
        "SUMMARY_PARALLEL_TIMEOUT_SEC": "18",
        "SUMMARY_PARALLEL_MIN_TIMEOUT_SEC": "18",
        "SUMMARY_CHILD_JOB_TIMEOUT_SEC": "18",
        "SUMMARY_PARENT_TICK_TIMEOUT_SEC": "25",
        "SUMMARY_PARALLEL_PARENT_TIMEOUT_SEC": "25",
        "SUMMARY_MAIN_TICK_TIMEOUT_CAP_SEC": "18",
        "SUMMARY_MTF_PUSH_RAW_FALLBACK_ENABLED": "1",
        "SUMMARY_MTF_DIFF_FROM_1M_ENABLED": "1",
        "SUMMARY_LATEST_PREFER_HEALTH": "1",
    }.items():
        _force_env(name, value, changed)

    # 4) PUSH DB writer復旧は今回対象外。writer_ready=False / memory_only=True を
    #    理由に SUMMARY_AI を止めず、後段のfresh quote/order guardで最終安全確認する。
    for name, value in {
        "SUMMARY_AI_REQUIRE_PUSH_WRITER_READY": "0",
        "SUMMARY_AI_REQUIRE_FRESH_PUSH_1M": "0",
        "SUMMARY_AI_WRITER_CHECK_ALLOW_FRESH_RAW_DB": "1",
        "SUMMARY_AI_SCORE_BRIDGE_ENABLED": "1",
        "SUMMARY_AI_REFILL_RETRY_WITHOUT_TONOSAMA": "1",
        "SUMMARY_AI_REFILL_TOP_N": "80",
        "SUMMARY_AI_REFILL_RETRY_TOP_N": "100",
        "SUMMARY_AI_REFILL_TONOSAMA_MAX_CANDIDATES": "80",
        "SUMMARY_AI_REFILL_RETRY_TONOSAMA_MAX_CANDIDATES": "100",
    }.items():
        _force_env(name, value, changed)

    # 3) Tonosama no-ratio recovery should be able to use memory PUSH too.
    for name, value in {
        "TONOSAMA_RAW1_HISTORY_RESAMPLE": "1",
        "TONOSAMA_RAW1_RESAMPLE_FALLBACK": "1",
        "TONOSAMA_PUSH_MEMORY_HISTORY_ENABLED": "1",
        "TONOSAMA_PUSH_MEMORY_HISTORY_LOOKBACK_MIN": "30",
        "TONOSAMA_PUSH_MEMORY_HISTORY_MAX_ROWS": "20000",
        "TONOSAMA_PUSH_MEMORY_HISTORY_MIN_VOLUME_NONZERO": "1",
        "TONOSAMA_SURGE_RATIO_MIN_PERIODS": "1",
    }.items():
        _force_env(name, value, changed)

    if changed:
        logger.warning("[MAIN ENTRY OPERATOR FIX] env applied version=%s changed=%s", VERSION, {k: v[1] for k, v in changed.items()})


def _is_df_like(obj: Any) -> bool:
    try:
        import pandas as pd
        return isinstance(obj, pd.DataFrame)
    except Exception:
        return False


def _first_col(df: Any, names: tuple[str, ...]) -> str | None:
    try:
        cols = set(getattr(df, "columns", []))
        for name in names:
            if name in cols:
                return name
    except Exception:
        pass
    return None


def _normalize_memory_push_frame(df: Any, *, label: str) -> Any:
    try:
        import pandas as pd
        if not isinstance(df, pd.DataFrame) or df.empty:
            return pd.DataFrame()
        if len(df) > int(float(os.getenv("TONOSAMA_PUSH_MEMORY_HISTORY_MAX_ROWS", "20000"))):
            df = df.tail(int(float(os.getenv("TONOSAMA_PUSH_MEMORY_HISTORY_MAX_ROWS", "20000"))))
        x = df.copy()
        symbol_col = _first_col(x, ("symbol", "Symbol", "code", "Code", "銘柄コード"))
        price_col = _first_col(x, ("price", "current_price", "CurrentPrice", "close", "close_price", "last_price"))
        dt_col = _first_col(x, ("datetime", "received_at", "timestamp", "time", "updated_at", "last_update"))
        if symbol_col is None or price_col is None or dt_col is None:
            return pd.DataFrame()

        x["symbol"] = x[symbol_col].astype(str).str.strip()
        x["datetime"] = pd.to_datetime(x[dt_col], errors="coerce")
        x["price"] = pd.to_numeric(x[price_col], errors="coerce")
        x = x.dropna(subset=["symbol", "datetime", "price"])
        x = x[(x["symbol"] != "") & (x["price"] > 0)].copy()
        if x.empty:
            return pd.DataFrame()

        lookback_min = max(5, int(float(os.getenv("TONOSAMA_PUSH_MEMORY_HISTORY_LOOKBACK_MIN", "30"))))
        latest = x["datetime"].max()
        since = latest - pd.Timedelta(minutes=lookback_min)
        x = x[x["datetime"] >= since].copy()
        if x.empty:
            return pd.DataFrame()

        vol_col = _first_col(x, ("volume", "Volume", "trading_volume", "cum_volume", "売買高"))
        tv_col = _first_col(x, ("trading_value", "TradingValue", "turnover", "売買代金"))
        name_col = _first_col(x, ("symbolname", "symbol_name", "name", "銘柄名"))
        x["volume_raw"] = pd.to_numeric(x[vol_col], errors="coerce").fillna(0.0) if vol_col else 0.0
        x["trading_value_raw"] = pd.to_numeric(x[tv_col], errors="coerce").fillna(0.0) if tv_col else 0.0
        x["symbolname"] = x[name_col].astype(str) if name_col else ""
        x["slot"] = x["datetime"].dt.floor("1min")

        grouped = x.sort_values(["symbol", "slot", "datetime"]).groupby(["symbol", "slot"], sort=False)
        out = pd.DataFrame({
            "symbol": grouped["symbol"].last(),
            "symbolname": grouped["symbolname"].last(),
            "datetime": grouped["slot"].last(),
            "open": grouped["price"].first(),
            "high": grouped["price"].max(),
            "low": grouped["price"].min(),
            "close": grouped["price"].last(),
            "cum_volume": grouped["volume_raw"].max(),
            "cum_trading_value": grouped["trading_value_raw"].max(),
        }).reset_index(drop=True)
        if out.empty:
            return pd.DataFrame()

        out = out.sort_values(["symbol", "datetime"])
        out["volume"] = out.groupby("symbol")["cum_volume"].diff().fillna(0.0)
        out.loc[(out["volume"] < 0) | out["volume"].isna(), "volume"] = 0.0
        raw_positive = pd.to_numeric(out["cum_volume"], errors="coerce").fillna(0.0) > 0
        delta_positive = pd.to_numeric(out["volume"], errors="coerce").fillna(0.0) > 0
        if int(delta_positive.sum()) < max(1, int(raw_positive.sum() * 0.05)):
            out["volume"] = pd.to_numeric(out["cum_volume"], errors="coerce").fillna(0.0)
            volume_mode = "raw_max"
        else:
            volume_mode = "cum_diff"

        out["trading_value"] = out.groupby("symbol")["cum_trading_value"].diff().fillna(0.0)
        out.loc[out["trading_value"] < 0, "trading_value"] = 0.0
        out["price"] = out["close"]
        out["current_price"] = out["close"]
        out["open_price"] = out["open"]
        out["high_price"] = out["high"]
        out["low_price"] = out["low"]
        out["close_price"] = out["close"]
        out["interval"] = 1
        out["source"] = f"tonosama_push_memory_history_1m:{label}"
        out = out.dropna(subset=["datetime", "close"]).reset_index(drop=True)
        if out.empty:
            return pd.DataFrame()
        logger.warning(
            "[TONOSAMA PUSH MEMORY HISTORY] usable source=%s rows=%s symbols=%s latest=%s volume_nonzero=%s volume_mode=%s",
            label,
            len(out),
            out["symbol"].nunique() if "symbol" in out.columns else 0,
            out["datetime"].max() if "datetime" in out.columns else None,
            int((pd.to_numeric(out.get("volume", 0), errors="coerce").fillna(0) > 0).sum()) if "volume" in out.columns else 0,
            volume_mode,
        )
        return out
    except Exception:
        logger.debug("[TONOSAMA PUSH MEMORY HISTORY] normalize failed source=%s", label, exc_info=True)
        try:
            import pandas as pd
            return pd.DataFrame()
        except Exception:
            return None


def _candidate_memory_frames() -> list[tuple[str, Any]]:
    frames: list[tuple[str, Any]] = []

    # Explicit common modules first.
    for mod_name in (
        "trading.push.push_stream.monitor",
        "trading.push.push_stream",
        "trading.push.push_manager",
        "global_data",
        "global_state",
        "core.global_context.context",
    ):
        try:
            mod = __import__(mod_name, fromlist=["*"])
        except Exception:
            continue
        try:
            for attr, val in vars(mod).items():
                if _is_df_like(val):
                    frames.append((f"{mod_name}.{attr}", val))
                elif attr in {"global_data", "global_context", "GC"}:
                    try:
                        for sub_attr, sub_val in vars(val).items():
                            if _is_df_like(sub_val):
                                frames.append((f"{mod_name}.{attr}.{sub_attr}", sub_val))
                    except Exception:
                        pass
        except Exception:
            pass

    # Last resort: scan already-loaded push/global modules for DataFrames.
    for mod_name, mod in list(sys.modules.items()):
        if mod is None:
            continue
        low = str(mod_name).lower()
        if not any(k in low for k in ("push", "global_context", "global_data")):
            continue
        try:
            for attr, val in vars(mod).items():
                if _is_df_like(val):
                    frames.append((f"{mod_name}.{attr}", val))
        except Exception:
            continue
    return frames


def _load_push_memory_1m_history() -> Any:
    try:
        import pandas as pd
        if not _env_on("TONOSAMA_PUSH_MEMORY_HISTORY_ENABLED", True):
            return pd.DataFrame()
        parts = []
        for label, frame in _candidate_memory_frames():
            norm = _normalize_memory_push_frame(frame, label=label)
            if isinstance(norm, pd.DataFrame) and not norm.empty:
                parts.append(norm)
        if not parts:
            return pd.DataFrame()
        out = pd.concat(parts, ignore_index=True, sort=False)
        out = out.drop_duplicates(subset=["symbol", "datetime"], keep="last").sort_values(["symbol", "datetime"])
        min_nonzero = int(float(os.getenv("TONOSAMA_PUSH_MEMORY_HISTORY_MIN_VOLUME_NONZERO", "1")))
        nonzero = int((pd.to_numeric(out.get("volume", 0), errors="coerce").fillna(0) > 0).sum()) if "volume" in out.columns else 0
        if nonzero < min_nonzero:
            logger.warning("[TONOSAMA PUSH MEMORY HISTORY] rejected no usable volume rows=%s symbols=%s nonzero=%s", len(out), out["symbol"].nunique() if "symbol" in out.columns else 0, nonzero)
            return pd.DataFrame()
        logger.warning("[TONOSAMA PUSH MEMORY HISTORY] loaded rows=%s symbols=%s latest=%s volume_nonzero=%s", len(out), out["symbol"].nunique() if "symbol" in out.columns else 0, out["datetime"].max() if "datetime" in out.columns else None, nonzero)
        return out.reset_index(drop=True)
    except Exception:
        logger.debug("[TONOSAMA PUSH MEMORY HISTORY] load failed", exc_info=True)
        try:
            import pandas as pd
            return pd.DataFrame()
        except Exception:
            return None


def _patch_tonosama_memory_history_once() -> bool:
    global _PATCHED_TONOSAMA_MEMORY
    if _PATCHED_TONOSAMA_MEMORY:
        return True
    try:
        import pandas as pd
        import core.startup.tonosama_history_missing_guard_patch as th
        cur = getattr(th, "_load_push_raw_db_1m_history", None)
        if not callable(cur):
            return False
        if getattr(cur, "_main_entry_memory_history_v1", False):
            _PATCHED_TONOSAMA_MEMORY = True
            return True
        orig = cur

        def patched_load_push_raw_db_1m_history():
            mem = _load_push_memory_1m_history()
            if isinstance(mem, pd.DataFrame) and not mem.empty:
                return mem
            db = orig()
            if isinstance(db, pd.DataFrame) and not db.empty:
                return db
            return mem if isinstance(mem, pd.DataFrame) else pd.DataFrame()

        patched_load_push_raw_db_1m_history._main_entry_memory_history_v1 = True  # type: ignore[attr-defined]
        patched_load_push_raw_db_1m_history._original = orig  # type: ignore[attr-defined]
        th._load_push_raw_db_1m_history = patched_load_push_raw_db_1m_history
        _PATCHED_TONOSAMA_MEMORY = True
        logger.warning("[MAIN ENTRY OPERATOR FIX] patched Tonosama raw1 fallback with in-memory PUSH history")
        return True
    except Exception:
        logger.debug("[MAIN ENTRY OPERATOR FIX] Tonosama memory patch wait/failed", exc_info=True)
        return False


def _patch_summary_parallel_attrs() -> None:
    try:
        import core.startup.summary_parallel_intervals_runtime_patch as sp
        for attr, value in {
            "SUMMARY_PARALLEL_INTERVAL_TIMEOUT_SEC": 18.0,
            "SUMMARY_PARALLEL_TIMEOUT_SEC": 18.0,
            "SUMMARY_CHILD_JOB_TIMEOUT_SEC": 18.0,
            "SUMMARY_PARENT_TICK_TIMEOUT_SEC": 25.0,
        }.items():
            try:
                if hasattr(sp, attr):
                    setattr(sp, attr, value)
            except Exception:
                pass
    except Exception:
        pass


def _watcher() -> None:
    deadline = time.monotonic() + 90.0
    while time.monotonic() < deadline:
        try:
            _set_env_defaults()
            _patch_summary_parallel_attrs()
            if _patch_tonosama_memory_history_once():
                return
        except Exception:
            logger.debug("[MAIN ENTRY OPERATOR FIX] watcher iteration failed", exc_info=True)
        time.sleep(0.75)


def install() -> bool:
    global _INSTALLED, _WATCHER_STARTED
    if _INSTALLED:
        return True
    try:
        _set_env_defaults()
        _patch_summary_parallel_attrs()
        _patch_tonosama_memory_history_once()
        if not _WATCHER_STARTED:
            _WATCHER_STARTED = True
            threading.Thread(target=_watcher, name="main-entry-operator-fix", daemon=True).start()
        _INSTALLED = True
        logger.warning("[MAIN ENTRY OPERATOR FIX] installed version=%s watcher=%s tonosama_memory=%s", VERSION, _WATCHER_STARTED, _PATCHED_TONOSAMA_MEMORY)
        return True
    except Exception:
        logger.exception("[MAIN ENTRY OPERATOR FIX] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[MAIN ENTRY OPERATOR FIX] auto install failed")

__all__ = ["VERSION", "install"]
