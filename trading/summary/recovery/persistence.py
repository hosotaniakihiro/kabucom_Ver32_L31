# ============================================================
# File   : trading/summary/recovery/persistence.py
# Ver    : PRODUCTION-STABLE-REV9.0-COMPAT-SHIM
# ------------------------------------------------------------
# 【概要】
#   旧 import パス互換 shim
#
# 【目的】
#   既存コード:
#       from trading.summary.recovery.persistence import ...
#   を壊さず、新パッケージ:
#       trading.summary.recovery.persistence_pkg
#   へ委譲する。
#
# 【公開API】
#   - finalize_for_upsert
#   - upsert_summary_df
#   - update_global_cache
# ============================================================

from __future__ import annotations

from trading.summary.recovery.persistence_pkg import *  # noqa: F401,F403