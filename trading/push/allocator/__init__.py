# ============================================================
# File   : trading/push/allocator/__init__.py
# ------------------------------------------------------------
# PUSH スロット割り当てパッケージの公開API。
#
# 2026-08-19: このファイルは実体と合っておらず、import した時点で
#   ImportError: cannot import name 'allocate_push_slots'
# になる状態だった (4 名前すべてが存在しない)。
#   allocate_push_slots               -> PushSlotAllocator.allocate へ移行済み
#   build_candidate_df_from_summary   -> 存在しない
#   get_symbols_to_register_and_unregister -> 存在しない
#   PushSlotState                     -> AllocatorState へ改名済み
# 実際に存在するものへ揃えた。
#
# なお旧実装の trading/push/push_slot_allocator.py は、ヘルパ 14 個が
# どこにも存在せず呼べば必ず落ちる状態で放置されていたため削除した
# (import 参照はゼロだった)。
# ============================================================

from trading.push.allocator.core.allocator import PushSlotAllocator, push_slot_allocator
from trading.push.allocator.core.convenience import run_allocator
from trading.push.allocator.state.state import AllocatorState

__all__ = [
    "PushSlotAllocator",
    "push_slot_allocator",
    "run_allocator",
    "AllocatorState",
]
