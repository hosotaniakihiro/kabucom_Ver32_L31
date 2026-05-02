# ============================================================
# File   : trading/ranking/postprocess.py
# Version: Ver1.0-RANKING-POSTPROCESS
# ------------------------------------------------------------
# ✔ ranking summary 更新
# ✔ ranking MA 構築
# ✔ entry 候補処理
# ✔ followup pipeline
# ✔ closed-day reuse mode
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
from collections import defaultdict

import pandas as pd

from trading.ranking.job import run_ranking_job
from trading.entry.ai_enricher import enrich_pending_entries_with_ai
from trading.entry.run_entry_pipeline import run_entry_pipeline
from trading.ranking.ranking_ma_builder import build_ranking_ma_1min
from trading.ranking.ranking_trigger import trigger_ranking_entry
from trading.ranking.active_symbol_manager import update_active_symbols
from trading.ranking.ma75_filter import pass_ma75_filter
from trading.ranking.ranking_entry_judge_buy import judge_ranking_entry_buy
from trading.ranking.ranking_entry_judge_sell import judge_ranking_entry_sell
from trading.ranking.ranking_summary_engine import (
    update_ranking_summaries,
    set_ranking_summary_universe,
    get_ranking_summary_status,
)

from .normalizers import safe_len, snapshot_df_to_rows, normalize_snapshot_rows_for_db
from .runtime_state import (
    ensure_global_defaults,
    get_global_data,
    get_existing_snapshot_df_from_global,
    refresh_runtime_symbols_if_needed,
    try_initialize_runtime_universe_once,
    get_runtime_symbol_selector_status_safe,
)

logger = logging.getLogger(__name__)

ensure_global_defaults()
global_data = get_global_data()

RANKING_SUMMARY_ANNOUNCE = True
RANKING_SUMMARY_USE_DISCORD = False
RANKING_SUMMARY_USE_RUNTIME_FILTER = False
RANKING_SUMMARY_REFRESH_RUNTIME_SYMBOLS = False


def get_latest_summary_row(symbol: str, interval: int = 1):
    try:
        latest_map = getattr(global_data, "latest_summary_by_interval", None)
        if latest_map is None:
            return None

        df = latest_map.get(interval)
        if df is None or df.empty:
            return None

        if "symbol" not in df.columns:
            return None

        rows = df[df["symbol"] == symbol]
        return rows.iloc[-1] if not rows.empty else None
    except Exception:
        return None


def update_ranking_summary_cache(snapshot_rows: list[dict]) -> None:
    if not snapshot_rows:
        logger.info("[RANKING SUMMARY] skipped: empty snapshot_rows")
        return

    try:
        try_initialize_runtime_universe_once()

        if RANKING_SUMMARY_REFRESH_RUNTIME_SYMBOLS:
            universe = refresh_runtime_symbols_if_needed(force=True)
            if universe:
                try:
                    set_ranking_summary_universe(universe)
                except Exception:
                    logger.exception("[RANKING SUMMARY] set refreshed universe failed")

        result = update_ranking_summaries(
            snapshot_rows,
            announce=RANKING_SUMMARY_ANNOUNCE,
            use_discord=RANKING_SUMMARY_USE_DISCORD,
            use_runtime_filter=RANKING_SUMMARY_USE_RUNTIME_FILTER,
            refresh_runtime_symbols=RANKING_SUMMARY_REFRESH_RUNTIME_SYMBOLS,
        )

        if isinstance(result, dict):
            df1 = result.get(1)
            df3 = result.get(3)
            df5 = result.get(5)

            status = {}
            try:
                status = get_ranking_summary_status()
            except Exception:
                pass

            logger.info(
                "[RANKING SUMMARY] updated 1m=%d 3m=%d 5m=%d runtime_filter=%s result_type=%s keys=%s",
                safe_len(df1),
                safe_len(df3),
                safe_len(df5),
                RANKING_SUMMARY_USE_RUNTIME_FILTER,
                type(result).__name__,
                sorted(result.keys()),
            )
            try:
                state = dict(getattr(global_data, "ranking_scheduler_state", {}) or {})
                state["ranking_summary_status"] = status
                state["runtime_symbol_selector_status"] = get_runtime_symbol_selector_status_safe()
                state["ranking_summary_result_keys"] = sorted(result.keys())
                state["ranking_summary_rows"] = {
                    "1m": safe_len(df1),
                    "3m": safe_len(df3),
                    "5m": safe_len(df5),
                }
                global_data.ranking_scheduler_state = state
            except Exception:
                logger.exception("[RANKING SUMMARY] save state failed")

    except Exception:
        logger.exception("[RANKING SUMMARY] update failed")


def build_ranking_ma(snapshot_rows: list[dict], now_dt: dt.datetime) -> None:
    if not snapshot_rows:
        logger.info("[RANKING MA] skipped: empty snapshot_rows")
        return

    try:
        build_ranking_ma_1min(snapshot_rows, now=now_dt)
    except Exception:
        logger.exception("[RANKING MA] build failed")


def process_entry_candidates(snapshot_rows: list[dict]) -> None:
    if not snapshot_rows:
        logger.info("[RANKING ENTRY] skipped: empty snapshot_rows")
        return

    by_symbol = defaultdict(list)
    for r in snapshot_rows:
        try:
            sym = r["symbol"]
        except Exception:
            continue
        by_symbol[sym].append(r)

    for sym, rows in by_symbol.items():
        latest = rows[-1]

        summary_row = get_latest_summary_row(sym, interval=1)

        if summary_row is None or (
            isinstance(summary_row, pd.Series) and summary_row.empty
        ):
            continue

        try:
            if not pass_ma75_filter(summary_row):
                continue
        except Exception:
            logger.exception("[RANKING ENTRY] ma75 filter failed symbol=%s", sym)
            continue

        try:
            res_buy = judge_ranking_entry_buy(symbol=sym, summary_row=summary_row)
        except Exception:
            logger.exception("[RANKING BUY] judge failed symbol=%s", sym)
            continue

        if isinstance(res_buy, dict) and res_buy.get("ok"):
            try:
                trigger_ranking_entry(
                    symbol=sym,
                    symbolname=latest.get("symbolname", ""),
                    entry_decision="BUY",
                    trend_score=res_buy.get("score", 0.0),
                    volume_speed=latest.get("volume_speed", 0.0),
                    reason=f"ranking_buy {res_buy.get('reasons')}",
                    market=latest.get("market", "ALL"),
                )
            except Exception:
                logger.exception("[RANKING BUY] trigger failed symbol=%s", sym)
            continue

        try:
            res_sell = judge_ranking_entry_sell(symbol=sym, summary_row=summary_row)
        except Exception:
            logger.exception("[RANKING SELL] judge failed symbol=%s", sym)
            continue

        if isinstance(res_sell, dict) and res_sell.get("ok"):
            try:
                trigger_ranking_entry(
                    symbol=sym,
                    symbolname=latest.get("symbolname", ""),
                    entry_decision="BUY",
                    trend_score=res_sell.get("score", 0.0),
                    volume_speed=latest.get("volume_speed", 0.0),
                    reason=f"ranking_reversal {res_sell.get('reasons')}",
                    market=latest.get("market", "ALL"),
                )
            except Exception:
                logger.exception("[RANKING REVERSAL] trigger failed symbol=%s", sym)


def run_followup_pipelines() -> None:
    try:
        run_ranking_job(global_data)
    except Exception:
        logger.exception("[RANKING JOB] pipeline stage1 failed")

    try:
        enrich_pending_entries_with_ai()
    except Exception:
        logger.exception("[RANKING JOB] AI enrich failed")

    try:
        run_entry_pipeline()
    except Exception:
        logger.exception("[RANKING JOB] entry pipeline failed")

    try:
        update_active_symbols(force=True)
    except Exception:
        logger.exception("[RANKING JOB] active update failed")


def run_closed_day_reuse_mode(started_dt: dt.datetime) -> tuple[int, int]:
    """
    市場時間外の縮退運転:
    - 既存 global snapshot があれば ranking summary を更新
    - APIは叩かない
    - DB再保存はしない
    """
    existing_df = get_existing_snapshot_df_from_global()
    if existing_df.empty:
        logger.info("[RANKING CLOSED DAY] no reusable global snapshot")
        return 0, 0

    snapshot_rows = snapshot_df_to_rows(existing_df)
    snapshot_rows = normalize_snapshot_rows_for_db(snapshot_rows, base_time=started_dt)

    if not snapshot_rows:
        logger.info("[RANKING CLOSED DAY] reusable snapshot rows empty after normalize")
        return 0, 0

    logger.info(
        "[RANKING CLOSED DAY] reuse existing snapshot rows=%d latest=%s",
        len(snapshot_rows),
        existing_df["snapshot_time"].max() if "snapshot_time" in existing_df.columns else None,
    )

    try:
        update_ranking_summary_cache(snapshot_rows)
    except Exception:
        logger.exception("[RANKING CLOSED DAY] summary cache update failed")

    try:
        build_ranking_ma(snapshot_rows, started_dt)
    except Exception:
        logger.exception("[RANKING CLOSED DAY] ranking MA build failed")

    try:
        process_entry_candidates(snapshot_rows)
    except Exception:
        logger.exception("[RANKING CLOSED DAY] entry candidate process failed")

    try:
        run_followup_pipelines()
    except Exception:
        logger.exception("[RANKING CLOSED DAY] followup pipelines failed")

    return 0, len(snapshot_rows)