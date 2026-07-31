# ============================================================
# File   : core/startup/global_context_summary_repair_patch.py
# Version: V2-INLINED-INTO-GLOBAL-CONTEXT
# ------------------------------------------------------------
# V2:
#   - set_merged_summary の技術指標(macd/signal/mtf)履歴補完は
#     core/global_context/context.py の set_merged_summary 本体 (REV11) の
#     _repair_merged_summary_from_history へインライン化済み。
#   - get_merged_summary の「古いpush行を除外」フィルタは、既存の
#     drop_stale_summary_rows (REV9、set/get 双方に既に適用済み) がより
#     厳密な基準で同じ役割を果たしていたため、重複として撤去した。
#   - この2つの機能はどちらも、GlobalContext のシングルトンinstance属性へ
#     MethodType 経由で差し替える方式だった。同時期に
#     core/startup/summary_main_push_db_refresh_patch.py が
#     GlobalContext の class属性へ set_merged_summary を差し替えており、
#     instance属性がclass属性を覆い隠すPythonの解決順序のため、
#     どちらが後にインストールされるかで一方が恒久的に無効化される
#     起動順序依存の不具合があった。両方を本体へ統合したことで解消。
#
# V1.2 (旧):
#   - set_merged_summary だけでなく get_merged_summary もラップ
#   - source=push または source未指定fallbackで返る古い completed summary を除外
# V1.1 (旧):
#   - source=push の merged 保存前に、古い日付の行を除外
# ============================================================

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_PATCHED = True


def install() -> bool:
    return True


__all__ = ["install"]
