# ============================================================
# File   : core/startup/mtf_history_bootstrap_runner.py
# Version: REV1.0-MTF-HISTORY-BOOTSTRAP-RUNNER
# ------------------------------------------------------------
# 【概要】
#   起動時 MTF history bootstrap を startup.py から分離
#
# 【主な機能】
#   - run_mtf_history_bootstrap(intervals=(1,3,5))
#   - 結果 compact 化
#   - global_data flags 更新
#
# 【目的】
#   - 1min の rsi / macd / signal / slope / mtf が 0 のままになる問題を改善
#   - 3min / 5min の symbol_hist_len=1 問題を改善
#   - DBスキーマ追加済みの score / ready / indicator 系列をDBへ保存
# ============================================================

from __future__ import annotations

import logging

import pandas as pd

from core.startup.startup_flags import set_mtf_history_bootstrap_flags

logger = logging.getLogger(__name__)


def compact_mtf_history_bootstrap_result(result: dict) -> dict:
    compact = {}

    try:
        for k, v in (result or {}).items():
            interval = int(k)

            if not isinstance(v, pd.DataFrame) or v.empty:
                compact[interval] = {
                    "rows": 0,
                    "symbols": 0,
                    "dt_min": None,
                    "dt_max": None,
                    "score_nonzero": 0,
                    "rsi_nonnull": 0,
                    "macd_nonnull": 0,
                    "signal_nonnull": 0,
                    "slope_nonnull": 0,
                    "mtf_nonnull": 0,
                    "mtf_nonzero": 0,
                    "score_mtf_nonzero": 0,
                    "mtf_score_nonzero": 0,
                    "display_ready": 0,
                    "technical_ready": 0,
                    "hist_min": 0,
                    "hist_median": 0.0,
                    "hist_max": 0,
                }
                continue

            hist_min = 0
            hist_median = 0.0
            hist_max = 0

            try:
                if "symbol_hist_len" in v.columns:
                    hist_s = pd.to_numeric(v["symbol_hist_len"], errors="coerce").dropna()
                    if not hist_s.empty:
                        hist_min = int(hist_s.min())
                        hist_median = float(hist_s.median())
                        hist_max = int(hist_s.max())
                elif "symbol" in v.columns and "datetime" in v.columns:
                    hist_s = (
                        v.assign(datetime=pd.to_datetime(v["datetime"], errors="coerce"))
                        .dropna(subset=["datetime"])
                        .groupby("symbol")["datetime"]
                        .nunique()
                    )
                    if not hist_s.empty:
                        hist_min = int(hist_s.min())
                        hist_median = float(hist_s.median())
                        hist_max = int(hist_s.max())
            except Exception:
                logger.debug("[STARTUP] MTF history hist profile build failed", exc_info=True)

            def _nonzero(col: str) -> int:
                if col not in v.columns:
                    return 0
                return int(pd.to_numeric(v[col], errors="coerce").fillna(0).ne(0).sum())

            def _nonnull(col: str) -> int:
                if col not in v.columns:
                    return 0
                return int(pd.to_numeric(v[col], errors="coerce").notna().sum())

            compact[interval] = {
                "rows": int(len(v)),
                "symbols": int(v["symbol"].nunique()) if "symbol" in v.columns else 0,
                "dt_min": str(v["datetime"].min()) if "datetime" in v.columns else None,
                "dt_max": str(v["datetime"].max()) if "datetime" in v.columns else None,
                "score_nonzero": _nonzero("score"),
                "rsi_nonnull": _nonnull("rsi"),
                "macd_nonnull": _nonnull("macd"),
                "signal_nonnull": _nonnull("signal"),
                "slope_nonnull": _nonnull("slope"),
                "mtf_nonnull": _nonnull("mtf"),
                "mtf_nonzero": _nonzero("mtf"),
                "score_mtf_nonzero": _nonzero("score_mtf"),
                "mtf_score_nonzero": _nonzero("mtf_score"),
                "display_ready": _nonzero("display_ready"),
                "technical_ready": _nonzero("technical_ready"),
                "hist_min": hist_min,
                "hist_median": hist_median,
                "hist_max": hist_max,
            }

    except Exception:
        logger.debug("[STARTUP] MTF history bootstrap compact result build failed", exc_info=True)
        compact = {}

    return compact


def run_mtf_history_bootstrap_safe(*, market_open_now: bool) -> None:
    set_mtf_history_bootstrap_flags(
        started=True,
        done=False,
        failed=False,
        results=None,
    )

    try:
        from trading.summary.recovery.mtf_history_bootstrap import (
            run_mtf_history_bootstrap,
        )

        logger.info(
            "🧱 MTF history bootstrap start intervals=(1,3,5) market_open_now=%s",
            bool(market_open_now),
        )

        result = run_mtf_history_bootstrap(
            intervals=(1, 3, 5),
            max_rows_per_symbol_1m=420,
            lookback_days=3,
            persist=True,
            update_cache=True,
        )

        compact = compact_mtf_history_bootstrap_result(result)

        set_mtf_history_bootstrap_flags(
            started=True,
            done=True,
            failed=False,
            results=compact,
        )

        logger.info("✅ MTF history bootstrap complete results=%s", compact)

    except ModuleNotFoundError:
        set_mtf_history_bootstrap_flags(
            started=True,
            done=False,
            failed=True,
            results={},
        )
        logger.warning(
            "⚠ MTF history bootstrap module not found -> skip. "
            "Create trading/summary/recovery/mtf_history_bootstrap.py first."
        )

    except Exception:
        set_mtf_history_bootstrap_flags(
            started=True,
            done=False,
            failed=True,
            results={},
        )
        logger.exception("❌ MTF history bootstrap failed")


__all__ = [
    "compact_mtf_history_bootstrap_result",
    "run_mtf_history_bootstrap_safe",
]