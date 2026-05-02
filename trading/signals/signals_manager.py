# ============================================================
# File   : trading/signals/signals_manager.py
# Version: Ver2.0-SIGNALS-MANAGER-SETUP-AWARE
# ------------------------------------------------------------
# ✔ signals engine統合
# ✔ state管理
# ✔ deduplicate
# ✔ decision resolver
# ✔ entry用decision生成
# ✔ setup_mapper連携
# ✔ buy/short の setup 分類を追加
# ✔ AI gate / top_candidates / announce 連携しやすい出力
# ============================================================

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from trading.signals.signals_engine import evaluate_signals
from trading.signals.signal_state_manager import SignalStateManager
from trading.signals.signal_deduplicator import SignalDeduplicator
from trading.signals.signal_priority_resolver import resolve_signal_decision
from trading.signals.price_normalizer import normalize_inputs
from trading.signals.setup_mapper import map_signals_to_setups

logger = logging.getLogger(__name__)


# ============================================================
# singleton managers
# ============================================================

state_manager = SignalStateManager()
deduplicator = SignalDeduplicator(ttl_sec=60)


# ============================================================
# internal helpers
# ============================================================

def _safe_num(v, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except Exception:
        return default


def _resolve_setup_aware_decision(
    *,
    buy_signals: list[str],
    short_signals: list[str],
    score_buy: float | None = None,
    score_short: float | None = None,
    ranking_buy: float | None = None,
    ranking_short: float | None = None,
) -> Dict[str, Any]:
    """
    既存 decision と setup分類の両方を解決する。
    """

    # --- setup mapping ---
    mapped = map_signals_to_setups(
        buy_reasons=buy_signals,
        short_reasons=short_signals,
        score_buy=score_buy,
        score_short=score_short,
        ranking_buy=ranking_buy,
        ranking_short=ranking_short,
    )

    # --- legacy decision resolver ---
    legacy_decision = resolve_signal_decision(
        buy_signals=buy_signals,
        short_signals=short_signals,
        score_buy=score_buy,
        score_short=score_short,
        ranking_buy=ranking_buy,
        ranking_short=ranking_short,
    )

    # --- setup-based decision ---
    setup_side = mapped.get("decision_side")
    setup_name = mapped.get("decision_setup", "")

    # 基本は既存 resolver を尊重し、setup と矛盾しなければ採用
    final_decision = legacy_decision
    final_setup = ""

    if final_decision == "BUY":
        final_setup = mapped.get("buy_best_setup", "")
    elif final_decision == "SHORT":
        final_setup = mapped.get("short_best_setup", "")
    else:
        # legacy で決まらない場合は setup 側を採用
        final_decision = setup_side
        final_setup = setup_name

    return {
        "legacy_decision": legacy_decision,
        "decision": final_decision,
        "decision_setup": final_setup or "",
        "buy_best_setup": mapped.get("buy_best_setup", ""),
        "short_best_setup": mapped.get("short_best_setup", ""),
        "buy_best_score": _safe_num(mapped.get("buy_best_score")),
        "short_best_score": _safe_num(mapped.get("short_best_score")),
        "buy_setup_scores": mapped.get("buy_setup_scores", {}),
        "short_setup_scores": mapped.get("short_setup_scores", {}),
        "setup_reason_text": mapped.get("setup_reason_text", ""),
    }


# ============================================================
# public api
# ============================================================

def evaluate_symbol_signal(
    symbol: str,
    curr: dict,
    prev: dict | None = None,
    recent=None,
    *,
    score_buy: float | None = None,
    score_short: float | None = None,
    ranking_buy: float | None = None,
    ranking_short: float | None = None,
    dedup: bool = True,
):
    """
    既存 signals_manager の setup-aware 版。

    Parameters
    ----------
    symbol : str
    curr : dict
    prev : dict | None
    recent : DataFrame | None
    score_buy : float | None
        外部 scoring がある場合に与える
    score_short : float | None
        外部 scoring がある場合に与える
    ranking_buy : float | None
        ランキング順位（小さいほど強い）
    ranking_short : float | None
        ランキング順位（小さいほど強い）
    dedup : bool
        True の場合 deduplicator を通す
    """

    try:
        curr, prev, recent = normalize_inputs(curr, prev, recent)

        signals = evaluate_signals(
            curr=curr,
            prev=prev,
            recent=recent
        )

        buy = signals.get("buy", []) or []
        short = signals.get("short", []) or []

        raw_buy = list(buy)
        raw_short = list(short)

        # --------------------------------
        # deduplicate
        # --------------------------------
        if dedup:
            buy, short = deduplicator.filter_signals(
                symbol,
                buy,
                short
            )

        # --------------------------------
        # setup-aware decision
        # --------------------------------
        resolved = _resolve_setup_aware_decision(
            buy_signals=buy,
            short_signals=short,
            score_buy=score_buy,
            score_short=score_short,
            ranking_buy=ranking_buy,
            ranking_short=ranking_short,
        )

        decision = resolved["decision"]

        # --------------------------------
        # update state
        # --------------------------------
        state_manager.update_state(
            symbol=symbol,
            buy_signals=buy,
            short_signals=short,
            decision=decision
        )

        return {
            "symbol": symbol,

            # raw / dedup 後
            "raw_buy_signals": raw_buy,
            "raw_short_signals": raw_short,
            "buy_signals": buy,
            "short_signals": short,

            # external score/ranking
            "score_buy": score_buy,
            "score_short": score_short,
            "ranking_buy": ranking_buy,
            "ranking_short": ranking_short,

            # decision
            "legacy_decision": resolved["legacy_decision"],
            "decision": decision,
            "decision_setup": resolved["decision_setup"],

            # setup detail
            "buy_best_setup": resolved["buy_best_setup"],
            "short_best_setup": resolved["short_best_setup"],
            "buy_best_score": resolved["buy_best_score"],
            "short_best_score": resolved["short_best_score"],
            "buy_setup_scores": resolved["buy_setup_scores"],
            "short_setup_scores": resolved["short_setup_scores"],
            "setup_reason_text": resolved["setup_reason_text"],
        }

    except Exception:
        logger.exception("[SignalsManager] evaluation failed")

        return {
            "symbol": symbol,
            "raw_buy_signals": [],
            "raw_short_signals": [],
            "buy_signals": [],
            "short_signals": [],
            "score_buy": score_buy,
            "score_short": score_short,
            "ranking_buy": ranking_buy,
            "ranking_short": ranking_short,
            "legacy_decision": None,
            "decision": None,
            "decision_setup": "",
            "buy_best_setup": "",
            "short_best_setup": "",
            "buy_best_score": 0.0,
            "short_best_score": 0.0,
            "buy_setup_scores": {},
            "short_setup_scores": {},
            "setup_reason_text": "",
        }


def get_last_signal_state(symbol: str):
    """
    state_manager の保持状態を取得
    """
    try:
        return state_manager.get_state(symbol)
    except Exception:
        logger.exception("[SignalsManager] get_last_signal_state failed")
        return None


def get_last_decision(symbol: str):
    """
    直近 decision を取得
    """
    try:
        return state_manager.get_last_decision(symbol)
    except Exception:
        logger.exception("[SignalsManager] get_last_decision failed")
        return None