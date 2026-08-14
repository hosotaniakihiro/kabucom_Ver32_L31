# ============================================================
# File   : trading/ranking/__init__.py
# Version: PRODUCTION-STABLE-RANKING-INIT-V1-LOCK-PATCH
# ------------------------------------------------------------
# Purpose:
#   trading.ranking package 初期化。
#   ranking DB writer の SQLite locked 対策パッチを自動適用する。
# ============================================================

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# ranking_db_writer_lock_patch は trading/ranking/ranking_db_writer.py 本体へ
# 本文化した (_rdl_*)。RankingDBWriter.flush の差し替えも同ファイル末尾で
# import 時に行うため、ここでの install は不要になった。
# 合成順 (lock <- relief(_rwr_flush) <- _flush_core) は保持している。


__all__ = []
