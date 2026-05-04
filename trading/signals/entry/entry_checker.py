# ============================================================
# File   : trading/signals/entry/entry_checker.py
# Version: PRODUCTION-STABLE-REV2.0-SETUP-AWARE
# ------------------------------------------------------------
# ✔ ENTRY 判定の唯一の司令塔
# ✔ signal_state / prev_state / position_state を統合
# ✔ multi_tf → setup gate → rules(BUY/SELL) → retest gate → state 更新
# ✔ 注文実行は行わない（純粋判定）
# ✔ signals_manager の decision_setup / buy_best_score 連携
# ✔ retest_entry の追加判定対応
# ============================================================

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple, List

from trading.signals.state.signal_state import SignalState
from trading.signals.state.prev_state import PrevSignalState
from trading.signals.state.position_state import PositionState

from trading.signals.timeframe.multi_tf import check_multi_tf
from trading.signals.rules import buy_rules, sell_rules
from trading.signals.entry.retest_entry import check_retest


# ============================================================
# 返却型
# ============================================================

EntryResult = Tuple[
    Optional[str],        # "BUY" / "SELL" / None
    List[str],            # reasons
    Dict[str, object],    # debug info
]


# ============================================================
# setup helper
# ============================================================

BUY_SETUP_ALLOW = {
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
}

SELL_SETUP_ALLOW = {
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
}


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except Exception:
        return default


def _safe_str(v: Any, default: str = "") -> str:
    try:
        if v is None:
            return default
        return str(v)
    except Exception:
        return default


def _get_setup_fields(tf_1m: Dict[str, Any]) -> Dict[str, Any]:
    """
    tf_1m から setup情報を取り出す。
    signals_manager / setup_mapper / add_entry_signals のどちらでも最低限対応。
    """
    setup = _safe_str(tf_1m.get("decision_setup") or tf_1m.get("entry_setup_type"), "")
    setup_reason = _safe_str(tf_1m.get("setup_reason_text") or tf_1m.get("setup_reason"), "")

    buy_best_setup = _safe_str(tf_1m.get("buy_best_setup"), "")
    short_best_setup = _safe_str(tf_1m.get("short_best_setup"), "")

    buy_best_score = _safe_float(tf_1m.get("buy_best_score") or tf_1m.get("setup_score") or tf_1m.get("setup_score_buy"), 0.0)
    short_best_score = _safe_float(tf_1m.get("short_best_score") or tf_1m.get("setup_score_short"), 0.0)

    is_setup_entry = tf_1m.get("is_setup_entry")
    if is_setup_entry is None:
        is_setup_entry = bool(setup)

    return {
        "decision_setup": setup,
        "setup_reason_text": setup_reason,
        "buy_best_setup": buy_best_setup,
        "short_best_setup": short_best_setup,
        "buy_best_score": buy_best_score,
        "short_best_score": short_best_score,
        "is_setup_entry": bool(is_setup_entry),
    }


def _setup_gate_buy(
    tf_1m: Dict[str, Any],
    *,
    min_setup_score_buy: float,
) -> Tuple[bool, List[str], Dict[str, Any]]:
    info = _get_setup_fields(tf_1m)
    setup = info["buy_best_setup"] or info["decision_setup"]
    score = info["buy_best_score"]

    debug = {
        "setup_buy": setup,
        "setup_buy_score": score,
        "setup_is_entry": info["is_setup_entry"],
    }

    reasons: List[str] = []

    if not info["is_setup_entry"] and not setup:
        debug["blocked_by_setup"] = "setup_missing"
        return False, reasons, debug

    if setup and setup not in BUY_SETUP_ALLOW:
        debug["blocked_by_setup"] = "setup_not_allowed"
        return False, reasons, debug

    if score < float(min_setup_score_buy):
        debug["blocked_by_setup"] = "setup_score_low"
        return False, reasons, debug

    if setup:
        reasons.append(f"setup={setup}")
    if info["setup_reason_text"]:
        reasons.append(info["setup_reason_text"])

    return True, reasons, debug


def _setup_gate_sell(
    tf_1m: Dict[str, Any],
    *,
    min_setup_score_sell: float,
) -> Tuple[bool, List[str], Dict[str, Any]]:
    info = _get_setup_fields(tf_1m)
    setup = info["short_best_setup"] or info["decision_setup"]
    score = info["short_best_score"]

    debug = {
        "setup_sell": setup,
        "setup_sell_score": score,
        "setup_is_entry": info["is_setup_entry"],
    }

    reasons: List[str] = []

    if not info["is_setup_entry"] and not setup:
        debug["blocked_by_setup"] = "setup_missing"
        return False, reasons, debug

    if setup and setup not in SELL_SETUP_ALLOW:
        debug["blocked_by_setup"] = "setup_not_allowed"
        return False, reasons, debug

    if score < float(min_setup_score_sell):
        debug["blocked_by_setup"] = "setup_score_low"
        return False, reasons, debug

    if setup:
        reasons.append(f"setup={setup}")
    if info["setup_reason_text"]:
        reasons.append(info["setup_reason_text"])

    return True, reasons, debug


def _need_retest_check(setup_name: str) -> bool:
    return setup_name in {
        "pullback",
        "retest_success",
        "vwap_reclaim",
        "fakeout_reversal",
        "retest_failure",
        "vwap_fail",
    }


def _run_buy_retest_if_needed(
    *,
    setup_name: str,
    tf_1m: Dict[str, Any],
) -> Tuple[bool, List[str], Dict[str, Any]]:
    debug: Dict[str, Any] = {"retest_check": False}
    reasons: List[str] = []

    if not _need_retest_check(setup_name):
        return True, reasons, debug

    debug["retest_check"] = True

    ok, retest_reasons = check_retest(
        side="BUY",
        price=_safe_float(tf_1m.get("open_price", tf_1m.get("open", 0.0))),
        low=_safe_float(tf_1m.get("low_price", tf_1m.get("low", 0.0))),
        close=_safe_float(tf_1m.get("close_price", tf_1m.get("close", 0.0))),
        prev_high=_safe_float(tf_1m.get("prev_high", tf_1m.get("recent_breakout_level", 0.0))),
        ma25=_safe_float(tf_1m.get("ma25")) if tf_1m.get("ma25") is not None else None,
        vwap=_safe_float(tf_1m.get("vwap")) if tf_1m.get("vwap") is not None else None,
        rsi=_safe_float(tf_1m.get("rsi")),
        rci=_safe_float(tf_1m.get("rci")),
        volume_now=_safe_float(tf_1m.get("volume")),
        volume_avg=_safe_float(tf_1m.get("volume_ma5", tf_1m.get("avg_volume", 0.0))),
        prev_open=_safe_float(tf_1m.get("prev_open")) if tf_1m.get("prev_open") is not None else None,
        prev_close=_safe_float(tf_1m.get("prev_close")) if tf_1m.get("prev_close") is not None else None,
        tolerance=_safe_float(tf_1m.get("retest_tolerance", 0.003), 0.003),
    )
    debug["retest_ok"] = ok
    debug["retest_reasons"] = retest_reasons

    if ok:
        reasons.extend(retest_reasons)

    return ok, reasons, debug


def _run_sell_retest_if_needed(
    *,
    setup_name: str,
    tf_1m: Dict[str, Any],
) -> Tuple[bool, List[str], Dict[str, Any]]:
    debug: Dict[str, Any] = {"retest_check": False}
    reasons: List[str] = []

    if not _need_retest_check(setup_name):
        return True, reasons, debug

    debug["retest_check"] = True

    ok, retest_reasons = check_retest(
        side="SELL",
        price=_safe_float(tf_1m.get("open_price", tf_1m.get("open", 0.0))),
        high=_safe_float(tf_1m.get("high_price", tf_1m.get("high", 0.0))),
        close=_safe_float(tf_1m.get("close_price", tf_1m.get("close", 0.0))),
        prev_low=_safe_float(tf_1m.get("prev_low", tf_1m.get("recent_breakout_level", 0.0))),
        ma25=_safe_float(tf_1m.get("ma25")) if tf_1m.get("ma25") is not None else None,
        vwap=_safe_float(tf_1m.get("vwap")) if tf_1m.get("vwap") is not None else None,
        rsi=_safe_float(tf_1m.get("rsi")),
        rci=_safe_float(tf_1m.get("rci")),
        volume_now=_safe_float(tf_1m.get("volume")),
        volume_avg=_safe_float(tf_1m.get("volume_ma5", tf_1m.get("avg_volume", 0.0))),
        prev_open=_safe_float(tf_1m.get("prev_open")) if tf_1m.get("prev_open") is not None else None,
        prev_close=_safe_float(tf_1m.get("prev_close")) if tf_1m.get("prev_close") is not None else None,
        tolerance=_safe_float(tf_1m.get("retest_tolerance", 0.003), 0.003),
    )
    debug["retest_ok"] = ok
    debug["retest_reasons"] = retest_reasons

    if ok:
        reasons.extend(retest_reasons)

    return ok, reasons, debug


# ============================================================
# ENTRY 判定本体
# ============================================================

def check_entry(
    *,
    symbol: str,
    tf_1m: Dict,
    tf_3m: Dict,
    tf_5m: Dict,
    signal_state: SignalState,
    prev_state: PrevSignalState,
    position_state: PositionState,
    min_setup_score_buy: float = 20.0,
    min_setup_score_sell: float = 20.0,
    use_setup_gate: bool = True,
    use_retest_gate: bool = True,
) -> EntryResult:
    """
    ENTRY 判定を行う（注文は出さない）

    戻り値:
        (signal, reasons, debug)
        signal: "BUY" / "SELL" / None
    """

    debug: Dict[str, object] = {
        "symbol": symbol,
    }

    # ============================================================
    # 0. グローバル状態チェック
    # ============================================================

    if not signal_state.is_allow():
        debug["blocked_by"] = "signal_state"
        return None, [], debug

    if position_state.has_position():
        debug["blocked_by"] = "already_has_position"
        return None, [], debug

    if prev_state.is_cooldown():
        debug["blocked_by"] = "prev_state_cooldown"
        return None, [], debug

    # ============================================================
    # 1. Multi Timeframe 環境チェック
    # ============================================================

    mtf_result = check_multi_tf(
        tf_1m=tf_1m,
        tf_3m=tf_3m,
        tf_5m=tf_5m,
    )
    debug["multi_tf"] = mtf_result

    if not mtf_result["multi_tf_ok"]:
        debug["blocked_by"] = "multi_tf"
        return None, [], debug

    direction = mtf_result["dir_5m"]
    debug["direction"] = direction

    # setup 情報は先に保存
    setup_info = _get_setup_fields(tf_1m)
    debug["setup_info"] = setup_info

    # ============================================================
    # 2. BUY / SELL ルール判定
    # ============================================================

    # --- BUY（LONG） ---
    if direction >= 0:
        setup_reasons_buy: List[str] = []

        if use_setup_gate:
            ok_setup, setup_reasons_buy, setup_debug = _setup_gate_buy(
                tf_1m,
                min_setup_score_buy=min_setup_score_buy,
            )
            debug["setup_gate_buy"] = setup_debug
            if not ok_setup:
                debug["blocked_by_buy"] = "setup_gate"
            else:
                ok, reasons = buy_rules.check_buy_rules(**tf_1m)
                debug["buy_rule_ok"] = ok
                debug["buy_rule_reasons"] = reasons

                if ok and prev_state.can_emit("BUY", symbol):
                    retest_reasons_buy: List[str] = []
                    if use_retest_gate:
                        setup_name = setup_info["buy_best_setup"] or setup_info["decision_setup"]
                        ok_retest, retest_reasons_buy, retest_debug = _run_buy_retest_if_needed(
                            setup_name=setup_name,
                            tf_1m=tf_1m,
                        )
                        debug["buy_retest"] = retest_debug
                        if not ok_retest:
                            debug["blocked_by_buy"] = "retest_gate"
                        else:
                            all_reasons = list(setup_reasons_buy) + list(reasons) + list(retest_reasons_buy)
                            debug["rule"] = "BUY"
                            return "BUY", all_reasons, debug
                    else:
                        all_reasons = list(setup_reasons_buy) + list(reasons)
                        debug["rule"] = "BUY"
                        return "BUY", all_reasons, debug

    # --- SELL（SHORT） ---
    if direction <= 0:
        setup_reasons_sell: List[str] = []

        if use_setup_gate:
            ok_setup, setup_reasons_sell, setup_debug = _setup_gate_sell(
                tf_1m,
                min_setup_score_sell=min_setup_score_sell,
            )
            debug["setup_gate_sell"] = setup_debug
            if not ok_setup:
                debug["blocked_by_sell"] = "setup_gate"
            else:
                ok, reasons = sell_rules.check_sell_rules(**tf_1m)
                debug["sell_rule_ok"] = ok
                debug["sell_rule_reasons"] = reasons

                if ok and prev_state.can_emit("SELL", symbol):
                    retest_reasons_sell: List[str] = []
                    if use_retest_gate:
                        setup_name = setup_info["short_best_setup"] or setup_info["decision_setup"]
                        ok_retest, retest_reasons_sell, retest_debug = _run_sell_retest_if_needed(
                            setup_name=setup_name,
                            tf_1m=tf_1m,
                        )
                        debug["sell_retest"] = retest_debug
                        if not ok_retest:
                            debug["blocked_by_sell"] = "retest_gate"
                        else:
                            all_reasons = list(setup_reasons_sell) + list(reasons) + list(retest_reasons_sell)
                            debug["rule"] = "SELL"
                            return "SELL", all_reasons, debug
                    else:
                        all_reasons = list(setup_reasons_sell) + list(reasons)
                        debug["rule"] = "SELL"
                        return "SELL", all_reasons, debug

    debug["blocked_by"] = "rules_not_matched"
    return None, [], debug


# ============================================================
# ENTRY 確定後の state 更新
# ============================================================

def commit_entry(
    *,
    signal: str,
    symbol: str,
    reasons: List[str],
    score: Optional[float],
    prev_state: PrevSignalState,
    signal_state: SignalState,
) -> None:
    """
    ENTRY 確定後に呼ぶ
    """

    # --- prev_state 更新 ---
    prev_state.update(
        signal=signal,
        symbol=symbol,
        reason=", ".join(reasons),
        score=score,
        start_cooldown=True,
    )

    # --- signal_state は通常状態へ ---
    signal_state.reset()