# ============================================================
# File   : sitecustomize.py
# Version: Ver07-SUMMARY-AI-LIQ-RESCUE-AUTO-INSTALL
# ------------------------------------------------------------
# Python 起動時に自動 import されるフック。
# main.py を直接壊さず、監査ログ/バックテスト用DB保存を自動有効化する。
#
# Ver07:
#   - SUMMARY AI の直前流動性チェックで全AI_OK候補が落ちる場合の救済patchを自動install
#
# Ver06:
#   - TONOSAMA 出来高急増履歴不足時の fail-open 既定値を Python 起動直後に明示
#   - allow_without_history=False に倒れて base feature empty で全落ちする問題を防止
#
# Ver05:
#   - summary_controller.concat_frames の重複カラム対策 patch を自動install
#   - pandas InvalidIndexError: Reindexing only valid with uniquely valued Index objects を防止
#
# Ver04:
#   - main.py では SUMMARY MTF CATCHUP の自動起動を既定スキップ
#   - main_database.py / data collectors 側でDB補完を担当
#   - main.pyで強制実行したい場合のみ SUMMARY_MTF_CATCHUP_RUN_IN_MAIN=1
#
# 注意:
#   - sitecustomize.py は Python の site 初期化時に自動で読み込まれる。
#   - ここで失敗しても本体起動は止めない。
#   - 無効化: DISABLE_AUDIT_LOGGING=1 / DISABLE_SUMMARY_MTF_CATCHUP=1
# ============================================================

from __future__ import annotations

import os
import sys
import logging
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

BOOT_EVIDENCE_DIR = Path(r"\\192.168.0.22\AutoStockBuyAndSell\Logs\boot_evidence")


def _boot_today() -> str:
    return datetime.now().strftime('%Y%m%d')


def _boot_evidence_path() -> Path:
    try:
        BOOT_EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return BOOT_EVIDENCE_DIR / f'boot_evidence_{_boot_today()}.log'


def _write_boot_evidence(event: str, detail: Any = None) -> None:
    """logging 初期化前でも残る起動証跡。失敗しても起動は止めない。"""
    try:
        now = datetime.now().isoformat(timespec='seconds')
        pid = os.getpid()
        cwd = os.getcwd()
        argv = ' '.join(str(x) for x in sys.argv)
        line = f'[{now}] pid={pid} event={event} cwd={cwd} argv={argv} detail={detail}\n'
        path = _boot_evidence_path()
        with open(path, 'a', encoding='utf-8') as f:
            f.write(line)
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                pass
    except Exception:
        pass


def _ensure_project_root() -> str:
    try:
        root = os.path.dirname(os.path.abspath(__file__))
        if root and root not in sys.path:
            sys.path.insert(0, root)
        return root
    except Exception:
        return ''


def _env_on(name: str, default: bool = False) -> bool:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == '':
            return bool(default)
        return str(v).strip().lower() in {'1', 'true', 'yes', 'y', 'on', 'ok', 'enable', 'enabled'}
    except Exception:
        return bool(default)


def _is_main_py_process() -> bool:
    try:
        argv = [str(x).replace('\\', '/').lower() for x in sys.argv]
        return any(x.endswith('/main.py') or x == 'main.py' for x in argv)
    except Exception:
        return False


def _is_database_process() -> bool:
    return any(
        _env_on(name, False)
        for name in (
            'AUTOSTOCK_DATA_COLLECTORS_PROCESS',
            'AUTOSTOCK_SUMMARY_DB_WRITER',
            'AUTOSTOCK_MAIN_DATABASE_PROCESS',
        )
    )


def _install_boot_exception_hook() -> None:
    try:
        old_hook = sys.excepthook

        def _hook(exc_type, exc, tb):
            try:
                text = ''.join(traceback.format_exception(exc_type, exc, tb))
                _write_boot_evidence('UNCAUGHT_EXCEPTION', text)
            except Exception:
                pass
            try:
                old_hook(exc_type, exc, tb)
            except Exception:
                pass

        sys.excepthook = _hook
        _write_boot_evidence('BOOT_EXCEPTION_HOOK_INSTALLED')
    except Exception:
        pass


def _install_tonosama_surge_defaults() -> None:
    """TONOSAMA ENTRY が履歴不足だけで base feature empty にならないよう既定値を明示する。"""
    try:
        os.environ.setdefault('TONOSAMA_VOLUME_SURGE_FAILOPEN_IF_HISTORY_MISSING', '1')
        os.environ.setdefault('TONOSAMA_ALLOW_ENTRY_WITHOUT_SURGE_HISTORY', '1')
        os.environ.setdefault('TONOSAMA_VOLUME_SURGE_FAILOPEN_VALUE', '3.0')
        _write_boot_evidence('TONOSAMA_SURGE_DEFAULTS_SET', {
            'failopen': os.environ.get('TONOSAMA_VOLUME_SURGE_FAILOPEN_IF_HISTORY_MISSING'),
            'allow_without_history': os.environ.get('TONOSAMA_ALLOW_ENTRY_WITHOUT_SURGE_HISTORY'),
            'failopen_value': os.environ.get('TONOSAMA_VOLUME_SURGE_FAILOPEN_VALUE'),
        })
        try:
            logging.getLogger(__name__).warning(
                '[SITECUSTOMIZE] tonosama surge defaults failopen=%s allow_without_history=%s value=%s',
                os.environ.get('TONOSAMA_VOLUME_SURGE_FAILOPEN_IF_HISTORY_MISSING'),
                os.environ.get('TONOSAMA_ALLOW_ENTRY_WITHOUT_SURGE_HISTORY'),
                os.environ.get('TONOSAMA_VOLUME_SURGE_FAILOPEN_VALUE'),
            )
        except Exception:
            pass
    except Exception:
        _write_boot_evidence('TONOSAMA_SURGE_DEFAULTS_EXCEPTION', traceback.format_exc())


def _install_summary_ai_liq_rescue_safely() -> None:
    """SUMMARY AI のAI_OK候補が直前流動性判定だけで全落ちする場合の救済patch。"""
    try:
        if os.environ.get('DISABLE_SUMMARY_AI_LIQ_RESCUE_PATCH', '').strip() == '1':
            _write_boot_evidence('SUMMARY_AI_LIQ_RESCUE_DISABLED_BY_ENV')
            return

        _ensure_project_root()
        _write_boot_evidence('SUMMARY_AI_LIQ_RESCUE_INSTALL_START')

        from core.startup.summary_ai_liquidity_rescue_patch import install

        ok = install()
        _write_boot_evidence('SUMMARY_AI_LIQ_RESCUE_INSTALL_DONE', {'ok': ok})
        logging.getLogger(__name__).warning('[SITECUSTOMIZE] summary ai liq rescue auto install ok=%s', ok)
    except Exception:
        text = traceback.format_exc()
        _write_boot_evidence('SUMMARY_AI_LIQ_RESCUE_EXCEPTION', text)
        try:
            logging.getLogger(__name__).exception('[SITECUSTOMIZE] summary ai liq rescue auto install failed')
        except Exception:
            pass


def _install_audit_logging_safely() -> None:
    try:
        if os.environ.get('DISABLE_AUDIT_LOGGING', '').strip() == '1':
            _write_boot_evidence('AUDIT_LOGGING_DISABLED_BY_ENV')
            return

        _ensure_project_root()
        _write_boot_evidence('AUDIT_INSTALL_START')

        from trading.audit_logging.install_audit_logging import install_audit_logging

        ok = install_audit_logging()
        _write_boot_evidence('AUDIT_INSTALL_DONE', {'ok': ok})
        logging.getLogger(__name__).warning('[SITECUSTOMIZE] audit logging auto install ok=%s', ok)

    except Exception:
        text = traceback.format_exc()
        _write_boot_evidence('AUDIT_INSTALL_EXCEPTION', text)
        try:
            logging.getLogger(__name__).exception('[SITECUSTOMIZE] audit logging auto install failed')
        except Exception:
            pass


def _install_summary_controller_dupcol_patch_safely() -> None:
    """summary_controller の concat 前後で重複カラムを潰す runtime patch。"""
    try:
        if os.environ.get('DISABLE_SUMMARY_CONTROLLER_DUPCOL_PATCH', '').strip() == '1':
            _write_boot_evidence('SUMMARY_CONTROLLER_DUPCOL_PATCH_DISABLED_BY_ENV')
            return

        _ensure_project_root()
        _write_boot_evidence('SUMMARY_CONTROLLER_DUPCOL_PATCH_INSTALL_START')

        from core.startup.summary_controller_concat_duplicate_columns_patch import install

        ok = install()
        _write_boot_evidence('SUMMARY_CONTROLLER_DUPCOL_PATCH_INSTALL_DONE', {'ok': ok})
        logging.getLogger(__name__).warning('[SITECUSTOMIZE] summary controller dupcol patch auto install ok=%s', ok)

    except Exception:
        text = traceback.format_exc()
        _write_boot_evidence('SUMMARY_CONTROLLER_DUPCOL_PATCH_EXCEPTION', text)
        try:
            logging.getLogger(__name__).exception('[SITECUSTOMIZE] summary controller dupcol patch auto install failed')
        except Exception:
            pass


def _install_summary_mtf_catchup_safely() -> None:
    """3分足/5分足サマリーを1分足DBから起動時に差分補完する。"""
    try:
        if os.environ.get('DISABLE_SUMMARY_MTF_CATCHUP', '').strip() == '1':
            _write_boot_evidence('SUMMARY_MTF_CATCHUP_DISABLED_BY_ENV')
            return

        if _is_main_py_process() and not _is_database_process() and not _env_on('SUMMARY_MTF_CATCHUP_RUN_IN_MAIN', False):
            _write_boot_evidence('SUMMARY_MTF_CATCHUP_SKIPPED_IN_MAIN_PROCESS')
            try:
                logging.getLogger(__name__).warning(
                    '[SITECUSTOMIZE] summary mtf catchup skipped in main.py; main_database.py handles DB catchup'
                )
            except Exception:
                pass
            return

        _ensure_project_root()

        os.environ.setdefault('SUMMARY_MTF_STARTUP_CATCHUP_ENABLED', '1')
        os.environ.setdefault('SUMMARY_MTF_CATCHUP_INTERVALS', '3,5')
        os.environ.setdefault('SUMMARY_MTF_CATCHUP_MA_BARS', '75')
        os.environ.setdefault('SUMMARY_MTF_CATCHUP_EXTRA_MINUTES', '30')
        os.environ.setdefault('SUMMARY_MTF_CATCHUP_INCLUDE_CURRENT_PARTIAL', '1')
        os.environ.setdefault('SUMMARY_MTF_CATCHUP_MAX_ROWS', '80000')
        os.environ.setdefault('SUMMARY_MTF_CATCHUP_RUN_ASYNC', '1')

        _write_boot_evidence('SUMMARY_MTF_CATCHUP_INSTALL_START')

        from core.startup.summary_multiframe_startup_catchup_patch import install

        ok = install()
        _write_boot_evidence('SUMMARY_MTF_CATCHUP_INSTALL_DONE', {'ok': ok})
        logging.getLogger(__name__).warning('[SITECUSTOMIZE] summary mtf catchup auto install ok=%s', ok)

    except Exception:
        text = traceback.format_exc()
        _write_boot_evidence('SUMMARY_MTF_CATCHUP_EXCEPTION', text)
        try:
            logging.getLogger(__name__).exception('[SITECUSTOMIZE] summary mtf catchup auto install failed')
        except Exception:
            pass


_write_boot_evidence('PYTHON_START')
_install_boot_exception_hook()
_install_tonosama_surge_defaults()
_install_summary_ai_liq_rescue_safely()
_install_audit_logging_safely()
_install_summary_controller_dupcol_patch_safely()
_install_summary_mtf_catchup_safely()
_write_boot_evidence('SITECUSTOMIZE_DONE')
