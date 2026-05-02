# ============================================================
# File   : trading/signals/entry/runner_bridge.py
# Version: PRODUCTION-STABLE-REV1.0-ENTRY-RUNNER-BRIDGE
# ------------------------------------------------------------
# 【概要】
#   signals_manager → cooldown → entry_checker をつなぐ橋渡し層
#
# 【主な機能】
#   - tf_1m / tf_3m / tf_5m の最新行抽出
#   - signals_manager 結果を tf_1m へマージ
#   - cooldown 適用
#   - entry_checker 実行
#   - 必要なら commit_entry 実行
#
# 【想定用途】
#   - 定時 summary 後の entry 判定
#   - PUSH / RANKING / YAHOO 補完後の entry 判定
#   - 銘柄単位の thin runner
# ============================================================

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

import pandas as pd

from trading.signals.entry.cooldown import apply_all_cooldowns
from trading.signals.entry.entry_checker import check_entry, commit_entry
from trading.signals.signals_manager import evaluate_symbol_signal

logger = logging.getLogger(__name__)


# ============================================================
# util
# ============================================================

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


def _ensure_datetime(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    out = df.copy()
    if "datetime" in out.columns and not pd.api.types.is_datetime64_any_dtype(out["datetime"]):
        out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
    return out


def _latest_row_as_dict(df: Optional[pd.DataFrame], symbol: str) -> Dict[str, Any]:
    """
    symbol の最新行を dict で返す
    """
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return {}

    out = _ensure_datetime(df)

    if "symbol" in out.columns:
        out = out[out["symbol"].astype(str) == str(symbol)].copy()

    if out.empty:
        return {}

    sort_cols = []
    if "datetime" in out.columns:
        sort_cols.append("datetime")

    if sort_cols:
        out = out.sort_values(sort_cols)

    try:
        row = out.iloc[-1]
        return row.to_dict()
    except Exception:
        return {}


def _prev_row_as_dict(df: Optional[pd.DataFrame], symbol: str) -> Dict[str, Any]:
    """
    symbol の1つ前の行を dict で返す
    """
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return {}

    out = _ensure_datetime(df)

    if "symbol" in out.columns:
        out = out[out["symbol"].astype(str) == str(symbol)].copy()

    if len(out) < 2:
        return {}

    sort_cols = []
    if "datetime" in out.columns:
        sort_cols.append("datetime")

    if sort_cols:
        out = out.sort_values(sort_cols)

    try:
        row = out.iloc[-2]
        return row.to_dict()
    except Exception:
        return {}


def _recent_df(df: Optional[pd.DataFrame], symbol: str, recent_bars: int = 30) -> pd.DataFrame:
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame()

    out = _ensure_datetime(df)

    if "symbol" in out.columns:
        out = out[out["symbol"].astype(str) == str(symbol)].copy()

    if out.empty:
        return pd.DataFrame()

    sort_cols = []
    if "datetime" in out.columns:
        sort_cols.append("datetime")

    if sort_cols:
        out = out.sort_values(sort_cols)

    return out.tail(recent_bars).copy().reset_index(drop=True)


def _merge_dict(base: Dict[str, Any], extra: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    out = dict(base or {})
    if not extra:
        return out
    for k, v in extra.items():
        out[k] = v
    return out


def _derive_external_scores(curr_1m: Dict[str, Any]) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
    """
    signals_manager に渡す外部 score / ranking を決める
    """
    score_buy = curr_1m.get("score_buy")
    score_short = curr_1m.get("score_short")

    # score_sell がある場合は short に寄せる
    if score_short is None and "score_sell" in curr_1m:
        score_short = curr_1m.get("score_sell")

    ranking_buy = curr_1m.get("ranking_buy")
    ranking_short = curr_1m.get("ranking_short")

    return score_buy, score_short, ranking_buy, ranking_short


# ============================================================
# main bridge
# ============================================================

def run_entry_bridge_for_symbol(
    *,
    symbol: str,
    df_1m: pd.DataFrame,
    df_3m: pd.DataFrame,
    df_5m: pd.DataFrame,
    signal_state,
    prev_state,
    position_state,
    recent_realized_pnl: Optional[float] = None,
    now: Optional[datetime] = None,
    commit: bool = True,
    recent_bars_1m: int = 30,
    min_setup_score_buy: float = 20.0,
    min_setup_score_sell: float = 20.0,
    use_setup_gate: bool = True,
    use_retest_gate: bool = True,
) -> Dict[str, Any]:
    """
    1銘柄分の entry 判定を実行する。

    Returns
    -------
    {
        "symbol": ...,
        "signal": "BUY"/"SELL"/None,
        "reasons": [...],
        "debug": {...},
        "tf_1m": {...},
        "tf_3m": {...},
        "tf_5m": {...},
        "signals_result": {...},
        "cooldown_snapshot": {...},
        "committed": bool,
    }
    """
    result: Dict[str, Any] = {
        "symbol": symbol,
        "signal": None,
        "reasons": [],
        "debug": {},
        "tf_1m": {},
        "tf_3m": {},
        "tf_5m": {},
        "signals_result": {},
        "cooldown_snapshot": {},
        "committed": False,
    }

    try:
        # ----------------------------------------------------
        # latest bars
        # ----------------------------------------------------
        curr_1m = _latest_row_as_dict(df_1m, symbol)
        prev_1m = _prev_row_as_dict(df_1m, symbol)
        recent_1m = _recent_df(df_1m, symbol, recent_bars=recent_bars_1m)

        curr_3m = _latest_row_as_dict(df_3m, symbol)
        curr_5m = _latest_row_as_dict(df_5m, symbol)

        result["tf_1m"] = curr_1m
        result["tf_3m"] = curr_3m
        result["tf_5m"] = curr_5m

        if not curr_1m:
            result["debug"]["blocked_by"] = "tf_1m_missing"
            return result

        # ----------------------------------------------------
        # signals_manager
        # ----------------------------------------------------
        score_buy, score_short, ranking_buy, ranking_short = _derive_external_scores(curr_1m)

        signals_result = evaluate_symbol_signal(
            symbol=symbol,
            curr=curr_1m,
            prev=prev_1m if prev_1m else None,
            recent=recent_1m if not recent_1m.empty else None,
            score_buy=score_buy,
            score_short=score_short,
            ranking_buy=ranking_buy,
            ranking_short=ranking_short,
            dedup=True,
        )
        result["signals_result"] = signals_result

        # signals_manager の結果を tf_1m へマージ
        tf_1m = _merge_dict(curr_1m, signals_result)

        # prev値を retest gate 用に保持
        if prev_1m:
            for k in ("open_price", "close_price", "high_price", "low_price", "open", "close", "high", "low"):
                if k in prev_1m:
                    tf_1m[f"prev_{k.replace('_price', '')}" if k.endswith("_price") else f"prev_{k}"] = prev_1m[k]

            if "high_price" in prev_1m:
                tf_1m["prev_high"] = prev_1m["high_price"]
            elif "high" in prev_1m:
                tf_1m["prev_high"] = prev_1m["high"]

            if "low_price" in prev_1m:
                tf_1m["prev_low"] = prev_1m["low_price"]
            elif "low" in prev_1m:
                tf_1m["prev_low"] = prev_1m["low"]

            if "open_price" in prev_1m:
                tf_1m["prev_open"] = prev_1m["open_price"]
            elif "open" in prev_1m:
                tf_1m["prev_open"] = prev_1m["open"]

            if "close_price" in prev_1m:
                tf_1m["prev_close"] = prev_1m["close_price"]
            elif "close" in prev_1m:
                tf_1m["prev_close"] = prev_1m["close"]

        # recent平均も補う
        if not recent_1m.empty and "volume" in recent_1m.columns:
            try:
                tf_1m["volume_ma5"] = recent_1m["volume"].tail(5).mean()
                tf_1m["avg_volume"] = recent_1m["volume"].mean()
            except Exception:
                pass

        result["tf_1m"] = tf_1m

        # ----------------------------------------------------
        # cooldown
        # ----------------------------------------------------
        cooldown_snapshot = apply_all_cooldowns(
            now=now,
            signal_state=signal_state,
            prev_state=prev_state,
            position_state=position_state,
            recent_realized_pnl=recent_realized_pnl,
        )
        result["cooldown_snapshot"] = cooldown_snapshot

        # ----------------------------------------------------
        # entry check
        # ----------------------------------------------------
        signal, reasons, debug = check_entry(
            symbol=symbol,
            tf_1m=tf_1m,
            tf_3m=curr_3m,
            tf_5m=curr_5m,
            signal_state=signal_state,
            prev_state=prev_state,
            position_state=position_state,
            min_setup_score_buy=min_setup_score_buy,
            min_setup_score_sell=min_setup_score_sell,
            use_setup_gate=use_setup_gate,
            use_retest_gate=use_retest_gate,
        )

        result["signal"] = signal
        result["reasons"] = reasons
        result["debug"] = debug

        # ----------------------------------------------------
        # commit
        # ----------------------------------------------------
        if signal and commit:
            score = None
            if signal == "BUY":
                score = tf_1m.get("buy_best_score", tf_1m.get("score_buy"))
            elif signal == "SELL":
                score = tf_1m.get("short_best_score", tf_1m.get("score_short", tf_1m.get("score_sell")))

            commit_entry(
                signal=signal,
                symbol=symbol,
                reasons=reasons,
                score=score,
                prev_state=prev_state,
                signal_state=signal_state,
            )
            result["committed"] = True

        return result

    except Exception:
        logger.exception("[entry.runner_bridge] failed symbol=%s", symbol)
        result["debug"]["exception"] = True
        return result


# ============================================================
# batch helper
# ============================================================

def run_entry_bridge_for_symbols(
    *,
    symbols,
    df_1m: pd.DataFrame,
    df_3m: pd.DataFrame,
    df_5m: pd.DataFrame,
    signal_state_map: Dict[str, Any],
    prev_state_map: Dict[str, Any],
    position_state_map: Dict[str, Any],
    recent_realized_pnl_map: Optional[Dict[str, float]] = None,
    now: Optional[datetime] = None,
    commit: bool = True,
    recent_bars_1m: int = 30,
    min_setup_score_buy: float = 20.0,
    min_setup_score_sell: float = 20.0,
    use_setup_gate: bool = True,
    use_retest_gate: bool = True,
) -> Dict[str, Dict[str, Any]]:
    """
    複数銘柄版
    """
    outputs: Dict[str, Dict[str, Any]] = {}

    for symbol in symbols:
        try:
            s = str(symbol)

            signal_state = signal_state_map[s]
            prev_state = prev_state_map[s]
            position_state = position_state_map[s]
            pnl = None
            if recent_realized_pnl_map:
                pnl = recent_realized_pnl_map.get(s)

            outputs[s] = run_entry_bridge_for_symbol(
                symbol=s,
                df_1m=df_1m,
                df_3m=df_3m,
                df_5m=df_5m,
                signal_state=signal_state,
                prev_state=prev_state,
                position_state=position_state,
                recent_realized_pnl=pnl,
                now=now,
                commit=commit,
                recent_bars_1m=recent_bars_1m,
                min_setup_score_buy=min_setup_score_buy,
                min_setup_score_sell=min_setup_score_sell,
                use_setup_gate=use_setup_gate,
                use_retest_gate=use_retest_gate,
            )
        except Exception:
            logger.exception("[entry.runner_bridge] batch failed symbol=%s", symbol)

    return outputs