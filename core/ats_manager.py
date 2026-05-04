# core/ats_manager.py
import threading
import logging
from ats import ats_register_loop
from global_state import global_data

logger = logging.getLogger(__name__)


def start_ats_loop():
    token = global_data.token_value
    threading.Thread(
        target=ats_register_loop,
        args=(token, 10),
        daemon=True
    ).start()
    logger.info("ATS loop started")
