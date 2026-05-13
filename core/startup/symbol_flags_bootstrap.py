# ============================================================
# File   : core/startup/symbol_flags_bootstrap.py
# Version: PRODUCTION-SYMBOL-FLAGS-DB-BOOTSTRAP-V1
# ------------------------------------------------------------
# 目的:
#   起動時に信用銘柄マスターDBをまとめて読み込み、global_data にキャッシュする。
#
# 読み込み元:
#   \\192.168.0.22\AutoStockBuyAndSell\Basic\symbol_flags.db
#   table: symbol_flags
#   key  : symbol / code / stock_code 等を自動検出
#   判定 : short_ok = 1 の銘柄を信用SELL可能として扱う
#
# 保存先:
#   global_data.symbol_flags_info_map        dict[symbol] -> row dict
#   global_data.symbol_flags_map             alias
#   global_data.symbol_flag_info_map         alias
#   global_data.symbol_flags_eligible_symbols set[str] short_ok=1
#   global_data.short_ok_symbols             alias set[str]
#
# 方針:
#   - Excel マスターに依存しない
#   - 起動時に一括ロードして、SELL判定中のSQLite連打を避ける
#   - DBが無くてもシステム本体は止めない
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import sqlite3
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)

SYMBOL_FLAGS_DB_PATH = Path(r"\\192.168.0.22\AutoStockBuyAndSell\Basic\symbol_flags.db")
SYMBOL_FLAGS_TABLE = "symbol_flags"

_TRUE_VALUES = {True, 1, "1", "true", "True", "TRUE", "yes", "Yes", "YES", "可能", "可"}


def _normalize_symbol(v: Any) -> str:
    try:
        if v is None:
            return ""
        s = str(v).strip()
        if s.endswith(".0"):
            s = s[:-2]
        return s
    except Exception:
        return ""


def _as_bool(v: Any) -> bool:
    if v in _TRUE_VALUES:
        return True
    if isinstance(v, str) and v.strip() in _TRUE_VALUES:
        return True
    try:
        return bool(int(float(v)))
    except Exception:
        return False


def _detect_symbol_column(columns: list[str]) -> str | None:
    for c in ("symbol", "code", "stock_code", "Symbol", "Code", "銘柄コード"):
        if c in columns:
            return c
    return None


def _row_to_dict(columns: list[str], row: tuple[Any, ...]) -> Dict[str, Any]:
    return {columns[i]: row[i] for i in range(min(len(columns), len(row)))}


def load_symbol_flags_from_db(
    *,
    db_path: str | Path = SYMBOL_FLAGS_DB_PATH,
    table: str = SYMBOL_FLAGS_TABLE,
) -> tuple[dict[str, Dict[str, Any]], set[str]]:
    """
    symbol_flags.db を読み、全銘柄mapと short_ok=1 のsetを返す。
    """
    path = Path(db_path)
    if not path.exists():
        raise FileNotFoundError(str(path))

    with sqlite3.connect(str(path), timeout=5.0) as conn:
        conn.row_factory = None
        info = conn.execute(f"PRAGMA table_info({table})").fetchall()
        columns = [str(r[1]) for r in info]

        if not columns:
            raise RuntimeError(f"table_not_found_or_empty_schema:{table}")

        if "short_ok" not in columns:
            raise RuntimeError(f"short_ok column not found columns={columns}")

        symbol_col = _detect_symbol_column(columns)
        if not symbol_col:
            raise RuntimeError(f"symbol column not found columns={columns}")

        quoted_cols = ", ".join([f'"{c}"' for c in columns])
        rows = conn.execute(f"SELECT {quoted_cols} FROM {table}").fetchall()

    flags_map: dict[str, Dict[str, Any]] = {}
    eligible: set[str] = set()

    for row in rows:
        d = _row_to_dict(columns, row)
        sym = _normalize_symbol(d.get(symbol_col))
        if not sym:
            continue

        short_ok = d.get("short_ok")

        d["symbol"] = sym
        d["short_ok"] = short_ok
        d.setdefault("sell_target", short_ok)
        d.setdefault("short_sellable", short_ok)
        d.setdefault("source", "symbol_flags_db")

        flags_map[sym] = d

        if _as_bool(short_ok):
            eligible.add(sym)

    return flags_map, eligible


def install_symbol_flags_cache(
    *,
    force: bool = False,
    db_path: str | Path = SYMBOL_FLAGS_DB_PATH,
    table: str = SYMBOL_FLAGS_TABLE,
) -> bool:
    """
    起動時に呼ぶ公開API。
    読み込み結果を global_data に保持する。
    """
    try:
        from global_state import global_data
    except Exception:
        logger.exception("[SYMBOL FLAGS BOOTSTRAP] global_data import failed")
        return False

    try:
        if not force:
            current = getattr(global_data, "symbol_flags_info_map", None)
            if isinstance(current, dict) and current:
                logger.warning(
                    "[SYMBOL FLAGS BOOTSTRAP] already loaded rows=%s eligible=%s",
                    len(current),
                    len(getattr(global_data, "symbol_flags_eligible_symbols", set()) or set()),
                )
                return True

        flags_map, eligible = load_symbol_flags_from_db(db_path=db_path, table=table)

        # 既存コード互換のため複数名に同じmapを積む。
        global_data.symbol_flags_info_map = flags_map
        global_data.symbol_flag_info_map = flags_map
        global_data.symbol_flags_map = flags_map
        global_data.symbol_info_map = flags_map

        global_data.symbol_flags_eligible_symbols = eligible
        global_data.short_ok_symbols = eligible
        global_data.symbol_flags_db_path = str(db_path)
        global_data.symbol_flags_loaded_at = dt.datetime.now()
        global_data.symbol_flags_load_error = ""

        logger.warning(
            "[SYMBOL FLAGS BOOTSTRAP] loaded path=%s table=%s rows=%s short_ok_1=%s loaded_at=%s",
            db_path,
            table,
            len(flags_map),
            len(eligible),
            global_data.symbol_flags_loaded_at,
        )
        return True

    except Exception as e:
        try:
            global_data.symbol_flags_load_error = str(e)
            global_data.symbol_flags_db_path = str(db_path)
        except Exception:
            pass

        logger.exception(
            "[SYMBOL FLAGS BOOTSTRAP] load failed path=%s table=%s reason=%s",
            db_path,
            table,
            e,
        )
        return False


# 後方互換用エイリアス
install = install_symbol_flags_cache
