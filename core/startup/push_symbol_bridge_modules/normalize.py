# ============================================================
# File   : core/startup/push_symbol_bridge_modules/normalize.py
# Version: PRODUCTION-STABLE-REV3.0
# ------------------------------------------------------------
# Purpose:
#   - 銘柄コード正規化
#   - FILLER / None / nan / 空文字 / 不正コード除外
#   - DataFrame / dict / list / str などから銘柄コード抽出
# ============================================================

from __future__ import annotations

import logging
from typing import Any, Iterable, List, Optional

from .constants import DEFAULT_MAX_SYMBOLS

logger = logging.getLogger(__name__)


def is_filler_symbol(x: Any) -> bool:
    s = str(x).strip().upper()
    return (
        not s
        or s.startswith("FILLER")
        or s in {"NONE", "NULL", "NAN", "NA", "-", "0"}
    )


def is_real_symbol(x: Any) -> bool:
    if x is None:
        return False

    s = str(x).strip().upper()

    if is_filler_symbol(s):
        return False

    # 7203.T のような入力を許容するため、判定前に .T を外す
    if s.endswith(".T"):
        s = s[:-2]

    if s.endswith(".0"):
        s2 = s[:-2]
        if s2.isdigit():
            s = s2

    if not s.isalnum():
        return False

    # 日本株コード: 7203 / 130A / 147A / 438A などを想定
    if not (3 <= len(s) <= 5):
        return False

    return True


def normalize_symbol(x: Any) -> Optional[str]:
    if x is None:
        return None

    s = str(x).strip().upper()

    if s.endswith(".T"):
        s = s[:-2]

    if s.endswith(".0"):
        s2 = s[:-2]
        if s2.isdigit():
            s = s2

    if not is_real_symbol(s):
        return None

    return s


def dedupe_keep_order(items: Iterable[str]) -> List[str]:
    out: List[str] = []
    seen: set[str] = set()

    for x in items:
        s = str(x).strip().upper()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)

    return out


def clean_symbols(src: Any, *, limit: int = DEFAULT_MAX_SYMBOLS) -> List[str]:
    """
    任意の入力から実銘柄だけを抽出する。

    対応:
      - list / tuple / set
      - str
      - dict
      - pandas DataFrame / Series風オブジェクト
    """
    if src is None:
        return []

    # pandas DataFrame / Series 対応
    try:
        if hasattr(src, "columns"):
            cols = list(getattr(src, "columns", []))
            symbol_col = None
            for c in ("symbol", "Symbol", "code", "Code", "銘柄コード"):
                if c in cols:
                    symbol_col = c
                    break
            if symbol_col:
                src = src[symbol_col].tolist()
    except Exception:
        pass

    # dict 対応
    if isinstance(src, dict):
        for key in (
            "symbols",
            "codes",
            "items",
            "monitor_symbols",
            "active_symbols",
            "candidate_push_symbols",
            "push_candidate_symbols",
            "push_symbols_100",
            "push_symbols",
            "register_symbols",
            "ats_targets",
            "ats_register_targets",
            "data",
        ):
            if key in src and src[key]:
                src = src[key]
                break
        else:
            src = list(src.keys())

    if isinstance(src, str):
        src = [src]

    try:
        seq = list(src)  # type: ignore[arg-type]
    except Exception:
        return []

    out: List[str] = []
    filler = 0
    invalid = 0

    for x in seq:
        if is_filler_symbol(x):
            filler += 1
            continue

        s = normalize_symbol(x)
        if s:
            out.append(s)
        else:
            invalid += 1

    out = dedupe_keep_order(out)

    if filler or invalid:
        logger.info(
            "[PUSH SYMBOL BRIDGE] clean_symbols raw=%d real=%d filler=%d invalid=%d head=%s",
            len(seq),
            len(out),
            filler,
            invalid,
            out[:10],
        )

    return out[: int(limit or DEFAULT_MAX_SYMBOLS)]


__all__ = [
    "is_filler_symbol",
    "is_real_symbol",
    "normalize_symbol",
    "dedupe_keep_order",
    "clean_symbols",
]
