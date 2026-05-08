# ============================================================
# File: AI/sell_credit_guard.py
# Version: PRODUCTION-STABLE-V2-SAFE-INPUT
# ------------------------------------------------------------
# 殿様イナゴ（SELL）専用 信用・売禁ガード
#
# ✔ 信用売り可否を最優先で判定
# ✔ 売禁・規制・高保証金率を完全遮断
# ✔ ENTRY ロジックとは完全独立
# ✔ True / False のみを返す純関数
# ✔ symbol 文字列 / dict / pandas.Series / object の型揺れで落ちない
# ✔ entry_controller から can_sell_symbol("4970") が来ても AttributeError を出さない
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
}


# ============================================================
# 内部ユーティリティ
# ============================================================

def _to_dict(value: Any) -> Dict[str, Any]:
    """
    dict / pandas.Series / str / int / object を安全に dict 化する。

    旧実装は dict 前提だったため、entry_controller から symbol 文字列だけが
    渡ると AttributeError: 'str' object has no attribute 'get' で落ちていた。
    """
    if value is None:
        return {}

    if isinstance(value, dict):
        return dict(value)

    # pandas.Series / dataclass 風オブジェクト対応
    if hasattr(value, "to_dict"):
        try:
            d = value.to_dict()
            if isinstance(d, dict):
                return dict(d)
        except Exception:
            pass

    # symbol だけが渡された場合
    if isinstance(value, (str, int)):
        s = str(value).strip()
        return {"symbol": s} if s else {}

    # 汎用オブジェクトから主要属性だけ吸収
    result: Dict[str, Any] = {}
    for key in (
        "symbol",
        "code",
        "stock_code",
        "short_sellable",
        "margin_sellable",
        "credit_sellable",
        "can_short",
        "is_shortable",
        "shortable",
        "sell_ban",
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
        v = flags.get(key)
        if v is not None and str(v).strip():
            return str(v).strip()
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


# ============================================================
# メイン API
# ============================================================

def can_sell_symbol(symbol_flags: Any, *, default: bool = False) -> bool:
    """
    SELL 殿様で信用売り可能かを判定する。

    Parameters
    ----------
    symbol_flags : Any
        推奨は dict。

        例:
        {
            "symbol": "4970",
            "short_sellable": True,
            "sell_ban": False,
            "margin_rate": 1.0,
        }

        ただし既存コード互換のため、"4970" のような symbol 文字列が来ても
        例外を出さず False を返す。

    default : bool
        情報不足時の戻り値。
        新規信用売りは危険側なので、デフォルトは False。

    Returns
    -------
    bool
        True  : SELL 可
        False : SELL 禁止 / 情報不足 / 規制あり
    """

    flags = _to_dict(symbol_flags)
    symbol = _pick_symbol(flags)

    if not flags:
        logger.info(
            "[SELL_CREDIT_GUARD] NG symbol=%s reason=no_flags default=%s",
            symbol,
            default,
        )
        return bool(default)

    # symbol 文字列だけでは信用売り可否は判定できない。
    # ここで例外を出さず、安全側に倒して False を返す。
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
            "margin_sellable",
            "credit_sellable",
            "can_short",
            "is_shortable",
            "shortable",
            "is_margin_sellable",
        ),
        default,
    )

    if not _as_bool(short_sellable, default=default):
        logger.info(
            "[SELL_CREDIT_GUARD] NG symbol=%s reason=not_short_sellable value=%r",
            symbol,
            short_sellable,
        )
        return False

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

    logger.debug("[SELL_CREDIT_GUARD] OK symbol=%s", symbol)
    return True
