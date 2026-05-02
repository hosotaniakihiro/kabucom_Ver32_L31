# ============================================================
# File   : trading/entry/summary_ai/runner.py
# Version: PRODUCTION-STABLE-REV3.1-4ROUTE-SAFE-RUNNER-AI20
# ------------------------------------------------------------
# 【概要】
#   SUMMARY / PUSH / YAHOO / RANKING / TONOSAMA のAI ENTRY runner。
#
# 【重要】
#   - PUSH/Yahoo/通常SUMMARY:
#       slope > min_top10_slope を必須にする
#
#   - RANKING/RANKING_SUMMARY:
#       本物ATR/slopeは使えないため slope filter を強制しない
#       ranking_score / ranking_momentum / price_delta_pct 等で判定
#
#   - TONOSAMA:
#       殿様イナゴは候補抽出フィルタとして使う
#       最終発注はAI gate + executorに集約
#
# 【REV3.1 修正】
#   - AI確認候補数のデフォルトを 10 -> 20 に変更
#   - TONOSAMAフィルタ候補数のデフォルトも 10 -> 20 に変更
#   - runner.py単体でAI確認20銘柄化が有効になるように定数化
#
# 【ENV】
#   SUMMARY_AI_MIN_TOP10_SLOPE=0.03
#   ENTRY_MIN_BUY_SLOPE=0.03
#   ENTRY_BYPASS_SLOPE_FILTER=1
#
#   RANKING_AI_MIN_SCORE=0.0
#   RANKING_AI_MIN_MOMENTUM=0.0
# ============================================================

from __future__ import annotations

import logging
import math
import os
from typing import Any, Callable, Dict, Optional

import pandas as pd

from .ai_gate_runner import DEFAULT_MIN_AI_CONFIDENCE, run_ai_gate_for_candidates
from .candidates import (
    DEFAULT_MAX_SELL_SCORE,
    DEFAULT_MIN_BUY_SCORE,
    DEFAULT_MIN_PRICE,
    DEFAULT_MIN_VOLUME,
    DEFAULT_TOP_N,
    build_summary_ai_entry_candidates,
)
from .executor import DEFAULT_MAX_ENTRIES, execute_ai_ok_entries_bulk
from .utils import safe_df, to_records

logger = logging.getLogger(__name__)


DEFAULT_MIN_TOP10_SLOPE = 0.01

# AIに確認する候補数。
# 旧DEFAULT_TOP_Nが10でも、このrunnerでは20を優先する。
DEFAULT_AI_ENTRY_TOP_N = 20

# TONOSAMAフィルタを使う場合、ここが10のままだと
# top_n=20にしても前段で10銘柄に絞られるため20にする。
DEFAULT_TONOSAMA_AI_CANDIDATES = 20


# ============================================================
# env / safe helpers
# ============================================================

def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        x = float(v)
        if math.isnan(x) or math.isinf(x):
            return float(default)
        return x
    except Exception:
        return float(default)


def _env_bool(name: str, default: bool = False) -> bool:
    try:
        v = os.getenv(name)
        if v is None:
            return bool(default)

        s = str(v).strip().lower()
        if s in ("1", "true", "yes", "y", "on", "ok"):
            return True
        if s in ("0", "false", "no", "n", "off", "ng", ""):
            return False

        return bool(default)
    except Exception:
        return bool(default)


def _safe_bool(v: Any, default: bool = False) -> bool:
    try:
        if isinstance(v, bool):
            return v

        if v is None:
            return default

        s = str(v).strip().lower()
        if s in ("1", "true", "yes", "y", "on", "ok"):
            return True
        if s in ("0", "false", "no", "n", "off", "ng", ""):
            return False

        return default
    except Exception:
        return default


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        x = float(v)
        if math.isnan(x) or math.isinf(x):
            return default
        return x
    except Exception:
        return default


def _safe_source(source: Any, default: str = "SUMMARY") -> str:
    try:
        s = str(source or default).strip().upper()
        return s if s else default
    except Exception:
        return default


def _is_ranking_source(source: Any) -> bool:
    s = _safe_source(source, "")
    return "RANKING" in s


def _is_yahoo_source(source: Any) -> bool:
    s = _safe_source(source, "")
    return "YAHOO" in s


def _is_push_source(source: Any) -> bool:
    s = _safe_source(source, "")
    return "PUSH" in s or s in {"SUMMARY", "PUSH_SUMMARY", "STOCK_SUMMARY"}


def _is_tonosama_source(source: Any) -> bool:
    s = _safe_source(source, "")
    return "TONOSAMA" in s


def _requires_real_slope_filter(source: Any) -> bool:
    """
    本物OHLC由来だけ slope filter を必須にする。

    RANKING:
      擬似OHLCなので本物ATR/slope判定は不可。

    TONOSAMA:
      検知トリガー。PUSH/Yahoo情報が混ざっていれば候補化可。
    """
    if _is_ranking_source(source):
        return False

    if _is_tonosama_source(source):
        return False

    return True


def _resolve_min_top10_slope() -> float:
    v1 = os.getenv("SUMMARY_AI_MIN_TOP10_SLOPE")
    if v1 is not None and str(v1).strip() != "":
        return _env_float("SUMMARY_AI_MIN_TOP10_SLOPE", DEFAULT_MIN_TOP10_SLOPE)

    v2 = os.getenv("ENTRY_MIN_BUY_SLOPE")
    if v2 is not None and str(v2).strip() != "":
        return _env_float("ENTRY_MIN_BUY_SLOPE", DEFAULT_MIN_TOP10_SLOPE)

    return float(DEFAULT_MIN_TOP10_SLOPE)


def _select_slope_col(df: pd.DataFrame) -> Optional[str]:
    if df is None or df.empty:
        return None

    for c in (
        "slope_atr_scaled",
        "score_slope",
        "slope",
        "disp_slope",
    ):
        if c in df.columns:
            return c

    return None


def _empty_result(
    *,
    dry_run: bool,
    error: Optional[str] = None,
    skip_reason: Optional[str] = None,
) -> Dict[str, Any]:
    execution = {
        "executed": False,
        "dry_run": dry_run,
        "approved_rows": [],
        "result": None,
        "skip_reason": skip_reason or error,
    }

    out: Dict[str, Any] = {
        "candidates": [],
        "ai_results": [],
        "ai_ok": [],
        "approved_rows": [],
        "execution": execution,
        "dry_run": dry_run,
    }

    if error:
        out["error"] = error

    if skip_reason:
        out["skip_reason"] = skip_reason

    return out


def _coalesce_confidence(
    *,
    min_ai_confidence: Optional[float] = None,
    min_confidence: Optional[float] = None,
    min_conf: Optional[float] = None,
) -> float:
    for v in (min_conf, min_confidence, min_ai_confidence):
        try:
            if v is not None:
                return float(v)
        except Exception:
            pass

    return float(DEFAULT_MIN_AI_CONFIDENCE)


def _resolve_summary_df(
    summary_df: Optional[pd.DataFrame] = None,
    df: Optional[pd.DataFrame] = None,
) -> pd.DataFrame:
    if isinstance(summary_df, pd.DataFrame):
        return safe_df(summary_df)

    if isinstance(df, pd.DataFrame):
        return safe_df(df)

    return pd.DataFrame()


# ============================================================
# ranking pre-filter
# ============================================================

def _apply_ranking_pre_filter(
    df: pd.DataFrame,
    *,
    source: str,
    interval: int | str,
    enabled: bool = True,
    min_ranking_score: Optional[float] = None,
    min_ranking_momentum: Optional[float] = None,
) -> pd.DataFrame:
    """
    RANKING/RANKING_SUMMARY用のAI前フィルタ。

    本物ATR/slopeは使わない。
    以下のいずれかがプラスなら通す:
      - ranking_score
      - ranking_momentum
      - price_delta_pct
      - price_delta
      - rank_improve
      - volume_delta
    """
    if df is None or df.empty:
        return pd.DataFrame()

    if not enabled:
        return df

    out = df.copy()

    min_score = (
        float(min_ranking_score)
        if min_ranking_score is not None
        else _env_float("RANKING_AI_MIN_SCORE", 0.0)
    )
    min_mom = (
        float(min_ranking_momentum)
        if min_ranking_momentum is not None
        else _env_float("RANKING_AI_MIN_MOMENTUM", 0.0)
    )

    for c in (
        "ranking_score",
        "ranking_momentum",
        "price_delta_pct",
        "price_delta",
        "rank_improve",
        "volume_delta",
    ):
        if c not in out.columns:
            out[c] = 0.0
        out[c] = pd.to_numeric(out[c], errors="coerce").fillna(0.0)

    before = len(out)

    mask = (
        (out["ranking_score"] > min_score)
        | (out["ranking_momentum"] > min_mom)
        | (out["price_delta_pct"] > 0)
        | (out["price_delta"] > 0)
        | (out["rank_improve"] > 0)
        | (out["volume_delta"] > 0)
    )

    skipped_df = out[~mask].copy()
    out = out[mask].copy()

    try:
        skipped_head = (
            skipped_df[
                [
                    c
                    for c in (
                        "symbol",
                        "ranking_score",
                        "ranking_momentum",
                        "price_delta_pct",
                        "rank_improve",
                        "volume_delta",
                    )
                    if c in skipped_df.columns
                ]
            ]
            .head(30)
            .to_dict(orient="records")
        )
    except Exception:
        skipped_head = []

    logger.info(
        "[SUMMARY AI RUNNER] RANKING_PRE_FILTER result source=%s interval=%s "
        "before=%s after=%s skipped=%s min_score=%.4f min_momentum=%.4f skipped_head=%s",
        source,
        interval,
        before,
        len(out),
        before - len(out),
        min_score,
        min_mom,
        skipped_head,
    )

    return out


# ============================================================
# slope pre-filter for PUSH/Yahoo/real summary
# ============================================================

def _apply_top10_pre_slope_filter(
    df: pd.DataFrame,
    *,
    source: str,
    interval: int | str,
    enabled: bool = True,
    min_slope: Optional[float] = None,
) -> pd.DataFrame:
    """
    TOP候補抽出前に slope が弱い銘柄を除外する。

    注意:
      RANKING/RANKING_SUMMARYでは呼ばない。
    """
    if df is None or df.empty:
        return pd.DataFrame()

    if not enabled:
        logger.warning(
            "[SUMMARY AI RUNNER] TOP_PRE_SLOPE disabled source=%s interval=%s rows=%s",
            source,
            interval,
            len(df),
        )
        return df

    if _env_bool("ENTRY_BYPASS_SLOPE_FILTER", False):
        logger.warning(
            "[SUMMARY AI RUNNER] TOP_PRE_SLOPE bypassed by ENTRY_BYPASS_SLOPE_FILTER "
            "source=%s interval=%s rows=%s",
            source,
            interval,
            len(df),
        )
        return df

    min_slope_v = float(min_slope) if min_slope is not None else _resolve_min_top10_slope()

    out = df.copy()
    slope_col = _select_slope_col(out)

    if slope_col is None:
        logger.warning(
            "[SUMMARY AI RUNNER] TOP_PRE_SLOPE skipped because slope column missing "
            "source=%s interval=%s rows=%s cols=%s",
            source,
            interval,
            len(out),
            list(out.columns),
        )
        return out

    out[slope_col] = pd.to_numeric(out[slope_col], errors="coerce").fillna(0.0)

    before = len(out)
    skipped_df = out[out[slope_col] <= min_slope_v].copy()
    out = out[out[slope_col] > min_slope_v].copy()

    try:
        skipped_symbols = (
            skipped_df[["symbol", slope_col]]
            .head(30)
            .to_dict(orient="records")
            if "symbol" in skipped_df.columns
            else []
        )
    except Exception:
        skipped_symbols = []

    logger.info(
        "[SUMMARY AI RUNNER] TOP_PRE_SLOPE result source=%s interval=%s "
        "slope_col=%s condition='%s > %.4f' before=%s after=%s skipped=%s skipped_head=%s",
        source,
        interval,
        slope_col,
        slope_col,
        min_slope_v,
        before,
        len(out),
        before - len(out),
        skipped_symbols,
    )

    return out


def _apply_source_pre_filter(
    df: pd.DataFrame,
    *,
    source: str,
    interval: int | str,
    use_pre_slope_filter: bool,
    min_top10_slope: Optional[float],
    min_ranking_score: Optional[float],
    min_ranking_momentum: Optional[float],
) -> pd.DataFrame:
    """
    source別のAI前フィルタ統合。
    """
    if df is None or df.empty:
        return pd.DataFrame()

    if _is_ranking_source(source):
        return _apply_ranking_pre_filter(
            df,
            source=source,
            interval=interval,
            enabled=True,
            min_ranking_score=min_ranking_score,
            min_ranking_momentum=min_ranking_momentum,
        )

    if _requires_real_slope_filter(source):
        return _apply_top10_pre_slope_filter(
            df,
            source=source,
            interval=interval,
            enabled=use_pre_slope_filter,
            min_slope=min_top10_slope,
        )

    logger.info(
        "[SUMMARY AI RUNNER] source pre-filter pass-through source=%s interval=%s rows=%s",
        source,
        interval,
        len(df),
    )
    return df


# ============================================================
# tonosama helpers
# ============================================================

def _resolve_tonosama_ranking_db_path(ranking_db_path: Any = None) -> Optional[Any]:
    if ranking_db_path:
        return ranking_db_path

    try:
        from trading.ranking.tonosama.db_path import resolve_existing_ranking_db_path

        return resolve_existing_ranking_db_path()
    except Exception:
        logger.exception("[SUMMARY AI RUNNER] tonosama ranking db path resolve failed")
        return None


def _apply_tonosama_filter_before_candidate_build(
    df_original: pd.DataFrame,
    *,
    use_tonosama_filter: bool,
    tonosama_ranking_db_path: Any = None,
    tonosama_max_candidates: int = DEFAULT_TONOSAMA_AI_CANDIDATES,
    fail_open_tonosama: bool = True,
) -> pd.DataFrame:
    """
    summary_df を ranking_snapshot_1min 由来の殿様候補で絞る。
    """
    if not use_tonosama_filter:
        return df_original

    if df_original is None or df_original.empty:
        return pd.DataFrame()

    try:
        from trading.entry.summary_ai.tonosama_bridge import filter_summary_by_ranking_tonosama
    except Exception:
        logger.exception("[SUMMARY AI RUNNER] tonosama bridge import failed")
        return df_original if fail_open_tonosama else pd.DataFrame()

    ranking_db_path = _resolve_tonosama_ranking_db_path(tonosama_ranking_db_path)

    if ranking_db_path is None:
        logger.warning("[SUMMARY AI RUNNER] TONOSAMA ranking db not found")
        return df_original if fail_open_tonosama else pd.DataFrame()

    try:
        tonosama_df = filter_summary_by_ranking_tonosama(
            summary_df=df_original,
            ranking_db_path=ranking_db_path,
            max_candidates=tonosama_max_candidates,
        )

        if tonosama_df is None or tonosama_df.empty:
            logger.warning(
                "[SUMMARY AI RUNNER] TONOSAMA no matched candidates fail_open=%s",
                fail_open_tonosama,
            )
            return df_original if fail_open_tonosama else pd.DataFrame()

        try:
            symbols = (
                list(tonosama_df["symbol"].astype(str).head(tonosama_max_candidates))
                if "symbol" in tonosama_df.columns
                else []
            )
        except Exception:
            symbols = []

        logger.warning(
            "[SUMMARY AI RUNNER] TONOSAMA ENABLED filtered rows %s -> %s max_candidates=%s symbols=%s",
            len(df_original),
            len(tonosama_df),
            tonosama_max_candidates,
            symbols,
        )

        return tonosama_df

    except Exception:
        logger.exception("[SUMMARY AI RUNNER] TONOSAMA filter failed")
        return df_original if fail_open_tonosama else pd.DataFrame()


# ============================================================
# optional dedupe guard
# ============================================================

def _apply_optional_entry_dedupe_guard(ai_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    entry_dedupe_guard.py が存在する場合だけ使う。
    存在しない環境でも runner が落ちないようにする。
    """
    if not ai_results:
        return ai_results

    try:
        from trading.entry.summary_ai.entry_dedupe_guard import (
            can_attempt_entry,
            mark_entry_attempt,
        )
    except Exception:
        return ai_results

    out: list[dict[str, Any]] = []

    for r in ai_results:
        if not isinstance(r, dict):
            continue

        if not bool(r.get("allow")):
            out.append(r)
            continue

        symbol = str(r.get("symbol") or "").strip()
        ok, reason = can_attempt_entry(symbol, cooldown_sec=300)

        if not ok:
            rr = dict(r)
            rr["allow"] = False
            rr["reason"] = f"{rr.get('reason', '')}|dedupe_skip:{reason}"
            logger.info(
                "[SUMMARY AI RUNNER] AI_OK canceled by dedupe guard symbol=%s reason=%s",
                symbol,
                reason,
            )
            out.append(rr)
            continue

        mark_entry_attempt(symbol)
        out.append(r)

    return out


# ============================================================
# main public runner
# ============================================================

def run_summary_ai_entry_from_df(
    summary_df: Optional[pd.DataFrame] = None,
    *,
    df: Optional[pd.DataFrame] = None,
    interval: int | str = 1,
    source: str = "SUMMARY",
    top_n: int = DEFAULT_AI_ENTRY_TOP_N,
    max_entries: int = DEFAULT_MAX_ENTRIES,
    min_ai_confidence: Optional[float] = DEFAULT_MIN_AI_CONFIDENCE,
    min_confidence: Optional[float] = None,
    min_conf: Optional[float] = None,
    min_buy_score: float = DEFAULT_MIN_BUY_SCORE,
    max_sell_score: float = DEFAULT_MAX_SELL_SCORE,
    min_volume: float = DEFAULT_MIN_VOLUME,
    min_price: float = DEFAULT_MIN_PRICE,
    require_buy_target: bool = False,
    exclude_etf_fund: bool = True,
    require_market_open: bool = True,
    dry_run: bool = False,
    default_dominant_ratio: float = 1.0,
    entry_pipeline: Optional[Callable[..., Any]] = None,
    now: Any = None,

    # TONOSAMA options
    use_tonosama_filter: bool = True,
    tonosama_ranking_db_path: Any = None,
    tonosama_max_candidates: int = DEFAULT_TONOSAMA_AI_CANDIDATES,
    fail_open_tonosama: bool = True,

    # source pre filter
    use_pre_slope_filter: bool = True,
    min_top10_slope: Optional[float] = None,
    min_ranking_score: Optional[float] = None,
    min_ranking_momentum: Optional[float] = None,

    # optional
    use_entry_dedupe_guard: bool = True,

    **kwargs: Any,
) -> Dict[str, Any]:
    confidence = _coalesce_confidence(
        min_ai_confidence=min_ai_confidence,
        min_confidence=min_confidence,
        min_conf=min_conf,
    )
    source_s = _safe_source(source)

    # kwargs compatibility
    if "source" in kwargs:
        source_s = _safe_source(kwargs.pop("source"), source_s)

    if "top_n" in kwargs:
        try:
            top_n = int(kwargs.pop("top_n"))
        except Exception:
            top_n = DEFAULT_AI_ENTRY_TOP_N

    if "max_candidates" in kwargs:
        try:
            top_n = int(kwargs.pop("max_candidates"))
        except Exception:
            pass

    if "candidate_limit" in kwargs:
        try:
            top_n = int(kwargs.pop("candidate_limit"))
        except Exception:
            pass

    if top_n <= 0:
        top_n = DEFAULT_AI_ENTRY_TOP_N

    if "use_tonosama_filter" in kwargs:
        use_tonosama_filter = _safe_bool(kwargs.pop("use_tonosama_filter"), use_tonosama_filter)

    if "tonosama_ranking_db_path" in kwargs:
        tonosama_ranking_db_path = kwargs.pop("tonosama_ranking_db_path")

    if "tonosama_max_candidates" in kwargs:
        try:
            tonosama_max_candidates = int(kwargs.pop("tonosama_max_candidates"))
        except Exception:
            tonosama_max_candidates = DEFAULT_TONOSAMA_AI_CANDIDATES

    if tonosama_max_candidates <= 0:
        tonosama_max_candidates = DEFAULT_TONOSAMA_AI_CANDIDATES

    if "fail_open_tonosama" in kwargs:
        fail_open_tonosama = _safe_bool(kwargs.pop("fail_open_tonosama"), fail_open_tonosama)

    if "use_pre_slope_filter" in kwargs:
        use_pre_slope_filter = _safe_bool(kwargs.pop("use_pre_slope_filter"), use_pre_slope_filter)

    if "min_top10_slope" in kwargs:
        try:
            min_top10_slope = float(kwargs.pop("min_top10_slope"))
        except Exception:
            min_top10_slope = None

    if "min_ranking_score" in kwargs:
        try:
            min_ranking_score = float(kwargs.pop("min_ranking_score"))
        except Exception:
            min_ranking_score = None

    if "min_ranking_momentum" in kwargs:
        try:
            min_ranking_momentum = float(kwargs.pop("min_ranking_momentum"))
        except Exception:
            min_ranking_momentum = None

    if "use_entry_dedupe_guard" in kwargs:
        use_entry_dedupe_guard = _safe_bool(
            kwargs.pop("use_entry_dedupe_guard"),
            use_entry_dedupe_guard,
        )

    resolved_min_slope = (
        float(min_top10_slope)
        if min_top10_slope is not None
        else _resolve_min_top10_slope()
    )

    try:
        logger.info(
            "[SUMMARY AI RUNNER] start interval=%s source=%s top_n=%s max_entries=%s "
            "dry_run=%s min_conf=%.2f min_buy=%.2f max_sell=%.2f "
            "min_vol=%.1f min_price=%.1f now=%s tonosama=%s tonosama_max=%s fail_open=%s "
            "pre_slope=%s min_top10_slope=%.4f ranking_source=%s real_slope_required=%s",
            interval,
            source_s,
            top_n,
            max_entries,
            dry_run,
            confidence,
            min_buy_score,
            max_sell_score,
            min_volume,
            min_price,
            now,
            use_tonosama_filter,
            tonosama_max_candidates,
            fail_open_tonosama,
            use_pre_slope_filter,
            resolved_min_slope,
            _is_ranking_source(source_s),
            _requires_real_slope_filter(source_s),
        )

        df_original = _resolve_summary_df(summary_df=summary_df, df=df)

        if df_original.empty:
            logger.info(
                "[SUMMARY AI RUNNER] summary_df empty interval=%s source=%s",
                interval,
                source_s,
            )
            return _empty_result(
                dry_run=dry_run,
                error="empty_summary_df",
                skip_reason="empty_summary_df",
            )

        # TONOSAMA filter は通常SUMMARY/PUSH/Yahooにだけかける。
        # RANKING_SUMMARY自体にかけると候補が消えやすいため除外。
        apply_tonosama = bool(use_tonosama_filter) and not _is_ranking_source(source_s)

        df_for_candidates = _apply_tonosama_filter_before_candidate_build(
            df_original,
            use_tonosama_filter=apply_tonosama,
            tonosama_ranking_db_path=tonosama_ranking_db_path,
            tonosama_max_candidates=tonosama_max_candidates,
            fail_open_tonosama=fail_open_tonosama,
        )

        if df_for_candidates is None or df_for_candidates.empty:
            logger.warning(
                "[SUMMARY AI RUNNER] no df after TONOSAMA filter interval=%s source=%s fail_open=%s",
                interval,
                source_s,
                fail_open_tonosama,
            )
            return _empty_result(
                dry_run=dry_run,
                skip_reason="no_tonosama_candidates",
            )

        before_pre = len(df_for_candidates)

        df_for_candidates = _apply_source_pre_filter(
            df_for_candidates,
            source=source_s,
            interval=interval,
            use_pre_slope_filter=use_pre_slope_filter,
            min_top10_slope=resolved_min_slope,
            min_ranking_score=min_ranking_score,
            min_ranking_momentum=min_ranking_momentum,
        )

        after_pre = len(df_for_candidates) if isinstance(df_for_candidates, pd.DataFrame) else 0

        logger.info(
            "[SUMMARY AI RUNNER] source pre-filter result interval=%s source=%s "
            "before=%s after=%s skipped=%s",
            interval,
            source_s,
            before_pre,
            after_pre,
            before_pre - after_pre,
        )

        if df_for_candidates is None or df_for_candidates.empty:
            return _empty_result(
                dry_run=dry_run,
                skip_reason="no_candidates_after_source_pre_filter",
            )

        candidates_df = build_summary_ai_entry_candidates(
            df_for_candidates,
            interval=interval,
            top_n=top_n,
            min_buy_score=min_buy_score,
            max_sell_score=max_sell_score,
            min_volume=min_volume,
            min_price=min_price,
            require_buy_target=require_buy_target,
            exclude_etf_fund=exclude_etf_fund,
            source=source_s,
        )

        if candidates_df is None or candidates_df.empty:
            logger.info(
                "[SUMMARY AI RUNNER] no AI candidates interval=%s source=%s rows=%s top_n=%s",
                interval,
                source_s,
                len(df_for_candidates),
                top_n,
            )
            return _empty_result(
                dry_run=dry_run,
                skip_reason="no_candidates",
            )

        try:
            symbols_head = list(candidates_df["symbol"].astype(str).head(top_n))
        except Exception:
            symbols_head = []

        logger.info(
            "[SUMMARY AI RUNNER] candidates ready count=%s requested_top_n=%s symbols=%s",
            len(candidates_df),
            top_n,
            symbols_head,
        )

        candidates = to_records(candidates_df)

        ai_results = run_ai_gate_for_candidates(
            candidates_df,
            interval=interval,
            source=source_s,
            min_ai_confidence=confidence,
            default_dominant_ratio=default_dominant_ratio,
        )

        if not isinstance(ai_results, list):
            logger.warning(
                "[SUMMARY AI RUNNER] ai_results non-list type=%s -> treat empty",
                type(ai_results).__name__,
            )
            ai_results = []

        if use_entry_dedupe_guard:
            ai_results = _apply_optional_entry_dedupe_guard(ai_results)

        ai_ok = [
            x
            for x in ai_results
            if isinstance(x, dict) and bool(x.get("allow"))
        ]

        logger.info(
            "[SUMMARY AI RUNNER] AI gate done candidates=%s ok=%s ng=%s",
            len(ai_results),
            len(ai_ok),
            len(ai_results) - len(ai_ok),
        )

        if ai_ok:
            logger.info(
                "[SUMMARY AI RUNNER] AI_OK symbols=%s",
                [str(x.get("symbol")) for x in ai_ok],
            )
        else:
            logger.warning(
                "[SUMMARY AI RUNNER] no AI_OK symbols interval=%s source=%s",
                interval,
                source_s,
            )

        execution = execute_ai_ok_entries_bulk(
            ai_results,
            df_summary=df_original,
            interval=interval,
            max_entries=max_entries,
            dry_run=dry_run,
            require_market_open=require_market_open,
            entry_pipeline=entry_pipeline,
        )

        if not isinstance(execution, dict):
            execution = {
                "executed": False,
                "dry_run": dry_run,
                "approved_rows": [],
                "result": execution,
                "skip_reason": "executor_returned_non_dict",
            }

        approved_rows = execution.get("approved_rows", [])
        if approved_rows is None:
            approved_rows = []

        logger.info(
            "[SUMMARY AI RUNNER] executor returned executed=%s dry_run=%s approved=%s skip=%s",
            bool(execution.get("executed")),
            bool(execution.get("dry_run")) if "dry_run" in execution else dry_run,
            len(approved_rows),
            execution.get("skip_reason"),
        )

        if approved_rows:
            logger.info(
                "[SUMMARY AI RUNNER] approved symbols=%s",
                [str(x.get("symbol")) for x in approved_rows if isinstance(x, dict)],
            )
        else:
            logger.warning(
                "[SUMMARY AI RUNNER] no approved rows after executor interval=%s source=%s",
                interval,
                source_s,
            )

        logger.info(
            "[SUMMARY AI RUNNER] done candidates=%s ai_ok=%s approved=%s executed=%s dry_run=%s skip=%s",
            len(candidates),
            len(ai_ok),
            len(approved_rows),
            bool(execution.get("executed")),
            dry_run,
            execution.get("skip_reason"),
        )

        return {
            "candidates": candidates,
            "ai_results": ai_results,
            "ai_ok": ai_ok,
            "approved_rows": approved_rows,
            "execution": execution,
            "dry_run": dry_run,
        }

    except Exception:
        logger.exception("[SUMMARY AI RUNNER] failed")
        return _empty_result(
            dry_run=dry_run,
            error="runner_exception",
            skip_reason="runner_exception",
        )


# ============================================================
# public aliases
# ============================================================

def run_summary_ai_entry(
    summary_df: Optional[pd.DataFrame] = None,
    *,
    df: Optional[pd.DataFrame] = None,
    interval: int | str = 1,
    **kwargs: Any,
) -> Dict[str, Any]:
    return run_summary_ai_entry_from_df(
        summary_df=summary_df,
        df=df,
        interval=interval,
        **kwargs,
    )


def run_push_summary_ai_entry(
    summary_df: Optional[pd.DataFrame] = None,
    *,
    df: Optional[pd.DataFrame] = None,
    interval: int | str = 1,
    source: str = "PUSH_SUMMARY",
    **kwargs: Any,
) -> Dict[str, Any]:
    kwargs.pop("source", None)

    return run_summary_ai_entry_from_df(
        summary_df=summary_df,
        df=df,
        interval=interval,
        source=source or "PUSH_SUMMARY",
        **kwargs,
    )


def run_yahoo_summary_ai_entry(
    summary_df: Optional[pd.DataFrame] = None,
    *,
    df: Optional[pd.DataFrame] = None,
    interval: int | str = 1,
    source: str = "YAHOO_SUMMARY",
    **kwargs: Any,
) -> Dict[str, Any]:
    kwargs.pop("source", None)

    return run_summary_ai_entry_from_df(
        summary_df=summary_df,
        df=df,
        interval=interval,
        source=source or "YAHOO_SUMMARY",
        **kwargs,
    )


def run_ranking_summary_ai_entry(
    summary_df: Optional[pd.DataFrame] = None,
    *,
    df: Optional[pd.DataFrame] = None,
    interval: int | str = 1,
    source: str = "RANKING_SUMMARY",
    **kwargs: Any,
) -> Dict[str, Any]:
    kwargs.pop("source", None)

    # ランキング由来では殿様フィルタとslopeフィルタを強制しない。
    kwargs.setdefault("use_tonosama_filter", False)
    kwargs.setdefault("use_pre_slope_filter", False)

    return run_summary_ai_entry_from_df(
        summary_df=summary_df,
        df=df,
        interval=interval,
        source=source or "RANKING_SUMMARY",
        **kwargs,
    )


def run_tonosama_summary_ai_entry(
    summary_df: Optional[pd.DataFrame] = None,
    *,
    df: Optional[pd.DataFrame] = None,
    interval: int | str = 1,
    source: str = "TONOSAMA",
    **kwargs: Any,
) -> Dict[str, Any]:
    kwargs.pop("source", None)

    return run_summary_ai_entry_from_df(
        summary_df=summary_df,
        df=df,
        interval=interval,
        source=source or "TONOSAMA",
        **kwargs,
    )


def run_summary_ai_gate(
    summary_df: Optional[pd.DataFrame] = None,
    *,
    df: Optional[pd.DataFrame] = None,
    interval: int | str = 1,
    **kwargs: Any,
) -> Dict[str, Any]:
    return run_summary_ai_entry_from_df(
        summary_df=summary_df,
        df=df,
        interval=interval,
        **kwargs,
    )


def run_ai_gate_once(
    summary_df: Optional[pd.DataFrame] = None,
    *,
    df: Optional[pd.DataFrame] = None,
    interval: int | str = 1,
    **kwargs: Any,
) -> Dict[str, Any]:
    return run_summary_ai_entry_from_df(
        summary_df=summary_df,
        df=df,
        interval=interval,
        **kwargs,
    )


__all__ = [
    "run_summary_ai_entry_from_df",
    "run_summary_ai_entry",
    "run_push_summary_ai_entry",
    "run_yahoo_summary_ai_entry",
    "run_ranking_summary_ai_entry",
    "run_tonosama_summary_ai_entry",
    "run_summary_ai_gate",
    "run_ai_gate_once",
]