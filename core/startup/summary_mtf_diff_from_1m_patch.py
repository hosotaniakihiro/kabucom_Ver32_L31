# ============================================================
# File   : core/startup/summary_mtf_diff_from_1m_patch.py
# Version: V1.9-MAIN-MEMORY-1M-HISTORY-BRIDGE
# ------------------------------------------------------------
# 目的:
#   main.py ではNAS SQLite直読み/保存を避ける。
#   ただし entry / Summary-AI が参照する 1分足PUSH summary を
#   global_data / global_context 相当へ必ず公開し、その1分足履歴から
#   3m/5mを軽量生成する。
#
# V1.9:
#   - summary_main_memory_latest_1m_patch._publish_latest をwrapし、
#     latestだけでなく set_summary_history(tf=1) にも必ず保存。
#   - 09:39ログの MERGED SET tf=1 rows>0 だが SUMMARY HISTORY GET tf=1 rows=0
#     になる状態を解消。
#
# V1.8:
#   - interval=1 の diff_update 結果を必ず push merged / history / latest へ publish。
#   - interval=1 が空でも、global_data上の push summary / raw push から1mを復元。
#   - main.py では 3m/5m を保存せず、公開済み1m履歴から resample して publish。
#   - 3m/5m は volume=0 / 履歴不足を entry 用 ready にしない。
#   - main_database.py 等の非mainプロセスでは original diff_update を優先。
# ============================================================

from __future__ import annotations

import inspect
import logging
import os
import sys
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

VERSION = "V1.9-MAIN-MEMORY-1M-HISTORY-BRIDGE"
_INSTALLED = False
_ORIG_DIFF_UPDATE = None
_LAST_MAIN_SKIP_LOG: dict[int, float] = {}


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "enable", "enabled"}
    except Exception:
        return bool(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


def _safe_len(x: Any) -> int:
    try:
        return 0 if x is None else len(x)
    except Exception:
        return 0


def _is_main_py_process() -> bool:
    try:
        argv = " ".join(str(x) for x in (getattr(sys, "argv", None) or []))
        return "main.py" in argv.replace("\\", "/").lower()
    except Exception:
        return False


def _main_should_skip_nas_diff_update(interval: int | None = None) -> bool:
    try:
        if interval is not None and int(interval) == 1:
            return False
    except Exception:
        pass
    if not _is_main_py_process():
        return False
    if _env_bool("AUTOSTOCK_MAIN_ALLOW_NAS_SUMMARY_DIFF_UPDATE", False):
        return False
    if _env_bool("AUTOSTOCK_MAIN_SKIP_NAS_SUMMARY_DIFF_UPDATE", True):
        return True
    if _env_bool("SUMMARY_MAIN_ENTRY_ONLY", False):
        return True
    if _env_bool("SUMMARY_SKIP_DB_SAVE_IN_MAIN", False):
        return True
    role = str(os.getenv("SUMMARY_DB_WRITER_ROLE") or "").strip().lower()
    return role in {"entry_only", "main_entry_only", "read_only", "no_save"}


def _ensure_basic_score_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy() if isinstance(df, pd.DataFrame) else pd.DataFrame()
    if out.empty:
        return out
    defaults = {
        "score": 0.0,
        "score_total": 0.0,
        "final_score": 0.0,
        "display_score": 0.0,
        "combined_score": 0.0,
        "score_buy": 0.0,
        "buy_score": 0.0,
        "score_sell": 0.0,
        "sell_score": 0.0,
        "slope": 0.0,
        "slope_atr_scaled": 0.0,
        "score_slope": 0.0,
        "mtf": 0.0,
        "score_mtf": 0.0,
        "mtf_score": 0.0,
        "rsi": 50.0,
        "macd": 0.0,
        "signal": 0.0,
        "technical_ready": False,
        "display_ready": False,
        "usable_ready": False,
    }
    for col, val in defaults.items():
        if col not in out.columns:
            out[col] = val
        else:
            try:
                out[col] = out[col].fillna(val)
            except Exception:
                pass
    return out


def _normalize_summary_df(df: Any, *, interval: int) -> pd.DataFrame:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame()
    try:
        try:
            from trading.summary.controller_utils import normalize_summary_df
            out = normalize_summary_df(df)
        except Exception:
            out = df.copy()
        if out is None or out.empty:
            return pd.DataFrame()
        out = out.copy()
        out = out.loc[:, ~pd.Index(out.columns).duplicated()].copy()
        if "symbol" not in out.columns:
            for c in ("Symbol", "code", "Code", "銘柄コード"):
                if c in out.columns:
                    out["symbol"] = out[c]
                    break
        if "symbol" in out.columns:
            out["symbol"] = out["symbol"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
            out = out[out["symbol"].astype(str).str.len() > 0]
        if "datetime" not in out.columns:
            for c in ("time", "timestamp", "dt", "last_tick_at", "first_tick_at", "DateTime"):
                if c in out.columns:
                    out["datetime"] = out[c]
                    break
        if "datetime" in out.columns:
            out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
            out = out.dropna(subset=["datetime"])
        else:
            return pd.DataFrame()
        if "close" not in out.columns:
            for c in ("close_price", "current_price", "price", "last", "現在値"):
                if c in out.columns:
                    out["close"] = out[c]
                    break
        if "close" not in out.columns:
            return pd.DataFrame()
        for c in ("open", "high", "low"):
            if c not in out.columns:
                alt = f"{c}_price"
                out[c] = out[alt] if alt in out.columns else out["close"]
        if "volume" not in out.columns:
            for c in ("vol", "trading_volume", "Volume", "出来高"):
                if c in out.columns:
                    out["volume"] = out[c]
                    break
        if "volume" not in out.columns:
            out["volume"] = 0.0
        for c in ("open", "high", "low", "close", "volume"):
            out[c] = pd.to_numeric(out[c], errors="coerce")
        out = out.dropna(subset=["close"])
        out["interval"] = int(interval)
        out = _ensure_basic_score_columns(out)
        if out.empty:
            return pd.DataFrame()
        return out.reset_index(drop=True)
    except Exception:
        logger.exception("[SUMMARY MTF DIFF 1M PATCH] normalize failed interval=%s", interval)
        return pd.DataFrame()


def _numeric_series(df: pd.DataFrame, col: str, default: float = 0.0) -> pd.Series:
    try:
        if col in df.columns:
            return pd.to_numeric(df[col], errors="coerce").fillna(default)
    except Exception:
        pass
    return pd.Series(default, index=df.index, dtype="float64")


def _nonzero_count(df: pd.DataFrame, col: str) -> int:
    try:
        if col not in df.columns:
            return 0
        return int((_numeric_series(df, col, 0.0).abs() > 0).sum())
    except Exception:
        return 0


def _max_hist_per_symbol(df: pd.DataFrame) -> int:
    try:
        if df.empty or "symbol" not in df.columns:
            return 0
        return int(df.groupby("symbol").size().max())
    except Exception:
        return 0


def _mtf_is_usable(df: pd.DataFrame, *, interval: int, label: str) -> bool:
    if df is None or df.empty:
        return False
    if "symbol" not in df.columns or "datetime" not in df.columns:
        return False
    volume_nonzero = _nonzero_count(df, "volume")
    hist_max = _max_hist_per_symbol(df)
    if _env_bool("SUMMARY_MTF_REJECT_ZERO_VOLUME", True) and volume_nonzero <= 0:
        logger.warning("[SUMMARY MTF DIFF 1M PATCH] reject %s interval=%s reason=zero_volume rows=%s hist_max=%s", label, interval, len(df), hist_max)
        return False
    min_mtf_rows = max(1, _env_int("SUMMARY_MTF_FALLBACK_MIN_MTF_ROWS", 2))
    if int(interval) in (3, 5) and hist_max < min_mtf_rows:
        logger.warning("[SUMMARY MTF DIFF 1M PATCH] reject %s interval=%s reason=short_mtf_history rows=%s hist_max=%s min=%s", label, interval, len(df), hist_max, min_mtf_rows)
        return False
    return True


def _latest_by_symbol(hist: pd.DataFrame) -> pd.DataFrame:
    try:
        return hist.sort_values(["symbol", "datetime"], kind="stable").groupby("symbol", as_index=False).tail(1).reset_index(drop=True)
    except Exception:
        return hist.copy() if isinstance(hist, pd.DataFrame) else pd.DataFrame()


def _call_global_method(obj: Any, method_name: str, interval_i: int, df: pd.DataFrame) -> None:
    try:
        fn = getattr(obj, method_name, None)
        if not callable(fn):
            return
        try:
            fn(tf=interval_i, df=df.copy(), source="push")
            return
        except TypeError:
            pass
        try:
            fn(interval_i, df.copy())
            return
        except TypeError:
            pass
        try:
            fn(interval_i, df.copy(), "push")
            return
        except TypeError:
            pass
    except Exception:
        logger.debug("[SUMMARY MTF DIFF 1M PATCH] global method publish failed method=%s interval=%s", method_name, interval_i, exc_info=True)


def _publish_to_global(interval: int, df_hist: pd.DataFrame, df_latest: pd.DataFrame | None = None, *, require_mtf_ready: bool = True) -> bool:
    interval_i = int(interval)
    hist = _normalize_summary_df(df_hist, interval=interval_i)
    if hist.empty:
        return False
    if interval_i in (3, 5) and require_mtf_ready and not _mtf_is_usable(hist, interval=interval_i, label="publish_hist"):
        logger.warning("[SUMMARY MTF DIFF 1M PATCH] publish skipped interval=%s reason=unusable_hist rows=%s", interval_i, _safe_len(hist))
        return False
    latest = _normalize_summary_df(df_latest, interval=interval_i) if isinstance(df_latest, pd.DataFrame) and not df_latest.empty else pd.DataFrame()
    if latest.empty:
        latest = _latest_by_symbol(hist)
    try:
        from global_state import global_data
        # merged/latest はlatest、historyはhistを明示保存する。
        for method_name, df_arg in (
            ("set_push_merged_summary", latest.copy() if interval_i == 1 else hist.copy()),
            ("set_merged_summary", latest.copy() if interval_i == 1 else hist.copy()),
            ("set_summary_history", hist.copy()),
            ("set_latest_summary", latest.copy()),
            ("set_push_summary", latest.copy() if interval_i == 1 else hist.copy()),
        ):
            _call_global_method(global_data, method_name, interval_i, df_arg)
        for name, value in (
            (f"summary_{interval_i}m_df", hist.copy()),
            (f"latest_summary_{interval_i}m_df", latest.copy()),
            (f"summary_{interval_i}m_latest_df", latest.copy()),
            (f"push_summary_{interval_i}m_df", hist.copy()),
            (f"push_merged_summary_{interval_i}m_df", latest.copy() if interval_i == 1 else hist.copy()),
        ):
            try:
                setattr(global_data, name, value)
            except Exception:
                pass
        logger.warning(
            "[SUMMARY MTF DIFF 1M PATCH] published interval=%s hist_rows=%s latest_rows=%s symbols=%s latest_dt=%s volume_nonzero=%s score_nonzero=%s version=%s",
            interval_i,
            len(hist),
            len(latest),
            int(latest["symbol"].nunique()) if "symbol" in latest.columns else 0,
            hist["datetime"].max() if "datetime" in hist.columns else None,
            _nonzero_count(hist, "volume"),
            _nonzero_count(hist, "score_total"),
            VERSION,
        )
        return True
    except Exception:
        logger.exception("[SUMMARY MTF DIFF 1M PATCH] global publish failed interval=%s", interval_i)
        return False


def _patch_memory_latest_publish_history() -> bool:
    """summary_main_memory_latest_1m_patch は latest だけを publish するため、history 保存を補う。"""
    try:
        import core.startup.summary_main_memory_latest_1m_patch as mem
        old = getattr(mem, "_publish_latest", None)
        if not callable(old):
            return False
        if getattr(old, "_summary_mtf_memory_history_bridge_v19", False):
            return True

        def _publish_latest_with_history(df: pd.DataFrame) -> None:
            try:
                old(df)
            finally:
                try:
                    hist = _normalize_summary_df(df, interval=1)
                    if not hist.empty:
                        latest = _latest_by_symbol(hist)
                        _publish_to_global(1, hist, latest, require_mtf_ready=False)
                        logger.warning(
                            "[SUMMARY MTF DIFF 1M PATCH] memory 1m history bridge published hist_rows=%s latest_rows=%s latest_dt=%s version=%s",
                            len(hist),
                            len(latest),
                            hist["datetime"].max() if "datetime" in hist.columns else None,
                            VERSION,
                        )
                except Exception:
                    logger.exception("[SUMMARY MTF DIFF 1M PATCH] memory 1m history bridge failed")

        _publish_latest_with_history._summary_mtf_memory_history_bridge_v19 = True  # type: ignore[attr-defined]
        _publish_latest_with_history._original = old  # type: ignore[attr-defined]
        mem._publish_latest = _publish_latest_with_history
        logger.warning("[SUMMARY MTF DIFF 1M PATCH] memory 1m _publish_latest history bridge installed version=%s", VERSION)
        return True
    except Exception:
        logger.exception("[SUMMARY MTF DIFF 1M PATCH] memory history bridge install failed")
        return False


def _raw_or_summary_to_1m(x: Any) -> pd.DataFrame:
    if not isinstance(x, pd.DataFrame) or x.empty:
        return pd.DataFrame()
    df = x.copy()
    if {"symbol", "datetime", "close"}.issubset(set(df.columns)):
        return _normalize_summary_df(df, interval=1)
    try:
        if "symbol" not in df.columns:
            for c in ("Symbol", "code", "Code", "銘柄コード"):
                if c in df.columns:
                    df["symbol"] = df[c]
                    break
        if "datetime" not in df.columns:
            for c in ("datetime", "time", "timestamp", "dt", "last_tick_at", "first_tick_at", "recv_at", "received_at"):
                if c in df.columns:
                    df["datetime"] = df[c]
                    break
        price_col = None
        for c in ("price", "current_price", "close", "close_price", "CurrentPrice", "現在値"):
            if c in df.columns:
                price_col = c
                break
        volume_col = None
        for c in ("volume", "vol", "trading_volume", "Volume", "出来高"):
            if c in df.columns:
                volume_col = c
                break
        if "symbol" not in df.columns or "datetime" not in df.columns or price_col is None:
            return pd.DataFrame()
        df["symbol"] = df["symbol"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
        df["price"] = pd.to_numeric(df[price_col], errors="coerce")
        df["volume"] = pd.to_numeric(df[volume_col], errors="coerce").fillna(0.0) if volume_col else 0.0
        df = df.dropna(subset=["symbol", "datetime", "price"])
        if df.empty:
            return pd.DataFrame()
        df["minute"] = df["datetime"].dt.floor("min")
        frames = []
        for sym, g in df.sort_values("datetime", kind="stable").groupby("symbol", sort=False):
            r = g.set_index("minute").resample("1min", label="right", closed="right").agg(
                open=("price", "first"), high=("price", "max"), low=("price", "min"), close=("price", "last"), volume=("volume", "sum")
            ).dropna(subset=["close"])
            if r.empty:
                continue
            r["symbol"] = str(sym)
            frames.append(r.reset_index().rename(columns={"minute": "datetime"}))
        if not frames:
            return pd.DataFrame()
        return _normalize_summary_df(pd.concat(frames, ignore_index=True, sort=False), interval=1)
    except Exception:
        logger.debug("[SUMMARY MTF DIFF 1M PATCH] raw push -> 1m failed", exc_info=True)
        return pd.DataFrame()


def _cached_1m_history_from_global() -> pd.DataFrame:
    try:
        from global_state import global_data
        candidates: list[Any] = []
        for name in ("summary_1m_df", "latest_summary_1m_df", "summary_1m_latest_df", "push_summary_1m_df", "push_merged_summary_1m_df", "push_summary_1min", "push_summary_1min_df", "merged_summary_1", "merged_summary_1min", "push_df"):
            try:
                candidates.append(getattr(global_data, name, None))
            except Exception:
                pass
        for method_name, args in (("get_summary_history", (1,)), ("get_push_merged_summary", (1,)), ("get_latest_summary", (1,)), ("get_push_summary", (1,)), ("get_push_df", ()),):
            try:
                fn = getattr(global_data, method_name, None)
                if callable(fn):
                    candidates.append(fn(*args))
            except Exception:
                pass
        dfs = []
        for x in candidates:
            df = _raw_or_summary_to_1m(x)
            if isinstance(df, pd.DataFrame) and not df.empty:
                dfs.append(df)
        if not dfs:
            return pd.DataFrame()
        out = pd.concat(dfs, ignore_index=True, sort=False)
        out = out.loc[:, ~pd.Index(out.columns).duplicated()].copy()
        out = _normalize_summary_df(out, interval=1)
        if out.empty:
            return pd.DataFrame()
        out = out.drop_duplicates(subset=["symbol", "datetime"], keep="last")
        out = out.sort_values(["symbol", "datetime"], kind="stable")
        return out.reset_index(drop=True)
    except Exception:
        logger.exception("[SUMMARY MTF DIFF 1M PATCH] cached 1m history load failed")
        return pd.DataFrame()


def _publish_1m_from_any(df: Any, *, reason: str) -> pd.DataFrame:
    one = _raw_or_summary_to_1m(df)
    if one.empty:
        one = _cached_1m_history_from_global()
    if one.empty:
        return pd.DataFrame()
    latest = _latest_by_symbol(one)
    _publish_to_global(1, one, latest, require_mtf_ready=False)
    logger.warning("[SUMMARY MTF DIFF 1M PATCH] interval=1 published reason=%s hist_rows=%s latest_rows=%s latest_dt=%s version=%s", reason, len(one), len(latest), one["datetime"].max() if "datetime" in one.columns else None, VERSION)
    return latest.reset_index(drop=True)


def _min_1m_rows_required(interval: int) -> int:
    default = max(int(interval) * 3, 10)
    return max(1, _env_int("SUMMARY_MTF_FALLBACK_MIN_1M_ROWS", default))


def _resample_cached_1m_to_mtf(interval: int) -> pd.DataFrame:
    interval_i = int(interval)
    if interval_i not in (3, 5):
        return pd.DataFrame()
    one = _cached_1m_history_from_global()
    if one.empty:
        return pd.DataFrame()
    try:
        one = _normalize_summary_df(one, interval=1)
        if one.empty:
            return pd.DataFrame()
        hist_max_1m = _max_hist_per_symbol(one)
        min_rows = _min_1m_rows_required(interval_i)
        if _nonzero_count(one, "volume") <= 0:
            logger.warning("[SUMMARY MTF DIFF 1M PATCH] cached 1m -> %sm skipped reason=one_volume_zero rows=%s hist_max_1m=%s", interval_i, len(one), hist_max_1m)
            return pd.DataFrame()
        if hist_max_1m < min_rows:
            logger.warning("[SUMMARY MTF DIFF 1M PATCH] cached 1m -> %sm skipped reason=short_1m_history rows=%s hist_max_1m=%s min=%s", interval_i, len(one), hist_max_1m, min_rows)
            return pd.DataFrame()
        frames = []
        rule = f"{interval_i}min"
        agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
        optional_last = ["symbolname", "name", "market", "exchange", "source", "price", "vwap", "ma5", "ma25", "ma75", "score", "score_total", "final_score", "display_score", "combined_score", "score_buy", "buy_score", "score_sell", "sell_score", "slope", "slope_atr_scaled", "score_slope", "rsi", "macd", "signal", "hist", "mtf", "score_mtf", "mtf_score", "atr", "atr_1m", "atr_3m", "atr_5m"]
        for col in optional_last:
            if col in one.columns and col not in agg:
                agg[col] = "last"
        for sym, g in one.sort_values("datetime", kind="stable").groupby("symbol", sort=False):
            g = g.drop_duplicates(subset=["datetime"], keep="last").set_index("datetime")
            if g.empty:
                continue
            r = g.resample(rule, label="right", closed="right").agg(agg).dropna(subset=["close"])
            if r.empty:
                continue
            r["symbol"] = str(sym)
            frames.append(r.reset_index())
        if not frames:
            return pd.DataFrame()
        hist = _normalize_summary_df(pd.concat(frames, ignore_index=True, sort=False), interval=interval_i)
        if hist.empty:
            return pd.DataFrame()
        try:
            from trading.summary.pipeline.indicator_pipeline import run_indicator_pipeline
            hist = _normalize_summary_df(run_indicator_pipeline(hist.copy(), interval_i), interval=interval_i)
        except Exception:
            logger.debug("[SUMMARY MTF DIFF 1M PATCH] cached mtf indicator failed interval=%s", interval_i, exc_info=True)
        try:
            from trading.scoring.core.scoring_pipeline import run_scoring_pipeline
            hist = _normalize_summary_df(run_scoring_pipeline(hist.copy(), interval=f"{interval_i}min"), interval=interval_i)
        except Exception:
            logger.debug("[SUMMARY MTF DIFF 1M PATCH] cached mtf scoring failed interval=%s", interval_i, exc_info=True)
        hist = _ensure_basic_score_columns(hist)
        ready = _mtf_is_usable(hist, interval=interval_i, label="resampled_hist_final")
        if "technical_ready" in hist.columns:
            hist["technical_ready"] = bool(ready)
            hist["display_ready"] = bool(ready)
            hist["usable_ready"] = bool(ready)
        if not ready:
            return pd.DataFrame()
        latest = _latest_by_symbol(hist)
        _publish_to_global(interval_i, hist, latest, require_mtf_ready=True)
        logger.warning("[SUMMARY MTF DIFF 1M PATCH] main.py cached 1m -> %sm fallback published hist_rows=%s latest_rows=%s symbols=%s latest_dt=%s score_nonzero=%s volume_nonzero=%s hist_max=%s version=%s", interval_i, len(hist), len(latest), int(latest["symbol"].nunique()) if "symbol" in latest.columns else 0, hist["datetime"].max() if "datetime" in hist.columns else None, _nonzero_count(hist, "score_total"), _nonzero_count(hist, "volume"), _max_hist_per_symbol(hist), VERSION)
        return latest.reset_index(drop=True)
    except Exception:
        logger.exception("[SUMMARY MTF DIFF 1M PATCH] cached 1m -> mtf fallback failed interval=%s", interval_i)
        return pd.DataFrame()


def _log_main_skip(interval: int, rows: int) -> None:
    try:
        import time
        now = time.time()
        last = float(_LAST_MAIN_SKIP_LOG.get(int(interval), 0.0) or 0.0)
        if now - last < 30.0:
            return
        _LAST_MAIN_SKIP_LOG[int(interval)] = now
        logger.warning("[SUMMARY MTF DIFF 1M PATCH] main.py NAS diff_update skipped interval=%s cached_rows=%s reason=entry_only_no_db_save version=%s", interval, rows, VERSION)
    except Exception:
        pass


def _invoke_original_diff_update(self, interval_i: int, *args, **kwargs):
    orig = _ORIG_DIFF_UPDATE
    if not callable(orig):
        return pd.DataFrame()
    try:
        sig = inspect.signature(orig)
        params = sig.parameters
        accepts_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
        if accepts_var_kw:
            return orig(self, interval_i, **kwargs)
        allowed = {k: v for k, v in kwargs.items() if k in params and k not in {"self", "interval"}}
        if allowed:
            return orig(self, interval_i, **allowed)
    except Exception:
        pass
    return orig(self, interval_i)


def _patched_diff_update(self, interval: int, *args, **kwargs):
    interval_i = int(interval)
    try:
        if interval_i == 1:
            out = _invoke_original_diff_update(self, interval_i, *args, **kwargs)
            if isinstance(out, pd.DataFrame) and not out.empty:
                published = _publish_1m_from_any(out, reason="original_diff_update")
                return published if not published.empty else out
            recovered = _publish_1m_from_any(pd.DataFrame(), reason="cached_or_raw_recovery")
            if not recovered.empty:
                return recovered
            return out if isinstance(out, pd.DataFrame) else pd.DataFrame()
        if interval_i in (3, 5) and _main_should_skip_nas_diff_update(interval_i):
            fallback = _resample_cached_1m_to_mtf(interval_i)
            _log_main_skip(interval_i, _safe_len(fallback))
            return fallback
        out = _invoke_original_diff_update(self, interval_i, *args, **kwargs)
        if isinstance(out, pd.DataFrame) and not out.empty and interval_i in (3, 5):
            hist = _normalize_summary_df(out, interval=interval_i)
            _publish_to_global(interval_i, hist, _latest_by_symbol(hist), require_mtf_ready=True)
        return out if isinstance(out, pd.DataFrame) else pd.DataFrame()
    except Exception:
        logger.exception("[SUMMARY MTF DIFF 1M PATCH] diff_update failed interval=%s version=%s", interval_i, VERSION)
        if interval_i in (3, 5):
            fallback = _resample_cached_1m_to_mtf(interval_i)
            if not fallback.empty:
                return fallback
        if interval_i == 1:
            recovered = _publish_1m_from_any(pd.DataFrame(), reason="exception_recovery")
            if not recovered.empty:
                return recovered
        return pd.DataFrame()


def install() -> bool:
    global _INSTALLED, _ORIG_DIFF_UPDATE
    if _INSTALLED:
        return True
    try:
        if not _env_bool("SUMMARY_MTF_DIFF_FROM_1M_ENABLED", True):
            logger.warning("[SUMMARY MTF DIFF 1M PATCH] disabled by env")
            return False
        import trading.summary.summary_controller as sc
        cls = getattr(sc, "SummaryController", None)
        if cls is None:
            logger.warning("[SUMMARY MTF DIFF 1M PATCH] SummaryController unavailable")
            return False
        cur = getattr(cls, "diff_update", None)
        if not callable(cur):
            logger.warning("[SUMMARY MTF DIFF 1M PATCH] diff_update unavailable")
            return False
        if getattr(cur, "_summary_mtf_diff_from_1m_v19", False):
            _patch_memory_latest_publish_history()
            _INSTALLED = True
            return True
        _ORIG_DIFF_UPDATE = getattr(cur, "_original", cur)
        _patched_diff_update._summary_mtf_diff_from_1m_v19 = True  # type: ignore[attr-defined]
        _patched_diff_update._summary_mtf_diff_from_1m_v18 = True  # type: ignore[attr-defined]
        _patched_diff_update._summary_mtf_diff_from_1m_v17 = True  # type: ignore[attr-defined]
        _patched_diff_update._original = _ORIG_DIFF_UPDATE  # type: ignore[attr-defined]
        cls.diff_update = _patched_diff_update
        try:
            inst = getattr(sc, "summary_controller", None)
            if inst is not None:
                setattr(inst.__class__, "diff_update", _patched_diff_update)
        except Exception:
            pass
        bridge_ok = _patch_memory_latest_publish_history()
        _INSTALLED = True
        logger.warning("[SUMMARY MTF DIFF 1M PATCH] installed %s publish_1m=True memory_history_bridge=%s cached_1m_mtf_fallback=True reject_zero_volume=%s min_1m_rows=%s min_mtf_rows=%s main_nas_skip=%s interval1_skip=%s", VERSION, bridge_ok, _env_bool("SUMMARY_MTF_REJECT_ZERO_VOLUME", True), os.getenv("SUMMARY_MTF_FALLBACK_MIN_1M_ROWS", "auto"), os.getenv("SUMMARY_MTF_FALLBACK_MIN_MTF_ROWS", "2"), _main_should_skip_nas_diff_update(3), _main_should_skip_nas_diff_update(1))
        return True
    except Exception:
        logger.exception("[SUMMARY MTF DIFF 1M PATCH] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY MTF DIFF 1M PATCH] auto install failed")


__all__ = ["install", "VERSION"]
