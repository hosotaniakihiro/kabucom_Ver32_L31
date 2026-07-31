# ============================================================
# File   : core/startup/entry_pipeline_pending_root_prefilter_patch.py
# Version: V2-FULLY-INLINED
# ------------------------------------------------------------
# V2: pipeline_source/interval 不一致の pending をスキャン前に間引く最適化は
#     trading/handlers/entry_controller.py の run_entry_pipeline 本体 (Ver2.8、
#     get_bucket() 直後の軽量フィルタ) へ統合済みのため、このファイルは撤去済み。
#     entry_controller_pipeline_bucket_filter_patch.py と同じ問題を別方式
#     (pending_entries全体の一時差し替え) で解決していた重複実装だった。
# ============================================================

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_PATCHED = True


def install() -> bool:
    return True


__all__ = ["install"]
