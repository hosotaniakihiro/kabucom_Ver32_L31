# ============================================================
# File   : core/startup/__init__.py
# Ver    : PRODUCTION-STABLE-REV20.0-STARTUP-PACKAGE
# ------------------------------------------------------------
# 【概要】
#   core.startup パッケージの公開入口
#
# 【主な機能】
#   - system_startup の公開
#   - bootstrap_summary の公開
#   - 必要最小限の startup API を再エクスポート
#
# 【設計方針】
#   - 本ファイルは薄い re-export のみ
#   - 実ロジックは各モジュールへ分離
#   - import パスの安定性を優先
# ============================================================

from __future__ import annotations

from .startup import system_startup
from .summary_bootstrap import (
    bootstrap_summary,
    run_bootstrap_incremental_rebuild_if_available,
)

__all__ = [
    "system_startup",
    "bootstrap_summary",
    "run_bootstrap_incremental_rebuild_if_available",
]

