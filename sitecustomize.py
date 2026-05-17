# ============================================================
# File   : sitecustomize.py
# Version: Ver01-AUTO-AUDIT-BOOTSTRAP
# ------------------------------------------------------------
# Python 起動時に自動 import されるフック。
# main.py を直接壊さず、監査ログ/バックテスト用DB保存を自動有効化する。
#
# 保存開始対象:
#   - entry_controller 監査
#   - buy_sell_entry 発注監査
#   - audit DB schema 作成
#
# 注意:
#   - sitecustomize.py は Python の site 初期化時に自動で読み込まれる。
#   - 監査ログ初期化に失敗しても本体起動は止めない。
#   - 無効化したい場合は環境変数 DISABLE_AUDIT_LOGGING=1 を設定する。
# ============================================================

from __future__ import annotations

import os
import sys
import logging


def _install_audit_logging_safely() -> None:
    try:
        if os.environ.get('DISABLE_AUDIT_LOGGING', '').strip() == '1':
            return

        root = os.path.dirname(os.path.abspath(__file__))
        if root and root not in sys.path:
            sys.path.insert(0, root)

        from trading.audit_logging.install_audit_logging import install_audit_logging

        ok = install_audit_logging()
        logging.getLogger(__name__).warning(
            '[SITECUSTOMIZE] audit logging auto install ok=%s',
            ok,
        )

    except Exception:
        # ここで例外を外に出すと Python 起動自体に影響するため握りつぶす。
        try:
            logging.getLogger(__name__).exception(
                '[SITECUSTOMIZE] audit logging auto install failed'
            )
        except Exception:
            pass


_install_audit_logging_safely()
