# ============================================================
# File   : trading/signals/setup_mapper.py
# Version: PRODUCTION-STABLE-REV1.0-SETUP-MAPPER
# ------------------------------------------------------------
# 【概要】
#   既存の BUY / SELL condition reasons を
#   setup（pullback / breakout / reversal ...）へ束ねる adapter
#
# 【目的】
#   - 既存 conditions_* を壊さず活用
#   - setup別スコアを後段で使えるようにする
#   - top_candidates / announce / AI gate に渡しやすくする
#
# 【入力想定】
#   1) buy_signals / short_signals を持つ dict
#   2) conditions(list[str]) を持つ row / dict
#   3) candidate_reasons を持つ row / dict
#
# 【出力】
#   {
#       "buy_setup_scores": {...},
#       "short_setup_scores": {...},
#       "buy_best_setup": "pullback",
#       "short_best_setup": "trend_breakdown",
#       "buy_best_score": 42.0,
#       "short_best_score": 18.0,
#       "decision_setup": "pullback",
#       "decision_side": "BUY",
#       "setup_reason_text": "...",
#   }
#
# 【設計】
#   - 既存 signal reason を setup に再分類
#   - exact match + alias 吸収
#   - side別に独立スコアリング
#   - 新条件が増えても mapping を追加するだけで拡張可能
# ============================================================

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import math


# ============================================================
# setup 定義
# ============================================================

BUY_SETUPS: Tuple[str, ...] = (
    "pullback",
    "breakout",
    "reversal",
    "trend_continuation",
    "vwap_reclaim",
    "range_break",
    "retest_success",
    "opening_range_break",
    "multi_tf_resonance",
    "relative_strength",
    "phase_shift",
    "ranking_persistence",
    "fakeout_reversal",
    "gap_go",
    "volatility_squeeze",
)

SHORT_SETUPS: Tuple[str, ...] = (
    "trend_breakdown",
    "breakdown",
    "deadcat_reversal",
    "vwap_fail",
    "range_breakdown",
    "retest_failure",
    "opening_range_breakdown",
    "relative_weakness",
    "phase_shift_down",
    "gap_down_go",
    "volatility_expansion_down",
)


# ============================================================
# 既存条件 → setup 重み
# 既存ファイルの condition / reason 名をベースに設計
# ============================================================

BUY_REASON_TO_SETUP_WEIGHTS: Dict[str, Dict[str, float]] = {
    # --- trend / MA ---
    "dir_up": {"trend_continuation": 10, "multi_tf_resonance": 6},
    "ma_up": {"trend_continuation": 10, "pullback": 4},
    "ma_uptrend": {"trend_continuation": 12, "pullback": 4},
    "ma_alignment": {"trend_continuation": 12, "multi_tf_resonance": 8},
    "perfect_order": {"trend_continuation": 14, "multi_tf_resonance": 8},
    "perfect_order_event": {"trend_continuation": 12, "breakout": 4},
    "ma5_ma25_cross": {"breakout": 10, "phase_shift": 10, "reversal": 6},

    # --- momentum ---
    "macd_cross": {"breakout": 10, "reversal": 10, "phase_shift": 8},
    "macd_gc": {"breakout": 12, "reversal": 8, "phase_shift": 8},
    "rsi_rebound": {"pullback": 10, "reversal": 12, "phase_shift": 8},
    "rci_trio_up": {"trend_continuation": 8, "breakout": 6},
    "rci9_uptrend": {"trend_continuation": 8, "multi_tf_resonance": 6},

    # --- vwap / price ---
    "vwap_break": {"vwap_reclaim": 14, "breakout": 8, "trend_continuation": 4},
    "first_pullback": {"pullback": 16, "retest_success": 6},
    "pullback_entry": {"pullback": 18, "retest_success": 8},
    "rebound_on_ma25": {"pullback": 16, "retest_success": 10},
    "fib_rebound": {"pullback": 10, "reversal": 8},
    "bollinger_rebound": {"reversal": 8, "pullback": 6},
    "bb_3sigma_rebound": {"reversal": 12},

    # --- volume / breakout ---
    "volume_surge": {"breakout": 10, "gap_go": 6, "relative_strength": 4},
    "volume_price_breakout": {"breakout": 16, "range_break": 10},
    "volume_zone_break": {"range_break": 14, "breakout": 8},

    # --- candle patterns ---
    "bullish_engulfing": {"reversal": 16, "fakeout_reversal": 10},
    "bullish_counterattack": {"reversal": 12},
    "bull_big_combo": {"breakout": 12, "trend_continuation": 8},
    "hammer_rebound": {"reversal": 10, "pullback": 6},

    # --- gap ---
    "gap_up_breakout": {"gap_go": 18, "breakout": 10, "opening_range_break": 8},

    # --- meta / other common aliases ---
    "retest_success": {"retest_success": 20, "pullback": 8},
    "range_break": {"range_break": 18, "breakout": 8},
    "opening_range_break": {"opening_range_break": 20, "breakout": 8},
    "squeeze_break": {"volatility_squeeze": 20, "breakout": 8},
    "fakeout_reclaim": {"fakeout_reversal": 20, "reversal": 8},
    "ranking_persistent": {"ranking_persistence": 16, "relative_strength": 6},
    "relative_strength_positive": {"relative_strength": 18, "trend_continuation": 4},
    "phase_shift": {"phase_shift": 18, "reversal": 6},
    "multi_tf_resonance": {"multi_tf_resonance": 20, "trend_continuation": 6},
}

SHORT_REASON_TO_SETUP_WEIGHTS: Dict[str, Dict[str, float]] = {
    # --- trend / MA ---
    "dir_down": {"trend_breakdown": 10, "phase_shift_down": 6},
    "ma_alignment_down": {"trend_breakdown": 12, "relative_weakness": 6},
    "ma5_downtrend": {"trend_breakdown": 10},
    "ma5_below_ma25": {"trend_breakdown": 8, "breakdown": 4},
    "perfect_order_down": {"trend_breakdown": 14},

    # --- momentum ---
    "macd_dc": {"breakdown": 10, "phase_shift_down": 8},
    "macd_dead_cross": {"breakdown": 12, "phase_shift_down": 8},
    "rsi_falling": {"trend_breakdown": 8, "relative_weakness": 6},
    "rsi_down": {"trend_breakdown": 10, "phase_shift_down": 8},

    # --- price / vwap / breakdown ---
    "below_ma75": {"trend_breakdown": 12, "breakdown": 6},
    "vwap_fail": {"vwap_fail": 16, "breakdown": 8},
    "vwap_breakdown": {"vwap_fail": 18, "breakdown": 8},
    "volume_drop": {"deadcat_reversal": 6, "relative_weakness": 8},
    "volume_peak_out": {"deadcat_reversal": 8, "phase_shift_down": 6},
    "volume_price_breakdown": {"breakdown": 16, "range_breakdown": 10},
    "volume_zone_breakdown": {"range_breakdown": 16, "breakdown": 8},

    # --- patterns / gap ---
    "bearish_engulfing": {"deadcat_reversal": 16, "retest_failure": 8},
    "bear_big_combo": {"breakdown": 12, "trend_breakdown": 8},
    "gap_down_breakdown": {"gap_down_go": 18, "breakdown": 10, "opening_range_breakdown": 8},

    # --- meta ---
    "retest_failure": {"retest_failure": 20, "deadcat_reversal": 6},
    "range_breakdown": {"range_breakdown": 18, "breakdown": 8},
    "opening_range_breakdown": {"opening_range_breakdown": 20, "breakdown": 8},
    "relative_weakness": {"relative_weakness": 18, "trend_breakdown": 4},
    "phase_shift_down": {"phase_shift_down": 18, "breakdown": 6},
    "volatility_expansion_down": {"volatility_expansion_down": 20, "breakdown": 8},
}

# alias 吸収
REASON_ALIASES: Dict[str, str] = {
    "macd_gc": "macd_cross",
    "macd_dead_cross": "macd_dc",
    "ma_dead_cross": "macd_dc",  # 既存キーが曖昧でも最低限吸収
    "vwap_breakdown": "vwap_fail",
    "rsi_down": "rsi_falling",
}


# ============================================================
# helper
# ============================================================

def _safe_str(v: Any) -> str:
    if v is None:
        return ""
    try:
        if isinstance(v, float) and math.isnan(v):
            return ""
    except Exception:
        pass
    return str(v).strip()


def _to_reason_list(obj: Any) -> List[str]:
    """
    list[str] / tuple / set / comma joined string / row-like を安全に list[str] 化
    """
    if obj is None:
        return []

    if isinstance(obj, (list, tuple, set)):
        return [_safe_str(x) for x in obj if _safe_str(x)]

    if isinstance(obj, str):
        s = obj.strip()
        if not s:
            return []
        # "a,b,c" 形式も吸収
        if "," in s:
            return [_safe_str(x) for x in s.split(",") if _safe_str(x)]
        return [s]

    if isinstance(obj, Mapping):
        # conditions / buy_signals / short_signals / candidate_reasons 優先
        for key in ("conditions", "buy_signals", "short_signals", "candidate_reasons"):
            if key in obj:
                return _to_reason_list(obj.get(key))
        return []

    return []


def _normalize_reason(reason: str) -> str:
    reason = _safe_str(reason)
    if not reason:
        return ""
    return REASON_ALIASES.get(reason, reason)


def _empty_score_dict(setups: Sequence[str]) -> Dict[str, float]:
    return {k: 0.0 for k in setups}


def _best_setup(score_dict: Mapping[str, float]) -> Tuple[str, float]:
    if not score_dict:
        return "", 0.0
    best_name = ""
    best_score = 0.0
    for k, v in score_dict.items():
        score = float(v or 0.0)
        if score > best_score:
            best_name = k
            best_score = score
    return best_name, best_score


def _score_reasons(
    reasons: Sequence[str],
    mapping: Mapping[str, Mapping[str, float]],
    setups: Sequence[str],
) -> Dict[str, float]:
    scores = _empty_score_dict(setups)

    for raw_reason in reasons:
        reason = _normalize_reason(raw_reason)
        if not reason:
            continue

        weights = mapping.get(reason)
        if not weights:
            continue

        for setup, w in weights.items():
            if setup in scores:
                scores[setup] += float(w)

    return scores


def _merge_setup_scores(
    base: Dict[str, float],
    extra: Optional[Mapping[str, float]] = None,
    factor: float = 1.0,
) -> Dict[str, float]:
    out = dict(base)
    if not extra:
        return out
    for k, v in extra.items():
        if k in out:
            out[k] += float(v or 0.0) * factor
    return out


# ============================================================
# public
# ============================================================

def map_signals_to_setups(
    *,
    buy_reasons: Optional[Sequence[str]] = None,
    short_reasons: Optional[Sequence[str]] = None,
    score_buy: Optional[float] = None,
    score_short: Optional[float] = None,
    ranking_buy: Optional[float] = None,
    ranking_short: Optional[float] = None,
) -> Dict[str, Any]:
    """
    既存シグナル理由を setup 別スコアに変換する。
    """

    buy_reason_list = _to_reason_list(buy_reasons)
    short_reason_list = _to_reason_list(short_reasons)

    buy_scores = _score_reasons(
        buy_reason_list,
        BUY_REASON_TO_SETUP_WEIGHTS,
        BUY_SETUPS,
    )
    short_scores = _score_reasons(
        short_reason_list,
        SHORT_REASON_TO_SETUP_WEIGHTS,
        SHORT_SETUPS,
    )

    # score / ranking を軽く反映
    # ranking は小さい方が強い前提
    if score_buy is not None:
        buy_scores = _merge_setup_scores(
            buy_scores,
            {
                "breakout": max(float(score_buy), 0.0) * 0.20,
                "trend_continuation": max(float(score_buy), 0.0) * 0.15,
                "relative_strength": max(float(score_buy), 0.0) * 0.10,
            },
        )
    if score_short is not None:
        short_scores = _merge_setup_scores(
            short_scores,
            {
                "breakdown": max(float(score_short), 0.0) * 0.20,
                "trend_breakdown": max(float(score_short), 0.0) * 0.15,
                "relative_weakness": max(float(score_short), 0.0) * 0.10,
            },
        )

    if ranking_buy is not None:
        rb = float(ranking_buy)
        if rb > 0:
            bonus = max(0.0, 12.0 - min(rb, 12.0))
            buy_scores = _merge_setup_scores(
                buy_scores,
                {
                    "ranking_persistence": bonus,
                    "relative_strength": bonus * 0.5,
                },
            )

    if ranking_short is not None:
        rs = float(ranking_short)
        if rs > 0:
            bonus = max(0.0, 12.0 - min(rs, 12.0))
            short_scores = _merge_setup_scores(
                short_scores,
                {
                    "relative_weakness": bonus,
                    "trend_breakdown": bonus * 0.5,
                },
            )

    buy_best_setup, buy_best_score = _best_setup(buy_scores)
    short_best_setup, short_best_score = _best_setup(short_scores)

    decision_side = None
    decision_setup = ""
    if buy_best_score > short_best_score:
        decision_side = "BUY"
        decision_setup = buy_best_setup
    elif short_best_score > buy_best_score:
        decision_side = "SHORT"
        decision_setup = short_best_setup

    setup_reason_text = ""
    if decision_side == "BUY":
        setup_reason_text = ", ".join(buy_reason_list[:6])
    elif decision_side == "SHORT":
        setup_reason_text = ", ".join(short_reason_list[:6])

    return {
        "buy_reasons": buy_reason_list,
        "short_reasons": short_reason_list,
        "buy_setup_scores": buy_scores,
        "short_setup_scores": short_scores,
        "buy_best_setup": buy_best_setup,
        "short_best_setup": short_best_setup,
        "buy_best_score": buy_best_score,
        "short_best_score": short_best_score,
        "decision_side": decision_side,
        "decision_setup": decision_setup,
        "setup_reason_text": setup_reason_text,
    }


def map_from_signal_summary(summary: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """
    signals_manager / signal_priority_resolver / pipeline の出力 dict を想定したラッパ
    """
    summary = summary or {}

    return map_signals_to_setups(
        buy_reasons=summary.get("buy_signals") or summary.get("buy_reasons"),
        short_reasons=summary.get("short_signals") or summary.get("short_reasons"),
        score_buy=summary.get("score_buy"),
        score_short=summary.get("score_short"),
        ranking_buy=summary.get("ranking_buy"),
        ranking_short=summary.get("ranking_short"),
    )


def map_from_row(row: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """
    DataFrame row / dict から setup を推定する。
    優先順:
      buy_signals, short_signals
      conditions
      candidate_reasons
    """
    row = row or {}

    buy_reasons = row.get("buy_signals")
    short_reasons = row.get("short_signals")

    # buy/short が無ければ conditions から side 非依存に一旦BUYへ寄せて判定
    # （dispatcher / runner の過渡対応）
    if not buy_reasons and not short_reasons:
        generic = row.get("conditions") or row.get("candidate_reasons") or []
        generic_list = _to_reason_list(generic)

        buy_like = []
        short_like = []

        for r in generic_list:
            nr = _normalize_reason(r)
            if nr in BUY_REASON_TO_SETUP_WEIGHTS:
                buy_like.append(nr)
            if nr in SHORT_REASON_TO_SETUP_WEIGHTS:
                short_like.append(nr)

        buy_reasons = buy_like
        short_reasons = short_like

    return map_signals_to_setups(
        buy_reasons=buy_reasons,
        short_reasons=short_reasons,
        score_buy=row.get("score_buy"),
        score_short=row.get("score_short"),
        ranking_buy=row.get("ranking_buy"),
        ranking_short=row.get("ranking_short"),
    )


def attach_setup_columns(df):
    """
    DataFrame に setup列を追加する。
    既存の conditions / buy_signals / short_signals / candidate_reasons を利用。
    """
    import pandas as pd

    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return df

    out = df.copy()

    # 初期列
    out["buy_best_setup"] = ""
    out["short_best_setup"] = ""
    out["buy_best_score"] = 0.0
    out["short_best_score"] = 0.0
    out["decision_setup"] = ""
    out["decision_side_setup"] = ""
    out["setup_reason_text"] = ""

    # 主要 setup score 列
    for name in BUY_SETUPS:
        col = f"setup_score_buy_{name}"
        if col not in out.columns:
            out[col] = 0.0

    for name in SHORT_SETUPS:
        col = f"setup_score_short_{name}"
        if col not in out.columns:
            out[col] = 0.0

    for idx, row in out.iterrows():
        mapped = map_from_row(row)

        out.at[idx, "buy_best_setup"] = mapped["buy_best_setup"]
        out.at[idx, "short_best_setup"] = mapped["short_best_setup"]
        out.at[idx, "buy_best_score"] = mapped["buy_best_score"]
        out.at[idx, "short_best_score"] = mapped["short_best_score"]
        out.at[idx, "decision_setup"] = mapped["decision_setup"]
        out.at[idx, "decision_side_setup"] = mapped["decision_side"] or ""
        out.at[idx, "setup_reason_text"] = mapped["setup_reason_text"]

        for setup, score in mapped["buy_setup_scores"].items():
            out.at[idx, f"setup_score_buy_{setup}"] = score

        for setup, score in mapped["short_setup_scores"].items():
            out.at[idx, f"setup_score_short_{setup}"] = score

    return out