# ============================================================
# File   : trading/push/subscription_manager/register_symbol_logger.py
# Version: V2.0-KABU-REGISTER-SYMBOL-NAME-ONE-LINE
# ------------------------------------------------------------
# Purpose:
#   kabu Station へ登録予定の銘柄をログ表示する。
#
# Features:
#   - 登録対象50銘柄を1行で表示
#   - 銘柄コード + 銘柄名で表示
#   - added / removed / current も必要に応じて表示
#   - symbol_name_map / global_data / optional_data / symbol_flags DB から銘柄名解決
#   - 失敗しても登録処理を止めない fail-safe
#
# Output example:
#   [KABU REGISTER TARGETS LINE] reason=rotation_A count=50 symbols=7083(AHCグループ), 9242(メディア総研), ...
# ============================================================

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence

logger = logging.getLogger(__name__)


DEFAULT_SYMBOL_FLAGS_DB = Path(
    r"\\192.168.0.22\AutoStockBuyAndSell\Basic\symbol_flags.db"
)

DEFAULT_OPTIONAL_DB = Path(
    r"\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabutan\optional_data.db"
)

DEFAULT_MAX_LINE_CHARS = int(
    os.environ.get("KABU_REGISTER_LOG_MAX_LINE_CHARS", "12000")
)


# ============================================================
# normalize
# ============================================================

def _normalize_symbol(v: Any) -> str:
    if v is None:
        return ""

    s = str(v).strip().upper()
    if not s:
        return ""

    if s.endswith(".T"):
        s = s[:-2]

    if s.endswith(".0"):
        s2 = s[:-2]
        if s2.isdigit():
            s = s2

    if s in {"NONE", "NULL", "NAN", "NA", "-", "0"}:
        return ""

    if s.startswith("FILLER"):
        return ""

    return s


def _dedupe_keep_order(items: Iterable[Any]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()

    for item in items or []:
        s = _normalize_symbol(item)
        if not s:
            continue
        if s in seen:
            continue
        seen.add(s)
        out.append(s)

    return out


def _safe_str(v: Any) -> str:
    if v is None:
        return ""
    s = str(v).strip()
    if s.lower() in {"none", "nan", "null"}:
        return ""
    return s


# ============================================================
# global_data / symbol_name_map
# ============================================================

def _import_attr(module_name: str, attr_name: str) -> Any:
    try:
        import importlib

        mod = importlib.import_module(module_name)
        return getattr(mod, attr_name, None)
    except Exception:
        return None


def _get_global_data() -> Any:
    candidates = (
        ("global_state", "global_data"),
        ("core.global_context.context", "global_data"),
    )

    for module_name, attr_name in candidates:
        gd = _import_attr(module_name, attr_name)
        if gd is not None:
            return gd

    return None


def _mapping_from_any(obj: Any) -> Dict[str, str]:
    """
    dict / DataFrame / list[dict] などから symbol -> symbolname map を作る。
    """
    result: Dict[str, str] = {}

    if obj is None:
        return result

    # dict
    if isinstance(obj, Mapping):
        for k, v in obj.items():
            symbol = _normalize_symbol(k)

            if isinstance(v, Mapping):
                name = (
                    _safe_str(v.get("symbolname"))
                    or _safe_str(v.get("symbol_name"))
                    or _safe_str(v.get("name"))
                    or _safe_str(v.get("SymbolName"))
                    or _safe_str(v.get("銘柄名"))
                )
            else:
                name = _safe_str(v)

            if symbol and name:
                result[symbol] = name

        return result

    # pandas DataFrame
    try:
        if hasattr(obj, "columns"):
            cols = list(getattr(obj, "columns", []))

            symbol_col = None
            name_col = None

            for c in ("symbol", "Symbol", "code", "Code", "銘柄コード"):
                if c in cols:
                    symbol_col = c
                    break

            for c in (
                "symbolname",
                "symbol_name",
                "name",
                "Name",
                "SymbolName",
                "銘柄名",
                "銘柄名称",
            ):
                if c in cols:
                    name_col = c
                    break

            if symbol_col and name_col:
                for _, row in obj.iterrows():
                    symbol = _normalize_symbol(row.get(symbol_col))
                    name = _safe_str(row.get(name_col))
                    if symbol and name:
                        result[symbol] = name

            return result
    except Exception:
        pass

    # list[dict]
    try:
        seq = list(obj)
    except Exception:
        return result

    for item in seq:
        if not isinstance(item, Mapping):
            continue

        symbol = _normalize_symbol(
            item.get("symbol")
            or item.get("Symbol")
            or item.get("code")
            or item.get("Code")
            or item.get("銘柄コード")
        )
        name = (
            _safe_str(item.get("symbolname"))
            or _safe_str(item.get("symbol_name"))
            or _safe_str(item.get("name"))
            or _safe_str(item.get("Name"))
            or _safe_str(item.get("SymbolName"))
            or _safe_str(item.get("銘柄名"))
        )

        if symbol and name:
            result[symbol] = name

    return result


def _load_symbol_name_map_from_global_data() -> Dict[str, str]:
    gd = _get_global_data()
    if gd is None:
        return {}

    result: Dict[str, str] = {}

    attr_candidates = (
        "symbol_name_map",
        "symbolname_map",
        "symbol_map",
        "name_map",
        "optional_data",
        "optional_df",
        "symbol_flags_df",
        "symbol_master_df",
    )

    for attr in attr_candidates:
        try:
            obj = getattr(gd, attr, None)
        except Exception:
            obj = None

        if obj is None:
            continue

        result.update(_mapping_from_any(obj))

    return result


def _load_symbol_name_map_from_modules() -> Dict[str, str]:
    result: Dict[str, str] = {}

    candidates = (
        ("core.bootstrap.load_symbol_map", "symbol_name_map"),
        ("core.bootstrap.load_symbol_map", "SYMBOL_NAME_MAP"),
        ("symbol_loader", "symbol_name_map"),
        ("symbol_loader", "SYMBOL_NAME_MAP"),
    )

    for module_name, attr_name in candidates:
        obj = _import_attr(module_name, attr_name)
        result.update(_mapping_from_any(obj))

    return result


# ============================================================
# DB fallback
# ============================================================

def _read_symbol_names_from_db(
    db_path: Path,
    *,
    table_candidates: Sequence[str],
) -> Dict[str, str]:
    result: Dict[str, str] = {}

    if not db_path.exists():
        return result

    try:
        with sqlite3.connect(str(db_path), timeout=5) as con:
            con.row_factory = sqlite3.Row

            for table in table_candidates:
                try:
                    cols = con.execute(f"PRAGMA table_info({table})").fetchall()
                except Exception:
                    continue

                col_names = [str(r[1]) for r in cols]
                if not col_names:
                    continue

                symbol_col = None
                name_col = None

                for c in ("symbol", "Symbol", "code", "Code", "銘柄コード"):
                    if c in col_names:
                        symbol_col = c
                        break

                for c in (
                    "symbolname",
                    "symbol_name",
                    "name",
                    "Name",
                    "SymbolName",
                    "銘柄名",
                    "銘柄名称",
                ):
                    if c in col_names:
                        name_col = c
                        break

                if not symbol_col or not name_col:
                    continue

                try:
                    rows = con.execute(
                        f"""
                        SELECT {symbol_col} AS symbol, {name_col} AS symbolname
                        FROM {table}
                        WHERE {symbol_col} IS NOT NULL
                        """
                    ).fetchall()
                except Exception:
                    continue

                for r in rows:
                    symbol = _normalize_symbol(r["symbol"])
                    name = _safe_str(r["symbolname"])
                    if symbol and name:
                        result[symbol] = name

                if result:
                    return result

    except Exception:
        logger.debug(
            "[KABU REGISTER SYMBOL LOGGER] db symbolname load failed path=%s",
            db_path,
            exc_info=True,
        )

    return result


def _load_symbol_name_map_from_dbs() -> Dict[str, str]:
    result: Dict[str, str] = {}

    symbol_flags_db = Path(
        os.environ.get("SYMBOL_FLAGS_DB_PATH", str(DEFAULT_SYMBOL_FLAGS_DB))
    )
    optional_db = Path(
        os.environ.get("OPTIONAL_DB_PATH", str(DEFAULT_OPTIONAL_DB))
    )

    result.update(
        _read_symbol_names_from_db(
            symbol_flags_db,
            table_candidates=("symbol_flags", "symbols", "symbol_master"),
        )
    )

    result.update(
        _read_symbol_names_from_db(
            optional_db,
            table_candidates=(
                "symbol_flags",
                "optional_data",
                "daily_watchlist",
                "symbols",
                "symbol_master",
            ),
        )
    )

    return result


_SYMBOL_NAME_MAP_CACHE: Optional[Dict[str, str]] = None


def load_symbol_name_map(*, force_reload: bool = False) -> Dict[str, str]:
    global _SYMBOL_NAME_MAP_CACHE

    if _SYMBOL_NAME_MAP_CACHE is not None and not force_reload:
        return dict(_SYMBOL_NAME_MAP_CACHE)

    result: Dict[str, str] = {}

    try:
        result.update(_load_symbol_name_map_from_global_data())
    except Exception:
        logger.debug(
            "[KABU REGISTER SYMBOL LOGGER] global_data symbol map failed",
            exc_info=True,
        )

    try:
        result.update(_load_symbol_name_map_from_modules())
    except Exception:
        logger.debug(
            "[KABU REGISTER SYMBOL LOGGER] module symbol map failed",
            exc_info=True,
        )

    try:
        result.update(_load_symbol_name_map_from_dbs())
    except Exception:
        logger.debug(
            "[KABU REGISTER SYMBOL LOGGER] db symbol map failed",
            exc_info=True,
        )

    _SYMBOL_NAME_MAP_CACHE = dict(result)

    logger.info(
        "[KABU REGISTER SYMBOL LOGGER] symbol_name_map loaded size=%d",
        len(result),
    )

    return dict(result)


# ============================================================
# format
# ============================================================

def resolve_symbol_name(
    symbol: Any,
    symbol_name_map: Optional[Mapping[str, str]] = None,
) -> str:
    s = _normalize_symbol(symbol)
    if not s:
        return ""

    if symbol_name_map is None:
        symbol_name_map = load_symbol_name_map()

    name = (
        _safe_str(symbol_name_map.get(s))
        or _safe_str(symbol_name_map.get(str(s)))
        or _safe_str(symbol_name_map.get(str(s).upper()))
        or _safe_str(symbol_name_map.get(str(s).lower()))
    )

    return name


def format_symbol_with_name(
    symbol: Any,
    *,
    symbol_name_map: Optional[Mapping[str, str]] = None,
    fallback_to_dash: bool = True,
) -> str:
    s = _normalize_symbol(symbol)
    if not s:
        return ""

    name = resolve_symbol_name(s, symbol_name_map=symbol_name_map)

    if not name and fallback_to_dash:
        name = "-"

    return f"{s}({name})"


def format_symbols_one_line(
    symbols: Sequence[Any],
    *,
    symbol_name_map: Optional[Mapping[str, str]] = None,
    max_line_chars: int = DEFAULT_MAX_LINE_CHARS,
) -> str:
    clean = _dedupe_keep_order(symbols)

    if symbol_name_map is None:
        symbol_name_map = load_symbol_name_map()

    parts = [
        format_symbol_with_name(s, symbol_name_map=symbol_name_map)
        for s in clean
    ]
    parts = [p for p in parts if p]

    line = ", ".join(parts)

    max_len = int(max_line_chars or DEFAULT_MAX_LINE_CHARS)
    if max_len > 0 and len(line) > max_len:
        line = line[:max_len] + "...[TRUNCATED]"

    return line


# ============================================================
# public logger
# ============================================================

def log_kabustation_register_symbols(
    target_symbols: Sequence[Any],
    *,
    current_symbols: Optional[Sequence[Any]] = None,
    added_symbols: Optional[Sequence[Any]] = None,
    removed_symbols: Optional[Sequence[Any]] = None,
    reason: str = "",
    one_line: bool = True,
    show_current: bool = False,
    show_diff: bool = True,
    force_reload_symbol_names: bool = False,
) -> None:
    """
    kabu Station登録予定銘柄をログ表示する。

    Parameters:
      target_symbols:
        今から登録する銘柄。通常50件。
      current_symbols:
        現在登録済みの銘柄。
      added_symbols:
        追加予定銘柄。
      removed_symbols:
        削除予定銘柄。
      reason:
        rotation_A / rotation_B / on_open / manual など。
      one_line:
        Trueなら50銘柄を1行で表示。
    """
    try:
        target = _dedupe_keep_order(target_symbols or [])
        current = _dedupe_keep_order(current_symbols or [])
        added = _dedupe_keep_order(added_symbols or [])
        removed = _dedupe_keep_order(removed_symbols or [])

        symbol_name_map = load_symbol_name_map(
            force_reload=force_reload_symbol_names
        )

        target_line = format_symbols_one_line(
            target,
            symbol_name_map=symbol_name_map,
        )

        logger.info(
            "[KABU REGISTER TARGETS LINE] reason=%s count=%d symbols=%s",
            reason,
            len(target),
            target_line,
        )

        # 50件上限チェック用
        if len(target) > 50:
            logger.warning(
                "[KABU REGISTER TARGETS WARNING] reason=%s count=%d exceeds 50. "
                "This may exceed kabu Station PUSH registration limit.",
                reason,
                len(target),
            )

        # 先頭だけの短縮ログ
        logger.info(
            "[KABU REGISTER TARGETS HEAD] reason=%s count=%d head=%s",
            reason,
            len(target),
            ", ".join(target[:10]),
        )

        if show_diff:
            if added:
                added_line = format_symbols_one_line(
                    added,
                    symbol_name_map=symbol_name_map,
                )
                logger.info(
                    "[KABU REGISTER ADDED LINE] reason=%s count=%d symbols=%s",
                    reason,
                    len(added),
                    added_line,
                )

            if removed:
                removed_line = format_symbols_one_line(
                    removed,
                    symbol_name_map=symbol_name_map,
                )
                logger.info(
                    "[KABU REGISTER REMOVED LINE] reason=%s count=%d symbols=%s",
                    reason,
                    len(removed),
                    removed_line,
                )

        if show_current and current:
            current_line = format_symbols_one_line(
                current,
                symbol_name_map=symbol_name_map,
            )
            logger.info(
                "[KABU REGISTER CURRENT LINE] reason=%s count=%d symbols=%s",
                reason,
                len(current),
                current_line,
            )

        if not one_line:
            logger.info(
                "========== KABU REGISTER TARGETS DETAIL reason=%s count=%d ==========",
                reason,
                len(target),
            )
            for i, s in enumerate(target, start=1):
                name = resolve_symbol_name(s, symbol_name_map=symbol_name_map) or "-"
                logger.info("%2d. %s %s", i, s, name)
            logger.info("===================================================================")

    except Exception:
        logger.exception(
            "[KABU REGISTER SYMBOL LOGGER] failed reason=%s",
            reason,
        )


# ============================================================
# compatibility aliases
# ============================================================

def log_register_symbols(*args, **kwargs) -> None:
    return log_kabustation_register_symbols(*args, **kwargs)


def log_symbols_for_register(*args, **kwargs) -> None:
    return log_kabustation_register_symbols(*args, **kwargs)


def log_subscription_targets(*args, **kwargs) -> None:
    return log_kabustation_register_symbols(*args, **kwargs)


__all__ = [
    "load_symbol_name_map",
    "resolve_symbol_name",
    "format_symbol_with_name",
    "format_symbols_one_line",
    "log_kabustation_register_symbols",
    "log_register_symbols",
    "log_symbols_for_register",
    "log_subscription_targets",
]