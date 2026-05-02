# ============================================================
# File   : core/startup/runtime_mode.py
# Version: REV1.1-STARTUP-RUNTIME-MODE-SUMMARY-READY-GUARD
# ------------------------------------------------------------
# 【概要】
#   realtime combat mode の開始処理を分離する
#
# 【主な機能】
#   - HTF immediate sync
#   - PUSH merged summary の readiness 確認
#   - summary bootstrap async 実行中の短時間待機
#   - summary DB 最新行 fallback load
#   - immediate scoring
#   - immediate entry pipeline
#   - active symbols refresh
#
# 【今回の修正】
#   - fast boot 時、summary bootstrap が background 実行中のまま
#     scoring / entry が先に走る問題を防止
#
#   - merged summary が空の場合:
#       1. bootstrap 完了を短時間待つ
#       2. それでも空なら summary DB から最新行を読む
#       3. 読めた場合は global_data の push merged summary へ反映
#
#   - 1min / 3min / 5min の summary が全て空の場合、
#     immediate entry pipeline を起動しない
#
# 【重要】
#   起動直後のログで以下のようになっていた:
#
#       summary bootstrap started in background
#       [MERGED GET] tf=1 source=push rows=0
#       scoring immediate skip tf=1
#
#   これは、summary bootstrap 完了前に runtime_mode が
#   scoring を実行していたため。
#
#   本版では、summary が準備できるまで短時間待ち、
#   それでも空なら summary DB fallback を試す。
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import time
from typing import Any, Optional

import pandas as pd
from sqlalchemy import text

from utils.business_day_utils import is_market_open
from trading.aggregation.higher_tf.incremental_higher_tf_engine import (
    incremental_higher_tf_engine,
)
from trading.handlers.entry_controller import run_entry_pipeline
from trading.ranking.active_symbol_manager import update_active_symbols

from core.startup.merged_summary_access import (
    get_push_merged_summary_safe,
    set_push_merged_summary_safe,
)

logger = logging.getLogger(__name__)


# ============================================================
# settings
# ============================================================

SUMMARY_TFS = (1, 3, 5)

# 起動直後に summary bootstrap 完了を少しだけ待つ。
# 長く待つと fast boot の意味がなくなるため、短めにする。
SUMMARY_READY_WAIT_SEC = 6.0
SUMMARY_READY_POLL_SEC = 0.5

_INTERVAL_TABLE_MAP = {
    1: "stock_summary_1min",
    3: "stock_summary_3min",
    5: "stock_summary_5min",
}


# ============================================================
# imports / resolvers
# ============================================================

def _get_scoring_main():
    from trading.scoring.core.scoring_core import scoring_main
    return scoring_main


def _get_global_data():
    try:
        from global_state import global_data
        return global_data
    except Exception:
        try:
            from core.global_context.context import global_data
            return global_data
        except Exception:
            return None


def _get_summary_engine():
    """
    database.session.summary_engine は startup 中に rebind されるため、
    関数内で遅延 import する。
    """
    try:
        from database.session import summary_engine
        return summary_engine
    except Exception:
        logger.exception("[runtime_mode] summary_engine import failed")
        return None


def _get_summary_bootstrap_state() -> dict[str, bool]:
    """
    summary_runtime の状態を安全に取得する。
    import 失敗時は global_data の flags から fallback する。
    """
    try:
        from core.startup.summary_runtime import get_summary_bootstrap_state
        state = get_summary_bootstrap_state()
        if isinstance(state, dict):
            return {
                "started": bool(state.get("started", False)),
                "done": bool(state.get("done", False)),
                "failed": bool(state.get("failed", False)),
            }
    except Exception:
        logger.debug("[runtime_mode] get_summary_bootstrap_state import/read failed", exc_info=True)

    gd = _get_global_data()
    if gd is None:
        return {"started": False, "done": False, "failed": False}

    try:
        return {
            "started": bool(getattr(gd, "summary_bootstrap_started", False)),
            "done": bool(getattr(gd, "summary_bootstrap_done", False)),
            "failed": bool(getattr(gd, "summary_bootstrap_failed", False)),
        }
    except Exception:
        return {"started": False, "done": False, "failed": False}


def _is_summary_bootstrap_running() -> bool:
    state = _get_summary_bootstrap_state()
    return bool(state.get("started") and not state.get("done") and not state.get("failed"))


# ============================================================
# df helpers
# ============================================================

def _is_nonempty_df(df: Any) -> bool:
    return isinstance(df, pd.DataFrame) and not df.empty


def _df_rows(df: Any) -> int:
    try:
        if isinstance(df, pd.DataFrame):
            return int(len(df))
    except Exception:
        pass
    return 0


def _symbols_count(df: Any) -> int:
    try:
        if isinstance(df, pd.DataFrame) and not df.empty and "symbol" in df.columns:
            return int(df["symbol"].fillna("").astype(str).str.strip().replace("", pd.NA).dropna().nunique())
    except Exception:
        pass
    return 0


def _latest_dt_str(df: Any) -> Optional[str]:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty:
            return None

        for c in ("datetime", "end_time", "time", "start_time"):
            if c in df.columns:
                s = pd.to_datetime(df[c], errors="coerce").dropna()
                if not s.empty:
                    return str(s.max())
    except Exception:
        pass
    return None


def _nonzero_count(df: pd.DataFrame, col: str) -> int:
    try:
        if col not in df.columns:
            return -1
        s = pd.to_numeric(df[col], errors="coerce").fillna(0)
        return int((s != 0).sum())
    except Exception:
        return -1


def _nonnull_count(df: pd.DataFrame, col: str) -> int:
    try:
        if col not in df.columns:
            return -1
        return int(pd.to_numeric(df[col], errors="coerce").notna().sum())
    except Exception:
        return -1


def _log_summary_profile(label: str, tf: int, df: Any) -> None:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty:
            logger.info(
                "[runtime_mode] %s tf=%s rows=0 symbols=0 latest_dt=None",
                label,
                tf,
            )
            return

        logger.info(
            "[runtime_mode] %s tf=%s rows=%d symbols=%d latest_dt=%s cols=%s",
            label,
            tf,
            len(df),
            _symbols_count(df),
            _latest_dt_str(df),
            list(df.columns[:30]),
        )

        logger.info(
            "[runtime_mode] %s tf=%s nonzero score=%s final_score=%s display_score=%s score_buy=%s score_sell=%s slope=%s score_slope=%s mtf=%s score_mtf=%s rsi_nonnull=%s macd_nonnull=%s close_nonnull=%s",
            label,
            tf,
            _nonzero_count(df, "score"),
            _nonzero_count(df, "final_score"),
            _nonzero_count(df, "display_score"),
            _nonzero_count(df, "score_buy"),
            _nonzero_count(df, "score_sell"),
            _nonzero_count(df, "slope"),
            _nonzero_count(df, "score_slope"),
            _nonzero_count(df, "mtf"),
            _nonzero_count(df, "score_mtf"),
            _nonnull_count(df, "rsi"),
            _nonnull_count(df, "macd"),
            _nonnull_count(df, "close"),
        )
    except Exception:
        logger.exception("[runtime_mode] summary profile log failed label=%s tf=%s", label, tf)


def _normalize_summary_df(df: Any, tf: int) -> pd.DataFrame:
    """
    DB fallback で読んだ DataFrame を最低限整形する。
    completed 判定は GlobalContext 側に任せる。
    """
    try:
        if df is None or not isinstance(df, pd.DataFrame) or df.empty:
            return pd.DataFrame()

        out = df.copy()
        out = out.loc[:, ~out.columns.duplicated()].copy()

        if "symbol" not in out.columns:
            for c in ("Symbol", "code", "Code", "ticker"):
                if c in out.columns:
                    out["symbol"] = out[c]
                    break

        if "symbol" not in out.columns:
            return pd.DataFrame()

        out["symbol"] = (
            out["symbol"]
            .fillna("")
            .astype(str)
            .str.strip()
            .str.replace(r"\.0$", "", regex=True)
        )
        out = out[out["symbol"] != ""].copy()
        if out.empty:
            return pd.DataFrame()

        # datetime 補完
        if "datetime" not in out.columns:
            if "date" in out.columns and "time" in out.columns:
                out["datetime"] = pd.to_datetime(
                    out["date"].astype(str).str.strip() + " " + out["time"].astype(str).str.strip(),
                    errors="coerce",
                )
            elif "date" in out.columns and "start_time" in out.columns:
                out["datetime"] = pd.to_datetime(
                    out["date"].astype(str).str.strip() + " " + out["start_time"].astype(str).str.strip(),
                    errors="coerce",
                )
            elif "end_time" in out.columns:
                out["datetime"] = pd.to_datetime(out["end_time"], errors="coerce")
            else:
                out["datetime"] = pd.NaT
        else:
            out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")

        try:
            out["datetime"] = out["datetime"].dt.tz_localize(None)
        except Exception:
            pass

        # score 系が無い場合は作らないのが理想だが、
        # GlobalContext の completed 判定が symbol + score を要求するため、
        # final_score / display_score / score_buy などから score を補完する。
        if "score" not in out.columns:
            for c in ("final_score", "display_score", "score_buy", "score_total"):
                if c in out.columns:
                    out["score"] = pd.to_numeric(out[c], errors="coerce")
                    break

        if "score" not in out.columns:
            out["score"] = 0.0

        for col in (
            "score",
            "score_total",
            "final_score",
            "display_score",
            "score_buy",
            "score_sell",
            "slope",
            "slope_atr_scaled",
            "score_slope",
            "mtf",
            "score_mtf",
            "mtf_score",
            "open",
            "high",
            "low",
            "close",
            "rsi",
            "macd",
            "signal",
        ):
            if col in out.columns:
                out[col] = pd.to_numeric(out[col], errors="coerce")

        if "final_score" not in out.columns and "score" in out.columns:
            out["final_score"] = out["score"]

        if "display_score" not in out.columns and "score" in out.columns:
            out["display_score"] = out["score"]

        if "score_buy" not in out.columns and "score" in out.columns:
            out["score_buy"] = out["score"]

        if "score_sell" not in out.columns:
            out["score_sell"] = 0.0

        if "symbolname" not in out.columns:
            out["symbolname"] = ""

        # 最新1行 / symbol にする
        try:
            if "datetime" in out.columns:
                out = out.sort_values(
                    ["symbol", "datetime"],
                    ascending=[True, True],
                    na_position="last",
                    kind="mergesort",
                )
            out = out.drop_duplicates(subset=["symbol"], keep="last").copy()
        except Exception:
            logger.debug("[runtime_mode] latest row per symbol failed tf=%s", tf, exc_info=True)

        out = out.reset_index(drop=True)
        return out

    except Exception:
        logger.exception("[runtime_mode] normalize summary df failed tf=%s", tf)
        return pd.DataFrame()


# ============================================================
# merged summary access
# ============================================================

def _get_push_summary(tf: int) -> pd.DataFrame:
    try:
        df = get_push_merged_summary_safe(tf)
        if isinstance(df, pd.DataFrame) and not df.empty:
            return df.copy()
    except Exception:
        logger.debug("[runtime_mode] get_push_merged_summary_safe failed tf=%s", tf, exc_info=True)
    return pd.DataFrame()


def _set_push_summary(tf: int, df: pd.DataFrame, *, reason: str) -> bool:
    if df is None or df.empty:
        return False

    try:
        set_push_merged_summary_safe(tf, df)
        logger.info(
            "[runtime_mode] push merged summary set tf=%s reason=%s rows=%d symbols=%d latest_dt=%s",
            tf,
            reason,
            len(df),
            _symbols_count(df),
            _latest_dt_str(df),
        )
        return True
    except Exception:
        logger.exception("[runtime_mode] set_push_merged_summary_safe failed tf=%s reason=%s", tf, reason)

    gd = _get_global_data()
    if gd is not None:
        try:
            if hasattr(gd, "set_merged_summary"):
                gd.set_merged_summary(tf, df, source="push")
                logger.info(
                    "[runtime_mode] global_data.set_merged_summary fallback set tf=%s reason=%s rows=%d",
                    tf,
                    reason,
                    len(df),
                )
                return True
        except Exception:
            logger.exception("[runtime_mode] global_data.set_merged_summary fallback failed tf=%s", tf)

    return False


def _has_any_summary(summary_map: dict[int, pd.DataFrame]) -> bool:
    return any(isinstance(df, pd.DataFrame) and not df.empty for df in summary_map.values())


# ============================================================
# DB fallback loader
# ============================================================

def _load_latest_summary_from_db(tf: int) -> pd.DataFrame:
    """
    summary DB から tf の最新 datetime 行を取得する。
    起動直後の global_data 空状態を救済するための fallback。
    """
    table = _INTERVAL_TABLE_MAP.get(int(tf))
    if not table:
        return pd.DataFrame()

    engine = _get_summary_engine()
    if engine is None:
        logger.warning("[runtime_mode] summary DB fallback skipped tf=%s reason=summary_engine_none", tf)
        return pd.DataFrame()

    try:
        sql = text(f"""
            WITH latest_dt AS (
                SELECT MAX(datetime) AS max_dt
                FROM {table}
                WHERE datetime IS NOT NULL
            )
            SELECT *
            FROM {table}
            WHERE datetime = (SELECT max_dt FROM latest_dt)
        """)

        with engine.connect() as conn:
            df = pd.read_sql(sql, conn)

        df = _normalize_summary_df(df, tf)
        _log_summary_profile("summary-db-fallback", tf, df)
        return df

    except Exception:
        logger.exception("[runtime_mode] summary DB fallback load failed tf=%s table=%s", tf, table)
        return pd.DataFrame()


def _load_latest_summaries_from_db() -> dict[int, pd.DataFrame]:
    out: dict[int, pd.DataFrame] = {}
    for tf in SUMMARY_TFS:
        out[tf] = _load_latest_summary_from_db(tf)
    return out


# ============================================================
# readiness wait
# ============================================================

def _collect_current_push_summaries() -> dict[int, pd.DataFrame]:
    out: dict[int, pd.DataFrame] = {}
    for tf in SUMMARY_TFS:
        df = _get_push_summary(tf)
        out[tf] = df
        _log_summary_profile("merged-current", tf, df)
    return out


def _wait_for_summary_ready(timeout_sec: float = SUMMARY_READY_WAIT_SEC) -> dict[int, pd.DataFrame]:
    """
    summary bootstrap が async の場合、短時間だけ merged summary が入るのを待つ。

    戻り値:
      tf -> DataFrame
    """
    start = time.monotonic()
    last_state: Optional[dict[str, bool]] = None

    while True:
        summary_map = _collect_current_push_summaries()
        if _has_any_summary(summary_map):
            logger.info(
                "[runtime_mode] summary ready from merged cache elapsed=%.2fs",
                time.monotonic() - start,
            )
            return summary_map

        state = _get_summary_bootstrap_state()
        last_state = state

        running = bool(state.get("started") and not state.get("done") and not state.get("failed"))
        done = bool(state.get("done"))
        failed = bool(state.get("failed"))

        if not running:
            logger.info(
                "[runtime_mode] summary bootstrap not running while waiting state=%s elapsed=%.2fs",
                state,
                time.monotonic() - start,
            )
            break

        if done or failed:
            logger.info(
                "[runtime_mode] summary bootstrap finished while waiting state=%s elapsed=%.2fs",
                state,
                time.monotonic() - start,
            )
            break

        elapsed = time.monotonic() - start
        if elapsed >= timeout_sec:
            logger.warning(
                "[runtime_mode] summary ready wait timeout elapsed=%.2fs state=%s",
                elapsed,
                state,
            )
            break

        logger.info(
            "[runtime_mode] waiting summary bootstrap elapsed=%.2fs state=%s",
            elapsed,
            state,
        )
        time.sleep(SUMMARY_READY_POLL_SEC)

    summary_map = _collect_current_push_summaries()
    if not _has_any_summary(summary_map):
        logger.warning(
            "[runtime_mode] merged summary still empty after wait state=%s -> try summary DB fallback",
            last_state,
        )

    return summary_map


def _prepare_summaries_for_immediate_scoring() -> dict[int, pd.DataFrame]:
    """
    immediate scoring 用の summary map を用意する。

    優先順位:
      1. 現在の global_data merged summary
      2. summary bootstrap 完了待ち後の merged summary
      3. summary DB 最新行 fallback
    """
    summary_map = _collect_current_push_summaries()

    if _has_any_summary(summary_map):
        return summary_map

    if _is_summary_bootstrap_running():
        logger.info("[runtime_mode] summary bootstrap running -> wait briefly before immediate scoring")
        summary_map = _wait_for_summary_ready(timeout_sec=SUMMARY_READY_WAIT_SEC)
        if _has_any_summary(summary_map):
            return summary_map

    logger.warning("[runtime_mode] no merged summary available -> loading latest summaries from summary DB")
    db_map = _load_latest_summaries_from_db()

    for tf, df in db_map.items():
        if df is not None and not df.empty:
            _set_push_summary(tf, df, reason="runtime_mode_summary_db_fallback")

    summary_map = _collect_current_push_summaries()
    if _has_any_summary(summary_map):
        logger.info("[runtime_mode] summary prepared by DB fallback")
        return summary_map

    logger.warning("[runtime_mode] summary unavailable after merged wait and DB fallback")
    return summary_map


# ============================================================
# immediate actions
# ============================================================

def _run_htf_immediate_sync() -> None:
    try:
        if hasattr(incremental_higher_tf_engine, "process"):
            incremental_higher_tf_engine.process()
        logger.info("⚡ HTF immediate sync done")
    except Exception:
        logger.exception("❌ HTF immediate sync failed")


def _run_scoring_immediate(summary_map: dict[int, pd.DataFrame]) -> bool:
    """
    immediate scoring を実行する。

    Returns
    -------
    bool
        1つ以上の tf で scoring を実行できた場合 True。
    """
    ran_any = False

    try:
        scoring_main = _get_scoring_main()
    except Exception:
        logger.exception("❌ scoring_main resolve failed")
        return False

    for tf in SUMMARY_TFS:
        df = summary_map.get(tf, pd.DataFrame())

        if df is None or df.empty:
            logger.info("⏸ scoring immediate skip tf=%s (merged summary empty)", tf)
            continue

        try:
            _log_summary_profile("scoring-input", tf, df)

            result = scoring_main(df, interval=tf)

            # scoring_main が DataFrame を返す場合はそれを保存。
            # None の場合は入力 df を維持。
            if isinstance(result, pd.DataFrame) and not result.empty:
                _log_summary_profile("scoring-output", tf, result)
                _set_push_summary(tf, result, reason="runtime_mode_scoring_output")
            else:
                _set_push_summary(tf, df, reason="runtime_mode_scoring_input_preserve")

            ran_any = True

        except Exception:
            logger.exception("❌ scoring immediate failed tf=%s", tf)

    if ran_any:
        logger.info("⚡ scoring immediate run done")
    else:
        logger.warning("⚠ scoring immediate skipped all tf because summary unavailable")

    return ran_any


def _run_entry_immediate(*, run_entry: bool, summary_ready: bool) -> None:
    if not run_entry:
        logger.info("⏸ entry pipeline skipped (run_entry=False)")
        return

    if not summary_ready:
        logger.warning(
            "⏸ entry pipeline immediate skipped reason=summary_not_ready"
        )
        return

    try:
        run_entry_pipeline()
        logger.info("⚡ entry pipeline immediate run done")
    except Exception:
        logger.exception("❌ entry pipeline immediate failed")


def _refresh_active_symbols_immediate() -> None:
    try:
        update_active_symbols(force=True)
        logger.info("⚡ active symbols refreshed")
    except Exception:
        logger.exception("❌ active symbol refresh failed")


# ============================================================
# public API
# ============================================================

def enter_realtime_combat_mode(run_entry: bool = True):
    """
    realtime combat mode を開始する。

    起動高速化のため summary bootstrap は background で走ることがある。
    そのため、ここでは immediate scoring / entry を急がず、
    summary readiness を確認してから実行する。
    """
    logger.info("🔥 ENTER REALTIME COMBAT MODE")

    _run_htf_immediate_sync()

    market_open_now = bool(is_market_open())

    summary_ready = False
    scoring_ran = False

    if market_open_now:
        try:
            summary_map = _prepare_summaries_for_immediate_scoring()
            summary_ready = _has_any_summary(summary_map)
            scoring_ran = _run_scoring_immediate(summary_map)
        except Exception:
            logger.exception("❌ scoring immediate orchestration failed")
            summary_ready = False
            scoring_ran = False
    else:
        logger.info("⏸ scoring immediate skipped (market closed mode)")

    # summary が無い場合、起動直後の未成熟状態で entry しない。
    # scoring_ran が False でも、summary_ready が True なら entry は許可する。
    _run_entry_immediate(
        run_entry=run_entry,
        summary_ready=bool(summary_ready or scoring_ran),
    )

    _refresh_active_symbols_immediate()

    logger.info(
        "🔥 REALTIME COMBAT MODE READY summary_ready=%s scoring_ran=%s run_entry=%s market_open=%s",
        summary_ready,
        scoring_ran,
        run_entry,
        market_open_now,
    )


__all__ = [
    "enter_realtime_combat_mode",
]