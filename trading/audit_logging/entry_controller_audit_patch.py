# ============================================================
# File   : trading/audit_logging/entry_controller_audit_patch.py
# Version: Ver03-ENTRY-CONTROLLER-AUDIT-FULLY-INLINED
# ------------------------------------------------------------
# entry_controller に監査ログを後付けするための安全パッチ。
#
# Ver03: candidate_history / order_history への監査ログ記録は
#     trading/handlers/entry_controller.py の _audit_candidate_ok_safe /
#     _audit_order_safe (Ver2.9、_execute_best_candidate_dispatch から呼ばれる) へ
#     インライン化済みのため撤去した。
#
# ENTRY_SKIP の監査ログ記録 (旧 patched_log_skip) は
# trading/handlers/entry_controller.py の _log_skip 本体 (Ver2.7) へ
# audit_entry_skip 呼び出しとしてインライン化済み。
# ============================================================

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_INSTALLED = True


def install() -> bool:
    return True
