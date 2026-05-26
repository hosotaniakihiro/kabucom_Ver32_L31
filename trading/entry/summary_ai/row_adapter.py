# ============================================================
# File   : trading/entry/summary_ai/row_adapter.py
# Version: PRODUCTION-STABLE-REV1.1-SUMMARY-AI-TECH-READY-PASS
# ------------------------------------------------------------
# 【概要】
#   SUMMARY row を AI/entry_gate.py の ai_final_entry_check(row)
#   が読める dict に変換する。
#
# 【目的】
#   サマリー側の列名:
#     score_buy / score_sell / final_score / close / volume
#
#   AI gate 側の期待キー:
#     buy_score / sell_score / score_total / turnover
#     dominant_ratio / entry_decision / side
#
#   を吸収する。
#
# REV1.1:
#   - technical_ready / display_ready / symbol_hist_len を AI row に渡す
#   - slope_atr_scaled / mtf_score も渡す
#   - サマリー側では ready なのに AI側で technical_not_ready 扱いになる問題を修正
# ============================================================

from __future__ import annotations

from typing import Any, Dict

import pandas as pd

from .utils import first_value, normalize_symbol, safe_float, safe_int, safe_str


def _safe_bool(v: Any, default: bool = False) -> bool:
    try:
        if isinstance(v, bool):
            return v
        if v is None:
            return default
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on", "ok"}:
            return True
        if s in {"0", "false", "no", "n", "off", "ng", "", "nan", "none", "<na>"}:
            return False
        return default
    except Exception:
        return default


def convert_summary_row_to_ai_gate_row(
    row: pd.Series | Dict[str, Any],
    *,
    interval: int | str = 1,
    source: str = "SUMMARY",
    default_dominant_ratio: float = 1.0,
    side: str = "BUY",
) -> Dict[str, Any]:
    symbol = normalize_symbol(first_value(row, ["symbol"], ""))
    symbolname = safe_str(first_value(row, ["symbolname_view", "symbolname", "name"], ""))

    buy_score = safe_float(
        first_value(row, ["ai_disp_buy_score", "disp_buy_score", "score_buy", "buy_score"], 0.0)
    )
    sell_score = safe_float(
        first_value(row, ["ai_disp_sell_score", "disp_sell_score", "score_sell", "sell_score"], 0.0)
    )
    score_total = safe_float(
        first_value(
            row,
            [
                "ai_disp_total_score",
                "disp_total_score",
                "score_total",
                "total_score",
                "final_score",
                "display_score",
                "score",
            ],
            0.0,
        )
    )
    final_score = safe_float(
        first_value(
            row,
            ["ai_disp_final_score", "disp_final_score", "final_score", "display_score", "score"],
            score_total,
        )
    )

    close_price = safe_float(
        first_value(row, ["ai_disp_close", "disp_close", "close", "close_price", "current_price", "price"], 0.0)
    )
    volume = safe_float(
        first_value(row, ["ai_disp_volume", "volume", "trading_volume"], 0.0)
    )
    turnover = safe_float(
        first_value(row, ["ai_disp_turnover", "turnover", "trading_value"], 0.0)
    )
    if turnover <= 0 and close_price > 0 and volume > 0:
        turnover = close_price * volume

    dominant_ratio = safe_float(
        first_value(row, ["dominant_ratio", "buy_dominant_ratio", "ai_dominant_ratio"], default_dominant_ratio),
        default_dominant_ratio,
    )

    dt_value = first_value(row, ["datetime", "end_time", "start_time", "time"], None)

    try:
        interval_int = int(str(interval).replace("min", "").strip())
    except Exception:
        interval_int = safe_int(first_value(row, ["interval"], 1), 1)

    side = str(side or "BUY").upper()

    score_mtf = safe_float(first_value(row, ["ai_disp_mtf", "disp_mtf", "score_mtf", "mtf_score", "mtf"], 0.0))
    mtf_score = safe_float(first_value(row, ["mtf_score", "score_mtf", "mtf", "ai_disp_mtf", "disp_mtf"], score_mtf))
    slope = safe_float(first_value(row, ["ai_disp_slope", "disp_slope", "slope", "score_slope"], 0.0))
    slope_atr_scaled = safe_float(first_value(row, ["slope_atr_scaled", "disp_slope_atr_scaled", "ai_disp_slope_atr_scaled", "score_slope"], slope))

    technical_ready = _safe_bool(first_value(row, ["technical_ready", "tech_ready", "ready"], True), True)
    display_ready = _safe_bool(first_value(row, ["display_ready", "disp_ready"], technical_ready), technical_ready)
    symbol_hist_len = safe_float(first_value(row, ["symbol_hist_len", "hist_len", "history_len"], 0.0), 0.0)

    return {
        "symbol": symbol,
        "symbolname": symbolname,
        "source": str(source or "SUMMARY").upper(),
        "interval": interval_int,
        "entry_decision": side,
        "side": side,
        "dominant_side": side,

        # AI/entry_gate.py 互換
        "buy_score": buy_score,
        "sell_score": sell_score,
        "score_total": score_total,
        "final_score": final_score,
        "turnover": turnover,
        "dominant_ratio": dominant_ratio,
        "close_price": close_price,
        "price": close_price,
        "volume": volume,
        "datetime": dt_value,

        # readiness / history
        "technical_ready": technical_ready,
        "display_ready": display_ready,
        "symbol_hist_len": symbol_hist_len,

        # 元のサマリー系
        "score": safe_float(first_value(row, ["ai_disp_score", "disp_score", "score"], 0.0)),
        "score_buy": buy_score,
        "score_sell": sell_score,
        "score_slope": safe_float(first_value(row, ["ai_disp_slope", "disp_slope", "score_slope", "slope"], 0.0)),
        "score_mtf": score_mtf,
        "slope": slope,
        "slope_atr_scaled": slope_atr_scaled,
        "mtf": safe_float(first_value(row, ["ai_disp_mtf", "disp_mtf", "mtf", "score_mtf"], 0.0)),
        "mtf_score": mtf_score,
        "rsi": safe_float(first_value(row, ["ai_disp_rsi", "disp_rsi", "rsi"], 50.0), 50.0),
        "macd": safe_float(first_value(row, ["ai_disp_macd", "disp_macd", "macd"], 0.0)),
        "signal": safe_float(first_value(row, ["ai_disp_signal", "disp_signal", "signal", "macd_signal"], 0.0)),

        # score breakdown
        "score_base": safe_float(first_value(row, ["ai_score_base", "disp_base", "score_base", "breakdown_base"], 0.0)),
        "score_trend": safe_float(first_value(row, ["ai_score_trend", "disp_trend", "score_trend", "breakdown_trend"], 0.0)),
        "score_momentum": safe_float(first_value(row, ["ai_score_momentum", "disp_mom", "score_momentum", "breakdown_mom"], 0.0)),
        "score_velocity": safe_float(first_value(row, ["ai_score_velocity", "disp_vel", "score_velocity", "breakdown_vel"], 0.0)),
        "score_penalty": safe_float(first_value(row, ["ai_score_penalty", "disp_pen", "score_penalty", "breakdown_pen"], 0.0)),

        # entry_gate.py の3m/5m補正用
        "score_3m": safe_float(first_value(row, ["score_3m", "buy_score_3m", "summary_score_3m"], 0.0)),
        "score_5m": safe_float(first_value(row, ["score_5m", "buy_score_5m", "summary_score_5m"], 0.0)),
    }
