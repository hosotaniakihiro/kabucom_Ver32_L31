# ============================================================
# ignition package initializer（Ver6.0）
# ------------------------------------------------------------
# 各種点火ロジックへの統一インターフェイス
# entry_controller はここから必要な関数を import するだけでよい。
# ============================================================

from .volume_spike import calc_volume_features
from .three_stage import (
    detect_three_stage_buy,
    detect_three_stage_sell
)
from .five_sec import analyze_five_sec
from .vwap_break import (
    is_vwap_break_buy,
    is_vwap_break_sell
)
from .ai_boost import ai_predict_up

__all__ = [
    "calc_volume_features",
    "detect_three_stage_buy",
    "detect_three_stage_sell",
    "analyze_five_sec",
    "is_vwap_break_buy",
    "is_vwap_break_sell",
    "ai_predict_up",
]
