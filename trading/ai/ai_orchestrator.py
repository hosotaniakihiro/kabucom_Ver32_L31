# ============================================================
# trading/ai/ai_orchestrator.py
#
# AI ORCHESTRATOR
#
# Central controller for all AI modules
#
# Controls
#
#   entry decision
#   exit decision
#   position sizing
#   regime switching
#
# ============================================================

from __future__ import annotations

import logging
import math
from typing import Dict

from trading.ai.meta_entry_ai import get_meta_entry_ai
from trading.ai.meta_exit_ai import get_meta_exit_ai
from trading.ai.risk_manager_ai import get_risk_manager_ai
from trading.ai.market_regime_ai import get_market_regime_ai

logger = logging.getLogger(__name__)


# ============================================================
# Utility
# ============================================================

def _safe(v):

    try:

        f = float(v)

        if not math.isfinite(f):
            return 0.0

        return f

    except Exception:

        return 0.0


# ============================================================
# AI Orchestrator
# ============================================================

class AIOrchestrator:

    def __init__(self):

        self.entry_ai = get_meta_entry_ai()
        self.exit_ai = get_meta_exit_ai()
        self.risk_ai = get_risk_manager_ai()
        self.regime_ai = get_market_regime_ai()

    # --------------------------------------------------------
    # Main pipeline
    # --------------------------------------------------------

    def evaluate(self, signals: Dict) -> Dict:

        try:

            regime_result = self._evaluate_regime(signals)

            signals.update(regime_result)

            entry_result = self.entry_ai.evaluate(signals)

            exit_result = self.exit_ai.evaluate(signals)

            risk_result = self.risk_ai.evaluate({

                **signals,
                **entry_result
            })

            return {

                "entry": entry_result,
                "exit": exit_result,
                "risk": risk_result,
                "regime": regime_result

            }

        except Exception:

            logger.exception("AIOrchestrator failure")

            return {

                "entry": {"entry_signal": "NO_TRADE"},
                "exit": {"exit_signal": "HOLD"},
                "risk": {"position_size": 0},
                "regime": {"regime": "UNKNOWN"}

            }

    # --------------------------------------------------------
    # Regime
    # --------------------------------------------------------

    def _evaluate_regime(self, signals):

        try:

            df = signals.get("price_df")

            spread = _safe(signals.get("spread"))

            bid_volume = _safe(signals.get("bid_volume"))
            ask_volume = _safe(signals.get("ask_volume"))

            algo_score = _safe(
                signals.get("algo_spike_score")
            )

            toxicity = _safe(
                signals.get("toxicity")
            )

            regime = self.regime_ai.detect(

                df=df,
                spread=spread,
                bid_volume=bid_volume,
                ask_volume=ask_volume,
                algo_score=algo_score,
                toxicity=toxicity

            )

            return regime

        except Exception:

            logger.exception("Regime evaluation failed")

            return {"regime": "UNKNOWN"}

    # --------------------------------------------------------
    # Entry decision shortcut
    # --------------------------------------------------------

    def should_enter(self, signals):

        result = self.evaluate(signals)

        entry_signal = result["entry"].get("entry_signal")

        size = result["risk"].get("position_size")

        if entry_signal in ("ENTRY", "STRONG_ENTRY") and size > 0:

            return True, result

        return False, result

    # --------------------------------------------------------
    # Exit decision shortcut
    # --------------------------------------------------------

    def should_exit(self, signals):

        result = self.evaluate(signals)

        exit_signal = result["exit"].get("exit_signal")

        if exit_signal in (

            "EXIT",
            "STRONG_EXIT",
            "STOP_LOSS",
            "TRAIL_EXIT"

        ):

            return True, result

        return False, result


# ============================================================
# Singleton
# ============================================================

_ai = None


def get_ai_orchestrator():

    global _ai

    if _ai is None:

        _ai = AIOrchestrator()

    return _ai



# ============================================================
# IGNITION ALERT
# ============================================================

def notify_ignition(symbol=None, message=None, **kwargs):
    try:
        print(f"[IGNITION] {symbol} {message}")
    except Exception:
        pass