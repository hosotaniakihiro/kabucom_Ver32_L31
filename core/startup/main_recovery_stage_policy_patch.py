# ============================================================
# File   : core/startup/main_recovery_stage_policy_patch.py
# Version: V1-MAIN-RECOVERY-STAGE-POLICY
# ------------------------------------------------------------
# main.py の段階復帰を環境変数で制御する。
#
#   safe       : entry/exit/summary発火を止める
#   entry_only : ranking_entryだけ復帰。exit/tonosama/summaryAIは止める
#   entry_exit : ranking_entry + exit_loop_5sを復帰。tonosama/summaryAIは止める
#   full       : 全復帰
#
# 既定は entry_only。
# ============================================================
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)
_DONE = False


def _is_main_py() -> bool:
    try:
        return Path(str(sys.argv[0] or '')).name.lower() == 'main.py'
    except Exception:
        return False


def _stage() -> str:
    raw = str(os.getenv('AUTOSTOCK_MAIN_RECOVERY_STAGE', 'entry_only') or 'entry_only').strip().lower().replace('-', '_')
    aliases = {'0': 'safe', '1': 'entry_only', 'entry': 'entry_only', 'entryonly': 'entry_only', '2': 'entry_exit', 'exit': 'entry_exit', 'entryexit': 'entry_exit', '3': 'full', 'all': 'full'}
    raw = aliases.get(raw, raw)
    if raw not in {'safe', 'entry_only', 'entry_exit', 'full'}:
        raw = 'entry_only'
    os.environ['AUTOSTOCK_MAIN_RECOVERY_STAGE'] = raw
    return raw


def install() -> bool:
    global _DONE
    if not _is_main_py():
        logger.warning('[MAIN RECOVERY STAGE] skipped not main.py argv=%s', sys.argv[:1])
        return False

    st = _stage()

    # 共通: main.pyからDB owner処理はまだ実行しない
    os.environ.setdefault('AUTOSTOCK_MAIN_SKIP_PUSH_DB_RESTORE', '1')
    os.environ.setdefault('AUTOSTOCK_MAIN_SKIP_PUSH_STACK', '1')
    os.environ.setdefault('AUTOSTOCK_MAIN_SKIP_PUSH_SUMMARY_FALLBACK', '1')
    os.environ.setdefault('AUTOSTOCK_MAIN_SKIP_RANKING_SUMMARY_BOOTSTRAP', '1')
    os.environ.setdefault('AUTOSTOCK_MAIN_SKIP_SUMMARY_PUSH_BG', '1')
    os.environ.setdefault('AUTOSTOCK_MAIN_SKIP_RANKING_SUMMARY_SCHEDULE', '1')
    os.environ.setdefault('AUTOSTOCK_MAIN_SKIP_SUMMARY_PARENT_TICK', '1')
    os.environ.setdefault('AUTOSTOCK_MAIN_SKIP_RANKING_WAL_GUARD', '1')
    os.environ.setdefault('AUTOSTOCK_MAIN_SCHEDULE_DUE_FILTER', '1')

    if st == 'safe':
        os.environ['AUTOSTOCK_MAIN_DISABLE_SCHEDULED_ENTRY_JOBS'] = '1'
        os.environ['AUTOSTOCK_MAIN_DISABLE_SCHEDULED_RANKING_ENTRY'] = '1'
        os.environ['AUTOSTOCK_MAIN_SKIP_RANKING_ENTRY'] = '1'
        os.environ['AUTOSTOCK_MAIN_SKIP_TONOSAMA_ENTRY'] = '1'
        os.environ['AUTOSTOCK_MAIN_DISABLE_SCHEDULED_EXIT_LOOP'] = '1'
        os.environ['SUMMARY_AI_ASYNC_ENTRY_DIRECT_SYNC'] = '0'
        os.environ['SUMMARY_AI_DIRECT_DISPATCH_ON_QUEUED_ASYNC'] = '0'
    elif st == 'entry_only':
        # ranking_entryだけ許可。tonosama/exit/summaryAIは止める。
        os.environ['AUTOSTOCK_MAIN_DISABLE_SCHEDULED_ENTRY_JOBS'] = '0'
        os.environ['AUTOSTOCK_MAIN_DISABLE_SCHEDULED_RANKING_ENTRY'] = '0'
        os.environ['AUTOSTOCK_MAIN_SKIP_RANKING_ENTRY'] = '0'
        os.environ['AUTOSTOCK_MAIN_SKIP_TONOSAMA_ENTRY'] = '1'
        os.environ['AUTOSTOCK_MAIN_DISABLE_SCHEDULED_EXIT_LOOP'] = '1'
        os.environ['SUMMARY_AI_ASYNC_ENTRY_DIRECT_SYNC'] = '0'
        os.environ['SUMMARY_AI_DIRECT_DISPATCH_ON_QUEUED_ASYNC'] = '0'
    elif st == 'entry_exit':
        # ranking_entry + exit_loop_5sを許可。tonosama/summaryAIはまだ止める。
        os.environ['AUTOSTOCK_MAIN_DISABLE_SCHEDULED_ENTRY_JOBS'] = '0'
        os.environ['AUTOSTOCK_MAIN_DISABLE_SCHEDULED_RANKING_ENTRY'] = '0'
        os.environ['AUTOSTOCK_MAIN_SKIP_RANKING_ENTRY'] = '0'
        os.environ['AUTOSTOCK_MAIN_SKIP_TONOSAMA_ENTRY'] = '1'
        os.environ['AUTOSTOCK_MAIN_DISABLE_SCHEDULED_EXIT_LOOP'] = '0'
        os.environ['SUMMARY_AI_ASYNC_ENTRY_DIRECT_SYNC'] = '0'
        os.environ['SUMMARY_AI_DIRECT_DISPATCH_ON_QUEUED_ASYNC'] = '0'
    else:
        os.environ['AUTOSTOCK_MAIN_DISABLE_SCHEDULED_ENTRY_JOBS'] = '0'
        os.environ['AUTOSTOCK_MAIN_DISABLE_SCHEDULED_RANKING_ENTRY'] = '0'
        os.environ['AUTOSTOCK_MAIN_SKIP_RANKING_ENTRY'] = '0'
        os.environ['AUTOSTOCK_MAIN_SKIP_TONOSAMA_ENTRY'] = '0'
        os.environ['AUTOSTOCK_MAIN_DISABLE_SCHEDULED_EXIT_LOOP'] = '0'
        os.environ.setdefault('SUMMARY_AI_ASYNC_ENTRY_DIRECT_SYNC', '1')
        os.environ.setdefault('SUMMARY_AI_DIRECT_DISPATCH_ON_QUEUED_ASYNC', '1')

    _DONE = True
    logger.warning('[MAIN RECOVERY STAGE] installed stage=%s entry_disabled=%s ranking_skip=%s tonosama_skip=%s exit_disabled=%s summary_ai_direct=%s/%s', st, os.environ.get('AUTOSTOCK_MAIN_DISABLE_SCHEDULED_ENTRY_JOBS'), os.environ.get('AUTOSTOCK_MAIN_SKIP_RANKING_ENTRY'), os.environ.get('AUTOSTOCK_MAIN_SKIP_TONOSAMA_ENTRY'), os.environ.get('AUTOSTOCK_MAIN_DISABLE_SCHEDULED_EXIT_LOOP'), os.environ.get('SUMMARY_AI_ASYNC_ENTRY_DIRECT_SYNC'), os.environ.get('SUMMARY_AI_DIRECT_DISPATCH_ON_QUEUED_ASYNC'))
    return True

try:
    install()
except Exception:
    logger.exception('[MAIN RECOVERY STAGE] auto install failed')

__all__ = ['install']
