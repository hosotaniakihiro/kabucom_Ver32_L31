# ============================================================
# File   : sitecustomize.py
# Version: Ver03-AUTO-AUDIT-AND-SUMMARY-MTF-CATCHUP
# ------------------------------------------------------------
# Python 起動時に自動 import されるフック。
# main.py を直接壊さず、監査ログ/バックテスト用DB保存を自動有効化する。
# 追加で、3分足/5分足サマリーを1分足DBから起動時に差分補完する。
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


def _install_summary_mtf_catchup_safely() -> None:
    """3分足/5分足サマリーを1分足DBから起動時に差分補完する。"""
    try:
        if os.environ.get('DISABLE_SUMMARY_MTF_CATCHUP', '').strip() == '1':
            _write_boot_evidence('SUMMARY_MTF_CATCHUP_DISABLED_BY_ENV')
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
_install_audit_logging_safely()
_install_summary_mtf_catchup_safely()
_write_boot_evidence('SITECUSTOMIZE_DONE')