# ============================================================
# File   : core/global_context/__init__.py
# Version: V32-FINAL-GLOBAL-CONTEXT-PACKAGE
# ------------------------------------------------------------
# ✔ GlobalContext singleton export
# ✔ 各Stateを名前付きで公開
# ✔ 旧global_data互換エイリアスを提供（任意）
# ============================================================

from __future__ import annotations

from .context import GlobalContext, global_context

from .push_state import PushState
from .summary_state import SummaryState
from .ranking_state import RankingState
from .position_state import PositionState
from .regime_state import RegimeState
from .bandit_state import BanditState
from .ai_state import AIState
from .monitor_state import MonitorState

# ------------------------------------------------------------
# 旧コード互換（必要なら使用）
# 例: from core.global_context import global_data
# ------------------------------------------------------------
global_data = global_context