# ============================================================
# File   : trading/summary/recovery/loaders_push.py
# Ver    : PRODUCTION-STABLE-REV4.0-LOADERS-PUSH-COMPAT-SHIM
# ------------------------------------------------------------
# 【概要】
#   PUSH DB / runtime push dataframe loader の互換 shim
#
# 【目的】
#   - 旧 import パスを維持する
#       from trading.summary.recovery.loaders_push import ...
#
#   - 実体は以下へ分割
#       trading.summary.recovery.loaders_push_pkg.*
#
# 【重要】
#   - このファイルは削除しない
#   - 既存コードの import を壊さないための re-export 専用
#   - 実ロジックは loaders_push_pkg 側に置く
# ============================================================

from __future__ import annotations

from .loaders_push_pkg import *  # noqa: F401,F403