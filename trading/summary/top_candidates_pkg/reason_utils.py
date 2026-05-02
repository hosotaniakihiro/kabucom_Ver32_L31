# ============================================================
# File   : trading/summary/top_candidates_pkg/reason_utils.py
# Version: Ver1.2-PRODUCTION-TOP-CANDIDATES-REASON-UTILS
# ------------------------------------------------------------
# Function:
#   - score_reasons の整形
#   - score_reason_top3 / score_reason_top5 / summary 付与
#   - Discord / log / AI gate 向け payload 整形
#   - フラグ名を表示用短縮名へ変換
#   - entry_setup_type を表示用日本語名へ変換
# ------------------------------------------------------------
# Main APIs:
#   ✔ normalize_score_reasons()
#   ✔ get_top_score_reasons()
#   ✔ format_score_reasons()
#   ✔ attach_score_reason_columns()
#   ✔ build_candidate_reason_payload()
#   ✔ build_candidate_log_line()
#   ✔ reason_label()
#   ✔ setup_label()
# ============================================================

from __future__ import annotations

import ast
import math
from typing import Any, Dict, List, Tuple

import pandas as pd


# ============================================================
# display label map
#   - score_reasons の flag 名を短く読みやすく表示する
#   - 無いキーは元名を返す
# ============================================================

_REASON_LABEL_MAP: Dict[str, str] = {
    # --------------------------------------------------------
    # core / trend
    # --------------------------------------------------------
    "flag_dir_up": "上向き",
    "flag_dir_down": "下向き",
    "flag_slope_positive": "傾き+",
    "flag_slope_negative": "傾き-",
    "flag_trend_strength": "トレンド強",
    "flag_ma5_ma25_cross": "MA5/25GC",
    "flag_ma_dead_cross": "MAデッド",
    "flag_macd_dead_cross": "MACDデッド",
    "flag_ma_up": "MA上向き",
    "flag_ma5_above_ma25": "MA5>25",
    "flag_ma25_above_ma75": "MA25>75",
    "flag_perfect_order_event": "PO上",
    "flag_perfect_order_down": "PO下",
    "flag_first_pullback": "初押し",
    "flag_below_ma75": "MA75割れ",
    "flag_above_ma75": "MA75上",

    # --------------------------------------------------------
    # breakout / range / retest
    # --------------------------------------------------------
    "flag_breakout_high": "高値更新",
    "flag_range_breakout": "レンジ上抜け",
    "flag_range_expansion": "値幅拡大",
    "flag_breakdown_3": "下放れ",
    "flag_breakout_level_retest": "ブレイク再試験",
    "flag_support_reclaim": "支持回復",
    "flag_retest_success": "再テスト成功",
    "flag_opening_range_break": "OR上抜け",
    "flag_opening_range_retest": "OR再試験",
    "flag_opening_range_break_volume": "OR出来高突破",
    "flag_opening_range_expansion": "OR拡大型",
    "flag_opening_range_fail": "OR失敗",

    # --------------------------------------------------------
    # pullback / rebound
    # --------------------------------------------------------
    "flag_fib_rebound": "Fib反発",
    "flag_fib_reversal": "Fib反転",
    "flag_rebound_on_ma25": "MA25反発",
    "flag_bollinger_rebound": "BB反発",
    "flag_bb_3sigma_rebound": "BB3σ反発",
    "flag_pullback_entry_down": "戻り売り",
    "flag_ma_reversal_after_touch_down": "MA反落",
    "flag_lower_wick_low_zone": "下ヒゲ安値圏",
    "flag_lower_wick_rebound": "下ヒゲ反発",

    # --------------------------------------------------------
    # momentum
    # --------------------------------------------------------
    "flag_macd_cross": "MACDGC",
    "flag_macd_gc": "MACDGC",
    "flag_macd_dead_cross": "MACDDC",
    "flag_macd_dc": "MACDDC",
    "flag_macd_hist_expand": "MACD拡大",
    "flag_macd_hist_contract": "MACD縮小",
    "flag_rsi_rebound": "RSI反発",
    "flag_rsi_midline_cross": "RSI50超え",
    "flag_rsi_falling": "RSI低下",
    "flag_rsi_oversold_30": "RSI売られ",
    "flag_rsi_overbought_70": "RSI買われ",
    "flag_stoch_rebound": "ストキャス反発",
    "flag_rci_rising": "RCI上昇",
    "flag_rci_trio_up": "RCI連続上",
    "flag_rci9_uptrend": "RCI上向き",

    # --------------------------------------------------------
    # VWAP
    # --------------------------------------------------------
    "flag_above_vwap": "VWAP上",
    "flag_vwap_break": "VWAP上抜け",
    "flag_vwap_breakout": "VWAP突破",
    "flag_vwap_reclaim": "VWAP回復",
    "flag_vwap_support": "VWAP支持",
    "flag_vwap_fail": "VWAP失敗",
    "flag_vwap_reject": "VWAP反落",
    "flag_vwap_resistance": "VWAP抵抗",
    "flag_vwap_trend_up": "VWAP上向き",
    "flag_vwap_trend_down": "VWAP下向き",
    "flag_vwap_fakeout_reclaim": "VWAP騙し否定",

    # --------------------------------------------------------
    # volume / flow
    # --------------------------------------------------------
    "flag_volume_spike": "出来高急増",
    "flag_volume_surge": "出来高増",
    "flag_volume_drop": "出来高減",
    "flag_volume_peak_out": "出来高失速",
    "flag_volume_expansion": "出来高拡大",
    "flag_volume_exhaustion": "出来高枯れ",
    "flag_volume_price_breakout": "出来高突破",
    "flag_volume_price_breakdown": "出来高下放れ",
    "flag_volume_zone_break": "出来高帯上抜け",
    "flag_volume_zone_breakdown": "出来高帯下抜け",
    "flag_tick_surge": "ティック増",
    "flag_trade_count_spike": "約定急増",
    "flag_bull_candle_volume": "陽線出来高",

    # --------------------------------------------------------
    # orderflow / board
    # --------------------------------------------------------
    "flag_bid_stack": "買い板厚",
    "flag_bid_dominance": "買い板優勢",
    "flag_orderflow_imbalance": "買いフロー偏り",
    "flag_board_pressure_up": "板圧上",
    "flag_ask_stack": "売り板厚",
    "flag_ask_dominance": "売り板優勢",
    "flag_board_pressure_down": "板圧下",

    # --------------------------------------------------------
    # candle patterns
    # --------------------------------------------------------
    "flag_bullish_engulfing": "包み陽線",
    "flag_bearish_engulfing": "包み陰線",
    "flag_bearish_engulfing2": "強包み陰線",
    "flag_bullish_counterattack": "反撃陽線",
    "flag_bullish_side_by_side": "並び陽線",
    "flag_bullish_mat_hold": "押し目継続",
    "flag_bullish_belt_hold": "ベルト陽線",
    "flag_bullish_harami": "陽のはらみ",
    "flag_bearish_harami": "陰のはらみ",
    "flag_bullish_breakaway": "強ブレイク陽",
    "flag_bearish_breakaway": "強ブレイク陰",
    "flag_bullish_kicker": "キッカー陽",
    "flag_bullish_tweezer_bottom": "毛抜き底",
    "flag_morning_star": "明けの明星",
    "flag_evening_star": "宵の明星",
    "flag_piercing_line": "切り込み線",
    "flag_dark_cloud_cover": "覆い陰線",
    "flag_hammer": "ハンマー",
    "flag_inverted_hammer": "逆ハンマー",
    "flag_dragonfly_doji": "トンボ",
    "flag_shooting_star": "流れ星",
    "flag_hanging_man": "首吊り",
    "flag_three_black_crows": "三羽烏",
    "flag_rising_three_methods": "上昇三法",
    "flag_bearish_doji_star": "陰ドージ",

    # --------------------------------------------------------
    # gap / combo
    # --------------------------------------------------------
    "flag_window_up": "GU",
    "flag_window_down": "GD",
    "flag_gap_up_breakout": "GU突破",
    "flag_gap_down_breakdown": "GD下放れ",
    "flag_gapdown_red": "GD陰線",
    "flag_bull_big_combo": "強気複合",
    "flag_multi_signal_cluster": "複合点灯",
    "double_top": "ダブルトップ",
    "upper_wick_series": "上ヒゲ連続",
    "bear_big_combo": "弱気複合",

    # --------------------------------------------------------
    # volatility / structure
    # --------------------------------------------------------
    "flag_volatility_expansion": "ボラ拡大",
    "flag_volatility_breakout": "ボラ突破",
    "flag_structure_higher_high": "高値切上げ",
    "flag_structure_higher_low": "安値切上げ",
    "flag_structure_lower_high": "高値切下げ",
    "flag_structure_lower_low": "安値切下げ",
    "flag_structure_break_up": "構造上抜け",
    "flag_structure_break_down": "構造下抜け",
    "flag_structure_range_expansion": "構造拡大",
    "flag_structure_range_compression": "構造圧縮",

    # --------------------------------------------------------
    # AI / special
    # --------------------------------------------------------
    "flag_ai_momentum_boost": "AI勢い",
    "flag_ai_ranking_boost": "AI順位補強",
    "flag_ai_confidence_high": "AI高確信",
    "flag_ai_exit_signal": "AI撤退",
    "flag_ai_reversal_warning": "AI反転警戒",
    "flag_tosama_entry": "殿様イナゴ",
    "flag_tosama_early": "殿様早期",

    # --------------------------------------------------------
    # relative strength / ranking / mtf / phase / fakeout
    # --------------------------------------------------------
    "flag_relative_strength_positive": "相対強度+",
    "flag_relative_strength_strong": "相対強度強",
    "flag_relative_strength_extreme": "相対強度極強",
    "flag_market_outperform": "指数超過",
    "flag_sector_outperform": "業種超過",
    "flag_ranking_good": "順位良",
    "flag_ranking_improving": "順位改善",
    "flag_ranking_persistent": "順位持続",
    "flag_ranking_reaccel": "順位再加速",
    "flag_ranking_top10": "順位Top10",
    "flag_tf3_ok": "3分良",
    "flag_tf5_ok": "5分良",
    "flag_mtf_consensus_up": "MTF一致",
    "flag_multi_tf_resonance": "多時間足共鳴",
    "flag_buy_over_sell_cross": "買優勢転換",
    "flag_phase_shift": "相転換",
    "flag_phase_recovery": "相回復",
    "flag_fakeout_reclaim": "騙し否定",
    "flag_ma_fakeout_reclaim": "MA騙し否定",
    "flag_break_low_reclaim": "安値割れ否定",
    "flag_signal_confidence_ok": "信頼度OK",
    "flag_signal_confidence_high": "信頼度高",

    # --------------------------------------------------------
    # time window flags
    # --------------------------------------------------------
    "flag_open_0900_0905": "寄付5分",
    "flag_open_0900_0910": "寄付10分",
    "flag_market_open_window": "寄付帯",
    "flag_open_pullback_0910_0930": "寄付後押し",
    "flag_morning_0930_1030": "前場中盤",
    "flag_pre_lunch_1100_1130": "前引け前",
    "flag_pre_lunch_pullback": "前引け前押し",
    "flag_afternoon_open_1230_1300": "後場寄り",
    "flag_afternoon_open_reclaim": "後場寄り回復",
    "flag_afternoon_1300_1400": "後場前半",
    "flag_reentry_1400_1500": "14時再流入",
    "flag_close_retry_1500_1530": "引け前再試し",
    "flag_market_close_window": "引け前帯",
    "flag_first_pullback_after_open": "寄付後初押し",
    "flag_lunch_break_hold": "昼跨ぎ維持",
    "flag_time_decay_pullback_resolved": "押し消化完了",
    "flag_morning_high_hold_to_1100": "朝高維持",
}

# ============================================================
# setup label map
# ============================================================

_SETUP_LABEL_MAP: Dict[str, str] = {
    "pullback": "押し目",
    "breakout": "ブレイクアウト",
    "reversal": "反転初動",
    "trend_continuation": "上昇継続",
    "vwap_reclaim": "VWAP回復",
    "range_break": "レンジ上抜け",
    "retest_success": "再テスト成功",
    "opening_range_break": "寄付レンジ突破",
    "multi_tf_resonance": "多時間足共鳴",
    "relative_strength": "相対強度",
    "phase_shift": "相転換",
    "ranking_persistence": "ランキング持続",
    "fakeout_reversal": "騙し否定",
    "gap_go": "GU継続",
    "volatility_squeeze": "ボラ圧縮突破",
    "volume_expansion": "出来高拡大型",
    "support_bounce": "支持反発",
    "generic_pullback": "押し目",
    "ma25_rebound": "MA25反発",
    "ma5_rebound": "MA5反発",
}


# ============================================================
# basic safe helpers
# ============================================================

def _is_na(v: Any) -> bool:
    try:
        return pd.isna(v)
    except Exception:
        return False


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        if v is None:
            return default
        if isinstance(v, bool):
            return int(v)
        if isinstance(v, (int, float)):
            if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                return default
            return int(v)
        s = str(v).strip()
        if not s:
            return default
        return int(float(s))
    except Exception:
        return default


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        if isinstance(v, (int, float)):
            if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                return default
            return float(v)
        s = str(v).strip()
        if not s:
            return default
        return float(s)
    except Exception:
        return default


def _safe_str(v: Any, default: str = "") -> str:
    try:
        if v is None:
            return default
        if _is_na(v):
            return default
        return str(v)
    except Exception:
        return default


# ============================================================
# label helpers
# ============================================================

def reason_label(key: str) -> str:
    """
    内部 flag 名を表示用短縮名へ変換する。
    未定義なら元の key を返す。
    """
    k = _safe_str(key).strip()
    if not k:
        return ""
    return _REASON_LABEL_MAP.get(k, k)


def setup_label(key: str) -> str:
    """
    entry_setup_type / subtype を表示用日本語名へ変換する。
    未定義なら元の key を返す。
    """
    k = _safe_str(key).strip()
    if not k:
        return ""
    return _SETUP_LABEL_MAP.get(k, k)


# ============================================================
# score_reasons normalize
# ============================================================

def normalize_score_reasons(value: Any) -> Dict[str, int]:
    if value is None:
        return {}

    if _is_na(value):
        return {}

    if isinstance(value, dict):
        out: Dict[str, int] = {}
        for k, v in value.items():
            key = _safe_str(k).strip()
            if not key:
                continue
            out[key] = out.get(key, 0) + _safe_int(v, 0)
        return out

    if isinstance(value, (list, tuple)):
        out: Dict[str, int] = {}
        for item in value:
            try:
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    key = _safe_str(item[0]).strip()
                    if not key:
                        continue
                    out[key] = out.get(key, 0) + _safe_int(item[1], 0)
            except Exception:
                continue
        return out

    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}

        try:
            parsed = ast.literal_eval(text)
            return normalize_score_reasons(parsed)
        except Exception:
            pass

        out: Dict[str, int] = {}
        for chunk in text.split(","):
            chunk = chunk.strip()
            if not chunk or ":" not in chunk:
                continue
            k, v = chunk.split(":", 1)
            key = _safe_str(k).strip()
            if not key:
                continue
            out[key] = out.get(key, 0) + _safe_int(v.strip(), 0)
        return out

    return {}


# ============================================================
# top reasons
# ============================================================

def get_top_score_reasons(
    value: Any,
    top_n: int = 3,
    *,
    include_negative: bool = True,
    sort_by_abs: bool = True,
) -> List[Tuple[str, int]]:
    reasons = normalize_score_reasons(value)
    if not reasons:
        return []

    items = list(reasons.items())

    if not include_negative:
        items = [(k, v) for k, v in items if _safe_int(v, 0) > 0]

    if sort_by_abs:
        items.sort(
            key=lambda x: (abs(_safe_int(x[1], 0)), _safe_int(x[1], 0), x[0]),
            reverse=True,
        )
    else:
        items.sort(
            key=lambda x: (_safe_int(x[1], 0), x[0]),
            reverse=True,
        )

    return items[: max(0, _safe_int(top_n, 3))]


def format_score_reasons(
    value: Any,
    top_n: int = 3,
    *,
    include_negative: bool = True,
    sort_by_abs: bool = True,
    sep: str = " / ",
    with_score: bool = True,
    use_label: bool = True,
) -> str:
    items = get_top_score_reasons(
        value,
        top_n=top_n,
        include_negative=include_negative,
        sort_by_abs=sort_by_abs,
    )
    if not items:
        return ""

    parts: List[str] = []
    for key, score in items:
        label = reason_label(key) if use_label else key
        if with_score:
            parts.append(f"{label}({score:+d})")
        else:
            parts.append(label)

    return sep.join(parts)


# ============================================================
# attach DataFrame columns
# ============================================================

def attach_score_reason_columns(
    df: pd.DataFrame | None,
    *,
    source_col: str = "score_reasons",
    top3_col: str = "score_reason_top3",
    top5_col: str = "score_reason_top5",
    summary_col: str = "score_reason_summary",
    setup_source_col: str = "entry_setup_type",
    setup_label_col: str = "entry_setup_label",
    include_negative: bool = True,
    sort_by_abs: bool = True,
) -> pd.DataFrame | None:
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return df

    out = df.copy()

    if source_col not in out.columns:
        if top3_col not in out.columns:
            out[top3_col] = ""
        if top5_col not in out.columns:
            out[top5_col] = ""
        if summary_col not in out.columns:
            out[summary_col] = ""
    else:
        out[top3_col] = out[source_col].apply(
            lambda v: format_score_reasons(
                v,
                top_n=3,
                include_negative=include_negative,
                sort_by_abs=sort_by_abs,
                use_label=True,
            )
        )
        out[top5_col] = out[source_col].apply(
            lambda v: format_score_reasons(
                v,
                top_n=5,
                include_negative=include_negative,
                sort_by_abs=sort_by_abs,
                use_label=True,
            )
        )
        out[summary_col] = out[top3_col]

    if setup_source_col in out.columns:
        out[setup_label_col] = out[setup_source_col].apply(setup_label)
    else:
        out[setup_label_col] = ""

    return out


# ============================================================
# payload builders
# ============================================================

def build_candidate_reason_payload(row: pd.Series | Dict[str, Any]) -> Dict[str, Any]:
    if row is None:
        return {}

    r = row if isinstance(row, dict) else row.to_dict()

    setup_type = _safe_str(r.get("entry_setup_type"))
    pullback_subtype = _safe_str(r.get("pullback_subtype"))

    payload: Dict[str, Any] = {
        "symbol": _safe_str(r.get("symbol")),
        "symbolname": _safe_str(r.get("symbolname") or r.get("name")),
        "datetime": _safe_str(r.get("datetime")),
        "close": _safe_float(r.get("close") if r.get("close") is not None else r.get("close_price")),
        "entry_setup_type": setup_type,
        "entry_setup_label": setup_label(setup_type),
        "pullback_subtype": pullback_subtype,
        "pullback_subtype_label": setup_label(pullback_subtype),
        "setup_score": _safe_float(r.get("setup_score")),
        "entry_score_v4": _safe_float(r.get("entry_score_v4")),
        "final_score": _safe_float(r.get("final_score")),
        "score_buy": _safe_float(r.get("score_buy")),
        "score_sell": _safe_float(r.get("score_sell")),
        "score_total": _safe_float(r.get("score_total")),
        "score_mtf": _safe_float(r.get("score_mtf")),
        "score_slope": _safe_float(r.get("score_slope")),
        "score_reason_top3": format_score_reasons(r.get("score_reasons"), top_n=3, use_label=True),
        "score_reason_top5": format_score_reasons(r.get("score_reasons"), top_n=5, use_label=True),
        "score_reason_summary": format_score_reasons(r.get("score_reasons"), top_n=3, use_label=True),
    }

    extra_score_cols = [
        "pullback_score_v2",
        "breakout_score",
        "reversal_score",
        "trend_continuation_score",
        "vwap_reclaim_score",
        "range_break_score",
        "retest_success_score",
        "opening_range_break_score",
        "multi_tf_resonance_score",
        "relative_strength_score",
        "phase_shift_score",
        "ranking_persistence_score",
        "fakeout_reversal_score",
        "gap_go_score",
        "volatility_squeeze_score",
        "danger_penalty_score",
        "entry_timing_score",
    ]
    for c in extra_score_cols:
        if c in r:
            payload[c] = _safe_float(r.get(c))

    return payload


def build_candidate_log_line(
    row: pd.Series | Dict[str, Any],
    *,
    include_price: bool = True,
    include_reason: bool = True,
) -> str:
    if row is None:
        return ""

    r = row if isinstance(row, dict) else row.to_dict()

    symbol = _safe_str(r.get("symbol"))
    name = _safe_str(r.get("symbolname") or r.get("name"))
    setup_type = _safe_str(r.get("entry_setup_type"))
    setup = setup_label(setup_type) if setup_type else ""
    score = _safe_float(r.get("entry_score_v4"))
    final_score = _safe_float(r.get("final_score"))
    buy = _safe_float(r.get("score_buy"))
    sell = _safe_float(r.get("score_sell"))
    close = _safe_float(r.get("close") if r.get("close") is not None else r.get("close_price"))
    reason = format_score_reasons(r.get("score_reasons"), top_n=3, use_label=True)

    parts: List[str] = []
    parts.append(f"{symbol}")
    if name:
        parts.append(f"{name}")
    if setup:
        parts.append(f"setup={setup}")
    parts.append(f"entry={score:.2f}")
    parts.append(f"final={final_score:.2f}")
    parts.append(f"buy={buy:.2f}")
    parts.append(f"sell={sell:.2f}")

    if include_price:
        parts.append(f"close={close:.1f}")

    if include_reason and reason:
        parts.append(f"reasons={reason}")

    return " | ".join(parts)