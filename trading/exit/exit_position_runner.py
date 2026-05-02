# ============================================================
# File   : trading/exit/exit_position_runner.py
# Version: V1.0-SPLIT-POSITION-RUNNER
# ------------------------------------------------------------
# 【概要】
#   1銘柄分のEXIT判定を担当。
#
# 【判定順序】
#   1. 価格取得
#   2. ctx / features 構築
#   3. collapse / inago
#   4. 殿様イナゴEXIT
#   5. collapse full exit
#   6. AI EXIT
#   7. boost guard
#   8. RL
#   9. manage_exit
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Dict, Optional, Tuple

from core.global_context.context import global_context as GC
from trading.exit.ai_exit_runner import apply_ai_exit_if_needed
from trading.exit.exit_features import build_exit_features_safe, inject_daily_features_safe
from trading.exit.exit_finalize import finalize_exit
from trading.exit.exit_price_source import get_latest_exit_price
from trading.exit.exit_state_machine import manage_exit
from trading.exit.exit_utils import safe_float
from trading.exit.tonosama_exit_runner import apply_tonosama_exit_if_needed

logger = logging.getLogger(__name__)


def evaluate_collapse(symbol: str, regime: int, features: Dict[str, Any], side: str) -> Tuple[float, int, Optional[str]]:
    collapse_prob = 0.0
    inago_state = 0
    full_reason = None

    try:
        if not hasattr(GC, "ai") or not GC.ai:
            return collapse_prob, inago_state, full_reason

        collapse_engine = GC.ai.get_collapse_engine()

        if collapse_engine:
            result = collapse_engine.evaluate(
                symbol=symbol,
                regime=regime,
                pre_feature_dict=features,
                regime_feature_dict=features,
            )

            collapse_prob = safe_float(result.get("strength"))

            if result.get("exit_ratio", 0) >= 1.0:
                full_reason = "COLLAPSE_ENGINE_FULL"
                return collapse_prob, inago_state, full_reason

        collapse_model = GC.ai.get_collapse_model()
        if collapse_model:
            legacy_prob = collapse_model.predict_proba(
                features,
                side=side,
            )
            collapse_prob = max(collapse_prob, safe_float(legacy_prob))

        inago_model = GC.ai.get_inago_model()
        if inago_model:
            ignite_prob, exhaust_prob = inago_model.predict(features)

            ignite_prob = safe_float(ignite_prob)
            exhaust_prob = safe_float(exhaust_prob)

            if ignite_prob > 0.7:
                inago_state = 1
            elif exhaust_prob > 0.6:
                inago_state = 2

    except Exception:
        logger.exception("[COLLAPSE/INAGO_ERROR]")

    return collapse_prob, inago_state, full_reason


def evaluate_rl(symbol: str, regime: int, cluster_id: int, inago_state: int, pnl: float):
    try:
        if not hasattr(GC, "ai") or not GC.ai:
            return None, None

        rl_agent = GC.ai.get_rl_agent()
        if not rl_agent:
            return None, None

        rl_state = rl_agent.encode_state(
            regime,
            cluster_id,
            inago_state,
            pnl,
        )

        rl_action = rl_agent.select_action(rl_state)

        return rl_action, rl_state

    except Exception:
        logger.exception("[RL_ERROR]")
        return None, None


def run_exit_for_position(
    *,
    symbol: str,
    pos: Dict[str, Any],
    now: dt.datetime,
    regime: int,
    boost_active: bool,
) -> bool:
    """
    1銘柄分のEXIT処理。

    戻り値:
      True:
        何らかのEXIT処理またはDRY_RUN EXITが発生した

      False:
        EXITなし
    """

    try:
        entry_price = safe_float(pos.get("avg_price"))
        side = pos.get("side")

        if not entry_price:
            return False

        price, bar5s = get_latest_exit_price(symbol)
        if not price:
            return False

        pnl = price - entry_price if str(side).upper() == "BUY" else entry_price - price
        pnl = safe_float(pnl)

        if not hasattr(GC, "ai") or not GC.ai:
            return False

        ctx = GC.ai.get_exit_ctx(symbol)
        if not ctx:
            return False

        features = build_exit_features_safe(ctx, price, pnl)
        features = inject_daily_features_safe(symbol, features)

        try:
            cluster_id = GC.positions.get_cluster(symbol) or 0
        except Exception:
            cluster_id = 0

        collapse_prob, inago_state, full_reason = evaluate_collapse(
            symbol,
            regime,
            features,
            side,
        )

        features["collapse_prob"] = collapse_prob

        # ====================================================
        # 1. TONOSAMA INAGO EXIT
        #    損切り・VWAP割れ・5秒足失速などをAIより前に判定
        # ====================================================
        if apply_tonosama_exit_if_needed(
            symbol=symbol,
            pos=pos,
            side=side,
            price=price,
            entry_price=entry_price,
            features=features,
            ctx=ctx,
            now=now,
            cluster_id=cluster_id,
            regime=regime,
            inago_state=inago_state,
            pnl=pnl,
            collapse_prob=collapse_prob,
            bar5s=bar5s,
        ):
            return True

        # ====================================================
        # 2. collapse full exit
        # ====================================================
        if full_reason or collapse_prob > 0.85:
            finalize_exit(
                symbol=symbol,
                price=price,
                reason=full_reason or "COLLAPSE_EXIT",
                cluster_id=cluster_id,
                regime=regime,
                inago_state=inago_state,
                pnl=pnl,
                collapse_prob=collapse_prob,
                ctx=ctx,
            )
            return True

        # ====================================================
        # 3. AI EXIT
        # ====================================================
        if apply_ai_exit_if_needed(
            symbol=symbol,
            side=side,
            price=price,
            entry_price=entry_price,
            pnl=pnl,
            features=features,
            ctx=ctx,
            now=now,
            cluster_id=cluster_id,
            regime=regime,
            inago_state=inago_state,
            collapse_prob=collapse_prob,
        ):
            return True

        # ====================================================
        # 4. boost guard
        #    boost中でATR利益が出ている場合は粘る
        # ====================================================
        if boost_active:
            atr = getattr(ctx, "atr_1min", 0.0)
            if atr and pnl > atr * 2:
                return False

        # ====================================================
        # 5. RL EXIT
        # ====================================================
        rl_action, rl_state = evaluate_rl(
            symbol,
            regime,
            cluster_id,
            inago_state,
            pnl,
        )

        if rl_action in ("EXIT", "TAKE"):
            finalize_exit(
                symbol=symbol,
                price=price,
                reason=f"RL_{rl_action}",
                cluster_id=cluster_id,
                regime=regime,
                inago_state=inago_state,
                pnl=pnl,
                collapse_prob=collapse_prob,
                ctx=ctx,
                rl_state=rl_state,
            )
            return True

        # ====================================================
        # 6. 通常 state machine EXIT
        # ====================================================
        action, reason = manage_exit(
            ctx=ctx,
            price=price,
            now=now,
        )

        if action == "EXIT":
            finalize_exit(
                symbol=symbol,
                price=price,
                reason=reason,
                cluster_id=cluster_id,
                regime=regime,
                inago_state=inago_state,
                pnl=pnl,
                collapse_prob=collapse_prob,
                ctx=ctx,
                rl_state=rl_state,
            )
            return True

        return False

    except Exception:
        logger.exception("[EXIT_SYMBOL_FATAL] symbol=%s", symbol)
        return False


__all__ = [
    "run_exit_for_position",
    "evaluate_collapse",
    "evaluate_rl",
]