# ============================================================
# File   : ats/ats_register_logging.py
# Version: Ver1.0-ATS-REGISTER-LOGGING
# ------------------------------------------------------------
# ATS登録ログ / symbol name / 表示 helper
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import sqlite3
from typing import List

from config.paths import get_path
from global_state import global_data
from database import Session_position
from database.models import Position

from .ats_rankinfo import format_symbol_ranking_info
from .ats_register_state import sanitize_symbols, unique_keep_order, get_registered_symbols

logger = logging.getLogger(__name__)


def get_open_positions(limit=10) -> List[str]:
    session = Session_position()
    try:
        rows = (
            session.query(Position)
            .filter(Position.status == "OPEN")
            .all()
        )
        return [
            str(r.symbol).strip()
            for r in rows
            if r.symbol and str(r.symbol).strip()
        ][:limit]
    finally:
        session.close()


def load_symbol_names(symbols: List[str]):
    if not symbols:
        return {}

    symbols = unique_keep_order([str(s).strip() for s in symbols if str(s).strip()])
    db = get_path("symbol_flags_db")

    q = f"""
        SELECT symbol, symbolname
        FROM symbol_flags
        WHERE symbol IN ({",".join("?" for _ in symbols)})
    """

    out = {}

    try:
        with sqlite3.connect(db) as conn:
            for s, name in conn.execute(q, symbols):
                out[str(s)] = name or ""
    except Exception:
        logger.exception("symbol name load failed")

    return out


def get_symbol_tier(symbol: str) -> str:
    symbol = str(symbol)
    tiers = getattr(global_data, "symbols_active_tier", {})
    for t in ("A", "B", "C"):
        if symbol in tiers.get(t, []):
            return t
    return "-"


def get_register_reason(symbol: str) -> str:
    symbol = str(symbol)

    for p in getattr(global_data, "open_positions", []):
        s = p.get("symbol") if isinstance(p, dict) else p
        if str(s) == symbol:
            return "OPEN"

    pending = getattr(global_data, "pending_entries", {})
    if isinstance(pending, dict) and symbol in pending:
        return "PENDING"

    tier = get_symbol_tier(symbol)
    if tier != "-":
        return f"ACTIVE_{tier}"

    if symbol in getattr(global_data, "symbols_light", []):
        return "LIGHT"

    return "UNKNOWN"


def save_ats_register_log(symbols: List[str]):
    if not symbols:
        return

    symbols = unique_keep_order(symbols)
    db = get_path("raw_ai") / "ats_registered_log.db"
    db.parent.mkdir(parents=True, exist_ok=True)

    names = load_symbol_names(symbols)
    now = dt.datetime.now().isoformat(timespec="seconds")

    try:
        with sqlite3.connect(db) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS ats_registered_log (
                    time TEXT,
                    symbol TEXT,
                    symbolname TEXT,
                    tier TEXT,
                    reason TEXT
                )
            """)

            for s in symbols:
                conn.execute(
                    "INSERT INTO ats_registered_log VALUES (?, ?, ?, ?, ?)",
                    (
                        now,
                        s,
                        names.get(s, ""),
                        get_symbol_tier(s),
                        get_register_reason(s),
                    )
                )

            conn.commit()

    except Exception:
        logger.exception("ATS register log save failed")


def log_registered_symbols(symbols: List[str], phase: str = ""):
    symbols = unique_keep_order(symbols)
    names = load_symbol_names(symbols)
    rows = []

    for s in symbols:
        rows.append(
            f"{s} {names.get(s,'')} "
            f"[Tier:{get_symbol_tier(s)} / {get_register_reason(s)}]"
        )

    logger.info(
        "📌 [ATS REGISTERED SYMBOLS%s] %d\n%s",
        f" {phase}" if phase else "",
        len(rows),
        "\n".join(rows),
    )


def _print_current_ats_registered_symbols(symbols: List[str], phase: str = "") -> None:
    try:
        symbols = sanitize_symbols(symbols)

        if not symbols:
            logger.info("[ATS CURRENT REGISTERED%s] 0 symbols", f" {phase}" if phase else "")
            return

        names = load_symbol_names(symbols)

        lines = []
        for s in symbols:
            lines.append(
                f"{s} {names.get(s, '')} "
                f"[Tier:{get_symbol_tier(s)} / {get_register_reason(s)}] "
                f"[{format_symbol_ranking_info(s)}]"
            )

        logger.info(
            "[ATS CURRENT REGISTERED%s] count=%d\n%s",
            f" {phase}" if phase else "",
            len(symbols),
            "\n".join(lines),
        )

    except Exception:
        logger.exception("print current ats registered symbols failed")


def show_current_ats_registered_symbols() -> None:
    try:
        symbols = get_registered_symbols()
        _print_current_ats_registered_symbols(symbols, phase="CURRENT")
    except Exception:
        logger.exception("show current ats registered symbols failed")