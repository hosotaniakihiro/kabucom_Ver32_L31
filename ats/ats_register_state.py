# ============================================================
# File   : ats/ats_register_state.py
# Version: Ver1.0-ATS-REGISTER-STATE
# ------------------------------------------------------------
# ATS register state / utility helper
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
from typing import List

from global_state import global_data

logger = logging.getLogger(__name__)

REGISTER_LIMIT = 50
ATS_BATCH_SIZE = 100

ATS_SYMBOL_BLACKLIST = {
    "5070",
    "3260",
}


def unique_keep_order(seq: List[str]) -> List[str]:
    return list(dict.fromkeys(seq))


def sanitize_symbols(seq: List[str]) -> List[str]:
    out = []
    for s in seq:
        try:
            s = str(s).strip()
            s = s.replace(".0", "")
        except Exception:
            continue
        if not s or s in ("nan", "None", "null", "<NA>"):
            continue
        out.append(s)
    return unique_keep_order(out)


def split_batches_50(symbols: List[str]):
    symbols = sanitize_symbols(symbols)
    first = symbols[:REGISTER_LIMIT]
    second = symbols[REGISTER_LIMIT:REGISTER_LIMIT * 2]
    return first, second


def same_symbol_set(a: List[str], b: List[str]) -> bool:
    return sanitize_symbols(a) == sanitize_symbols(b)


def set_registered_symbols(symbols: List[str]) -> None:
    global_data.ats_registered_symbols = sanitize_symbols(symbols)


def get_registered_symbols() -> List[str]:
    return sanitize_symbols(getattr(global_data, "ats_registered_symbols", []))


def set_last_good_batch(symbols: List[str], phase: str) -> None:
    symbols = sanitize_symbols(symbols)
    global_data.ats_last_good_batch = symbols
    global_data.ats_last_good_phase = phase
    global_data.ats_current_active_phase = phase
    global_data.ats_current_active_symbols = symbols


def get_last_good_batch() -> List[str]:
    return sanitize_symbols(getattr(global_data, "ats_last_good_batch", []))


def get_last_good_phase() -> str:
    try:
        return str(getattr(global_data, "ats_last_good_phase", "") or "")
    except Exception:
        return ""


def set_active_phase(phase: str, symbols: List[str]) -> None:
    global_data.ats_current_active_phase = str(phase)
    global_data.ats_current_active_symbols = sanitize_symbols(symbols)


def today_str() -> str:
    return dt.datetime.now().strftime("%Y%m%d")