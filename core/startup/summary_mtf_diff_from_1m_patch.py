# ============================================================
# File   : core/startup/summary_mtf_diff_from_1m_patch.py
# Version: V1.7-MAIN-CACHED-1M-MTF-VOLUME-HISTORY-GUARD
# ------------------------------------------------------------
# 目的:
#   main.py ではNAS SQLite直読み/保存を避け、global_data上の1分足履歴から
#   3m/5mを軽量生成する。ただし latest 1本だけ・volume=0 のMTFを
#   technical_ready=True として公開しない。
#
# V1.7:
#   - 3m/5m fallback は1分足履歴が最低限ある時だけ公開。
#   - volume が全行0のMTFはエントリー用global_contextへ公開しない。
#   - 既存cached MTFも volume=0 / 履歴不足なら使わず、1m再集計を試す。
#   - main.py はDB保存しない方針を維持。
# ============================================================

from __future__ import annotations

import inspect
import logging
import os
import sys
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

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


def _normalize_summary_df(df: pd.DataFrame, *, interval: int) -> pd.DataFrame:
    try:
        from trading.summary.controller_utils import normalize_summary_df
        out = normalize_summary_df(df)
    except Exception:
        out = df.copy() if isinstance(df, pd.DataFrame) else pd.DataFrame()
    if out is None or out.empty:
        return pd.DataFrame()
    try:
        out = out.copy()
        out = out.loc[:, ~pd.Index(out.columns).duplicated()].copy()
        if "symbol" in out.columns:
            out["symbol"] = out["symbol"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
        if "datetime" in out.columns:
            out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
            out = out.dropna(subset=["datetime"])
        out["interval"] = int(interval)
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


def _best_numeric_column(df: pd.DataFrame, candidates: list[str]) -> str | None:
    best_col = None
    best_nonzero = -1
    best_nonnull = -1
    try:
        for col in candidates:
            if col not in df.columns:
                continue
            s = pd.to_numeric(df[col], errors="coerce")
            nonzero = int((s.fillna(0.0).abs() > 0).sum())
            nonnull = int(s.notna().sum())
            if nonzero > best_nonzero or (nonzero == best_nonzero and nonnull > best_nonnull):
                best_col = col
                best_nonzero = nonzero
                best_nonnull = nonnull
    except Exception:
        return best_col
    return best_col


def _ensure_basic_score_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy() if isinstance(df, pd.DataFrame) else pd.DataFrame()
    if out.empty:
        return out
    defaults = {
        "score": 0.0,
        "score_total": 0.0,
        "final_score": 0.0,
        "display_score": 0.0,
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


def _max_hist_per_symbol(df: pd.DataFrame) -> int:
    try:
        if df.empty or "symbol" not in df.columns:
            return 0
        return int(df.groupby("symbol").size().max())
    except Exception:
        return 0


def _min_1m_rows_required(interval: int) -> int:
    # 5m足を1本だけで作るとATR/傾き/scoreが全部0になりやすい。
    # 起動直後は最低でも interval*3 分ぶん、設定があればそれ以上を要求する。
    default = max(int(interval) * 3, 10)
    return max(1, _env_int("SUMMARY_MTF_FALLBACK_MIN_1M_ROWS", default))


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
    if hist_max < min_mtf_rows:
        logger.warning("[SUMMARY MTF DIFF 1M PATCH] reject %s interval=%s reason=short_mtf_history rows=%s hist_max=%s min=%s", label, interval, len(df), hist_max, min_mtf_rows)
        return False
    return True


def _cached_latest_from_global(interval: int) -> pd.DataFrame:
    try:
        from global_state import global_data
        candidates = []
        for name in (
            f"latest_summary_{int(interval)}m_df",
            f"summary_{int(interval)}m_latest_df",
            f"summary_{int(interval)}m_df",
        ):
            try:
                candidates.append(getattr(global_data, name, None))
            except Exception:
                pass
        for method_name in ("get_latest_summary", "get_push_merged_summary", "get_summary_history"):
            try:
                fn = getattr(global_data, method_name, None)
                if callable(fn):
                    candidates.append(fn(int(interval)))
            except Exception:
                pass
        for x in candidates:
            if isinstance(x, pd.DataFrame) and not x.empty:
                df = _normalize_summary_df(x, interval=int(interval))
                if _mtf_is_usable(df, interval=int(interval), label="cached"):
                    return df
    except Exception:
        pass
    return pd.DataFrame()


def _cached_1m_history_from_global() -> pd.DataFrame:
    try:
        from global_state import global_data
        candidates = []
        for name in (
            "summary_1m_df",
            "latest_summary_1m_df",
            "summary_1m_latest_df",
            "push_summary_1m_df",
            "push_merged_summary_1m_df",
        ):
            try:
                candidates.append(getattr(global_data, name, None))
            except Exception:
                pass
        for method_name in ("get_summary_history", "get_push_merged_summary", "get_latest_summary"):
            try:
                fn = getattr(global_data, method_name, None)
                if callable(fn):
                    candidates.append(fn(1))
            except Exception:
                pass
        dfs = []
        for x in candidates:
            if isinstance(x, pd.DataFrame) and not x.empty:
                df = _normalize_summary_df(x, interval=1)
                if not df.empty:
                    dfs.append(df)
        if not dfs:
            return pd.DataFrame()
        out = pd.concat(dfs, ignore_index=True, sort=False)
        out = out.loc[:, ~pd.Index(out.columns).duplicated()].copy()
        if "symbol" in out.columns and "datetime" in out.columns:
            out = out.drop_duplicates(subset=["symbol", "datetime"], keep="last")
            out = out.sort_values(["symbol", "datetime"], kind="stable")
        return out.reset_index(drop=True)
    except Exception:
        logger.exception("[SUMMARY MTF DIFF 1M PATCH] cached 1m history load failed")
        return pd.DataFrame()


def _log_main_skip(interval: int, rows: int) -> None:
    try:
        import time
        now = time.time()
        last = float(_LAST_MAIN_SKIP_LOG.get(int(interval), 0.0) or 0.0)
        if now - last < 30.0:
            return
        _LAST_MAIN_SKIP_LOG[int(interval)] = now
        logger.warning(
            "[SUMMARY MTF DIFF 1M PATCH] main.py NAS diff_update skipped interval=%s cached_rows=%s reason=entry_only_no_db_save",
            interval,
            rows,
        )
    except Exception:
        pass


def _publish_to_global(interval: int, df_hist: pd.DataFrame, df_latest: pd.DataFrame) -> None:
    if not _mtf_is_usable(df_hist, interval=interval, label="publish_hist"):
        logger.warning("[SUMMARY MTF DIFF 1M PATCH] publish skipped interval=%s reason=unusable_hist rows=%s", interval, _safe_len(df_hist))
        return
    try:
        from global_state import global_data
        for method_name, args in (
            ("set_push_merged_summary", (interval, df_hist.copy())),
            ("set_summary_history", (interval, df_hist.copy())),
            ("set_latest_summary", (interval, df_latest.copy())),
        ):
            try:
                fn = getattr(global_data, method_name, None)
                if callable(fn):
                    fn(*args)
            except Exception:
                pass
        for name, value in (
            (f"summary_{interval}m_df", df_hist.copy()),
            (f"latest_summary_{interval}m_df", df_latest.copy()),
            (f"summary_{interval}m_latest_df", df_latest.copy()),
        ):
            try:
                setattr(global_data, name, value)
            except Exception:
                pass
    except Exception:
        logger.exception("[SUMMARY MTF DIFF 1M PATCH] global publish failed interval=%s", interval)


def _resample_cached_1m_to_mtf(interval: int) -> pd.DataFrame:
    interval_i = int(interval)
    if interval_i not in (3, 5):
        return pd.DataFrame()
    one = _cached_1m_history_from_global()
    if one.empty:
        return pd.DataFrame()
    try:
        one = one.copy()
        if "datetime" not in one.columns or "symbol" not in one.columns:
            return pd.DataFrame()
        one["datetime"] = pd.to_datetime(one["datetime"], errors="coerce")
        one = one.dropna(subset=["datetime", "symbol"])
        one["symbol"] = one["symbol"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
        if one.empty:
            return pd.DataFrame()

        min_rows = _min_1m_rows_required(interval_i)
        hist_max_1m = _max_hist_per_symbol(one)
        vol_source = _best_numeric_column(one, ["volume", "vol", "trading_volume", "出来高"])
        if vol_source is None or _nonzero_count(one.rename(columns={vol_source: "volume"}) if vol_source else one, "volume") <= 0:
            logger.warning("[SUMMARY MTF DIFF 1M PATCH] cached 1m -> %sm skipped reason=one_volume_zero rows=%s hist_max_1m=%s", interval_i, len(one), hist_max_1m)
            return pd.DataFrame()
        if hist_max_1m < min_rows:
            logger.warning("[SUMMARY MTF DIFF 1M PATCH] cached 1m -> %sm skipped reason=short_1m_history rows=%s hist_max_1m=%s min=%s", interval_i, len(one), hist_max_1m, min_rows)
            return pd.DataFrame()

        if "close" not in one.columns and "price" in one.columns:
            one["close"] = one["price"]
        for col in ("open", "high", "low"):
            if col not in one.columns:
                one[col] = one.get("close")
        if "close" not in one.columns:
            return pd.DataFrame()
        if vol_source != "volume":
            one["volume"] = pd.to_numeric(one[vol_source], errors="coerce").fillna(0.0)
        for col in ("open", "high", "low", "close", "volume"):
            one[col] = pd.to_numeric(one[col], errors="coerce")
        one = one.dropna(subset=["close"])
        if one.empty:
            return pd.DataFrame()

        frames = []
        rule = f"{interval_i}min"
        agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
        optional_last = [
            "symbolname", "name", "market", "exchange", "source", "price", "vwap", "ma5", "ma25", "ma75",
            "score", "score_total", "final_score", "display_score", "combined_score",
            "score_buy", "buy_score", "score_sell", "sell_score",
            "slope", "slope_atr_scaled", "score_slope", "rsi", "macd", "signal", "hist",
            "mtf", "score_mtf", "mtf_score", "atr", "atr_1m", "atr_3m", "atr_5m",
        ]
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
            r = r.reset_index()
            frames.append(r)
        if not frames:
            return pd.DataFrame()
        hist = pd.concat(frames, ignore_index=True, sort=False)
        hist = _normalize_summary_df(hist, interval=interval_i)
        if hist.empty:
            return pd.DataFrame()
        try:
            from trading.summary.pipeline.indicator_pipeline import run_indicator_pipeline
            hist = run_indicator_pipeline(hist.copy(), interval_i)
            hist = _normalize_summary_df(hist, interval=interval_i)
        except Exception:
            logger.debug("[SUMMARY MTF DIFF 1M PATCH] cached mtf indicator failed interval=%s", interval_i, exc_info=True)
        try:
            from trading.scoring.core.scoring_pipeline import run_scoring_pipeline
            hist = run_scoring_pipeline(hist.copy(), interval=f"{interval_i}min")
            hist = _normalize_summary_df(hist, interval=interval_i)
        except Exception:
            logger.debug("[SUMMARY MTF DIFF 1M PATCH] cached mtf scoring failed interval=%s", interval_i, exc_info=True)
        hist = _ensure_basic_score_columns(hist)
        if "technical_ready" in hist.columns:
            hist["technical_ready"] = _mtf_is_usable(hist, interval=interval_i, label="resampled_hist")
            hist["display_ready"] = hist["technical_ready"]
            hist["usable_ready"] = hist["technical_ready"]
        if not _mtf_is_usable(hist, interval=interval_i, label="resampled_hist_final"):
            return pd.DataFrame()
        latest = hist.sort_values(["symbol", "datetime"], kind="stable").groupby("symbol", as_index=False).tail(1)
        _publish_to_global(interval_i, hist, latest)
        logger.warning(
            "[SUMMARY MTF DIFF 1M PATCH] main.py cached 1m -> %sm fallback published hist_rows=%s latest_rows=%s symbols=%s latest_dt=%s score_nonzero=%s volume_nonzero=%s hist_max=%s",
            interval_i,
            _safe_len(hist),
            _safe_len(latest),
            int(latest["symbol"].nunique()) if "symbol" in latest.columns else 0,
            hist["datetime"].max() if "datetime" in hist.columns else None,
            _nonzero_count(hist, "score_total"),
            _nonzero_count(hist, "volume"),
            _max_hist_per_symbol(hist),
        )
        return latest.reset_index(drop=True)
    except Exception:
        logger.exception("[SUMMARY MTF DIFF 1M PATCH] cached 1m -> mtf fallback failed interval=%s", interval_i)
        return pd.DataFrame()


def _run_diff_from_1m(interval: int) -> pd.DataFrame:
    if int(interval) not in (3, 5):
        return pd.DataFrame()
    if not _env_bool("SUMMARY_MTF_DIFF_FROM_1M_ENABLED", True):
        return pd.DataFrame()
    if _main_should_skip_nas_diff_update(int(interval)):
        cached = _cached_latest_from_global(int(interval))
        if not cached.empty:
            _log_main_skip(int(interval), _safe_len(cached))
            return cached
        fallback = _resample_cached_1m_to_mtf(int(interval))
        _log_main_skip(int(interval), _safe_len(fallback))
        return fallback
    try:
        from trading.summary.pipeline.incremental_mtf_from_1min import build_incremental_mtf_from_1m, extract_diff_rows
        from trading.summary.pipeline.indicator_pipeline import run_indicator_pipeline
        from trading.scoring.core.scoring_pipeline import run_scoring_pipeline
        from trading.summary.persistence.summary_persistence import save_summary
        built = build_incremental_mtf_from_1m(interval)
        if not isinstance(built, dict) or not built.get("ok"):
            logger.info("[SUMMARY MTF DIFF 1M PATCH] no diff interval=%s reason=%s", interval, built.get("reason") if isinstance(built, dict) else None)
            return pd.DataFrame()
        hist_df = _normalize_summary_df(built.get("history_df"), interval=interval)
        diff_seed = _normalize_summary_df(built.get("diff_df"), interval=interval)
        if hist_df.empty or diff_seed.empty:
            return pd.DataFrame()
        df_hist = run_indicator_pipeline(hist_df.copy(), interval)
        df_hist = _normalize_summary_df(df_hist, interval=interval)
        df_hist = run_scoring_pipeline(df_hist, interval=f"{int(interval)}min")
        df_hist = _normalize_summary_df(df_hist, interval=interval)
        diff_rows = extract_diff_rows(df_hist, diff_seed, interval=interval)
        diff_rows = _normalize_summary_df(diff_rows, interval=interval)
        if diff_rows.empty:
            logger.info("[SUMMARY MTF DIFF 1M PATCH] diff rows empty after indicator/scoring interval=%s", interval)
            return pd.DataFrame()
        save_summary(diff_rows, int(interval), lock_timeout_sec=3.0, skip_if_busy=True, caller="summary_mtf_diff_from_1m_patch")
        try:
            latest = diff_rows.sort_values(["symbol", "datetime"], kind="stable").groupby("symbol", as_index=False).tail(1)
        except Exception:
            latest = diff_rows.copy()
        _publish_to_global(int(interval), df_hist, latest)
        logger.warning("[SUMMARY MTF DIFF 1M PATCH] saved interval=%s diff_rows=%s hist_rows=%s", interval, _safe_len(diff_rows), _safe_len(df_hist))
        return latest.reset_index(drop=True)
    except Exception:
        logger.exception("[SUMMARY MTF DIFF 1M PATCH] failed interval=%s", interval)
        return pd.DataFrame()


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


def _call_original_diff_update(self, interval_i: int, *args, **kwargs):
    if _main_should_skip_nas_diff_update(int(interval_i)):
        cached = _cached_latest_from_global(int(interval_i))
        if cached.empty and int(interval_i) in (3, 5):
            cached = _resample_cached_1m_to_mtf(int(interval_i))
        _log_main_skip(int(interval_i), _safe_len(cached))
        return cached
    return _invoke_original_diff_update(self, interval_i, *args, **kwargs)


def _patched_diff_update(self, interval: int, *args, **kwargs):
    interval_i = int(interval)
    precomputed_latest = pd.DataFrame()
    if interval_i in (3, 5):
        precomputed_latest = _run_diff_from_1m(interval_i)
    try:
        out = _call_original_diff_update(self, interval_i, *args, **kwargs)
        if isinstance(out, pd.DataFrame) and not out.empty:
            if interval_i in (3, 5) and not _mtf_is_usable(out, interval=interval_i, label="original_out"):
                if isinstance(precomputed_latest, pd.DataFrame) and not precomputed_latest.empty:
                    return precomputed_latest
                return pd.DataFrame()
            return out
        if isinstance(precomputed_latest, pd.DataFrame) and not precomputed_latest.empty:
            return precomputed_latest
        if interval_i == 1:
            try:
                direct = _invoke_original_diff_update(self, interval_i, *args, **kwargs)
                if isinstance(direct, pd.DataFrame) and not direct.empty:
                    logger.warning("[SUMMARY MTF DIFF 1M PATCH] interval=1 recovered by original diff_update rows=%s", _safe_len(direct))
                    return direct
            except Exception:
                logger.exception("[SUMMARY MTF DIFF 1M PATCH] interval=1 direct original failed")
        return out if isinstance(out, pd.DataFrame) else pd.DataFrame()
    except Exception:
        logger.exception("[SUMMARY MTF DIFF 1M PATCH] original diff_update failed interval=%s", interval_i)
        if isinstance(precomputed_latest, pd.DataFrame) and not precomputed_latest.empty:
            return precomputed_latest
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
        if getattr(cur, "_summary_mtf_diff_from_1m_v17", False):
            _INSTALLED = True
            return True
        _ORIG_DIFF_UPDATE = getattr(cur, "_original", cur)
        _patched_diff_update._summary_mtf_diff_from_1m_v17 = True  # type: ignore[attr-defined]
        _patched_diff_update._summary_mtf_diff_from_1m_v16 = True  # type: ignore[attr-defined]
        _patched_diff_update._original = _ORIG_DIFF_UPDATE  # type: ignore[attr-defined]
        cls.diff_update = _patched_diff_update
        try:
            inst = getattr(sc, "summary_controller", None)
            if inst is not None:
                setattr(inst.__class__, "diff_update", _patched_diff_update)
        except Exception:
            pass
        _INSTALLED = True
        logger.warning(
            "[SUMMARY MTF DIFF 1M PATCH] installed v1.7 cached_1m_mtf_fallback=True reject_zero_volume=%s min_1m_rows=%s min_mtf_rows=%s main_nas_skip=%s interval1_skip=%s",
            _env_bool("SUMMARY_MTF_REJECT_ZERO_VOLUME", True),
            os.getenv("SUMMARY_MTF_FALLBACK_MIN_1M_ROWS", "auto"),
            os.getenv("SUMMARY_MTF_FALLBACK_MIN_MTF_ROWS", "2"),
            _main_should_skip_nas_diff_update(3),
            _main_should_skip_nas_diff_update(1),
        )
        return True
    except Exception:
        logger.exception("[SUMMARY MTF DIFF 1M PATCH] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY MTF DIFF 1M PATCH] auto install failed")


__all__ = ["install"]
