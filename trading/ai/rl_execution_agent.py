# ============================================================
# trading/ai/rl_execution_agent.py
#
# PRODUCTION RL EXECUTION AGENT
#
# Reinforcement learning policy for trading decisions
#
# Handles:
#   entry
#   exit
#   execution aggressiveness
#   scaling
#
# ============================================================

from __future__ import annotations

import logging
import numpy as np
from typing import Dict, Optional
import torch
import torch.nn as nn
import torch.optim as optim
logger = logging.getLogger(__name__)

try:
    import torch
    import torch.nn as nn
except Exception:
    torch = None


# ============================================================
# Utility
# ============================================================

def _clip(x, lo=-1.0, hi=1.0):
    return max(lo, min(x, hi))


def _safe_array(features: Dict):

    vals = []

    for v in features.values():

        try:
            vals.append(float(v))
        except Exception:
            vals.append(0.0)

    return np.array(vals, dtype=float)


# ============================================================
# RL Policy Network
# ============================================================

class PolicyNetwork(nn.Module):

    def __init__(self, input_dim, hidden=64):

        super().__init__()

        self.net = nn.Sequential(

            nn.Linear(input_dim, hidden),
            nn.ReLU(),

            nn.Linear(hidden, hidden),
            nn.ReLU(),

            nn.Linear(hidden, 4)

        )

    def forward(self, x):

        return self.net(x)


# ============================================================
# RL Execution Agent
# ============================================================

class RLExecutionAgent:

    ACTIONS = [

        "HOLD",
        "BUY",
        "SELL",
        "EXIT"

    ]

    def __init__(self):

        self.model: Optional[object] = None

        self.input_dim = None

        self.loaded = False

    # --------------------------------------------------------
    # load model
    # --------------------------------------------------------

    def load(self, path: str, input_dim: int):

        if torch is None:

            logger.warning("PyTorch not installed")

            return

        try:

            self.input_dim = input_dim

            model = PolicyNetwork(input_dim)

            model.load_state_dict(

                torch.load(path, map_location="cpu")

            )

            model.eval()

            self.model = model

            self.loaded = True

            logger.info("RL agent loaded")

        except Exception:

            logger.exception("RL model load failure")

    # --------------------------------------------------------
    # act
    # --------------------------------------------------------

    def act(self, features: Dict) -> Dict:

        try:

            x = _safe_array(features)

            if len(x) == 0:

                return self._fallback()

            if self.loaded and self.model is not None:

                return self._policy_action(x)

            else:

                return self._rule_action(x)

        except Exception:

            logger.exception("RL act failure")

            return self._fallback()

    # --------------------------------------------------------
    # RL policy
    # --------------------------------------------------------

    def _policy_action(self, x):

        with torch.no_grad():

            tensor = torch.tensor(
                x,
                dtype=torch.float32
            ).unsqueeze(0)

            logits = self.model(tensor)

            probs = torch.softmax(logits, dim=1)

            action_id = int(torch.argmax(probs))

            confidence = float(probs[0][action_id])

            action = self.ACTIONS[action_id]

            return {

                "action": action,

                "confidence": confidence

            }

    # --------------------------------------------------------
    # fallback rule policy
    # --------------------------------------------------------

    def _rule_action(self, x):

        score = np.mean(x)

        if score > 0.5:

            return {

                "action": "BUY",

                "confidence": _clip(score)

            }

        if score < -0.5:

            return {

                "action": "SELL",

                "confidence": _clip(abs(score))

            }

        return {

            "action": "HOLD",

            "confidence": 0.5

        }

    # --------------------------------------------------------
    # fallback
    # --------------------------------------------------------

    def _fallback(self):

        return {

            "action": "HOLD",

            "confidence": 0.0

        }


# ============================================================
# Singleton
# ============================================================

_agent = None


def get_rl_execution_agent():

    global _agent

    if _agent is None:

        _agent = RLExecutionAgent()

    return _agent