# ============================================================
# File   : trading/scoring/config/flag_label_map.py
# Version: PRODUCTION-STABLE-REV1.0-FLAG-JA-LABELS
# ------------------------------------------------------------
# 【概要】
#   score_config.ini の flag_xxx をサマリー表示用の日本語名へ変換する
#
# 【目的】
#   - サマリー表示で英語 flag 名ではなく日本語の根拠を表示する
#   - AIが通した銘柄の理由を分かりやすくする
#   - BUY / SELL / EXIT / AI / 板 / 出来高 / ローソク足の理由表示に対応
# ============================================================

from __future__ import annotations

from typing import Iterable, List


FLAG_JA_LABELS: dict[str, str] = {
    # ========================================================
    # BUY: direction
    # ========================================================
    "flag_dir_up": "上昇方向",
    "flag_slope_positive": "傾きプラス",
    "flag_trend_strength": "トレンド強い",

    # ========================================================
    # BUY: moving average
    # ========================================================
    "flag_ma5_ma25_cross": "5MA/25MAゴールデンクロス",
    "flag_ma_up": "移動平均上向き",
    "flag_ma5_above_ma25": "5MAが25MA上",
    "flag_ma25_above_ma75": "25MAが75MA上",
    "flag_perfect_order_event": "上昇パーフェクトオーダー",
    "flag_first_pullback": "初押し",

    # ========================================================
    # BUY: breakout
    # ========================================================
    "flag_breakout_high": "高値ブレイク",
    "flag_range_breakout": "レンジ上抜け",
    "flag_range_expansion": "値幅拡大",

    # ========================================================
    # BUY: pullback / rebound
    # ========================================================
    "flag_fib_rebound": "フィボナッチ反発",
    "flag_rebound_on_ma25": "25MA反発",
    "flag_bollinger_rebound": "ボリンジャー反発",
    "flag_bb_3sigma_rebound": "BB-3σ反発",

    # ========================================================
    # BUY: momentum
    # ========================================================
    "flag_macd_cross": "MACDゴールデンクロス",
    "flag_macd_hist_expand": "MACDヒストグラム拡大",
    "flag_rsi_rebound": "RSI反発",
    "flag_rsi_midline_cross": "RSI中央値上抜け",
    "flag_stoch_rebound": "ストキャス反発",
    "flag_rci_rising": "RCI上昇",
    "flag_rci_trio_up": "RCI三線上向き",
    "flag_rci9_uptrend": "RCI9上昇トレンド",

    # ========================================================
    # BUY: VWAP
    # ========================================================
    "flag_above_vwap": "VWAP上",
    "flag_vwap_break": "VWAP上抜け",
    "flag_vwap_breakout": "VWAPブレイクアウト",
    "flag_vwap_reclaim": "VWAP回復",

    # ========================================================
    # BUY: volume
    # ========================================================
    "flag_volume_spike": "出来高急増",
    "flag_volume_surge": "出来高急伸",
    "flag_volume_expansion": "出来高拡大",
    "flag_volume_price_breakout": "出来高を伴う上抜け",
    "flag_volume_zone_break": "出来高帯上抜け",
    "flag_tick_surge": "約定回数急増",
    "flag_trade_count_spike": "取引回数急増",

    # ========================================================
    # BUY: orderflow / board
    # ========================================================
    "flag_bid_stack": "買い板厚い",
    "flag_bid_dominance": "買い優勢",
    "flag_orderflow_imbalance": "注文フロー買い優勢",
    "flag_board_pressure_up": "板圧力上向き",

    # ========================================================
    # BUY: candle patterns
    # ========================================================
    "flag_bull_candle_volume": "陽線＋出来高",
    "flag_bullish_engulfing": "陽の包み足",
    "flag_bullish_counterattack": "陽の切り込み線",
    "flag_bullish_side_by_side": "陽の並び赤",
    "flag_bullish_mat_hold": "上げ三法",
    "flag_bullish_belt_hold": "陽の寄付き坊主",
    "flag_bullish_harami": "陽のはらみ線",
    "flag_bullish_breakaway": "上放れ",
    "flag_bullish_kicker": "強気キッカー",
    "flag_bullish_tweezer_bottom": "毛抜き底",
    "flag_morning_star": "明けの明星",
    "flag_piercing_line": "切り込み線",
    "flag_hammer": "下ヒゲ陽線",
    "flag_inverted_hammer": "トンカチ",
    "flag_dragonfly_doji": "トンボ",
    "flag_rising_three_methods": "上げ三法",

    # ========================================================
    # BUY: gap
    # ========================================================
    "flag_window_up": "上窓",
    "flag_gap_up_breakout": "ギャップアップ上抜け",

    # ========================================================
    # BUY: strong combo
    # ========================================================
    "flag_bull_big_combo": "強気複合シグナル",
    "flag_multi_signal_cluster": "複数シグナル集中",
    "flag_lower_wick_low_zone": "安値圏下ヒゲ",
    "flag_lower_wick_rebound": "下ヒゲ反発",

    # ========================================================
    # BUY: volatility
    # ========================================================
    "flag_volatility_expansion": "ボラティリティ拡大",
    "flag_volatility_breakout": "ボラティリティ上抜け",

    # ========================================================
    # BUY: absolute
    # ========================================================
    "flag_rsi_oversold_30": "RSI売られすぎ反発",
    "flag_bb_lower_touch": "BB下限タッチ",

    # ========================================================
    # BUY: AI assist
    # ========================================================
    "flag_ai_momentum_boost": "AIモメンタム強化",
    "flag_ai_ranking_boost": "ランキングAI補正",
    "flag_ai_confidence_high": "AI信頼度高",

    # ========================================================
    # BUY: 殿様イナゴ
    # ========================================================
    "flag_tosama_entry": "殿様イナゴ本命",
    "flag_tosama_early": "殿様イナゴ初動",

    # ========================================================
    # SELL: direction
    # ========================================================
    "flag_dir_down": "下落方向",
    "flag_slope_negative": "傾きマイナス",
    "flag_ma_alignment_down": "移動平均下向き配列",

    # ========================================================
    # SELL: MA failure
    # ========================================================
    "flag_ma5_downtrend": "5MA下向き",
    "flag_ma5_below_ma25": "5MAが25MA下",
    "flag_perfect_order_down": "下落パーフェクトオーダー",

    # ========================================================
    # SELL: momentum failure
    # ========================================================
    "flag_macd_dc": "MACDデッドクロス",
    "flag_macd_hist_contract": "MACDヒストグラム縮小",
    "flag_rsi_falling": "RSI低下",
    "flag_rsi_overbought_70": "RSI買われすぎ",

    # ========================================================
    # SELL: VWAP
    # ========================================================
    "flag_vwap_fail": "VWAP割れ",
    "flag_vwap_reject": "VWAP反落",

    # ========================================================
    # SELL: volume
    # ========================================================
    "flag_volume_drop": "出来高減少",
    "flag_volume_peak_out": "出来高ピークアウト",
    "flag_volume_exhaustion": "出来高失速",
    "flag_volume_price_breakdown": "出来高を伴う下抜け",
    "flag_volume_zone_breakdown": "出来高帯下抜け",

    # ========================================================
    # SELL: reversal
    # ========================================================
    "flag_reversal_penalty": "反転警戒",
    "flag_fib_reversal": "フィボナッチ反落",
    "flag_pullback_entry_down": "戻り売り",
    "flag_ma_reversal_after_touch_down": "MA接触後反落",

    # ========================================================
    # SELL: breakdown
    # ========================================================
    "flag_breakdown_3": "3点下抜け",
    "flag_gap_down_breakdown": "ギャップダウン下抜け",
    "flag_bollinger_breakdown": "ボリンジャー下抜け",
    "flag_bb_3sigma_breakdown": "BB-3σ下抜け",

    # ========================================================
    # SELL: candle patterns
    # ========================================================
    "flag_bearish_engulfing": "陰の包み足",
    "flag_bearish_engulfing2": "強い陰の包み足",
    "flag_dark_cloud_cover": "かぶせ線",
    "flag_evening_star": "宵の明星",
    "flag_shooting_star": "流れ星",
    "flag_three_black_crows": "三羽烏",
    "flag_hanging_man": "首吊り線",
    "flag_bearish_harami": "陰のはらみ線",
    "flag_bearish_doji_star": "弱気同時線",
    "flag_bearish_breakaway": "下放れ",
    "flag_window_down": "下窓",
    "flag_gapdown_red": "ギャップダウン陰線",

    # ========================================================
    # SELL: board pressure
    # ========================================================
    "flag_ask_stack": "売り板厚い",
    "flag_ask_dominance": "売り優勢",
    "flag_board_pressure_down": "板圧力下向き",

    # ========================================================
    # SELL: AI
    # ========================================================
    "flag_ai_exit_signal": "AI手仕舞いシグナル",
    "flag_ai_reversal_warning": "AI反転警戒",
}


def flag_to_ja(flag_name: str) -> str:
    key = str(flag_name).strip()
    return FLAG_JA_LABELS.get(key, key)


def flags_to_ja_text(
    flags: Iterable[str],
    *,
    sep: str = " / ",
    max_items: int = 5,
) -> str:
    labels: List[str] = []

    for f in flags:
        if not f:
            continue
        labels.append(flag_to_ja(str(f)))

    if not labels:
        return "-"

    labels = labels[:max_items]
    return sep.join(labels)


def extract_active_flag_names_from_row(
    row,
    *,
    side: str = "BUY",
    max_items: int = 5,
) -> list[str]:
    if row is None:
        return []

    side_u = str(side).upper()
    flags: list[str] = []

    try:
        items = row.items()
    except Exception:
        return []

    for col, val in items:
        name = str(col)

        if not name.startswith("flag_"):
            continue

        try:
            if val is None:
                continue

            if isinstance(val, bool):
                active = val
                numeric = 1.0 if val else 0.0
            else:
                numeric = float(val)
                active = numeric != 0

            if not active:
                continue

            if side_u == "BUY" and numeric < 0:
                continue
            if side_u in {"SELL", "SHORT", "EXIT"} and numeric > 0:
                continue

            flags.append(name)

        except Exception:
            continue

    return flags[:max_items]


def build_reason_text_from_row(
    row,
    *,
    side: str = "BUY",
    max_items: int = 5,
) -> str:
    flags = extract_active_flag_names_from_row(
        row,
        side=side,
        max_items=max_items,
    )
    return flags_to_ja_text(flags, max_items=max_items)