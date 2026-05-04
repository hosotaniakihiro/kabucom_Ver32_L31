from __future__ import annotations
import logging
logger = logging.getLogger(__name__)

def yahoo_minutely_complement_job():
    try:
        from trading.yahoo.complement.download_flow import run_periodic_yahoo_complement
        return run_periodic_yahoo_complement()
    except Exception:
        logger.exception("❌ Yahoo差分補完（定期）失敗（runtime 継続）")
        return None

def run_yahoo_complement_once():
    try:
        from trading.yahoo.complement.download_flow import run_startup_yahoo_complement
        return run_startup_yahoo_complement()
    except Exception:
        logger.exception("❌ Yahoo補完（起動時）失敗 → 起動継続")
        return True
__all__ = ["yahoo_minutely_complement_job", "run_yahoo_complement_once"]
