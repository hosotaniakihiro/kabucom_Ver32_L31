# ============================================================
# File   : scheduler_jobs/summary/scoring_signal_japanese.py
# Version: Ver01-SCORING-SIGNAL-JAPANESE-FORMATTER
# ------------------------------------------------------------
# score_config.ini / scoring.ini に書かれている買いサイン・売りサインを
# 定時サマリー表示用に日本語化する helper。
#
# 目的:
#   定時サマリーに「なぜBUY/SELLスコアが付いたか」を日本語で全部表示する。
#
# 使い方:
#   format_active_scoring_signals(row, side="BUY")
#   -> "買いサイン=上方向 / MACD上抜け / 出来高価格ブレイク"
# ============================================================

from __future__ import annotations

import configparser
import logging
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

logger = logging.getLogger(__name__)


# ============================================================
# 日本語ラベル
# ============================================================

SIGNAL_JA: Dict[str, str] = {
    # direction
    "flag_dir_up": "上方向",
    "flag_dir_down": "下方向",
    "flag_slope_positive": "傾きプラス",
    "flag_slope_negative": "傾きマイナス",
    "flag_trend_strength": "トレンド強い",
    "flag_ma_alignment_down": "移動平均が下向き整列",

    # moving average
    "flag_ma5_ma25_cross": "5MAが25MAを上抜け",
    "flag_ma_dead_cross": "移動平均デッドクロス",
    "flag_ma_up": "移動平均上向き",
    "flag_ma5_above_ma25": "5MAが25MAより上",
    "flag_ma25_above_ma75": "25MAが75MAより上",
    "flag_perfect_order_event": "上昇パーフェクトオーダー",
    "flag_first_pullback": "初押し",
    "flag_above_ma75": "75MA上",
    "flag_ma5_downtrend": "5MA下向き",
    "flag_ma5_below_ma25": "5MAが25MAより下",
    "flag_perfect_order_down": "下落パーフェクトオーダー",
    "flag_below_ma75": "75MA下",

    # breakout / breakdown
    "flag_breakout_high": "高値ブレイク",
    "flag_range_breakout": "レンジ上抜け",
    "flag_range_expansion": "値幅拡大",
    "flag_volatility_expansion": "ボラ拡大",
    "flag_volatility_breakout": "ボラ伴うブレイク",
    "flag_breakdown_3": "下方向ブレイク",
    "flag_gap_down_breakdown": "ギャップダウン崩れ",
    "flag_bollinger_breakdown": "ボリンジャー下抜け",
    "flag_bb_3sigma_breakdown": "BB-3σ下抜け",

    # pullback / rebound / reversal
    "flag_fib_rebound": "フィボ反発",
    "flag_rebound_on_ma25": "25MA反発",
    "flag_bollinger_rebound": "ボリンジャー反発",
    "flag_bb_3sigma_rebound": "BB-3σ反発",
    "flag_reversal_penalty": "反転警戒",
    "flag_fib_reversal": "フィボ反落",
    "flag_pullback_entry_down": "戻り売り",
    "flag_ma_reversal_after_touch_down": "MA接触後反落",

    # momentum
    "flag_macd_cross": "MACD上抜け",
    "flag_macd_dead_cross": "MACDデッドクロス",
    "flag_macd_dc": "MACDデッドクロス",
    "flag_macd_hist_expand": "MACDヒストグラム拡大",
    "flag_macd_hist_contract": "MACDヒストグラム縮小",
    "flag_rsi_rebound": "RSI反発",
    "flag_rsi_midline_cross": "RSI50上抜け",
    "flag_rsi_falling": "RSI低下",
    "flag_rsi_overbought_70": "RSI70超え反落警戒",
    "flag_rsi_oversold_30": "RSI30以下反発候補",
    "flag_stoch_rebound": "ストキャス反発",
    "flag_rci_rising": "RCI上昇",
    "flag_rci_trio_up": "RCI三本上向き",
    "flag_rci9_uptrend": "RCI9上昇トレンド",

    # VWAP
    "flag_above_vwap": "VWAP上",
    "flag_vwap_break": "VWAP上抜け",
    "flag_vwap_breakout": "VWAPブレイク",
    "flag_vwap_reclaim": "VWAP回復",
    "flag_vwap_support": "VWAP支持",
    "flag_vwap_trend_up": "VWAP上向き",
    "flag_vwap_fail": "VWAP割れ",
    "flag_vwap_reject": "VWAPで反落",
    "flag_vwap_resistance": "VWAP抵抗",
    "flag_vwap_trend_down": "VWAP下向き",

    # volume / tick
    "flag_volume_spike": "出来高急増",
    "flag_volume_surge": "出来高増加",
    "flag_volume_expansion": "出来高拡大",
    "flag_volume_price_breakout": "出来高伴う上抜け",
    "flag_volume_zone_break": "出来高帯上抜け",
    "flag_tick_surge": "ティック急増",
    "flag_trade_count_spike": "約定回数急増",
    "flag_volume_drop": "出来高減少",
    "flag_volume_peak_out": "出来高ピークアウト",
    "flag_volume_exhaustion": "出来高失速",
    "flag_volume_price_breakdown": "出来高伴う下抜け",
    "flag_volume_zone_breakdown": "出来高帯下抜け",

    # orderflow / board
    "flag_bid_stack": "買い板厚い",
    "flag_bid_dominance": "買い優勢",
    "flag_orderflow_imbalance": "注文フロー買い偏り",
    "flag_board_pressure_up": "板圧上向き",
    "flag_ask_stack": "売り板厚い",
    "flag_ask_dominance": "売り優勢",
    "flag_board_pressure_down": "板圧下向き",

    # candle bullish
    "flag_bull_candle_volume": "陽線＋出来高",
    "flag_bullish_engulfing": "強気包み足",
    "flag_bullish_counterattack": "強気反撃線",
    "flag_bullish_side_by_side": "上放れ並び赤",
    "flag_bullish_mat_hold": "上昇途中の押し目維持",
    "flag_bullish_belt_hold": "強気寄付き坊主",
    "flag_bullish_harami": "強気はらみ",
    "flag_bullish_breakaway": "強気放れ線",
    "flag_bullish_kicker": "強気キッカー",
    "flag_bullish_tweezer_bottom": "毛抜き底",
    "flag_morning_star": "明けの明星",
    "flag_piercing_line": "切り込み線",
    "flag_hammer": "ハンマー",
    "flag_inverted_hammer": "逆ハンマー",
    "flag_dragonfly_doji": "トンボ",
    "flag_rising_three_methods": "上げ三法",
    "flag_lower_wick_low_zone": "安値圏下ヒゲ",
    "flag_lower_wick_rebound": "下ヒゲ反発",

    # candle bearish
    "flag_bearish_engulfing": "弱気包み足",
    "flag_bearish_engulfing2": "強い弱気包み足",
    "flag_dark_cloud_cover": "かぶせ線",
    "flag_evening_star": "宵の明星",
    "flag_shooting_star": "流れ星",
    "flag_three_black_crows": "三羽烏",
    "flag_hanging_man": "首吊り線",
    "flag_bearish_harami": "弱気はらみ",
    "flag_bearish_doji_star": "弱気同事星",
    "flag_bearish_breakaway": "弱気放れ線",

    # gap / window
    "flag_window_up": "上窓",
    "flag_gap_up_breakout": "ギャップアップ上抜け",
    "flag_window_down": "下窓",
    "flag_gapdown_red": "ギャップダウン陰線",

    # combo / top
    "flag_bull_big_combo": "強い買い複合サイン",
    "flag_multi_signal_cluster": "複数サイン集中",
    "double_top": "ダブルトップ",
    "upper_wick_series": "上ヒゲ連発",
    "bear_big_combo": "強い売り複合サイン",

    # AI / tonosama
    "flag_ai_momentum_boost": "AIモメンタム加点",
    "flag_ai_ranking_boost": "AIランキング加点",
    "flag_ai_confidence_high": "AI信頼度高い",
    "flag_ai_exit_signal": "AI撤退/売り警戒",
    "flag_ai_reversal_warning": "AI反転警戒",
    "flag_tosama_entry": "殿様イナゴエントリー",
    "flag_tosama_early": "殿様イナゴ早期検知",

    # opening range
    "flag_opening_range_break": "寄付きレンジ上抜け",
    "flag_opening_range_retest": "寄付きレンジ再確認",
    "flag_opening_range_break_volume": "寄付きレンジ出来高上抜け",
    "flag_opening_range_expansion": "寄付きレンジ拡大",
    "flag_opening_range_fail": "寄付きレンジ失敗",

    # relative strength / ranking
    "flag_relative_strength_positive": "相対強度プラス",
    "flag_relative_strength_strong": "相対強度強い",
    "flag_relative_strength_extreme": "相対強度非常に強い",
    "flag_market_outperform": "市場より強い",
    "flag_sector_outperform": "セクターより強い",
    "flag_ranking_good": "ランキング良好",
    "flag_ranking_improving": "ランキング改善",
    "flag_ranking_persistent": "ランキング継続",
    "flag_ranking_reaccel": "ランキング再加速",
    "flag_ranking_top10": "ランキングTOP10",

    # MTF
    "flag_tf3_ok": "3分足良好",
    "flag_tf5_ok": "5分足良好",
    "flag_mtf_consensus_up": "複数足上向き一致",
    "flag_multi_tf_resonance": "複数時間軸共振",

    # phase / fakeout / retest
    "flag_buy_over_sell_cross": "買いスコアが売りを上抜け",
    "flag_phase_shift": "局面転換",
    "flag_phase_recovery": "局面回復",
    "flag_fakeout_reclaim": "だまし下げ回復",
    "flag_ma_fakeout_reclaim": "MAだまし回復",
    "flag_vwap_fakeout_reclaim": "VWAPだまし回復",
    "flag_break_low_reclaim": "安値割れ回復",
    "flag_breakout_level_retest": "ブレイク水準再確認",
    "flag_support_reclaim": "支持線回復",
    "flag_retest_success": "リテスト成功",

    # confidence
    "flag_signal_confidence_ok": "シグナル信頼度OK",
    "flag_signal_confidence_high": "シグナル信頼度高い",

    # structure
    "flag_structure_higher_high": "高値切り上げ",
    "flag_structure_higher_low": "安値切り上げ",
    "flag_structure_break_up": "構造上抜け",
    "flag_structure_range_expansion": "構造レンジ拡大",
    "flag_structure_lower_high": "高値切り下げ",
    "flag_structure_lower_low": "安値切り下げ",
    "flag_structure_break_down": "構造下抜け",
    "flag_structure_range_compression": "構造レンジ収縮",

    # time window
    "flag_open_0900_0905": "寄付き直後09:00-09:05",
    "flag_open_0900_0910": "寄付き直後09:00-09:10",
    "flag_market_open_window": "寄付き時間帯",
    "flag_open_pullback_0910_0930": "寄付き後押し目09:10-09:30",
    "flag_morning_0930_1030": "前場中盤09:30-10:30",
    "flag_pre_lunch_1100_1130": "前引け前11:00-11:30",
    "flag_pre_lunch_pullback": "前引け前押し目",
    "flag_afternoon_open_1230_1300": "後場寄り12:30-13:00",
    "flag_afternoon_open_reclaim": "後場寄り回復",
    "flag_afternoon_1300_1400": "後場13:00-14:00",
    "flag_reentry_1400_1500": "14時以降再エントリー",
    "flag_close_retry_1500_1530": "大引け前再トライ",
    "flag_market_close_window": "大引け前時間帯",
    "flag_first_pullback_after_open": "寄付き後初押し",
    "flag_lunch_break_hold": "昼休み持ち越し良好",
    "flag_time_decay_pullback_resolved": "時間経過後押し目解消",
    "flag_morning_high_hold_to_1100": "前場高値維持",
}


SECTION_LABEL = {
    "buy_entry": "買い初動",
    "buy_bonus": "買い継続",
    "scoring": "買い旧互換",
    "sell_entry": "売り初動",
    "sell_bonus": "売り継続",
    "short_scoring": "売り旧互換",
}


_BUY_SECTIONS = ("buy_entry", "buy_bonus", "scoring")
_SELL_SECTIONS = ("sell_entry", "sell_bonus", "short_scoring")
_CONFIG_CACHE: Dict[str, Dict[str, Dict[str, float]]] = {}


def _project_root() -> Path:
    try:
        return Path(__file__).resolve().parents[2]
    except Exception:
        return Path.cwd()


def _candidate_config_paths() -> List[Path]:
    root = _project_root()
    env_path = os.getenv("SCORING_CONFIG_PATH") or os.getenv("SCORE_CONFIG_PATH")
    paths: List[Path] = []
    if env_path:
        paths.append(Path(env_path))
    paths.extend([
        root / "trading" / "scoring" / "config" / "score_config.ini",
        root / "trading" / "scoring" / "config" / "scoring.ini",
        root / "score_config.ini",
        root / "scoring.ini",
    ])
    out: List[Path] = []
    seen = set()
    for p in paths:
        try:
            s = str(p)
            if s not in seen:
                out.append(p)
                seen.add(s)
        except Exception:
            pass
    return out


def _load_score_config() -> Dict[str, Dict[str, float]]:
    cache_key = "score_config"
    if cache_key in _CONFIG_CACHE:
        return _CONFIG_CACHE[cache_key]

    parser = configparser.ConfigParser()
    parser.optionxform = str
    used_path = None
    for path in _candidate_config_paths():
        try:
            if path.exists():
                parser.read(path, encoding="utf-8")
                used_path = path
                break
        except Exception:
            continue

    data: Dict[str, Dict[str, float]] = {}
    for section in list(_BUY_SECTIONS) + list(_SELL_SECTIONS):
        data[section] = {}
        if parser.has_section(section):
            for key, value in parser.items(section):
                try:
                    data[section][str(key)] = float(str(value).strip())
                except Exception:
                    continue

    if used_path:
        logger.info("[SCORING SIGNAL JA] loaded config path=%s", used_path)
    else:
        logger.warning("[SCORING SIGNAL JA] score_config.ini not found; using Japanese labels only")

    _CONFIG_CACHE[cache_key] = data
    return data


def _truthy_flag(v: Any) -> bool:
    try:
        if v is None:
            return False
        if isinstance(v, bool):
            return bool(v)
        s = str(v).strip().lower()
        if s in {"", "0", "0.0", "false", "none", "nan", "no", "ng"}:
            return False
        return float(v) != 0.0
    except Exception:
        return bool(v)


def _row_get(row: Any, key: str) -> Any:
    try:
        if isinstance(row, dict):
            return row.get(key)
        if hasattr(row, "index") and key in row.index:
            return row[key]
        if hasattr(row, "get"):
            return row.get(key)
    except Exception:
        return None
    return None


def _collect_active(row: Any, sections: Iterable[str]) -> List[Tuple[str, str, float]]:
    cfg = _load_score_config()
    found: List[Tuple[str, str, float]] = []
    seen = set()
    for section in sections:
        for key, weight in cfg.get(section, {}).items():
            if key in seen:
                continue
            v = _row_get(row, key)
            if _truthy_flag(v):
                label = SIGNAL_JA.get(key, key)
                found.append((SECTION_LABEL.get(section, section), label, weight))
                seen.add(key)
    return found


def format_active_scoring_signals(row: Any, *, side: str = "BUY", max_items: int = 999) -> str:
    """
    row内でONになっている score_config.ini 由来フラグを日本語で返す。
    rowにフラグ列が無い場合は空文字を返す。
    """
    side_u = str(side or "BUY").upper()
    sections = _SELL_SECTIONS if side_u == "SELL" else _BUY_SECTIONS
    active = _collect_active(row, sections)
    if not active:
        return ""

    parts: List[str] = []
    for section_label, label, weight in active[: max(1, int(max_items or 999))]:
        sign = "+" if weight > 0 else ""
        parts.append(f"{section_label}:{label}({sign}{weight:g})")

    prefix = "売りサイン" if side_u == "SELL" else "買いサイン"
    return prefix + "=" + " / ".join(parts)


def format_score_config_catalog(*, side: str = "BUY", max_items: int = 999) -> str:
    """
    score_config.ini に定義されているサイン一覧を日本語で返す。
    定時サマリーのヘッダや診断表示用。
    """
    side_u = str(side or "BUY").upper()
    sections = _SELL_SECTIONS if side_u == "SELL" else _BUY_SECTIONS
    cfg = _load_score_config()
    parts: List[str] = []
    seen = set()
    for section in sections:
        for key, weight in cfg.get(section, {}).items():
            if key in seen:
                continue
            seen.add(key)
            label = SIGNAL_JA.get(key, key)
            sign = "+" if weight > 0 else ""
            parts.append(f"{SECTION_LABEL.get(section, section)}:{label}({sign}{weight:g})")
    prefix = "売りサイン定義" if side_u == "SELL" else "買いサイン定義"
    return prefix + "=" + " / ".join(parts[: max(1, int(max_items or 999))])


__all__ = [
    "format_active_scoring_signals",
    "format_score_config_catalog",
    "SIGNAL_JA",
]
