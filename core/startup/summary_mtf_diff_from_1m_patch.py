# ============================================================
# File   : core/startup/summary_mtf_diff_from_1m_patch.py
# Version: V1.5-MAIN-CACHED-1M-MTF-SCORE-REPAIR
# ------------------------------------------------------------
# 目的:
#   3分足/5分足のサマリー更新時に、既存3m/5m最新時刻以降の1分足をDBから読み、
#   MA75計算用の直前履歴込みで差分3m/5mサマリーを作成・保存する。
#
# V1.2:
#   - main.py 実行中は、NAS SQLite 直読みを伴う original diff_update / 1m差分生成を
#     既定でスキップする。main_database.py がDB生成・DB更新を担当する split 運用では、
#     main.py がNAS上DBを直接読みに行くと Windows 0xC0000006(in-page error) で
#     プロセスごと落ちることがあるため。
#   - スキップ時は global_data の既存 summary cache があれば返し、無ければ空DFで返す。
#   - AUTOSTOCK_MAIN_ALLOW_NAS_SUMMARY_DIFF_UPDATE=1 で旧動作に戻せる。
#
# V1.3:
#   - interval=1 は main.py エントリー判定の主入力なので、NAS差分スキップで空DFを返さない。
#     1分足は既存 SummaryController.diff_update / PUSH fallback 側に通す。
#
# V1.4:
#   - main.py で3m/5m NAS差分更新をスキップした場合、既存3m/5m cache が空なら
#     global_data上の1m PUSH履歴から軽量に3m/5m OHLCを再生成して publish する。
#
# V1.5:
#   - 1m cache に volume=0 列と trading_volume 等の非ゼロ列が混在する場合、非ゼロ列を優先する。
#   - 3m/5m fallback の indicator/scoring が hist不足で score=0 になった場合、1m側の最新スコアを
#     interval bucket単位で補完し、Summary-AI候補0件を防ぐ。
#   - score_total / score_buy / score_sell / slope / rsi / macd / mtf / technical_ready を補修する。
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
    """main.py では NAS SQLite 直読みサマリー更新を既定で止める。

    interval=1 はエントリー判定の主入力であり、ここで空DFを返すと
    Summary-AI/Tonosama が候補0件になる。1分足は既存controller/fallbackに任せる。
    """
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
                return _normalize_summary_df(x, interval=int(interval))
    except Exception:
        pass
    return pd.DataFrame()


def _cached_1m_history_from_global() -> pd.DataFrame:
    """NASを読まず、global_dataに残っている1m PUSH履歴だけを集める。"""
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
            "[SUMMARY MTF DIFF 1M PATCH] main.py NAS diff_update skipped interval=%s cached_rows=%s reason=avoid_windows_0xc0000006 allow_env=AUTOSTOCK_MAIN_ALLOW_NAS_SUMMARY_DIFF_UPDATE",
            interval,
            rows,
        )
    except Exception:
        pass


def _normalize_summary_df(df: pd.DataFrame, *, interval: int) -> pd.DataFrame:
    try:
        from trading.summary.controller_utils import normalize_summary_df
        out = normalize_summary_df(df)
    except Exception:
        out = df.copy() if isinstance(df, pd.DataFrame) else pd.DataFrame()
    if out is None or out.empty:
        return pd.DataFrame()
    try:
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
    """候補列のうち、非ゼロ値が最も多い列を選ぶ。"""
    try:
        best_col = None
        best_nonzero = -1
        best_nonnull = -1
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
        return best_col
    except Exception:
        return None


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
        "technical_ready": True,
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


def _repair_mtf_from_1m(hist: pd.DataFrame, one: pd.DataFrame, *, interval: int) -> pd.DataFrame:
    """3m/5m fallback が hist不足でゼロスコア化した時、1mの最新値から補修する。"""
    out = hist.copy() if isinstance(hist, pd.DataFrame) else pd.DataFrame()
    if out.empty or one is None or one.empty:
        return out
    try:
        if "symbol" not in out.columns or "datetime" not in out.columns or "symbol" not in one.columns or "datetime" not in one.columns:
            return out

        interval_i = int(interval)
        one2 = one.copy()
        one2["datetime"] = pd.to_datetime(one2["datetime"], errors="coerce")
        one2 = one2.dropna(subset=["datetime", "symbol"])
        if one2.empty:
            return out

        out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
        out = out.dropna(subset=["datetime", "symbol"])
        if out.empty:
            return out

        # 1mを3m/5m bucket終端に合わせて、bucket内の最新1m値を補完元にする。
        origin = "start_day"
        one2["_bucket_dt"] = one2["datetime"].dt.floor(f"{interval_i}min") + pd.Timedelta(minutes=interval_i)
        if "datetime" in one2.columns:
            one2 = one2.sort_values(["symbol", "_bucket_dt", "datetime"], kind="stable")
        latest_1m = one2.groupby(["symbol", "_bucket_dt"], as_index=False).tail(1).copy()
        latest_1m = latest_1m.rename(columns={"_bucket_dt": "datetime"})

        repair_cols = [
            "score", "score_total", "final_score", "display_score", "combined_score",
            "score_buy", "buy_score", "score_sell", "sell_score",
            "slope", "slope_atr_scaled", "score_slope",
            "rsi", "macd", "signal", "hist",
            "mtf", "score_mtf", "mtf_score",
            "atr", "atr_1m", "atr_3m", "atr_5m",
            "technical_ready", "display_ready", "usable_ready",
        ]
        repair_cols = [c for c in repair_cols if c in latest_1m.columns]
        if not repair_cols:
            return out

        src = latest_1m[["symbol", "datetime"] + repair_cols].copy()
        merged = out.merge(src, on=["symbol", "datetime"], how="left", suffixes=("", "_1mrepair"))

        repaired_cols: dict[str, int] = {}
        for col in repair_cols:
            rcol = f"{col}_1mrepair"
            if rcol not in merged.columns:
                continue
            before_nonzero = _nonzero_count(merged, col) if col in merged.columns else 0
            if col not in merged.columns:
                merged[col] = merged[rcol]
            else:
                if col in {"technical_ready", "display_ready", "usable_ready"}:
                    mask = merged[col].isna() | (merged[col].astype(str).str.lower().isin({"false", "0", "nan", "none", ""}))
                    merged.loc[mask, col] = merged.loc[mask, rcol]
                else:
                    cur = pd.to_numeric(merged[col], errors="coerce")
                    rep = pd.to_numeric(merged[rcol], errors="coerce")
                    mask = (cur.isna() | (cur.fillna(0.0).abs() == 0.0)) & rep.notna() & (rep.fillna(0.0).abs() > 0.0)
                    merged.loc[mask, col] = rep[mask]
            after_nonzero = _nonzero_count(merged, col)
            if after_nonzero > before_nonzero:
                repaired_cols[col] = after_nonzero - before_nonzero
            merged = merged.drop(columns=[rcol], errors="ignore")

        # score aliasesの整合
        if "score_total" in merged.columns:
            for alias in ("score", "final_score", "display_score", "combined_score"):
                if alias not in merged.columns or _nonzero_count(merged, alias) == 0:
                    merged[alias] = merged["score_total"]
        if "score_buy" in merged.columns and ("buy_score" not in merged.columns or _nonzero_count(merged, "buy_score") == 0):
            merged["buy_score"] = merged["score_buy"]
        if "score_sell" in merged.columns and ("sell_score" not in merged.columns or _nonzero_count(merged, "sell_score") == 0):
            merged["sell_score"] = merged["score_sell"]

        # 出来高補修: resample済みvolumeが全0なら、1mの非ゼロvolume合計で上書きする。
        vol_source = _best_numeric_column(one2, ["volume", "vol", "trading_volume", "出来高"])
        if vol_source is not None:
            vol_sum = one2.groupby(["symbol", "_bucket_dt"], as_index=False)[vol_source].sum().rename(columns={"_bucket_dt": "datetime", vol_source: "_bucket_volume_repair"})
            merged = merged.merge(vol_sum, on=["symbol", "datetime"], how="left")
            cur_vol = _numeric_series(merged, "volume", 0.0)
            rep_vol = pd.to_numeric(merged["_bucket_volume_repair"], errors="coerce")
            mask = (cur_vol.abs() == 0.0) & rep_vol.notna() & (rep_vol.fillna(0.0).abs() > 0.0)
            if "volume" not in merged.columns:
                merged["volume"] = 0.0
            merged.loc[mask, "volume"] = rep_vol[mask]
            merged = merged.drop(columns=["_bucket_volume_repair"], errors="ignore")

        if repaired_cols or _nonzero_count(out, "volume") == 0 < _nonzero_count(merged, "volume"):
            logger.warning(
                "[SUMMARY MTF DIFF 1M PATCH] mtf repaired from 1m interval=%s rows=%s repaired_cols=%s score_nonzero=%s volume_nonzero=%s",
                interval_i,
                len(merged),
                repaired_cols,
                _nonzero_count(merged, "score_total"),
                _nonzero_count(merged, "volume"),
            )

        return merged.reset_index(drop=True)
    except Exception:
        logger.exception("[SUMMARY MTF DIFF 1M PATCH] mtf repair from 1m failed interval=%s", interval)
        return out


def _publish_to_global(interval: int, df_hist: pd.DataFrame, df_latest: pd.DataFrame) -> None:
    try:
        from global_state import global_data
        try:
            global_data.set_push_merged_summary(interval, df_hist.copy())
        except Exception:
            logger.debug("[SUMMARY MTF DIFF 1M PATCH] set_push_merged_summary unavailable interval=%s", interval, exc_info=True)
        try:
            setattr(global_data, f"summary_{interval}m_df", df_hist.copy())
        except Exception:
            pass
        try:
            setattr(global_data, f"latest_summary_{interval}m_df", df_latest.copy())
        except Exception:
            pass
        try:
            setattr(global_data, f"summary_{interval}m_latest_df", df_latest.copy())
        except Exception:
            pass
        try:
            setter = getattr(global_data, "set_summary_history", None)
            if callable(setter):
                setter(interval, df_hist.copy())
        except Exception:
            pass
        try:
            setter = getattr(global_data, "set_latest_summary", None)
            if callable(setter):
                setter(interval, df_latest.copy())
        except Exception:
            pass
    except Exception:
        logger.exception("[SUMMARY MTF DIFF 1M PATCH] global publish failed interval=%s", interval)


def _resample_cached_1m_to_mtf(interval: int) -> pd.DataFrame:
    """main.py用: 1m cacheから3m/5mを軽量生成。DB保存はしない。"""
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
        if one.empty:
            return pd.DataFrame()

        # 必須OHLCの補完。PUSH由来はclose/priceだけのことがある。
        if "close" not in one.columns and "price" in one.columns:
            one["close"] = one["price"]
        for col in ("open", "high", "low"):
            if col not in one.columns:
                one[col] = one.get("close")
        if "close" not in one.columns:
            return pd.DataFrame()

        # volume列が存在しても全0のことがあるため、非ゼロが多い列を採用する。
        vol_col = _best_numeric_column(one, ["volume", "vol", "trading_volume", "出来高"])
        if vol_col is None:
            one["volume"] = 0.0
            vol_col = "volume"
        elif vol_col != "volume":
            one["volume"] = pd.to_numeric(one[vol_col], errors="coerce").fillna(0.0)
            vol_col = "volume"

        for col in ("open", "high", "low", "close", vol_col):
            one[col] = pd.to_numeric(one[col], errors="coerce")
        one = one.dropna(subset=["close"])
        if one.empty:
            return pd.DataFrame()

        frames = []
        rule = f"{interval_i}min"
        agg = {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            vol_col: "sum",
        }
        optional_last = [
            "symbolname", "name", "market", "exchange", "source",
            "price", "vwap", "ma5", "ma25", "ma75",
            "score", "score_total", "final_score", "display_score", "combined_score",
            "score_buy", "buy_score", "score_sell", "sell_score",
            "slope", "slope_atr_scaled", "score_slope",
            "rsi", "macd", "signal", "hist",
            "mtf", "score_mtf", "mtf_score",
            "atr", "atr_1m", "atr_3m", "atr_5m",
            "technical_ready", "display_ready", "usable_ready",
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
            if vol_col != "volume" and vol_col in r.columns and "volume" not in r.columns:
                r["volume"] = r[vol_col]
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
        hist = _repair_mtf_from_1m(hist, one, interval=interval_i)
        hist = _ensure_basic_score_columns(hist)

        latest = hist.sort_values(["symbol", "datetime"], kind="stable").groupby("symbol", as_index=False).tail(1)
        _publish_to_global(interval_i, hist, latest)
        logger.warning(
            "[SUMMARY MTF DIFF 1M PATCH] main.py cached 1m -> %sm fallback published hist_rows=%s latest_rows=%s symbols=%s latest_dt=%s score_nonzero=%s volume_nonzero=%s",
            interval_i,
            _safe_len(hist),
            _safe_len(latest),
            int(latest["symbol"].nunique()) if "symbol" in latest.columns else 0,
            hist["datetime"].max() if "datetime" in hist.columns else None,
            _nonzero_count(hist, "score_total"),
            _nonzero_count(hist, "volume"),
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
        from trading.summary.pipeline.incremental_mtf_from_1min import (
            build_incremental_mtf_from_1m,
            extract_diff_rows,
        )
        from trading.summary.pipeline.indicator_pipeline import run_indicator_pipeline
        from trading.scoring.core.scoring_pipeline import run_scoring_pipeline
        from trading.summary.persistence.summary_persistence import save_summary

        built = build_incremental_mtf_from_1m(interval)
        if not isinstance(built, dict) or not built.get("ok"):
            logger.info(
                "[SUMMARY MTF DIFF 1M PATCH] no diff interval=%s reason=%s one_raw_rows=%s latest_dt=%s",
                interval,
                built.get("reason") if isinstance(built, dict) else None,
                built.get("one_raw_rows") if isinstance(built, dict) else None,
                built.get("latest_dt") if isinstance(built, dict) else None,
            )
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

        save_summary(
            diff_rows,
            int(interval),
            lock_timeout_sec=3.0,
            skip_if_busy=True,
            caller="summary_mtf_diff_from_1m_patch",
        )

        try:
            latest = diff_rows.sort_values(["symbol", "datetime"], kind="stable").groupby("symbol", as_index=False).tail(1)
        except Exception:
            latest = diff_rows.copy()
        _publish_to_global(int(interval), df_hist, latest)

        logger.warning(
            "[SUMMARY MTF DIFF 1M PATCH] saved interval=%s diff_rows=%s diff_symbols=%s hist_rows=%s latest_dt=%s path=%s",
            interval,
            _safe_len(diff_rows),
            int(diff_rows["symbol"].nunique()) if "symbol" in diff_rows.columns else 0,
            _safe_len(df_hist),
            built.get("latest_dt"),
            built.get("path"),
        )
        return latest.reset_index(drop=True)
    except Exception:
        logger.exception("[SUMMARY MTF DIFF 1M PATCH] failed interval=%s", interval)
        return pd.DataFrame()


def _invoke_original_diff_update(self, interval_i: int, *args, **kwargs):
    """既存 diff_update(interval) 互換。scheduler由来の now/display/run_entry 等は渡さない。"""
    orig = _ORIG_DIFF_UPDATE
    if not callable(orig):
        return pd.DataFrame()
    if args:
        logger.debug("[SUMMARY MTF DIFF 1M PATCH] ignored original positional extras interval=%s args=%s", interval_i, len(args))
    if kwargs:
        logger.debug("[SUMMARY MTF DIFF 1M PATCH] ignored original kwargs interval=%s keys=%s", interval_i, sorted(kwargs.keys()))

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
            return out
        if isinstance(precomputed_latest, pd.DataFrame) and not precomputed_latest.empty:
            return precomputed_latest

        if interval_i == 1:
            try:
                direct = _invoke_original_diff_update(self, interval_i, *args, **kwargs)
                if isinstance(direct, pd.DataFrame) and not direct.empty:
                    logger.warning(
                        "[SUMMARY MTF DIFF 1M PATCH] interval=1 recovered by original diff_update rows=%s",
                        _safe_len(direct),
                    )
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
        if getattr(cur, "_summary_mtf_diff_from_1m_v15", False):
            _INSTALLED = True
            return True

        _ORIG_DIFF_UPDATE = getattr(cur, "_original", cur)
        _patched_diff_update._summary_mtf_diff_from_1m_v15 = True  # type: ignore[attr-defined]
        _patched_diff_update._summary_mtf_diff_from_1m_v14 = True  # type: ignore[attr-defined]
        _patched_diff_update._summary_mtf_diff_from_1m_v13 = True  # type: ignore[attr-defined]
        _patched_diff_update._summary_mtf_diff_from_1m_v12 = True  # type: ignore[attr-defined]
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
            "[SUMMARY MTF DIFF 1M PATCH] installed v1.5 enabled=True cached_1m_mtf_fallback=True score_repair=True history_rows=%s allow_partial=%s main_nas_skip=%s interval1_skip=%s",
            os.getenv("SUMMARY_MTF_DIFF_HISTORY_ROWS", "74"),
            os.getenv("SUMMARY_MTF_DIFF_ALLOW_PARTIAL_BAR", "0"),
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
