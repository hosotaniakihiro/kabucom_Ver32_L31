# ============================================================
# monitor_symbol_manager.py
# 場中 監視銘柄 動的制御
# Ver26.2-FINAL-PENDING-MANAGER-SAFE-GUARDEDSET-STRICT
# ------------------------------------------------------------
# ✔ pending_manager 完全準拠
# ✔ dict / list 混入事故ゼロ
# ✔ ACTIVE 制御ロジック維持
# ✔ symbols_active overwrite 完全防止
# ✔ ★ GuardedSet 型を厳密保証
# ============================================================

import logging
from typing import Dict, Set

from global_state import global_data, GuardedSet
from trading.entry.pending_manager import get_bucket

logger = logging.getLogger(__name__)

MAX_ACTIVE = 50


def update_active_symbols(candidate_scores: Dict[str, float]):
    """
    candidate_scores:
        symbol -> score（大きいほど重要）
    """

    # ----------------------------------
    # 0️⃣ 型ガード
    # ----------------------------------
    if not isinstance(candidate_scores, dict):
        logger.error(
            "🔥 candidate_scores invalid type: %s",
            type(candidate_scores),
        )
        return

    # ----------------------------------
    # ① 必須銘柄（絶対外さない）
    # ----------------------------------
    mandatory: Set[str] = set()

    # 建玉あり（dict 前提）
    open_positions = getattr(global_data, "open_positions", {})
    if isinstance(open_positions, dict):
        for sym in open_positions.keys():
            if sym and str(sym).isdigit():
                mandatory.add(str(sym))
    else:
        logger.error(
            "🔥 open_positions invalid type: %s",
            type(open_positions),
        )

    # pending あり（pending_manager 正本）
    pending = getattr(global_data, "pending_entries", {})
    if isinstance(pending, dict):
        for sym in pending.keys():
            bucket = get_bucket(sym)
            if bucket:
                mandatory.add(str(sym))

    # ----------------------------------
    # ② スコア順で並べる
    # ----------------------------------
    sorted_syms = sorted(
        candidate_scores.items(),
        key=lambda x: x[1],
        reverse=True,
    )

    # ----------------------------------
    # ③ ACTIVE 決定
    # ----------------------------------
    new_active: Set[str] = set(mandatory)

    for sym, _ in sorted_syms:
        if len(new_active) >= MAX_ACTIVE:
            break
        if sym and str(sym).isdigit():
            new_active.add(str(sym))

    # ----------------------------------
    # ④ 差分更新（GuardedSet 厳密準拠）
    # ----------------------------------
    current_active = getattr(global_data, "symbols_active", None)

    if not isinstance(current_active, GuardedSet):
        logger.critical(
            "🔥 symbols_active invalid type (must be GuardedSet): %s",
            type(current_active),
        )
        return

    to_add = new_active - current_active
    to_remove = current_active - new_active

    if to_add or to_remove:
        logger.info(
            "[ACTIVE UPDATE] +%d -%d",
            len(to_add),
            len(to_remove),
        )

    # ★ overwrite 禁止：clear + update のみ
    current_active.clear()
    current_active.update(new_active)
