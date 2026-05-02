# ============================================================
# File   : trading/summary/recovery/mtf_history_bootstrap.py
# Version: PRODUCTION-STABLE-REV1.4-COMPAT-SHIM
# ------------------------------------------------------------
# 【概要】
#   mtf_history_bootstrap_pkg への互換 shim
#
# 【目的】
#   旧 import パスを維持する:
#
#     from trading.summary.recovery.mtf_history_bootstrap import run_mtf_history_bootstrap
#
# 【重要】
#   実装本体は以下へ分割:
#
#     trading.summary.recovery.mtf_history_bootstrap_pkg
#
# ============================================================

from __future__ import annotations

from trading.summary.recovery.mtf_history_bootstrap_pkg.runner import run_mtf_history_bootstrap
from trading.summary.recovery.mtf_history_bootstrap_pkg.loader import load_1m_summary_history
from trading.summary.recovery.mtf_history_bootstrap_pkg.resampler import rebuild_higher_tf_from_1m_history
from trading.summary.recovery.mtf_history_bootstrap_pkg.indicators_scoring import apply_indicators_scoring_ready
from trading.summary.recovery.mtf_history_bootstrap_pkg.datetime_guard import normalize_higher_tf_datetime

__all__ = [
    "load_1m_summary_history",
    "rebuild_higher_tf_from_1m_history",
    "apply_indicators_scoring_ready",
    "normalize_higher_tf_datetime",
    "run_mtf_history_bootstrap",
]