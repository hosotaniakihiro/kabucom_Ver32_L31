# ============================================================
# File   : trading/summary/mtf_diff_from_1m.py
# Version: V1.9-MAIN-MEMORY-1M-HISTORY-BRIDGE
# ------------------------------------------------------------
# 目的:
#   main.py ではNAS SQLite直読み/保存を避ける。
#   ただし entry / Summary-AI が参照する 1分足PUSH summary を
#   global_data / global_context 相当へ必ず公開し、その1分足履歴から
#   3m/5mを軽量生成する。
#
# V1.9:
#   - scheduler_jobs.summary.memory_latest_1m._publish_latest をwrapし、
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

# ------------------------------------------------------------
# 旧 core/startup/summary_mtf_diff_from_1m_patch.py から移設した。
#
# SummaryController.diff_update の差し替えは既に撤去済みで、
# summary_controller.diff_update が _mtf_diff_update_hook() 経由で
# diff_update_with_mtf() を直接呼ぶ「呼び出し側から呼ぶ」方式になっている。
# 他モジュールの関数を差し替える処理は残っておらず
# (global_data への setattr はデータ公開)、install() は
# publish_1m_history_bridge / diff_update_with_mtf の有効判定フラグを
# 立てるだけなので、*_patch.py の名前を外した。
#
# 先に本文化した summary_mtf_push_raw_fallback / tz guard 相当も
# このモジュールに入っている。
# ------------------------------------------------------------
from __future__ import annotations

import inspect
import logging
import threading
import os
import sys
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

VERSION = "V1.9-MAIN-MEMORY-1M-HISTORY-BRIDGE"
_INSTALLED = False
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


def publish_1m_history_bridge(df: pd.DataFrame) -> bool:
    """memory_latest_1m は latest だけを publish するため、history 保存を補う。

    旧構成では install() が memory_latest_1m._publish_latest を差し替えていた。
    差し替えをやめ、memory_latest_1m 側から直接この関数を呼ぶ形にした
    (runner_core の tier1/tier2 と同じ「呼び出し側から呼ぶ」方式)。

    有効条件は旧 install スコープの再現: _INSTALLED が True
    (= SUMMARY_MTF_DIFF_FROM_1M_ENABLED を通り、SummaryController.diff_update の
     差し替えまで到達している) ときだけ history を publish する。
    """
    if not _INSTALLED:
        return False
    try:
        hist = _normalize_summary_df(df, interval=1)
        if hist.empty:
            return False
        latest = _latest_by_symbol(hist)
        _publish_to_global(1, hist, latest, require_mtf_ready=False)
        logger.warning(
            "[SUMMARY MTF DIFF 1M PATCH] memory 1m history bridge published hist_rows=%s latest_rows=%s latest_dt=%s version=%s",
            len(hist),
            len(latest),
            hist["datetime"].max() if "datetime" in hist.columns else None,
            VERSION,
        )
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


def _cached_1m_history_from_global_core() -> pd.DataFrame:
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


def _invoke_original_diff_update(orig, self, interval_i: int, *args, **kwargs):
    """本来の diff_update 実装を、その署名に合わせて呼ぶ。

    旧構成では install 時に退避したモジュールグローバルを見ていたが、
    本文化により呼び出し側から original を受け取る形にした。
    SummaryController.diff_update は (self, interval) しか取らないので、
    display= のような余分な kwarg はここで落とす (旧挙動の維持)。
    """
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


def diff_update_with_mtf(self, interval: int, original, *args, **kwargs):
    """SummaryController.diff_update を MTF ブリッジ込みで実行する。

    旧構成では install() がこの処理で SummaryController.diff_update を
    差し替えていた (クラス属性 + インスタンスの __class__ への setattr の 2 段)。
    差し替えをやめ、summary_controller 側から original を渡して呼ぶ形にした。
    ヘルパ (_repair_mtf_from_1m / _cached_1m_history_from_global 等) は
    このモジュールに残してあるので、summary_mtf_duplicate_datetime_guard_patch /
    summary_mtf_push_raw_fallback_patch のラップは従来どおり効く。
    """
    interval_i = int(interval)
    try:
        if interval_i == 1:
            out = _invoke_original_diff_update(original, self, interval_i, *args, **kwargs)
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
        out = _invoke_original_diff_update(original, self, interval_i, *args, **kwargs)
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
    global _INSTALLED
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
        # SummaryController.diff_update への差し替えは撤去した。
        # summary_controller.diff_update が _mtf_diff_update_hook() 経由で
        # diff_update_with_mtf() を直接呼ぶ (呼び出し側から呼ぶ方式)。
        # ここでの cls / cur のチェックは _INSTALLED のスコープを旧構成と
        # 一致させるために残してある (publish_1m_history_bridge も
        # _INSTALLED で有効判定するため)。
        _INSTALLED = True
        logger.warning("[SUMMARY MTF DIFF 1M PATCH] installed %s publish_1m=True memory_history_bridge=%s cached_1m_mtf_fallback=True reject_zero_volume=%s min_1m_rows=%s min_mtf_rows=%s main_nas_skip=%s interval1_skip=%s", VERSION, True, _env_bool("SUMMARY_MTF_REJECT_ZERO_VOLUME", True), os.getenv("SUMMARY_MTF_FALLBACK_MIN_1M_ROWS", "auto"), os.getenv("SUMMARY_MTF_FALLBACK_MIN_MTF_ROWS", "2"), _main_should_skip_nas_diff_update(3), _main_should_skip_nas_diff_update(1))
        return True
    except Exception:
        logger.exception("[SUMMARY MTF DIFF 1M PATCH] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY MTF DIFF 1M PATCH] auto install failed")


__all__ = ["install", "VERSION", "diff_update_with_mtf", "publish_1m_history_bridge"]


# ============================================================
# PUSH RAW 1m FALLBACK
# (旧 core/startup/summary_mtf_push_raw_fallback_patch.py を本文化)
# ------------------------------------------------------------
# summary の 1m 履歴が薄いとき、PUSH の生データから 1m 履歴を作って
# 合成する。MTF (3m/5m) の生成が「1m 不足」で止まるのを防ぐ。
# ネイティブは _cached_1m_history_from_global_core へ改名した。
#
# 旧パッチはこの属性を包む唯一のパッチだった (競合なし)。
# 有効/無効は旧 install と同じ SUMMARY_MTF_PUSH_RAW_FALLBACK_ENABLED
# (既定 True)。bootstrap ワーカーの env 既定値 4 件も同じ値を入れる。
# ============================================================

_PRF__INSTALLED = False
_PRF__ORIG_CACHED_1M = None
_PRF__BOOTSTRAP_STARTED = False
_PRF_VERSION = "V2-MAIN-NO-MTF-BOOTSTRAP-BY-DEFAULT"


def _prf_env_bool(name: str, default: bool) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "enable", "enabled", "ok"}
    except Exception:
        return bool(default)


def _prf_env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(str(v).strip()))
    except Exception:
        return int(default)


def _prf_env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).strip())
    except Exception:
        return float(default)


def _prf_setdefault_env(name: str, value: str) -> None:
    try:
        cur = os.getenv(name)
        if cur is None or str(cur).strip() == "":
            os.environ[name] = str(value)
            logger.warning("[SUMMARY MTF PUSH RAW FALLBACK] env default set %s=%s version=%s", name, value, _PRF_VERSION)
    except Exception:
        pass


def _prf_is_main_py() -> bool:
    try:
        argv = " ".join(str(x).replace("\\", "/").lower() for x in sys.argv)
        return "main.py" in argv
    except Exception:
        return False


def _prf_to_df(x: Any) -> pd.DataFrame:
    if isinstance(x, pd.DataFrame):
        return x.copy()
    return pd.DataFrame()


def _prf_pick_col(df: pd.DataFrame, names: tuple[str, ...]) -> str | None:
    for name in names:
        if name in df.columns:
            return name
    lower = {str(c).lower(): c for c in df.columns}
    for name in names:
        c = lower.get(name.lower())
        if c is not None:
            return str(c)
    return None


def _prf_normalize_raw_push_df(df: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame()
    try:
        out = df.copy()
        sym_col = _prf_pick_col(out, ("symbol", "Symbol", "code", "Code"))
        dt_col = _prf_pick_col(out, ("datetime", "received_at", "current_price_time", "CurrentPriceTime", "time", "Time"))
        price_col = _prf_pick_col(out, ("close", "price", "current_price", "CurrentPrice", "close_price", "Close"))
        if not sym_col or not dt_col or not price_col:
            return pd.DataFrame()

        ret = pd.DataFrame()
        ret["symbol"] = out[sym_col].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
        ret["datetime"] = pd.to_datetime(out[dt_col], errors="coerce")
        ret["close"] = pd.to_numeric(out[price_col], errors="coerce")
        ret = ret.dropna(subset=["symbol", "datetime", "close"])
        ret = ret[ret["symbol"] != ""].copy()
        if ret.empty:
            return pd.DataFrame()

        for dst, src_names in {
            "open": ("open", "opening_price", "OpenPrice", "open_price"),
            "high": ("high", "high_price", "HighPrice"),
            "low": ("low", "low_price", "LowPrice"),
        }.items():
            src = _prf_pick_col(out, src_names)
            if src:
                ret[dst] = pd.to_numeric(out.loc[ret.index, src], errors="coerce").fillna(ret["close"])
            else:
                ret[dst] = ret["close"]

        vol_col = _prf_pick_col(out, ("volume", "Volume", "vol", "CumVolume"))
        if vol_col:
            ret["volume"] = pd.to_numeric(out.loc[ret.index, vol_col], errors="coerce").fillna(0.0)
        else:
            ret["volume"] = 0.0

        name_col = _prf_pick_col(out, ("symbolname", "SymbolName", "name", "Name"))
        if name_col:
            ret["symbolname"] = out.loc[ret.index, name_col].astype(str)
        else:
            ret["symbolname"] = ret["symbol"]

        ret["price"] = ret["close"]
        ret["current_price"] = ret["close"]
        ret["source"] = "main_push_raw_mtf_fallback"
        ret["interval"] = 1
        limit = _prf_env_int("SUMMARY_MTF_PUSH_RAW_FALLBACK_LIMIT", 5000)
        ret = ret.sort_values(["symbol", "datetime"], kind="stable")
        ret = ret.drop_duplicates(subset=["symbol", "datetime"], keep="last")
        if len(ret) > limit:
            ret = ret.tail(limit)
        return ret.reset_index(drop=True)
    except Exception:
        logger.debug("[SUMMARY MTF PUSH RAW FALLBACK] normalize raw push failed", exc_info=True)
        return pd.DataFrame()


def _prf_raw_push_history_from_global() -> pd.DataFrame:
    try:
        from global_state import global_data
        candidates: list[pd.DataFrame] = []
        for name in (
            "push_df",
            "push_stream_df",
            "raw_push_df",
            "stream_df",
            "current_push_df",
            "push_data_df",
            "latest_push_df",
        ):
            try:
                df = _prf_normalize_raw_push_df(_prf_to_df(getattr(global_data, name, None)))
                if not df.empty:
                    candidates.append(df)
            except Exception:
                pass
        for method_name in ("get_push_df", "get_raw_push_df", "get_push_stream_df"):
            try:
                fn = getattr(global_data, method_name, None)
                if callable(fn):
                    df = _prf_normalize_raw_push_df(_prf_to_df(fn()))
                    if not df.empty:
                        candidates.append(df)
            except Exception:
                pass
        if not candidates:
            return pd.DataFrame()
        out = pd.concat(candidates, ignore_index=True, sort=False)
        out = out.drop_duplicates(subset=["symbol", "datetime"], keep="last")
        out = out.sort_values(["symbol", "datetime"], kind="stable")
        logger.warning(
            "[SUMMARY MTF PUSH RAW FALLBACK] raw push history rows=%s symbols=%s latest_dt=%s version=%s",
            len(out),
            int(out["symbol"].nunique()) if "symbol" in out.columns else 0,
            out["datetime"].max() if "datetime" in out.columns and not out.empty else None,
            _PRF_VERSION,
        )
        return out.reset_index(drop=True)
    except Exception:
        logger.exception("[SUMMARY MTF PUSH RAW FALLBACK] raw push history load failed")
        return pd.DataFrame()


def _prf_bootstrap_enabled() -> bool:
    # V2: main.py の tick timeout を防ぐため、3m/5m bootstrap resample は既定停止。
    return _prf_env_bool("SUMMARY_MTF_PUSH_RAW_BOOTSTRAP_ENABLED", False)


def _prf_bootstrap_worker() -> None:
    try:
        if not _prf_bootstrap_enabled():
            logger.warning("[SUMMARY MTF PUSH RAW FALLBACK] bootstrap disabled version=%s", _PRF_VERSION)
            return
        if not _prf_is_main_py() and not _prf_env_bool("SUMMARY_MTF_PUSH_RAW_BOOTSTRAP_FORCE", False):
            return
        # 旧 raw fallback パッチはこのモジュールを別 import して
        # _resample_cached_1m_to_mtf を getattr していたが、本文化で同居したので直呼び。
        repeats = max(1, _prf_env_int("SUMMARY_MTF_PUSH_RAW_BOOTSTRAP_REPEATS", 1))
        delay = max(2.0, _prf_env_float("SUMMARY_MTF_PUSH_RAW_BOOTSTRAP_DELAY_SEC", 15.0))
        gap = max(10.0, _prf_env_float("SUMMARY_MTF_PUSH_RAW_BOOTSTRAP_GAP_SEC", 30.0))
        for i in range(repeats):
            time.sleep(delay if i == 0 else gap)
            for interval in (3, 5):
                try:
                    df = _resample_cached_1m_to_mtf(interval)
                    logger.warning(
                        "[SUMMARY MTF PUSH RAW FALLBACK] bootstrap resample interval=%s rows=%s i=%s/%s version=%s",
                        interval,
                        len(df) if isinstance(df, pd.DataFrame) else 0,
                        i + 1,
                        repeats,
                        _PRF_VERSION,
                    )
                except Exception:
                    logger.debug("[SUMMARY MTF PUSH RAW FALLBACK] bootstrap resample failed interval=%s", interval, exc_info=True)
    except Exception:
        logger.exception("[SUMMARY MTF PUSH RAW FALLBACK] bootstrap worker failed")


def _prf_start_bootstrap() -> None:
    global _PRF__BOOTSTRAP_STARTED
    if _PRF__BOOTSTRAP_STARTED:
        return
    if not _prf_bootstrap_enabled():
        logger.warning("[SUMMARY MTF PUSH RAW FALLBACK] bootstrap not started disabled version=%s", _PRF_VERSION)
        return
    _PRF__BOOTSTRAP_STARTED = True
    threading.Thread(target=_prf_bootstrap_worker, name="SummaryMtfPushRawFallbackBootstrap", daemon=True).start()


def _prf_apply_env_defaults() -> None:
    _prf_setdefault_env("SUMMARY_MTF_PUSH_RAW_BOOTSTRAP_ENABLED", "0")
    _prf_setdefault_env("SUMMARY_MTF_PUSH_RAW_BOOTSTRAP_REPEATS", "1")
    _prf_setdefault_env("SUMMARY_MTF_PUSH_RAW_BOOTSTRAP_DELAY_SEC", "15")
    _prf_setdefault_env("SUMMARY_MTF_PUSH_RAW_BOOTSTRAP_GAP_SEC", "30")


def _cached_1m_history_from_global() -> pd.DataFrame:
    if not _env_bool("SUMMARY_MTF_PUSH_RAW_FALLBACK_ENABLED", True):
        return _cached_1m_history_from_global_core()
    dfs: list[pd.DataFrame] = []
    try:
        old = _cached_1m_history_from_global_core()
        if isinstance(old, pd.DataFrame) and not old.empty:
            dfs.append(old.copy())
    except Exception:
        logger.debug("[SUMMARY MTF PUSH RAW FALLBACK] original cached 1m failed", exc_info=True)
    raw = _prf_raw_push_history_from_global()
    if isinstance(raw, pd.DataFrame) and not raw.empty:
        dfs.append(raw)
    if not dfs:
        return pd.DataFrame()
    out = pd.concat(dfs, ignore_index=True, sort=False)
    if "datetime" in out.columns:
        out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
        out = out.dropna(subset=["datetime"])
    if "symbol" in out.columns and "datetime" in out.columns:
        out["symbol"] = out["symbol"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
        out = out[out["symbol"] != ""]
        out = out.drop_duplicates(subset=["symbol", "datetime"], keep="last")
        out = out.sort_values(["symbol", "datetime"], kind="stable")
    return out.reset_index(drop=True)


_prf_apply_env_defaults()
if _env_bool("SUMMARY_MTF_PUSH_RAW_FALLBACK_ENABLED", True):
    _prf_start_bootstrap()
