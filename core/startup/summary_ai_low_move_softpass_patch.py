# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)
VERSION = "V3.5-STRICT-1M-RANGE-RELAX-WIRE"
_INSTALLED = False


def _install_blowoff_prefilter() -> bool:
    try:
        from core.startup.summary_ai_blowoff_prefilter_patch import install as _install
        ok = bool(_install())
        logger.warning("[LOW MOVE GUARD] chained SUMMARY_AI blowoff prefilter installed=%s version=%s", ok, VERSION)
        return ok
    except Exception:
        logger.exception("[LOW MOVE GUARD] chained SUMMARY_AI blowoff prefilter install failed version=%s", VERSION)
        return False


def _install_1m_range_relax() -> bool:
    try:
        from core.startup.summary_ai_1m_range_relax_patch import install as _install
        ok = bool(_install())
        logger.warning("[LOW MOVE GUARD] SUMMARY_AI 1m range relax installed=%s version=%s", ok, VERSION)
        return ok
    except Exception:
        logger.exception("[LOW MOVE GUARD] SUMMARY_AI 1m range relax install failed version=%s", VERSION)
        return False


def _set_timeout_defaults() -> None:
    os.environ.setdefault("ENTRY_EXECUTE_ORIG_TIMEOUT_SEC", "15")
    os.environ.setdefault("SUMMARY_AI_EXECUTOR_ROLLING_RETRY", "1")
    os.environ.setdefault("SUMMARY_AI_EXECUTOR_CANDIDATE_SCAN_LIMIT", "12")
    os.environ.setdefault("SUMMARY_AI_EXECUTOR_BATCH_SIZE", "3")
    os.environ.setdefault("SUMMARY_AI_LOW_MOVE_SOFTPASS", "0")
    os.environ.setdefault("SUMMARY_AI_LOW_MOVE_SOFTPASS_WATCHER", "0")
    os.environ.setdefault("SUMMARY_AI_BLOWOFF_PREFILTER_ENABLED", "1")
    os.environ.setdefault("SUMMARY_AI_1M_MIN_RANGE_PCT", "0.0003")
    os.environ.setdefault("SUMMARY_AI_1M_MIN_RANGE_VALUE", "1.0")


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    _set_timeout_defaults()
    blowoff_ok = _install_blowoff_prefilter()
    relax_ok = _install_1m_range_relax()
    _INSTALLED = bool(blowoff_ok and relax_ok)
    logger.warning(
        "[LOW MOVE GUARD] SUMMARY_AI low move strict wire installed=%s version=%s softpass=%s min_pct=%s min_value=%s blowoff=%s relax=%s",
        _INSTALLED,
        VERSION,
        os.getenv("SUMMARY_AI_LOW_MOVE_SOFTPASS"),
        os.getenv("SUMMARY_AI_1M_MIN_RANGE_PCT"),
        os.getenv("SUMMARY_AI_1M_MIN_RANGE_VALUE"),
        blowoff_ok,
        relax_ok,
    )
    return _INSTALLED


try:
    install()
except Exception:
    logger.exception("[LOW MOVE GUARD] SUMMARY_AI low move strict wire auto install failed")


__all__ = ["VERSION", "install"]
