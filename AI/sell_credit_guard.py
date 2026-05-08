# ============================================================
# File: AI/sell_credit_guard.py
# Version: PRODUCTION-STABLE-V3-GLOBAL-SYMBOL-FLAGS-CACHE
# ------------------------------------------------------------
# 殿様イナゴ（SELL）専用 信用・売禁ガード
#
# ✔ 信用売り可否を最優先で判定
# ✔ 売禁・規制・高保証金率を完全遮断
# ✔ ENTRY ロジックとは完全独立
# ✔ True / False のみを返す純関数
# ✔ symbol 文字列 / dict / pandas.Series / object の型揺れで落ちない
# ✔ can_sell_symbol("4970") のような symbol のみ入力時は
#   起動時に global_data へ保持した symbol_flags_info_map を参照
# ✔ symbol_flags.sell_target / short_ok / credit_type=貸借銘柄 を信用売り可否に利用
# ============================================================

from __future__ import annotations

import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)


# ============================================================
# 固定パラメータ
# ============================================================

# 高保証金率とみなす閾値
MAX_MARGIN_RATE = 2.0   # 200% 以上は危険

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


def _load_flags_from_global_cache(symbol: str) -> Dict[str, Any]:
    symbol = _normalize_symbol(symbol)
    if not symbol or symbol == "-":
        return {}

    try:
        from global_state import global_data
    except Exception:
        return {}

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
    try:
        eligible = getattr(global_data, "symbol_flags_eligible_symbols", None)
        if eligible and symbol in eligible:
            return {"symbol": symbol, "sell_target": 1}
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
        "[SELL_CREDIT_GUARD] global flags used symbol=%s sell_target=%s short_ok=%s credit_type=%s",
        symbol,
        merged.get("sell_target"),
        merged.get("short_ok"),
        merged.get("credit_type"),
    )
    return merged


# ============================================================
# メイン API
# ============================================================

def can_sell_symbol(symbol_flags: Any, *, default: bool = False) -> bool:
    """
    SELL 殿様で信用売り可能かを判定する。

    symbol 文字列だけが渡された場合も、起動時に global_data へ保持した
    symbol_flags_info_map から sell_target / short_ok / credit_type を読んで判定する。
    """

    flags = _to_dict(symbol_flags)
    symbol = _pick_symbol(flags)
    flags = _merge_global_flags_if_needed(flags)
    symbol = _pick_symbol(flags)

    if not flags:
        logger.info(
            "[SELL_CREDIT_GUARD] NG symbol=%s reason=no_flags default=%s",
            symbol,
            default,
        )
        return bool(default)

    if set(flags.keys()) <= {"symbol"}:
        logger.info(
            "[SELL_CREDIT_GUARD] NG symbol=%s reason=symbol_only_no_short_sellable_info",
            symbol,
        )
        return bool(default)

    # --------------------------------------------------------
    # 信用売り可否
    # --------------------------------------------------------
    short_sellable = _get_first(
        flags,
        (
            "short_sellable",
            "short_ok",
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

    # --------------------------------------------------------
    # 売禁 / 注意銘柄
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

    is_attention = _get_first(flags, ("is_attention", "attention", "regulation_attention"), False)
    if _as_bool(is_attention, default=False):
        logger.info(
            "[SELL_CREDIT_GUARD] NG symbol=%s reason=is_attention value=%r",
            symbol,
            is_attention,
        )
        return False

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
        "[SELL_CREDIT_GUARD] OK symbol=%s short_sellable=%r sell_target=%r short_ok=%r credit_type=%s",
        symbol,
        short_sellable,
        flags.get("sell_target"),
        flags.get("short_ok"),
        credit_type,
    )
    return True
