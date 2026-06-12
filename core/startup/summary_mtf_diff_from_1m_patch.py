# ============================================================
# File   : core/startup/summary_mtf_diff_from_1m_patch.py
# Version: V1.3-MAIN-1M-ENTRY-FEED-NOT-SKIPPED
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
#   - これにより、ログの
#       main.py NAS diff_update skipped interval=1 cached_rows=0
#       runner returned empty PUSH interval=1
#     から Summary-AI / Tonosama の候補が消える問題を防ぐ。
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


def _run_diff_from_1m(interval: int) -> pd.DataFrame:
    if int(interval) not in (3, 5):
        return pd.DataFrame()
    if not _env_bool("SUMMARY_MTF_DIFF_FROM_1M_ENABLED", True):
        return pd.DataFrame()
    if _main_should_skip_nas_diff_update(int(interval)):
        cached = _cached_latest_from_global(int(interval))
        _log_main_skip(int(interval), _safe_len(cached))
        return cached

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

    # 現行 SummaryController.diff_update は self, interval のみ。
    # 将来kwargs対応になった場合だけ、受け取れるkwargsを限定して渡す。
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

        # V1.3 safety: interval=1 must not be killed by an empty cache. If any older wrapper or
        # transient cache returned empty, try the original once more directly so PUSH fallback can run.
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
        if getattr(cur, "_summary_mtf_diff_from_1m_v13", False):
            _INSTALLED = True
            return True

        # v1/v1.1/v1.2 wrapper が既に入っている場合は、そのoriginalを拾って二重kwargs不具合を回避する。
        _ORIG_DIFF_UPDATE = getattr(cur, "_original", cur)
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
            "[SUMMARY MTF DIFF 1M PATCH] installed v1.3 enabled=True history_rows=%s allow_partial=%s main_nas_skip=%s interval1_skip=%s",
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
