# ============================================================
# File   : trading/entry/summary_ai/__init__.py
# Version: PRODUCTION-STABLE-REV1.1-SAFE-PACKAGE-INIT
# ------------------------------------------------------------
# 【概要】
#   SUMMARY / RANKING SUMMARY の BUY TOP10 を AI gate に確認し、
#   AI_OK 銘柄だけ既存 entry_pipeline へ渡すパッケージ。
#
# 【修正内容】
#   - 存在しない .db_path import を削除
#   - __init__.py で DB path 系APIを公開しない
#   - tonosama_bridge import 時に package initializer で落ちないようにする
#
# 【公開API】
#   run_summary_ai_entry_from_df
#   run_summary_ai_entry
#   run_push_summary_ai_entry
#   run_ranking_summary_ai_entry
#
# 【重要】
#   ranking DB path の解決は runner.py / tonosama_bridge.py 側で行う。
#   __init__.py では副作用のある import を増やさない。
# ============================================================

from __future__ import annotations

__version__ = "PRODUCTION-STABLE-REV1.1-SAFE-PACKAGE-INIT"

try:
    from .runner import (
        run_summary_ai_entry_from_df,
        run_summary_ai_entry,
        run_push_summary_ai_entry,
        run_ranking_summary_ai_entry,
    )
except Exception:
    # --------------------------------------------------------
    # __init__.py の import 失敗で package 全体を壊さないための保険。
    #
    # 注意:
    #   通常運用では runner import は成功する想定。
    #   ただし、tonosama_bridge 等を単体 import したときに
    #   runner 側の依存不整合で package import 全体が落ちるのを防ぐ。
    # --------------------------------------------------------
    run_summary_ai_entry_from_df = None
    run_summary_ai_entry = None
    run_push_summary_ai_entry = None
    run_ranking_summary_ai_entry = None


__all__ = [
    "__version__",
    "run_summary_ai_entry_from_df",
    "run_summary_ai_entry",
    "run_push_summary_ai_entry",
    "run_ranking_summary_ai_entry",
]