# ==========================================================
# File   : trading/summary/summary_controller.py
# Version: Ver37.7.2-PRODUCTION-HARDENED-SUMMARY-CONTROLLER
#          -LATEST-ONLY-POLLUTION-BLOCK
#          -MERGED-HISTORY-SEPARATION
#          -REASON-COLUMNS-PRESERVE
#          -AI-COLUMNS-PRESERVE
#          -DISPLAY-STABLE-FALLBACK
#          -TECHNICAL-COLUMNS-PRESERVE
#          -MA-MTF-BREAKDOWN-PRESERVE
#          -MAIN-ENTRY-ONLY-SKIP-DB-SAVE
#          -HISTORY-PAYLOAD-DEDUPE-FIX
#          -HISTORY-PAYLOAD-LIMIT-SIGNATURE-FIX
# ----------------------------------------------------------
# ✔ main.py のサマリー計算結果はエントリー判定専用として利用可能
# ✔ SUMMARY_SKIP_DB_SAVE_IN_MAIN=1 のとき summary DB 保存だけをスキップ
# ✔ DB保存スキップ後も cache / ranking / AI / entry は継続
# ✔ Ver37.7.1: dedupe_symbol_datetime(out, normalize_summary_df) に修正
# ✔ Ver37.7.2: limit_history_rows_per_symbol(out, interval, normalize_summary_df) に修正
# ==========================================================

from __future__ import annotations

import os
import numpy as np
import datetime as dt
import logging
import threading
from typing import Any, Callable, Optional

import pandas as pd

from global_state import global_data

from trading.logger.timeframe_logger import log_tf_close
from trading.ranking.ranking_pipeline import run_ranking_pipeline
from trading.summary.controller_cache import (
    choose_merged_cache_payload,
    dedupe_symbol_datetime,
    limit_history_rows_per_symbol,
    merge_history_sources,
    safe_global_get_merged_summary,
    safe_global_set_history,
    safe_global_set_latest,
    safe_global_set_merged_summary,
    should_overwrite_merged_summary,
)
from trading.summary.controller_projection import (
    attach_history_len,
    latest_row_per_symbol,
    latest_row_per_symbol_mature_first,
    log_df_state,
    log_scoring_probe,
    rebuild_technical_ready,
)
from trading.summary.controller_utils import normalize_summary_df
from trading.summary.engine.summary_engine import run_summary_engine
from trading.summary.notify.discord_notifier import notify_entry_signals
from trading.summary.notify.tonosama_notifier import notify_tonosama
from trading.summary.persistence.summary_persistence import save_summary
from trading.summary.pipeline.ai_pipeline import run_ai_pipeline
from trading.summary.pipeline.entry_pipeline import run_entry_pipeline
from trading.summary.pipeline.fetch_pipeline import run_fetch_pipeline
from trading.summary.pipeline.indicator_pipeline import run_indicator_pipeline
from trading.summary.summary_logger_bridge import (
    log_summary_ranking_bridge,
    run_summary_loggers,
)

logger = logging.getLogger(__name__)

try:
    from trading.scoring.config.flag_label_map import build_reason_text_from_row
except Exception:
    build_reason_text_from_row = None


def _env_bool(name: str, default: bool = False) -> bool:
    try:
        v = os.environ.get(name)
        if v is None:
            return bool(default)
        return str(v).strip().lower() in ("1", "true", "yes", "on", "y")
    except Exception:
        return bool(default)


def _skip_db_save_for_entry_only_main() -> bool:
    if _env_bool("SUMMARY_SKIP_DB_SAVE_IN_MAIN", False):
        return True
    if _env_bool("SUMMARY_MAIN_ENTRY_ONLY", False):
        return True
    role = str(os.environ.get("SUMMARY_DB_WRITER_ROLE") or "").strip().lower()
    return role in ("entry_only", "main_entry_only", "read_only", "no_save")


_SUMMARY_INFLIGHT_GUARD = threading.Lock()
_SUMMARY_INFLIGHT: dict[int, dict[str, object]] = {}


def _thread_ident() -> int:
    try:
        return threading.get_ident()
    except Exception:
        return -1


def _thread_name() -> str:
    try:
        return threading.current_thread().name
    except Exception:
        return "unknown"


def _enter_interval(interval: int) -> bool:
    interval = int(interval)
    now = dt.datetime.now()

    with _SUMMARY_INFLIGHT_GUARD:
        meta = _SUMMARY_INFLIGHT.get(interval)
        if meta and bool(meta.get("running", False)):
            started_at = meta.get("started_at")
            tid = meta.get("tid")
            tname = meta.get("thread")
            held_sec = -1.0
            try:
                if isinstance(started_at, dt.datetime):
                    held_sec = max(0.0, (now - started_at).total_seconds())
            except Exception:
                pass
            logger.warning(
                "[summary_controller] duplicate diff_update skipped interval=%s waiter_tid=%s waiter_thread=%s holder_tid=%s holder_thread=%s holder_held=%.3fs",
                interval, _thread_ident(), _thread_name(), tid, tname, held_sec,
            )
            return False

        _SUMMARY_INFLIGHT[interval] = {
            "running": True,
            "started_at": now,
            "tid": _thread_ident(),
            "thread": _thread_name(),
        }
        return True


def _leave_interval(interval: int) -> None:
    interval = int(interval)
    with _SUMMARY_INFLIGHT_GUARD:
        _SUMMARY_INFLIGHT[interval] = {"running": False, "started_at": None, "tid": None, "thread": None}


_DISPLAY_PUSH_FN: Optional[Callable] = None
_DISPLAY_RANKING_FN: Optional[Callable] = None
_DISPLAY_RESOLVED = False


def _resolve_display_functions_once() -> tuple[Optional[Callable], Optional[Callable]]:
    global _DISPLAY_PUSH_FN, _DISPLAY_RANKING_FN, _DISPLAY_RESOLVED
    if _DISPLAY_RESOLVED:
        return _DISPLAY_PUSH_FN, _DISPLAY_RANKING_FN
    _DISPLAY_RESOLVED = True
    try:
        from scheduler_jobs.summary.dependencies import resolve_display_functions
        push_fn, ranking_fn = resolve_display_functions()
        _DISPLAY_PUSH_FN = push_fn if callable(push_fn) else None
        _DISPLAY_RANKING_FN = ranking_fn if callable(ranking_fn) else None
        logger.info("[summary_controller] display resolvers push=%s ranking=%s", getattr(_DISPLAY_PUSH_FN, "__name__", None), getattr(_DISPLAY_RANKING_FN, "__name__", None))
    except Exception:
        logger.exception("[summary_controller] resolve display functions failed")
    return _DISPLAY_PUSH_FN, _DISPLAY_RANKING_FN


def _overlay_preferred_score_columns(candidate_latest: pd.DataFrame, current_latest: pd.DataFrame) -> pd.DataFrame:
    base = normalize_summary_df(candidate_latest)
    src = normalize_summary_df(current_latest)
    if base.empty or src.empty or "symbol" not in base.columns or "symbol" not in src.columns:
        return base
    try:
        base = base.set_index("symbol")
        src = src.set_index("symbol")
        preserve_columns = (
            "open", "high", "low", "close", "close_price", "price", "volume", "trading_value", "tick_count", "first_tick_at", "last_tick_at",
            "score", "score_buy", "score_sell", "buy_score", "sell_score", "score_total", "combined_score", "final_score", "display_score", "ranking_score",
            "technical_ready", "symbol_hist_len", "rsi", "macd", "signal", "hist", "slope", "slope_atr_scaled", "mtf", "score_mtf", "mtf_score", "atr", "ma5", "ma25", "ma75",
            "score_base", "score_trend", "score_momentum", "score_velocity", "score_penalty", "breakdown_base", "breakdown_trend", "breakdown_mom", "breakdown_vel", "breakdown_pen", "base", "trend", "momentum", "mom", "velocity", "vel", "penalty", "pen",
            "buy_reason_ja", "sell_reason_ja", "exit_reason_ja", "buy_reason", "sell_reason", "exit_reason", "ai_reason", "ai_exit_reason", "ai_decision", "ai_exit_decision", "ai_side", "ai_passed", "ai_buy_passed", "ai_sell_passed", "ai_exit_passed", "ai_confidence", "ai_exit_confidence",
        )
        for c in preserve_columns:
            if c in src.columns:
                if c in base.columns:
                    try:
                        base[c] = base[c].combine_first(src[c])
                    except Exception:
                        try:
                            base[c] = base[c].where(base[c].notna(), src[c])
                        except Exception:
                            pass
                else:
                    base[c] = src[c]
        if "source" in src.columns:
            if "source" in base.columns:
                try:
                    base["source"] = base["source"].where(base["source"].notna() & (base["source"].astype(str).str.strip() != ""), src["source"])
                except Exception:
                    pass
            else:
                base["source"] = src["source"]
        return base.reset_index()
    except Exception:
        logger.exception("[summary_controller] overlay preferred score columns failed")
        return normalize_summary_df(candidate_latest)


def _choose_preferred_base_df(interval: int, fetched_df: pd.DataFrame, engine_df: pd.DataFrame) -> pd.DataFrame:
    fetched = normalize_summary_df(fetched_df)
    engine = normalize_summary_df(engine_df)
    if fetched.empty and engine.empty:
        return pd.DataFrame()
    if fetched.empty:
        return engine
    if engine.empty:
        return fetched
    try:
        fetched_score_cols = [c for c in ("score", "score_total", "score_buy", "score_sell") if c in fetched.columns]
        engine_score_cols = [c for c in ("score", "score_total", "score_buy", "score_sell") if c in engine.columns]
        fetched_score = tuple(int(pd.to_numeric(fetched[c], errors="coerce").fillna(0).ne(0).sum()) for c in fetched_score_cols)
        engine_score = tuple(int(pd.to_numeric(engine[c], errors="coerce").fillna(0).ne(0).sum()) for c in engine_score_cols)
        logger.info("[summary_controller] base choose interval=%s fetched_score=%s engine_score=%s", interval, fetched_score, engine_score)
        if sum(engine_score) > sum(fetched_score):
            return engine
        return fetched
    except Exception:
        return fetched


def _build_reason_from_row(row: pd.Series, side: str) -> str:
    if callable(build_reason_text_from_row):
        try:
            return str(build_reason_text_from_row(row, side=side) or "-")
        except Exception:
            pass
    return "-"


def _safe_run_display(interval: int, df_latest: pd.DataFrame) -> None:
    try:
        push_fn, _ranking_fn = _resolve_display_functions_once()
        if callable(push_fn):
            push_fn(interval, df_latest)
            return
    except Exception:
        logger.exception("[summary_controller] display function failed interval=%s", interval)
    try:
        run_summary_loggers(df_latest, interval=interval)
    except Exception:
        logger.exception("[summary_controller] run_summary_loggers fallback failed interval=%s", interval)


def _safe_log_market_closed(interval: int, df_latest: pd.DataFrame) -> None:
    try:
        log_summary_ranking_bridge(df_latest, interval=interval, source="market_closed")
    except Exception:
        logger.debug("[summary_controller] market closed bridge log skipped", exc_info=True)


def _prepare_history_payload(interval: int, df_hist: pd.DataFrame) -> pd.DataFrame:
    out = normalize_summary_df(df_hist)
    if out.empty:
        return out
    try:
        out = dedupe_symbol_datetime(out, normalize_summary_df)
        out = limit_history_rows_per_symbol(out, interval, normalize_summary_df)
    except Exception:
        logger.exception("[summary_controller] prepare history payload failed interval=%s", interval)
    return out


def _ensure_reason_and_ai_columns(df: pd.DataFrame, interval: int, context: str) -> pd.DataFrame:
    out = normalize_summary_df(df)
    if out.empty:
        return out
    try:
        for c in ("buy_reason_ja", "sell_reason_ja", "exit_reason_ja"):
            if c not in out.columns:
                out[c] = pd.NA
            try:
                out[c] = out[c].astype("object")
            except Exception:
                pass
        need_buy = out["buy_reason_ja"].isna() | out["buy_reason_ja"].astype(str).str.strip().isin(["", "nan", "None", "-"])
        need_sell = out["sell_reason_ja"].isna() | out["sell_reason_ja"].astype(str).str.strip().isin(["", "nan", "None", "-"])
        need_exit = out["exit_reason_ja"].isna() | out["exit_reason_ja"].astype(str).str.strip().isin(["", "nan", "None", "-"])
        if need_buy.any():
            out.loc[need_buy, "buy_reason_ja"] = out.loc[need_buy].apply(lambda r: _build_reason_from_row(r, "BUY"), axis=1)
        if need_sell.any():
            out.loc[need_sell, "sell_reason_ja"] = out.loc[need_sell].apply(lambda r: _build_reason_from_row(r, "SELL"), axis=1)
        if need_exit.any():
            out.loc[need_exit, "exit_reason_ja"] = out.loc[need_exit].apply(lambda r: _build_reason_from_row(r, "EXIT"), axis=1)
        for c in ["ai_passed", "ai_buy_passed", "ai_sell_passed", "ai_exit_passed"]:
            if c not in out.columns:
                out[c] = False
        for c in ["ai_reason", "ai_exit_reason", "ai_decision", "ai_exit_decision", "ai_side"]:
            if c not in out.columns:
                out[c] = ""
        for c in ["ai_confidence", "ai_exit_confidence"]:
            if c not in out.columns:
                out[c] = pd.NA
        try:
            logger.info(
                "[summary_controller] ensure reason/ai columns interval=%s context=%s rows=%s buy_reason_non_dash=%s sell_reason_non_dash=%s exit_reason_non_dash=%s",
                interval, context, len(out),
                int(out["buy_reason_ja"].astype(str).str.strip().ne("-").sum()),
                int(out["sell_reason_ja"].astype(str).str.strip().ne("-").sum()),
                int(out["exit_reason_ja"].astype(str).str.strip().ne("-").sum()),
            )
        except Exception:
            pass
        return out
    except Exception:
        logger.exception("[summary_controller] ensure reason/ai columns failed interval=%s context=%s", interval, context)
        return out


# ============================================================
# LATEST ENRICH / INTRADAY MTF BACKFILL
# (旧 core/startup/summary_controller_latest_enrich_patch.py から移設)
#
# df_latest が保存/cache/entryへ流れる直前に controller_enrich.enrich_summary_latest()
# を通す。3m/5mをDB保存せずメモリ生成する構成でも、3m/5mの最新スコアから
# 1m summaryのmtf/score_mtf/mtf_scoreをsymbol単位で厳格補完する。
# ============================================================

_LATEST_ENRICH_REFRESHING_1M = False


def _latest_enrich_nonzero(df: Any, col: str) -> int:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty or col not in df.columns:
            return 0
        return int(pd.to_numeric(df[col], errors="coerce").fillna(0).ne(0).sum())
    except Exception:
        return 0


def _latest_enrich_env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _latest_enrich_num_series(df: pd.DataFrame, col: str, default: float = 0.0) -> pd.Series:
    try:
        if col not in df.columns:
            return pd.Series(default, index=df.index, dtype="float64")
        return pd.to_numeric(df[col], errors="coerce").replace([float("inf"), float("-inf")], pd.NA).fillna(default).astype("float64")
    except Exception:
        return pd.Series(default, index=getattr(df, "index", None), dtype="float64")


def _latest_enrich_ensure_numeric_col(df: pd.DataFrame, col: str) -> pd.Series:
    if col not in df.columns:
        df[col] = 0.0
    return _latest_enrich_num_series(df, col, 0.0)


def _latest_enrich_first_nonzero(df: pd.DataFrame, cols: tuple[str, ...]) -> pd.Series:
    out = pd.Series(0.0, index=df.index, dtype="float64")
    for c in cols:
        if c not in df.columns:
            continue
        s = _latest_enrich_num_series(df, c, 0.0)
        out = out.where(out.ne(0), s)
    return out.fillna(0.0)


def _latest_enrich_symbolize(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy() if isinstance(df, pd.DataFrame) else pd.DataFrame()
    if out.empty:
        return out
    if "symbol" not in out.columns:
        for c in ("Symbol", "code", "Code", "stock_code", "銘柄コード"):
            if c in out.columns:
                out["symbol"] = out[c]
                break
    if "symbol" in out.columns:
        out["symbol"] = out["symbol"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True).str.replace(r"\.T$", "", regex=True)
        out = out[out["symbol"].astype(str).str.len() > 0].copy()
    return out


def _latest_enrich_normalize_dt(df: pd.DataFrame) -> pd.DataFrame:
    out = _latest_enrich_symbolize(df)
    if out.empty:
        return out
    if "datetime" not in out.columns:
        for c in ("time", "timestamp", "dt", "last_tick_at", "first_tick_at", "DateTime"):
            if c in out.columns:
                out["datetime"] = out[c]
                break
    if "datetime" in out.columns:
        out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
        out = out.dropna(subset=["datetime"])
    return out.reset_index(drop=True)


def _latest_enrich_latest_by_symbol(df: pd.DataFrame) -> pd.DataFrame:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty or "symbol" not in df.columns:
            return pd.DataFrame()
        work = _latest_enrich_normalize_dt(df)
        if work.empty:
            return pd.DataFrame()
        return work.sort_values(["symbol", "datetime"], kind="stable").groupby("symbol", as_index=False).tail(1).reset_index(drop=True)
    except Exception:
        return pd.DataFrame()


def _repair_zero_scores_from_ranking_mtf(df: pd.DataFrame, *, interval: int | None, context: str) -> pd.DataFrame:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty:
            return df
        out = df.copy()
        score_before = _latest_enrich_nonzero(out, "score")
        buy_before = _latest_enrich_nonzero(out, "score_buy")
        sell_before = _latest_enrich_nonzero(out, "score_sell")
        if score_before or buy_before or sell_before:
            return out

        rank = _latest_enrich_first_nonzero(out, ("ranking_score", "ranking_score_total"))
        mtf = _latest_enrich_first_nonzero(out, ("score_mtf", "mtf_score", "mtf", "mtf_alignment", "score_mtf_daily"))
        daily_buy = _latest_enrich_first_nonzero(out, ("daily_mtf_buy", "daily_mtf_buy_daily_src"))
        daily_sell = _latest_enrich_first_nonzero(out, ("daily_mtf_sell", "daily_mtf_sell_daily_src"))

        rank_boost = rank.clip(lower=0.0, upper=1.5) * 2.0
        buy_score = (daily_buy.clip(lower=0.0) + rank_boost).where(daily_buy.ne(0), 0.0)
        sell_score = (daily_sell.clip(lower=0.0) + rank_boost).where(daily_sell.ne(0), 0.0)

        no_side = buy_score.eq(0) & sell_score.eq(0) & mtf.ne(0)
        if bool(no_side.any()):
            neutral = (mtf.clip(lower=0.0, upper=6.5) * 0.5 + rank_boost).fillna(0.0)
            buy_score = buy_score.where(~no_side, neutral)
            sell_score = sell_score.where(~no_side, neutral)

        has_any = buy_score.ne(0) | sell_score.ne(0)
        if not bool(has_any.any()):
            return out

        signed = buy_score - sell_score
        display = pd.concat([buy_score.abs(), sell_score.abs(), mtf.abs(), rank_boost.abs()], axis=1).max(axis=1).fillna(0.0)

        for col in ("score_buy", "buy_score"):
            out[col] = _latest_enrich_ensure_numeric_col(out, col).where(_latest_enrich_ensure_numeric_col(out, col).ne(0), buy_score)
        for col in ("score_sell", "sell_score"):
            out[col] = _latest_enrich_ensure_numeric_col(out, col).where(_latest_enrich_ensure_numeric_col(out, col).ne(0), sell_score)
        for col in ("score", "score_total", "final_score"):
            out[col] = _latest_enrich_ensure_numeric_col(out, col).where(_latest_enrich_ensure_numeric_col(out, col).ne(0), signed)
        out["display_score"] = _latest_enrich_ensure_numeric_col(out, "display_score").where(_latest_enrich_ensure_numeric_col(out, "display_score").ne(0), display)
        out["summary_score_repaired_from_ranking_mtf"] = has_any

        logger.warning(
            "[SUMMARY CONTROLLER SCORE REPAIR] context=%s interval=%s rows=%s repaired=%s score %s->%s buy %s->%s sell %s->%s",
            context, interval, len(out), int(has_any.sum()), score_before, _latest_enrich_nonzero(out, "score"), buy_before, _latest_enrich_nonzero(out, "score_buy"), sell_before, _latest_enrich_nonzero(out, "score_sell"),
        )
        return out
    except Exception:
        logger.debug("[SUMMARY CONTROLLER SCORE REPAIR] failed context=%s interval=%s", context, interval, exc_info=True)
        return df


def _repair_score_aliases(df: Any, *, interval: int | None = None, context: str = "runtime") -> Any:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return df
    try:
        out = df.copy()
        changed: dict[str, tuple[int, int]] = {}
        out = _repair_zero_scores_from_ranking_mtf(out, interval=interval, context=context)

        for dst, src in (("buy_score", "score_buy"), ("sell_score", "score_sell")):
            if src in out.columns:
                before = _latest_enrich_nonzero(out, dst)
                src_s = _latest_enrich_num_series(out, src, 0.0)
                dst_s = _latest_enrich_ensure_numeric_col(out, dst)
                out[dst] = src_s if before == 0 and int(src_s.ne(0).sum()) > 0 else dst_s.where(dst_s.ne(0), src_s)
                after = _latest_enrich_nonzero(out, dst)
                if after != before:
                    changed[dst] = (before, after)

        if "display_score" in out.columns:
            before = _latest_enrich_nonzero(out, "display_score")
            disp = _latest_enrich_ensure_numeric_col(out, "display_score")
            if before == 0:
                for src in ("final_score", "score_total", "score"):
                    if src in out.columns:
                        src_s = _latest_enrich_num_series(out, src, 0.0)
                        if int(src_s.ne(0).sum()) > 0:
                            out["display_score"] = src_s.abs()
                            break
            else:
                for src in ("final_score", "score_total", "score"):
                    if src in out.columns:
                        out["display_score"] = disp.where(disp.ne(0), _latest_enrich_num_series(out, src, 0.0).abs())
                        break
            after = _latest_enrich_nonzero(out, "display_score")
            if after != before:
                changed["display_score"] = (before, after)

        if changed:
            logger.warning("[SUMMARY CONTROLLER SCORE ALIAS REPAIR] context=%s interval=%s rows=%s changed=%s", context, interval, len(out), changed)
        return out
    except Exception:
        logger.debug("[SUMMARY CONTROLLER SCORE ALIAS REPAIR] failed context=%s interval=%s", context, interval, exc_info=True)
        return df


def _read_global_summary(interval: int) -> pd.DataFrame:
    try:
        candidates: list[Any] = []
        for method in ("get_summary_history", "get_push_merged_summary", "get_latest_summary", "get_push_summary", "get_merged_summary"):
            try:
                fn = getattr(global_data, method, None)
                if callable(fn):
                    candidates.append(fn(int(interval)))
            except Exception:
                pass
        for name in (
            f"summary_{int(interval)}m_df", f"push_summary_{int(interval)}m_df", f"latest_summary_{int(interval)}m_df",
            f"summary_{int(interval)}m_latest_df", f"push_merged_summary_{int(interval)}m_df",
        ):
            try:
                candidates.append(getattr(global_data, name, None))
            except Exception:
                pass
        frames = []
        for x in candidates:
            if isinstance(x, pd.DataFrame) and not x.empty:
                d = _latest_enrich_normalize_dt(x)
                if not d.empty:
                    frames.append(d)
        if not frames:
            return pd.DataFrame()
        out = pd.concat(frames, ignore_index=True, sort=False)
        out = out.loc[:, ~pd.Index(out.columns).duplicated()].copy()
        out = _latest_enrich_normalize_dt(out)
        if out.empty:
            return pd.DataFrame()
        return out.drop_duplicates(subset=["symbol", "datetime"], keep="last").reset_index(drop=True)
    except Exception:
        logger.debug("[SUMMARY CONTROLLER INTRADAY MTF] read global summary failed interval=%s", interval, exc_info=True)
        return pd.DataFrame()


def _signed_mtf_score(df: pd.DataFrame) -> pd.Series:
    score = _latest_enrich_first_nonzero(df, ("score_total", "final_score", "score", "mtf", "score_mtf", "mtf_score", "slope_atr_scaled", "slope"))
    try:
        buy = _latest_enrich_first_nonzero(df, ("score_buy", "buy_score"))
        sell = _latest_enrich_first_nonzero(df, ("score_sell", "sell_score"))
        side_score = buy.where(buy.ne(0), 0.0) - sell.where(sell.ne(0), 0.0)
        score = score.where(score.ne(0), side_score)
    except Exception:
        pass
    return score.fillna(0.0)


def _fresh_latest_mtf(interval: int) -> pd.DataFrame:
    df = _latest_enrich_latest_by_symbol(_read_global_summary(interval))
    if df.empty:
        return pd.DataFrame()
    try:
        max_age = _latest_enrich_env_float("SUMMARY_INTRADAY_MTF_BACKFILL_MAX_AGE_SEC", 240.0)
        latest_dt = pd.to_datetime(df["datetime"], errors="coerce")
        now = pd.Timestamp.now()
        age = (now - latest_dt).dt.total_seconds()
        # future labels from right-closed resample are accepted as fresh; old labels are rejected.
        df = df[(age <= max_age) | (age < 0)].copy()
    except Exception:
        pass
    if df.empty:
        return pd.DataFrame()
    df[f"mtf_{interval}m"] = _signed_mtf_score(df)
    return df[["symbol", f"mtf_{interval}m"]].drop_duplicates("symbol", keep="last")


def _build_intraday_mtf_map() -> pd.DataFrame:
    try:
        m3 = _fresh_latest_mtf(3)
        m5 = _fresh_latest_mtf(5)
        if m3.empty and m5.empty:
            return pd.DataFrame()
        out = m3 if not m3.empty else pd.DataFrame(columns=["symbol"])
        if not m5.empty:
            out = out.merge(m5, on="symbol", how="outer") if "symbol" in out.columns and not out.empty else m5
        out = _latest_enrich_symbolize(out)
        s3 = pd.to_numeric(out.get("mtf_3m", pd.Series(index=out.index)), errors="coerce").fillna(0.0)
        s5 = pd.to_numeric(out.get("mtf_5m", pd.Series(index=out.index)), errors="coerce").fillna(0.0)
        both = s3.ne(0) & s5.ne(0)
        same_dir = both & ((s3 > 0) == (s5 > 0))
        one_side = s3.where(s3.ne(0), s5)
        combined = one_side.where(~same_dir, (s3 + s5) / 2.0)
        # 3m/5mが両方あり方向不一致なら補完しない。片側だけなら補完可。
        combined = combined.where(~(both & ~same_dir), 0.0)
        out["_intraday_mtf_backfill"] = combined.fillna(0.0)
        return out[["symbol", "_intraday_mtf_backfill"]][out["_intraday_mtf_backfill"].ne(0)].drop_duplicates("symbol", keep="last")
    except Exception:
        logger.debug("[SUMMARY CONTROLLER INTRADAY MTF] build map failed", exc_info=True)
        return pd.DataFrame()


def _apply_intraday_mtf_to_1m(df: Any, *, context: str) -> Any:
    if not isinstance(df, pd.DataFrame) or df.empty or "symbol" not in df.columns:
        return df
    try:
        out = _latest_enrich_symbolize(df)
        if out.empty:
            return df
        before = _latest_enrich_nonzero(out, "score_mtf") + _latest_enrich_nonzero(out, "mtf") + _latest_enrich_nonzero(out, "mtf_score")
        mtf_map = _build_intraday_mtf_map()
        if mtf_map.empty:
            return out
        merged = out.merge(mtf_map, on="symbol", how="left")
        src = pd.to_numeric(merged.get("_intraday_mtf_backfill", pd.Series(index=merged.index)), errors="coerce").fillna(0.0)
        for col in ("mtf", "score_mtf", "mtf_score", "mtf_alignment"):
            base = _latest_enrich_ensure_numeric_col(merged, col)
            merged[col] = base.where(base.ne(0), src)
        merged = merged.drop(columns=["_intraday_mtf_backfill"], errors="ignore")
        after = _latest_enrich_nonzero(merged, "score_mtf") + _latest_enrich_nonzero(merged, "mtf") + _latest_enrich_nonzero(merged, "mtf_score")
        if after != before:
            logger.warning(
                "[SUMMARY CONTROLLER INTRADAY MTF] context=%s interval=1 rows=%s mtf_nonzero %s->%s map_rows=%s",
                context, len(merged), before, after, len(mtf_map),
            )
        return merged
    except Exception:
        logger.debug("[SUMMARY CONTROLLER INTRADAY MTF] apply failed context=%s", context, exc_info=True)
        return df


def _enrich(df: Any, *, interval: int | None = None, context: str = "runtime") -> Any:
    if not isinstance(df, pd.DataFrame) or df.empty or "symbol" not in df.columns:
        return df
    try:
        from trading.summary.controller_enrich import enrich_summary_latest
        interval_i = int(interval or 1)
        before_rank = _latest_enrich_nonzero(df, "ranking_score")
        out = enrich_summary_latest(df, interval=interval_i, context=context)
        if interval_i == 1:
            out = _apply_intraday_mtf_to_1m(out, context=context)
        out = _repair_score_aliases(out, interval=interval_i, context=context)
        after_rank = _latest_enrich_nonzero(out, "ranking_score")
        if after_rank != before_rank or "ranking_score" not in df.columns:
            logger.warning(
                "[SUMMARY CONTROLLER LATEST ENRICH] context=%s interval=%s rows=%s ranking_score_nonzero %s->%s",
                context, interval_i, len(out) if isinstance(out, pd.DataFrame) else len(df), before_rank, after_rank,
            )
        return out if isinstance(out, pd.DataFrame) else df
    except Exception:
        logger.debug("[SUMMARY CONTROLLER LATEST ENRICH] enrich failed context=%s interval=%s", context, interval, exc_info=True)
        fallback = _apply_intraday_mtf_to_1m(df, context=context) if int(interval or 1) == 1 else df
        return _repair_score_aliases(fallback, interval=interval, context=context)


def _call_global_setter(name: str, interval: int, df: pd.DataFrame) -> None:
    try:
        fn = getattr(global_data, name, None)
        if not callable(fn):
            return
        for call in (
            lambda: fn(tf=interval, df=df.copy(), source="push"),
            lambda: fn(interval, df.copy()),
            lambda: fn(interval, df.copy(), "push"),
        ):
            try:
                call()
                return
            except TypeError:
                continue
    except Exception:
        logger.debug("[SUMMARY CONTROLLER INTRADAY MTF] global setter failed name=%s interval=%s", name, interval, exc_info=True)


def _refresh_1m_mtf_from_global(reason: str) -> None:
    global _LATEST_ENRICH_REFRESHING_1M
    if _LATEST_ENRICH_REFRESHING_1M:
        return
    try:
        _LATEST_ENRICH_REFRESHING_1M = True
        one = _read_global_summary(1)
        if one.empty:
            return
        before = _latest_enrich_nonzero(one, "score_mtf") + _latest_enrich_nonzero(one, "mtf") + _latest_enrich_nonzero(one, "mtf_score")
        enriched = _apply_intraday_mtf_to_1m(one, context=f"refresh-{reason}")
        after = _latest_enrich_nonzero(enriched, "score_mtf") + _latest_enrich_nonzero(enriched, "mtf") + _latest_enrich_nonzero(enriched, "mtf_score")
        if after <= before:
            return
        latest = _latest_enrich_latest_by_symbol(enriched)
        for method_name, df_arg in (
            ("set_summary_history", enriched),
            ("set_push_merged_summary", latest if not latest.empty else enriched),
            ("set_merged_summary", latest if not latest.empty else enriched),
        ):
            _call_global_setter(method_name, 1, df_arg)
        logger.warning("[SUMMARY CONTROLLER INTRADAY MTF] refreshed 1m from global reason=%s rows=%s mtf_nonzero %s->%s", reason, len(enriched), before, after)
    except Exception:
        logger.debug("[SUMMARY CONTROLLER INTRADAY MTF] refresh failed reason=%s", reason, exc_info=True)
    finally:
        _LATEST_ENRICH_REFRESHING_1M = False


class SummaryController:
    def update_engine(self, interval: int) -> pd.DataFrame:
        try:
            df = run_summary_engine(interval=interval)
            if isinstance(df, pd.DataFrame):
                df = normalize_summary_df(df)
                df = _ensure_reason_and_ai_columns(df, interval, "engine-output")
                log_df_state("engine-output", interval, df)
                return df
            return pd.DataFrame()
        except TypeError:
            try:
                df = run_summary_engine()
                if isinstance(df, pd.DataFrame):
                    df = normalize_summary_df(df)
                    df = _ensure_reason_and_ai_columns(df, interval, "engine-output-legacy")
                    log_df_state("engine-output-legacy", interval, df)
                    return df
            except Exception:
                logger.exception("[summary_controller] engine failed (legacy)")
                return pd.DataFrame()
        except Exception:
            logger.exception("[summary_controller] engine failed")
        return pd.DataFrame()

    def diff_update(self, interval: int) -> pd.DataFrame:
        df_latest = pd.DataFrame()
        hist_payload = pd.DataFrame()
        try:
            interval = int(interval)
            if not _enter_interval(interval):
                return pd.DataFrame()
            from utils.business_day_utils import is_market_open
            try:
                engine_df = self.update_engine(interval)
                fetched_df = run_fetch_pipeline(interval)
                fetched_df = normalize_summary_df(fetched_df)
                fetched_df = _ensure_reason_and_ai_columns(fetched_df, interval, "fetched")
                log_df_state("fetched", interval, fetched_df)
                log_df_state("engine-merged", interval, engine_df)
                base_df = _choose_preferred_base_df(interval=interval, fetched_df=fetched_df, engine_df=engine_df)
                base_df = _ensure_reason_and_ai_columns(base_df, interval, "base-selected")
                if base_df.empty:
                    logger.debug("[summary_controller] empty summary interval=%s", interval)
                    return pd.DataFrame()
                hist_df = merge_history_sources(interval=interval, current_df=base_df, fetched_df=fetched_df, engine_df=engine_df, normalize_fn=normalize_summary_df)
                if hist_df.empty:
                    hist_df = normalize_summary_df(base_df.copy())
                hist_df = _ensure_reason_and_ai_columns(hist_df, interval, "history-base")
                log_df_state("history-base", interval, hist_df)
                try:
                    df_hist = run_indicator_pipeline(hist_df.copy(), interval)
                    df_hist = normalize_summary_df(df_hist)
                    df_hist = _ensure_reason_and_ai_columns(df_hist, interval, "after-indicator-history")
                    log_df_state("after-indicator-history", interval, df_hist)
                    log_scoring_probe("after-indicator-history", interval, _enrich(df_hist, interval=interval, context="log-after-indicator-history"))
                    from trading.scoring.core.scoring_pipeline import run_scoring_pipeline
                    df_hist = run_scoring_pipeline(df_hist, interval=f"{interval}min")
                    df_hist = normalize_summary_df(df_hist)
                    df_hist = _ensure_reason_and_ai_columns(df_hist, interval, "after-scoring-history")
                    log_df_state("after-scoring-history", interval, df_hist)
                    log_scoring_probe("after-scoring-history", interval, _enrich(df_hist, interval=interval, context="log-after-scoring-history"))
                except Exception:
                    logger.exception("[summary_controller] indicator/scoring failed interval=%s", interval)
                    try:
                        df_latest = latest_row_per_symbol(base_df, normalize_summary_df)
                        df_latest = attach_history_len(df_latest, hist_df, normalize_summary_df)
                        df_latest = rebuild_technical_ready(df_latest)
                        df_latest = _ensure_reason_and_ai_columns(df_latest, interval, "fallback-latest")
                        _safe_run_display(interval, df_latest)
                    except Exception:
                        logger.exception("[summary_controller] fallback display failed interval=%s", interval)
                    return base_df
                try:
                    df_hist = attach_history_len(df_hist, df_hist, normalize_summary_df)
                    df_hist = rebuild_technical_ready(df_hist)
                    df_hist = _ensure_reason_and_ai_columns(df_hist, interval, "history-after-ready")
                    df_latest = latest_row_per_symbol_mature_first(df_hist, normalize_summary_df)
                    df_latest = attach_history_len(df_latest, df_hist, normalize_summary_df)
                    current_latest = latest_row_per_symbol(base_df, normalize_summary_df)
                    current_latest = attach_history_len(current_latest, hist_df, normalize_summary_df)
                    df_latest = _overlay_preferred_score_columns(df_latest, current_latest)
                    df_latest = attach_history_len(df_latest, df_hist, normalize_summary_df)
                    df_latest = rebuild_technical_ready(df_latest)
                    df_latest = _ensure_reason_and_ai_columns(df_latest, interval, "after-latest-projection")
                    log_df_state("after-latest-projection", interval, df_latest)
                    log_scoring_probe("after-latest-projection", interval, _enrich(df_latest, interval=interval, context="log-after-latest-projection"))
                except Exception:
                    logger.exception("[summary_controller] latest projection failed interval=%s", interval)
                    df_latest = latest_row_per_symbol(base_df, normalize_summary_df)
                    df_latest = attach_history_len(df_latest, hist_df, normalize_summary_df)
                    df_latest = rebuild_technical_ready(df_latest)
                    df_latest = _ensure_reason_and_ai_columns(df_latest, interval, "latest-projection-fallback")
                try:
                    hist_payload = _prepare_history_payload(interval, df_hist)
                    hist_payload = _ensure_reason_and_ai_columns(hist_payload, interval, "history-payload")
                except Exception:
                    logger.exception("[summary_controller] history payload build failed interval=%s", interval)
                    hist_payload = normalize_summary_df(df_hist)
                    hist_payload = _ensure_reason_and_ai_columns(hist_payload, interval, "history-payload-except")
                _safe_run_display(interval, df_latest)
                try:
                    if not is_market_open():
                        try:
                            safe_global_set_history(interval, hist_payload)
                            safe_global_set_latest(interval, _enrich(df_latest.copy(), interval=interval, context="cache-latest"))
                            if int(interval) in (3, 5):
                                _refresh_1m_mtf_from_global(f"summary-controller-cache-latest-{int(interval)}m")
                        except Exception:
                            logger.exception("[summary_controller] market-closed cache sync failed interval=%s", interval)
                        try:
                            existing_merged = safe_global_get_merged_summary(interval, normalize_summary_df)
                            merged_payload = choose_merged_cache_payload(interval, df_hist=hist_payload, df_latest=df_latest, normalize_fn=normalize_summary_df)
                            merged_payload = _ensure_reason_and_ai_columns(merged_payload, interval, "market-closed-merged")
                            if should_overwrite_merged_summary(existing_merged, merged_payload):
                                safe_global_set_merged_summary(interval, merged_payload.copy())
                                logger.info("[summary_controller] merged summary overwritten interval=%s market_closed=1", interval)
                            else:
                                logger.info("[summary_controller] merged summary preserved interval=%s market_closed=1", interval)
                        except Exception:
                            logger.exception("[summary_controller] market-closed merged cache sync failed interval=%s", interval)
                        _safe_log_market_closed(interval, df_latest)
                        return df_latest
                except Exception:
                    logger.exception("[summary_controller] market guard failed")
                if _skip_db_save_for_entry_only_main():
                    logger.warning("[summary_controller] DB save skipped interval=%s reason=main_entry_only rows=%s env_skip=%s role=%s", interval, len(df_latest) if isinstance(df_latest, pd.DataFrame) else None, os.environ.get("SUMMARY_SKIP_DB_SAVE_IN_MAIN"), os.environ.get("SUMMARY_DB_WRITER_ROLE"))
                else:
                    try:
                        save_summary(_enrich(df_latest, interval=interval, context="before-save"), interval, lock_timeout_sec=3.0, skip_if_busy=True, caller="summary_controller.diff_update")
                    except TypeError:
                        try:
                            save_summary(_enrich(df_latest, interval=interval, context="before-save"), interval)
                        except Exception:
                            logger.exception("[summary_controller] save failed interval=%s", interval)
                    except Exception:
                        logger.exception("[summary_controller] save failed interval=%s", interval)
                log_scoring_probe("before-cache-sync", interval, _enrich(df_latest, interval=interval, context="log-before-cache-sync"))
                try:
                    safe_global_set_history(interval, hist_payload)
                    safe_global_set_latest(interval, _enrich(df_latest.copy(), interval=interval, context="cache-latest"))
                    if int(interval) in (3, 5):
                        _refresh_1m_mtf_from_global(f"summary-controller-cache-latest-{int(interval)}m")
                except Exception:
                    logger.exception("[summary_controller] history/latest cache sync failed")
                try:
                    existing_merged = safe_global_get_merged_summary(interval, normalize_summary_df)
                    merged_payload = choose_merged_cache_payload(interval, df_hist=hist_payload, df_latest=df_latest, normalize_fn=normalize_summary_df)
                    merged_payload = _ensure_reason_and_ai_columns(merged_payload, interval, "merged-payload")
                    if should_overwrite_merged_summary(existing_merged, merged_payload):
                        safe_global_set_merged_summary(interval, merged_payload.copy())
                        logger.info("[summary_controller] merged summary overwritten interval=%s", interval)
                    else:
                        logger.info("[summary_controller] merged summary preserved interval=%s", interval)
                except Exception:
                    logger.exception("[summary_controller] cache sync failed")
                try:
                    global_data.last_summary_update = dt.datetime.now()
                except Exception:
                    pass
                try:
                    df_entry = run_ranking_pipeline(_enrich(df_latest, interval=interval, context="before-ranking-pipeline"), interval)
                except Exception:
                    logger.exception("[summary_controller] ranking failed")
                    return df_latest
                if df_entry is None:
                    return df_latest
                if isinstance(df_entry, pd.DataFrame) and df_entry.empty:
                    logger.debug("[summary_controller] no ranking")
                    return df_latest
                try:
                    approved_rows = run_ai_pipeline(df_entry, df_latest, interval)
                except Exception:
                    logger.exception("[summary_controller] ai failed")
                    approved_rows = []
                try:
                    notify_tonosama(df_latest)
                except Exception:
                    logger.exception("[summary_controller] tonosama notify failed")
                try:
                    notify_entry_signals(approved_rows)
                except Exception:
                    logger.exception("[summary_controller] entry notify failed")
                try:
                    run_entry_pipeline(approved_rows, df_latest, interval)
                except Exception:
                    logger.exception("[summary_controller] entry failed")
                return df_latest
            finally:
                _leave_interval(interval)
        except Exception:
            logger.exception("[summary_controller] fatal interval=%s", interval)
            return pd.DataFrame()


summary_controller = SummaryController()
