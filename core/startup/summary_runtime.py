# ============================================================
# File   : core/startup/summary_runtime.py
# Version: REV3.0-COMPAT-SHIM-TO-SUMMARY-RUNTIME-PKG
# ------------------------------------------------------------
# 【概要】
#   旧 import パス互換 shim
#
# 【目的】
#   既存コード:
#       from core.startup.summary_runtime import ...
#   を壊さず、新パッケージ:
#       core.startup.summary_runtime_pkg
#   へ委譲する。
#
# 【重要】
#   core/startup/summary_runtime.py と
#   core/startup/summary_runtime/ ディレクトリは同時に置けないため、
#   分割先は summary_runtime_pkg とする。
# ============================================================

from __future__ import annotations

from core.startup.summary_runtime_pkg import *  # noqa: F401,F403