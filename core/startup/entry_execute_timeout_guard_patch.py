# ============================================================
# File   : core/startup/entry_execute_timeout_guard_patch.py
# Version: V7-FULLY-INLINED
# ------------------------------------------------------------
# V7: 経過候補ガード (stale skip) + タイムアウト付き実行 (別スレッド + queue) は
#     trading/handlers/entry_controller.py の _execute_best_candidate 本体
#     (Ver2.9、_execute_best_candidate_core を timeout 付きで呼ぶ形) へ
#     インライン化済みのため撤去した。
#
#     このパッチが持っていた _BASE_EXECUTE_BEST_CANDIDATE ピン留め + 2秒毎の
#     perpetual watcher (自分を常に最外周へ再ラップし続ける仕組み) は、
#     他パッチとの間で不安定な奪い合いを起こしていた根本原因の一つだった。
#     _execute_best_candidate を単一の本体関数にしたことで、この仕組み自体が
#     不要になった。
# ============================================================
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

VERSION = "V7-FULLY-INLINED"
_PATCHED = True


def install() -> bool:
    return True


try:
    install()
except Exception:
    logger.exception("[ENTRY EXEC TIMEOUT GUARD] auto install failed")


__all__ = ["install", "VERSION"]
