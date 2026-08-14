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
from functools import wraps
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
# ------------------------------------------------------------
# 旧 core/startup/summary_ai_ranking_prefilter_signal_patch.py
# (V4-RANKING-PREFILTER-SIGNAL-FAST-ENTRY-EXIT-STAGNATION) を本文化。
#
# 旧パッチは _apply_ranking_pre_filter を「元実装を一切呼ばない完全な差し替え」
# として上書きし、さらに watcher スレッドが毎秒 install を再適用していたため、
# 同じ属性を奪い合っていた
#   - summary_ai_ranking_prefilter_score_fallback_patch
#   - summary_ai_ranking_best_rank_bridge_patch
# は実運用では一度も実効しなかった (両者とも `_original` を辿って素の実装を
# 掴み直す設計のため、後勝ちで signal 版に置き換えられる)。
# 実際の install 順 (usercustomize.py 615->616 / strict_board_rest 332->333) を
# 再現したシミュレーションで、最終的に残るのが signal 版であることと、
# 元実装が呼ばれないことを確認済み。
#
# 低スコア行を無条件通過させる fail-open ではない。既存の ranking シグナル
# 列 (display_score / score_buy / best_rank / rank_type 等) が存在する場合に
# 限って ranking_score / ranking_momentum へ橋渡しする。
# ============================================================

_RANKING_PREFILTER_VERSION = "V4-RANKING-PREFILTER-SIGNAL-FAST-ENTRY-EXIT-STAGNATION"


def _rpf_env_float(name: str, default: float) -> float:
    """ranking pre-filter 用 env 読み出し (カンマ区切りを許容する)。"""
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        x = float(str(v).replace(",", ""))
        if math.isnan(x) or math.isinf(x):
            return float(default)
        return x
    except Exception:
        return float(default)


def _rpf_num_series(df: Any, col: str):
    if col not in df.columns:
        return pd.Series(0.0, index=df.index)
    s = df[col]
    try:
        if getattr(s, "dtype", None) == object:
            s = (
                s.astype(str)
                .str.replace("％", "%", regex=False)
                .str.replace("%", "", regex=False)
                .str.replace(",", "", regex=False)
                .str.replace("+", "", regex=False)
            )
    except Exception:
        pass
    return pd.to_numeric(s, errors="coerce").fillna(0.0).astype(float)


def _rpf_num_first(df: Any, cols: tuple[str, ...]):
    out = pd.Series(0.0, index=df.index)
    for col in cols:
        if col in df.columns:
            v = _rpf_num_series(df, col)
            out = out.where(out.abs() > 0, v)
    return out.fillna(0.0)


def _rpf_str_series(df: Any, col: str):
    if col not in df.columns:
        return pd.Series("", index=df.index)
    return df[col].astype(str).fillna("").str.strip()


def _rpf_pct_to_ratio(s: Any):
    x = s.fillna(0.0).astype(float)
    try:
        return x.where(x.abs() <= 1.0, x / 100.0)
    except Exception:
        return x


def _rpf_max_series(*series: Any):
    frames = []
    for s in series:
        try:
            frames.append(pd.to_numeric(s, errors="coerce").fillna(0.0).astype(float))
        except Exception:
            pass
    if not frames:
        return None
    return pd.concat(frames, axis=1).max(axis=1).fillna(0.0)


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
    ranking_score / ranking_momentum が全0でも、既存の ranking シグナル列
    (display_score / score_buy / score_sell / best_rank / rank_type、
     値幅・出来高急増列) から署名を再構成して判定する。
    """
    if df is None or getattr(df, "empty", True):
        return pd.DataFrame()

    if not enabled:
        return df

    out = df.copy()
    min_score = (
        float(min_ranking_score)
        if min_ranking_score is not None
        else _rpf_env_float("RANKING_AI_MIN_SCORE", 0.0)
    )
    min_mom = (
        float(min_ranking_momentum)
        if min_ranking_momentum is not None
        else _rpf_env_float("RANKING_AI_MIN_MOMENTUM", 0.0)
    )

    ranking_score = _rpf_num_series(out, "ranking_score")
    ranking_momentum = _rpf_num_series(out, "ranking_momentum")
    price_delta_pct = _rpf_num_series(out, "price_delta_pct")
    price_delta = _rpf_num_series(out, "price_delta")
    rank_improve = _rpf_num_series(out, "rank_improve")
    volume_delta = _rpf_num_series(out, "volume_delta")

    score_cols = (
        "score_buy", "buy_score", "score_sell", "sell_score", "score_total",
        "final_score", "display_score", "score", "pending_score", "priority",
        "score_mtf", "mtf_score", "rank_strength", "summary_ai_priority",
    )
    score_signal = pd.Series(0.0, index=out.index)
    present_score_cols: list[str] = []
    for col in score_cols:
        if col in out.columns:
            present_score_cols.append(col)
            score_signal = _rpf_max_series(score_signal, _rpf_num_series(out, col).abs())

    rank_cols = (
        "best_rank", "best_rank_position", "rank_position", "rank",
        "ranking_rank", "rank_no", "display_rank", "順位",
    )
    rank_value = _rpf_num_first(out, rank_cols)
    present_rank_cols = [c for c in rank_cols if c in out.columns]
    max_rank = int(max(1, _rpf_env_float("RANKING_AI_PREFILTER_MAX_BEST_RANK", 50.0)))
    best_rank_signal = (rank_value > 0) & (rank_value <= max_rank)
    rank_score_signal = (
        ((float(max_rank) + 1.0 - rank_value) / float(max_rank))
        .where(best_rank_signal, 0.0)
        .clip(lower=0.0)
    )

    rank_type = (
        _rpf_str_series(out, "rank_type")
        if "rank_type" in out.columns
        else _rpf_str_series(out, "ranking_type")
    )
    ranking_type_signal = rank_type.ne("") & ~rank_type.str.lower().isin({"nan", "none", "<na>", "0"})

    change_signal = pd.Series(0.0, index=out.index)
    for col in ("change_rate", "change_ratio", "change_percentage", "change_pct", "騰落率", "rate"):
        if col in out.columns:
            change_signal = _rpf_max_series(change_signal, _rpf_pct_to_ratio(_rpf_num_series(out, col)))

    range_signal = pd.Series(False, index=out.index)
    for col in ("range_pct", "intraday_range_pct", "day_range_pct", "_intrabar_range_pct", "_max_price_change_pct"):
        if col in out.columns:
            rv = _rpf_pct_to_ratio(_rpf_num_series(out, col).abs())
            range_signal = range_signal | (rv >= _rpf_env_float("RANKING_AI_PREFILTER_MIN_RANGE_PCT", 0.0015))

    volume_signal = pd.Series(False, index=out.index)
    for col in ("volume_surge_ratio", "volume_surge_ratio_1m", "volume_surge_ratio_3m", "volume_surge_ratio_5m"):
        if col in out.columns:
            volume_signal = volume_signal | (
                _rpf_num_series(out, col) >= _rpf_env_float("RANKING_AI_PREFILTER_MIN_SURGE_RATIO", 1.2)
            )

    out["ranking_score"] = _rpf_max_series(ranking_score, score_signal, rank_score_signal)
    out["ranking_momentum"] = _rpf_max_series(ranking_momentum, price_delta_pct, change_signal.clip(lower=0.0))
    out["price_delta_pct"] = _rpf_max_series(price_delta_pct, change_signal)
    out["rank_improve"] = _rpf_max_series(rank_improve, best_rank_signal.astype(float) * 0.001)
    out["volume_delta"] = _rpf_max_series(volume_delta, volume_signal.astype(float) * 0.001)

    mask = (
        (out["ranking_score"] > min_score)
        | (out["ranking_momentum"] > min_mom)
        | (out["price_delta_pct"] > 0)
        | (price_delta > 0)
        | (out["rank_improve"] > 0)
        | (out["volume_delta"] > 0)
        | (ranking_type_signal & (range_signal | volume_signal | (score_signal > 0)))
    )

    before = len(out)
    skipped_df = out.loc[~mask].copy()
    out = out.loc[mask].copy()

    try:
        skipped_head = (
            skipped_df[
                [
                    c
                    for c in (
                        "symbol", "ranking_score", "ranking_momentum", "display_score",
                        "score_buy", "score_sell", "best_rank", "best_rank_position",
                        "rank_position", "rank", "rank_type", "range_pct",
                    )
                    if c in skipped_df.columns
                ]
            ]
            .head(20)
            .to_dict(orient="records")
        )
    except Exception:
        skipped_head = []

    logger.warning(
        "[SUMMARY AI RANKING PREFILTER SIGNAL] result source=%s interval=%s before=%s after=%s "
        "skipped=%s min_score=%.4f min_mom=%.4f score_cols=%s rank_cols=%s max_rank=%s "
        "signal_rows=%s version=%s skipped_head=%s",
        source, interval, before, len(out), before - len(out), min_score, min_mom,
        present_score_cols, present_rank_cols, max_rank, int(mask.sum()),
        _RANKING_PREFILTER_VERSION, skipped_head,
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

os.environ.setdefault("SUMMARY_AI_PREORDER_DEDUPE_ENABLED", "0")


def _apply_optional_entry_dedupe_guard(ai_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """事前重複/クールダウンガード (公開エントリポイント)。

    旧 core/startup/summary_ai_dedupe_fix_runtime_patch が、
    SUMMARY_AI_PREORDER_DEDUPE_ENABLED (既定 "0"=OFF) が無効な間は
    dedupe/cooldown 判定を完全にスキップして ai_results をそのまま返すよう、
    この関数を無条件に差し替えていた (AI_OKでも実発注が失敗すると、
    次回候補が dedupe_skip で消えてしまう問題への対処)。有効時のみ
    _apply_optional_entry_dedupe_guard_core (entry_dedupe_guard 使用) を呼ぶ。
    """
    if not ai_results:
        return ai_results

    if not _env_bool("SUMMARY_AI_PREORDER_DEDUPE_ENABLED", False):
        try:
            ok_symbols = [str(x.get("symbol")) for x in ai_results if isinstance(x, dict) and bool(x.get("allow"))]
        except Exception:
            ok_symbols = []
        logger.info(
            "[SUMMARY AI RUNNER] pre-order dedupe skipped ai_results=%s ai_ok_symbols=%s",
            len(ai_results),
            ok_symbols[:30],
        )
        return ai_results

    return _apply_optional_entry_dedupe_guard_core(ai_results)


def _apply_optional_entry_dedupe_guard_core(ai_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
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

def _run_summary_ai_entry_from_df_core(
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


# ============================================================
# ENTRY FIRE RESCUE (旧 core/startup/entry_fire_rescue_runtime_patch._patch_runner)
# ------------------------------------------------------------
# 1 以上のスコアしきい値が渡ってきたら env 由来の緩い値へ置き換え、
# 傾きフィルタの既定も env で切り替える。ネイティブは
# _run_summary_ai_entry_from_df_core へ改名した。
# 適用スコープは entry_fire_rescue.scope_enabled() (main.py のみ)。
# ============================================================


def run_summary_ai_entry_from_df(*args, **kwargs):
    try:
        from .entry_fire_rescue import (
            scope_enabled as _efr_scope,
            _env_bool as _efr_env_bool,
            _env_float as _efr_env_float,
            _safe_float as _efr_safe_float,
        )

        if _efr_scope():
            min_buy = _efr_env_float("SUMMARY_AI_MIN_BUY_SCORE", _efr_env_float("SUMMARY_ENTRY_MIN_SCORE", 0.12))
            min_sell = _efr_env_float("SUMMARY_AI_MIN_SELL_SCORE", _efr_env_float("SUMMARY_ENTRY_MIN_SCORE", 0.12))
            if "min_buy_score" not in kwargs or _efr_safe_float(kwargs.get("min_buy_score"), 999.0) >= 1.0:
                kwargs["min_buy_score"] = min_buy
            if "max_sell_score" not in kwargs or _efr_safe_float(kwargs.get("max_sell_score"), 999.0) >= 1.0:
                kwargs["max_sell_score"] = min_sell
            kwargs.setdefault("use_pre_slope_filter", not _efr_env_bool("ENTRY_BYPASS_SLOPE_FILTER", True))
    except Exception:
        logger.exception("[ENTRY FIRE RESCUE] runner kwargs relax failed; keep original kwargs")
    return _run_summary_ai_entry_from_df_core(*args, **kwargs)


# ============================================================
# STRICT ENTRY DEFAULTS
# (旧 core/startup/tonosama_history_failclose_strict_patch._patch_summary_ai)
# ------------------------------------------------------------
# strict モードではスコアしきい値を 3.0 未満へ落とさせない。
# core/startup/strict_entry_defaults.py が install 時と watcher から毎秒呼ぶ。
#
# 旧パッチと同じく「現在の run_summary_ai_entry_from_df の _original を取り出して
# 包み直す」= 直前に掛かっていた外側ラッパーを 1 枚剥がす挙動をそのまま残す。
# 実運用ではこれが最終決定権を持つ層なので、剥がす/剥がさないは観測挙動そのもの。
# ============================================================


def install_strict_kwargs_floor() -> bool:
    """strict のスコア下限ラッパーを run_summary_ai_entry_from_df へ掛ける。"""
    try:
        cur = globals().get("run_summary_ai_entry_from_df")
        if not callable(cur) or getattr(cur, "_strict_entry_defaults_runner_v4", False):
            return True
        orig = getattr(cur, "_original", cur)

        @wraps(orig)
        def wrapped(*args, **kwargs):
            kwargs["min_buy_score"] = max(float(kwargs.get("min_buy_score", 0) or 0), 3.0)
            kwargs["max_sell_score"] = max(float(kwargs.get("max_sell_score", 0) or 0), 3.0)
            kwargs.setdefault("use_pre_slope_filter", True)
            return orig(*args, **kwargs)

        wrapped._strict_entry_defaults_runner_v3 = True  # type: ignore[attr-defined]
        wrapped._strict_entry_defaults_runner_v4 = True  # type: ignore[attr-defined]
        wrapped._original = orig  # type: ignore[attr-defined]
        globals()["run_summary_ai_entry_from_df"] = wrapped
        logger.warning("[STRICT ENTRY DEFAULTS] summary_ai runner strict wrapper installed")
        return True
    except Exception:
        logger.debug("[STRICT ENTRY DEFAULTS] summary_ai runner strict wrapper skipped", exc_info=True)
        return False


def _efr_apply_top10_slope() -> None:
    global DEFAULT_MIN_TOP10_SLOPE
    try:
        from .entry_fire_rescue import scope_enabled as _efr_scope

        if not _efr_scope():
            return
        DEFAULT_MIN_TOP10_SLOPE = min(float(DEFAULT_MIN_TOP10_SLOPE), 0.0)
    except Exception:
        logger.debug("[ENTRY FIRE RESCUE] top10 slope relax skipped", exc_info=True)


_efr_apply_top10_slope()
