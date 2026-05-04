# ============================================================
# sync_executions.py（Ver23-NO-EXECUTION-API）
# ------------------------------------------------------------
# ・/executions API は 404 のため完全無効化
# ・代わりに、/positions API だけで建玉状態を同期する
# ・main.py から呼び出しても安全（何もしない）
# ============================================================

import logging
logger = logging.getLogger(__name__)

#============================================================
# ダミー関数：main.py はこの関数の存在だけを必要とする
#============================================================

def start_execution_sync_loop(interval_sec=1):
    """
    /executions API は利用しないため、この関数は何もしない。
    呼び出しは安全で、例外も発生しない。
    """
    logger.info("⚪ /executions API はOFFのため、ExecutionSyncLoopは起動しません。")
    return

