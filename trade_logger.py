# ============================================================
# trade_logger.py
# compatibility logger
# ============================================================

import logging

logger = logging.getLogger(__name__)


def log_trade_to_sqlite(*args, **kwargs):
    """
    旧システム互換用
    実際のDB保存ロジックが無い場合でも
    システムを落とさない
    """
    try:
        logger.info("[TRADE LOGGER] log skipped (compatibility mode)")
    except Exception:
        pass