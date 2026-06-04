from __future__ import annotations
import logging
import os
import threading
import time
logger = logging.getLogger(__name__)
_INSTALLED = False

def _set(name: str, value: str) -> None:
    try:
        os.environ[name] = str(value)
    except Exception:
        pass

def _apply_once() -> bool:
    try:
        _set("RANKING_ENTRY_PREFILTER_MAX_RANK", os.getenv("RANKING_ENTRY_WIDER_MAX_RANK", "80"))
        _set("RANKING_ENTRY_PREFILTER_MAX_ROWS", os.getenv("RANKING_ENTRY_WIDER_MAX_ROWS", "600"))
        _set("RANKING_ENTRY_PREFILTER_MAX_PER_TYPE", os.getenv("RANKING_ENTRY_WIDER_MAX_PER_TYPE", "90"))
        _set("RANKING_ENTRY_PREFILTER_MAX_PER_SIDE", os.getenv("RANKING_ENTRY_WIDER_MAX_PER_SIDE", "320"))
        _set("RANKING_ENTRY_FAST_MAX_PENDING_PER_RUN", os.getenv("RANKING_ENTRY_WIDER_MAX_PENDING_PER_RUN", "4"))
        _set("RANKING_ENTRY_MAX_PENDING_PER_RUN", os.getenv("RANKING_ENTRY_WIDER_MAX_PENDING_PER_RUN", "4"))
        _set("RANKING_ENTRY_INTERVAL_MIN", os.getenv("RANKING_ENTRY_WIDER_INTERVAL_MIN", "1"))
        _set("ENTRY_RANKING_SCALP_MIN_SCORE", os.getenv("ENTRY_RANKING_SCALP_MIN_SCORE", "45"))
        _set("ENTRY_RANKING_SCALP_RANGE_MIN_PCT", os.getenv("ENTRY_RANKING_SCALP_RANGE_MIN_PCT", "0.0035"))
        try:
            from config.ranking_entry_config import RANKING_ENTRY_CONFIG
            RANKING_ENTRY_CONFIG["RANKING"]["MAX_RANK_POSITION"] = int(float(os.environ["RANKING_ENTRY_PREFILTER_MAX_RANK"]))
            RANKING_ENTRY_CONFIG["SCORE"]["MIN_ENTRY_SCORE"] = float(os.getenv("RANKING_ENTRY_WIDER_MIN_SCORE", "60"))
            RANKING_ENTRY_CONFIG["PRICE_MOVE"]["MAX_STEP_MOVE_PCT"] = float(os.getenv("RANKING_ENTRY_WIDER_MAX_STEP_MOVE_PCT", "2.5"))
            RANKING_ENTRY_CONFIG["PRICE_MOVE"]["MAX_DAY_CHANGE_PCT"] = float(os.getenv("RANKING_ENTRY_WIDER_MAX_DAY_CHANGE_PCT", "12.0"))
        except Exception:
            pass
        return True
    except Exception:
        logger.exception("[RANKING ENTRY WIDER TOP] apply failed")
        return False

def _watch_loop() -> None:
    for i in range(240):
        ok = _apply_once()
        if i in (0, 1, 5, 15, 30, 60, 120, 180, 239):
            logger.warning(
                "[RANKING ENTRY WIDER TOP] enforce ok=%s max_rank=%s max_rows=%s per_type=%s per_side=%s pending_per_run=%s interval_min=%s min_score=%s scalp_score=%s range_min=%s",
                ok,
                os.environ.get("RANKING_ENTRY_PREFILTER_MAX_RANK"),
                os.environ.get("RANKING_ENTRY_PREFILTER_MAX_ROWS"),
                os.environ.get("RANKING_ENTRY_PREFILTER_MAX_PER_TYPE"),
                os.environ.get("RANKING_ENTRY_PREFILTER_MAX_PER_SIDE"),
                os.environ.get("RANKING_ENTRY_MAX_PENDING_PER_RUN"),
                os.environ.get("RANKING_ENTRY_INTERVAL_MIN"),
                os.getenv("RANKING_ENTRY_WIDER_MIN_SCORE", "60"),
                os.environ.get("ENTRY_RANKING_SCALP_MIN_SCORE"),
                os.environ.get("ENTRY_RANKING_SCALP_RANGE_MIN_PCT"),
            )
        time.sleep(0.5)

def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return _apply_once()
    ok = _apply_once()
    threading.Thread(target=_watch_loop, name="ranking-entry-wider-top", daemon=True).start()
    _INSTALLED = True
    logger.warning("[RANKING ENTRY WIDER TOP] installed ok=%s", ok)
    return ok

try:
    install()
except Exception:
    logger.exception("[RANKING ENTRY WIDER TOP] auto install failed")

__all__ = ["install"]
