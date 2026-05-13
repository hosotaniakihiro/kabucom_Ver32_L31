# ============================================================
# File: AI/sell_credit_guard.py
# Version: PRODUCTION-STABLE-V6-SYMBOL-FLAGS-DB-SHORT-OK
# ------------------------------------------------------------
# 殿様イナゴ / SUMMARY / RANKING SELL 共通 信用・売禁ガード
#
# ✔ 信用売り可否を最優先で判定
# ✔ Excel マスターに依存しない
# ✔ \\192.168.0.22\AutoStockBuyAndSell\Basic\symbol_flags.db
#   symbol_flags.short_ok = 1 の銘柄を SELL 信用売り可能として使用
# ✔ 起動時 global_data の symbol_flags_info_map があれば優先使用
# ✔ global_data に無い場合は symbol_flags.db を直接読む
# ✔ DBは短時間キャッシュして、候補ごとのSQLite連打を防止
# ✔ 売禁・高保証金率は従来どおり遮断
# ✔ is_attention=1 は売禁ではないため遮断せず、警告ログのみ出す
# ✔ symbol 文字列 / dict / pandas.Series / object の型揺れで落ちない
# ✔ kabu API 100368 は sell_order_reject_cache 側でキャッシュしない前提
# ============================================================

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)


# ============================================================
# 固定パラメータ
# ============================================================

# 高保証金率とみなす閾値
MAX_MARGIN_RATE = 2.0   # 200% 以上は危険

# 信用銘柄マスターDB。Excel が無くても、このDBの short_ok=1 を正として使う。
SYMBOL_FLAGS_DB_PATH = Path(r"\\192.168.0.22\AutoStockBuyAndSell\Basic\symbol_flags.db")
SYMBOL_FLAGS_TABLE = "symbol_flags"
SYMBOL_FLAGS_CACHE_TTL_SEC = 300.0

_TRUE_VALUES = {
    True,
    1,
    "1",
    "true",
    "True",
    "TRUE",
    "yes",
    "Yes",
    "YES",
    "y",
    "Y",
    "可能",
    "可",
    "信用売可",
    "信用売り可",
    "貸借銘柄",
    "short_sellable",
}

_FALSE_VALUES = {
    False,
    0,
    "0",
    "false",
    "False",
    "FALSE",
    "no",
    "No",
    "NO",
    "n",
    "N",
    "不可",
    "否",
    "売禁",
    "信用売不可",
    "信用売り不可",
    "信用銘柄",
    "非貸借",
}

_DB_CACHE_BY_SYMBOL: dict[str, Dict[str, Any]] = {}
_DB_CACHE_LOADED_AT: float = 0.0
_DB_CACHE_LOAD_FAILED_AT: float = 0.0
_DB_CACHE_LOAD_FAILED_REASON: str = ""


# ============================================================
# 内部ユーティリティ
# ============================================================

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


def _to_dict(value: Any) -> Dict[str, Any]:
    """
    dict / pandas.Series / str / int / object を安全に dict 化する。
    """
    if value is None:
        return {}

    if isinstance(value, dict):
        return dict(value)

    if hasattr(value, "to_dict"):
        try:
            d = value.to_dict()
            if isinstance(d, dict):
                return dict(d)
        except Exception:
            pass

    if isinstance(value, (str, int)):
        s = _normalize_symbol(value)
        return {"symbol": s} if s else {}

    result: Dict[str, Any] = {}
    for key in (
        "symbol",
        "code",
        "stock_code",
        "short_sellable",
        "short_ok",
        "margin_sellable",
        "credit_sellable",
        "can_short",
        "is_shortable",
        "shortable",
        "sell_target",
        "credit_type",
        "sell_ban",
        "is_attention",
        "margin_rate",
    ):
        try:
            if hasattr(value, key):
                result[key] = getattr(value, key)
        except Exception:
            pass

    return result


def _pick_symbol(flags: Dict[str, Any]) -> str:
    for key in ("symbol", "code", "stock_code", "Symbol", "Code"):
        s = _normalize_symbol(flags.get(key))
        if s:
            return s
    return "-"


def _as_bool(value: Any, default: bool = False) -> bool:
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False

    if isinstance(value, str):
        s = value.strip()
        if s in _TRUE_VALUES:
            return True
        if s in _FALSE_VALUES:
            return False
        return default

    if value is None:
        return default

    return bool(value)


def _get_first(flags: Dict[str, Any], keys: tuple[str, ...], default: Any = None) -> Any:
    for key in keys:
        if key in flags:
            return flags.get(key)
    return default


def _row_to_dict(columns: list[str], row: tuple[Any, ...]) -> Dict[str, Any]:
    return {columns[i]: row[i] for i in range(min(len(columns), len(row)))}


def _detect_symbol_column(columns: list[str]) -> str | None:
    for c in ("symbol", "code", "stock_code", "Symbol", "Code"):
        if c in columns:
            return c
    return None


def _load_symbol_flags_db_cache(force: bool = False) -> dict[str, Dict[str, Any]]:
    """
    symbol_flags.db の symbol_flags テーブルをまとめて読む。
    候補1件ごとにSQLiteを叩かないように、短時間だけメモリキャッシュする。
    """
    global _DB_CACHE_BY_SYMBOL, _DB_CACHE_LOADED_AT, _DB_CACHE_LOAD_FAILED_AT, _DB_CACHE_LOAD_FAILED_REASON

    now = time.time()
    if not force and _DB_CACHE_BY_SYMBOL and (now - _DB_CACHE_LOADED_AT) < SYMBOL_FLAGS_CACHE_TTL_SEC:
        return _DB_CACHE_BY_SYMBOL

    # 失敗直後に毎候補で例外ログを連発しない。
    if not force and _DB_CACHE_LOAD_FAILED_AT and (now - _DB_CACHE_LOAD_FAILED_AT) < 30.0:
        return _DB_CACHE_BY_SYMBOL

    path = SYMBOL_FLAGS_DB_PATH
    if not path.exists():
        _DB_CACHE_LOAD_FAILED_AT = now
        _DB_CACHE_LOAD_FAILED_REASON = f"db_not_found:{path}"
        logger.warning("[SELL_CREDIT_GUARD] symbol_flags.db not found path=%s", path)
        return _DB_CACHE_BY_SYMBOL

    try:
        with sqlite3.connect(str(path), timeout=3.0) as conn:
            conn.row_factory = None
            cur = conn.execute(f"PRAGMA table_info({SYMBOL_FLAGS_TABLE})")
            info = cur.fetchall()
            columns = [str(r[1]) for r in info]

            if not columns:
                raise RuntimeError(f"table_not_found_or_empty_schema:{SYMBOL_FLAGS_TABLE}")

            if "short_ok" not in columns:
                raise RuntimeError(f"short_ok column not found columns={columns}")

            symbol_col = _detect_symbol_column(columns)
            if not symbol_col:
                raise RuntimeError(f"symbol column not found columns={columns}")

            quoted_cols = ", ".join([f'"{c}"' for c in columns])
            rows = conn.execute(f"SELECT {quoted_cols} FROM {SYMBOL_FLAGS_TABLE}").fetchall()

        cache: dict[str, Dict[str, Any]] = {}
        short_ok_count = 0
        for row in rows:
            d = _row_to_dict(columns, row)
            sym = _normalize_symbol(d.get(symbol_col))
            if not sym:
                continue

            # can_sell_symbol 側が見る標準キーを必ず整える。
            d["symbol"] = sym
            d["short_ok"] = d.get("short_ok")
            d.setdefault("sell_target", d.get("short_ok"))
            d.setdefault("short_sellable", d.get("short_ok"))

            if _as_bool(d.get("short_ok"), default=False):
                short_ok_count += 1

            cache[sym] = d

        _DB_CACHE_BY_SYMBOL = cache
        _DB_CACHE_LOADED_AT = now
        _DB_CACHE_LOAD_FAILED_AT = 0.0
        _DB_CACHE_LOAD_FAILED_REASON = ""

        logger.warning(
            "[SELL_CREDIT_GUARD] symbol_flags.db loaded path=%s rows=%s short_ok_1=%s ttl_sec=%s",
            path,
            len(cache),
            short_ok_count,
            SYMBOL_FLAGS_CACHE_TTL_SEC,
        )
        return _DB_CACHE_BY_SYMBOL

    except Exception as e:
        _DB_CACHE_LOAD_FAILED_AT = now
        _DB_CACHE_LOAD_FAILED_REASON = str(e)
        logger.exception(
            "[SELL_CREDIT_GUARD] symbol_flags.db load failed path=%s table=%s reason=%s",
            path,
            SYMBOL_FLAGS_TABLE,
            e,
        )
        return _DB_CACHE_BY_SYMBOL


def _load_flags_from_symbol_flags_db(symbol: str) -> Dict[str, Any]:
    symbol = _normalize_symbol(symbol)
    if not symbol or symbol == "-":
        return {}

    cache = _load_symbol_flags_db_cache(force=False)
    d = cache.get(symbol)
    if isinstance(d, dict):
        out = dict(d)
        out["symbol"] = symbol
        logger.info(
            "[SELL_CREDIT_GUARD] symbol_flags.db used symbol=%s short_ok=%r sell_target=%r credit_type=%r",
            symbol,
            out.get("short_ok"),
            out.get("sell_target"),
            out.get("credit_type"),
        )
        return out

    return {}


def _load_flags_from_global_cache(symbol: str) -> Dict[str, Any]:
    symbol = _normalize_symbol(symbol)
    if not symbol or symbol == "-":
        return {}

    try:
        from global_state import global_data
    except Exception:
        return _load_flags_from_symbol_flags_db(symbol)

    for attr in (
        "symbol_flags_info_map",
        "symbol_flag_info_map",
        "symbol_flags_map",
        "symbol_info_map",
    ):
        try:
            m = getattr(global_data, attr, None)
            if isinstance(m, dict):
                d = m.get(symbol) or m.get(str(symbol))
                if isinstance(d, dict):
                    out = dict(d)
                    out["symbol"] = symbol
                    return out
        except Exception:
            pass

    # eligible set だけでもあれば、売建対象かの最低限判断に使う。
    # ただし short_ok=1 を正にしたいので、DBが読める場合はDBを優先する。
    db_flags = _load_flags_from_symbol_flags_db(symbol)
    if db_flags:
        return db_flags

    try:
        eligible = getattr(global_data, "symbol_flags_eligible_symbols", None)
        if eligible and symbol in eligible:
            return {"symbol": symbol, "sell_target": 1, "short_ok": 1, "short_sellable": 1}
    except Exception:
        pass

    return {}


def _merge_global_flags_if_needed(flags: Dict[str, Any]) -> Dict[str, Any]:
    symbol = _pick_symbol(flags)
    if not symbol or symbol == "-":
        return flags

    known_keys = {
        "short_sellable",
        "short_ok",
        "margin_sellable",
        "credit_sellable",
        "can_short",
        "is_shortable",
        "shortable",
        "is_margin_sellable",
        "sell_target",
        "credit_type",
    }
    if any(k in flags for k in known_keys):
        return flags

    cache_flags = _load_flags_from_global_cache(symbol)
    if not cache_flags:
        return flags

    merged = dict(cache_flags)
    merged.update({k: v for k, v in flags.items() if v is not None})

    logger.info(
        "[SELL_CREDIT_GUARD] flags used symbol=%s sell_target=%s short_ok=%s credit_type=%s is_attention=%s source=%s",
        symbol,
        merged.get("sell_target"),
        merged.get("short_ok"),
        merged.get("credit_type"),
        merged.get("is_attention"),
        "global_or_symbol_flags_db",
    )
    return merged


def _is_runtime_rejected(symbol: str) -> bool:
    try:
        from AI.sell_order_reject_cache import is_sell_rejected, get_sell_reject_reason

        if is_sell_rejected(symbol):
            logger.info(
                "[SELL_CREDIT_GUARD] NG symbol=%s reason=runtime_api_reject %s",
                symbol,
                get_sell_reject_reason(symbol),
            )
            return True
    except Exception:
        # reject cache が無くても既存判定は継続する
        return False
    return False


# ============================================================
# メイン API
# ============================================================

def can_sell_symbol(symbol_flags: Any, *, default: bool = False) -> bool:
    """
    SELL 信用売り可能かを判定する。

    symbol 文字列だけが渡された場合も、以下の順で sell_target / short_ok / credit_type を読む。
      1. global_data.symbol_flags_info_map 等
      2. \\192.168.0.22\AutoStockBuyAndSell\Basic\symbol_flags.db の symbol_flags テーブル
      3. global_data.symbol_flags_eligible_symbols

    方針:
      short_ok=1 を信用売り可能の正とする。
      short_ok=0 / 情報なしは原則NG。
    """

    flags = _to_dict(symbol_flags)
    symbol = _pick_symbol(flags)

    # API側ランタイム拒否キャッシュ。100368はキャッシュしない修正済み前提。
    if symbol and symbol != "-" and _is_runtime_rejected(symbol):
        return False

    flags = _merge_global_flags_if_needed(flags)
    symbol = _pick_symbol(flags)

    if symbol and symbol != "-" and _is_runtime_rejected(symbol):
        return False

    if not flags:
        logger.info(
            "[SELL_CREDIT_GUARD] NG symbol=%s reason=no_flags default=%s db_path=%s last_db_error=%s",
            symbol,
            default,
            SYMBOL_FLAGS_DB_PATH,
            _DB_CACHE_LOAD_FAILED_REASON,
        )
        return bool(default)

    if set(flags.keys()) <= {"symbol"}:
        logger.info(
            "[SELL_CREDIT_GUARD] NG symbol=%s reason=symbol_only_no_short_sellable_info db_path=%s last_db_error=%s",
            symbol,
            SYMBOL_FLAGS_DB_PATH,
            _DB_CACHE_LOAD_FAILED_REASON,
        )
        return bool(default)

    # --------------------------------------------------------
    # 信用売り可否
    # --------------------------------------------------------
    # ユーザー指定により、symbol_flags.short_ok=1 を主判定にする。
    # short_ok が存在する場合は最優先。credit_type=貸借銘柄だけでは通さない。
    if "short_ok" in flags:
        short_ok = flags.get("short_ok")
        if not _as_bool(short_ok, default=False):
            logger.info(
                "[SELL_CREDIT_GUARD] NG symbol=%s reason=short_ok_not_1 short_ok=%r sell_target=%r credit_type=%r source=symbol_flags_db_or_cache",
                symbol,
                short_ok,
                flags.get("sell_target"),
                flags.get("credit_type"),
            )
            return False
        short_sellable = short_ok
    else:
        short_sellable = _get_first(
            flags,
            (
                "short_sellable",
                "margin_sellable",
                "credit_sellable",
                "can_short",
                "is_shortable",
                "shortable",
                "is_margin_sellable",
                "sell_target",
            ),
            default,
        )

        credit_type = str(flags.get("credit_type") or "").strip()
        credit_type_ok = credit_type == "貸借銘柄"

        if not (_as_bool(short_sellable, default=default) or credit_type_ok):
            logger.info(
                "[SELL_CREDIT_GUARD] NG symbol=%s reason=not_short_sellable value=%r credit_type=%s sell_target=%r short_ok=%r",
                symbol,
                short_sellable,
                credit_type,
                flags.get("sell_target"),
                flags.get("short_ok"),
            )
            return False

    credit_type = str(flags.get("credit_type") or "").strip()

    # --------------------------------------------------------
    # 売禁
    # --------------------------------------------------------
    sell_ban = _get_first(
        flags,
        ("sell_ban", "is_sell_ban", "sell_restricted", "short_sell_ban"),
        False,
    )
    if _as_bool(sell_ban, default=False):
        logger.info(
            "[SELL_CREDIT_GUARD] NG symbol=%s reason=sell_ban value=%r",
            symbol,
            sell_ban,
        )
        return False

    # 注意銘柄は売禁とは別扱い。ここで止めると short_ok=1 でも全落ちする。
    is_attention = _get_first(flags, ("is_attention", "attention", "regulation_attention"), False)
    if _as_bool(is_attention, default=False):
        logger.warning(
            "[SELL_CREDIT_GUARD] WARN symbol=%s reason=is_attention_but_allowed value=%r sell_target=%r short_ok=%r credit_type=%s",
            symbol,
            is_attention,
            flags.get("sell_target"),
            flags.get("short_ok"),
            credit_type,
        )

    # --------------------------------------------------------
    # 高保証金率（踏み上げ地雷）
    # --------------------------------------------------------
    margin_rate = _get_first(
        flags,
        ("margin_rate", "required_margin_rate", "credit_margin_rate"),
        None,
    )
    if margin_rate is not None and margin_rate != "":
        try:
            if float(margin_rate) >= MAX_MARGIN_RATE:
                logger.info(
                    "[SELL_CREDIT_GUARD] NG symbol=%s reason=high_margin_rate margin_rate=%s",
                    symbol,
                    margin_rate,
                )
                return False
        except (TypeError, ValueError):
            logger.info(
                "[SELL_CREDIT_GUARD] NG symbol=%s reason=invalid_margin_rate margin_rate=%r",
                symbol,
                margin_rate,
            )
            return False

    logger.info(
        "[SELL_CREDIT_GUARD] OK symbol=%s short_sellable=%r sell_target=%r short_ok=%r credit_type=%s is_attention=%r source=symbol_flags_db_or_cache",
        symbol,
        short_sellable,
        flags.get("sell_target"),
        flags.get("short_ok"),
        credit_type,
        flags.get("is_attention"),
    )
    return True
