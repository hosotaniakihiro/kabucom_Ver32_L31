# ============================================================
# File   : trading/yahoo/yahoo_symbol_utils.py
# Version: Ver1.1-PRODUCTION-YAHOO-SYMBOL-UTILS
# ------------------------------------------------------------
# ✔ symbol sanitize
# ✔ duplicate排除
# ✔ dtype normalize
# ✔ 空値除去
# ✔ order保持
# ✔ chunk utility
# ✔ Yahoo symbol suffix helper
# ✔ loader互換
# ✔ production safe
# ============================================================

from __future__ import annotations

from typing import Iterable, List, Generator


# ============================================================
# sanitize
# ============================================================

def sanitize_symbols(symbols: Iterable) -> List[str]:
    """
    銘柄コードリストを安全に正規化する

    - None除去
    - 空文字除去
    - str化
    - 重複削除（順序保持）
    """

    if not symbols:
        return []

    clean: List[str] = []

    for s in symbols:

        if s is None:
            continue

        s = str(s).strip()

        if not s:
            continue

        clean.append(s)

    # 重複削除（順序保持）
    return list(dict.fromkeys(clean))


# ============================================================
# Yahoo symbol normalize
# ============================================================

def ensure_yahoo_symbol(symbol: str) -> str:
    """
    Yahoo Finance 用銘柄コードへ変換

    例
    ----
    7203 → 7203.T
    7203.T → 7203.T
    """

    if symbol is None:
        return ""

    s = str(symbol).strip()

    if not s:
        return ""

    if s.endswith(".T"):
        return s

    return f"{s}.T"


# ============================================================
# Yahoo symbol list normalize
# ============================================================

def ensure_yahoo_symbols(symbols: Iterable) -> List[str]:
    """
    銘柄リストをYahoo形式へ変換
    """

    clean = sanitize_symbols(symbols)

    result: List[str] = []

    for s in clean:

        ys = ensure_yahoo_symbol(s)

        if ys:
            result.append(ys)

    return result


# ============================================================
# chunk utility
# ============================================================

def chunk_symbols(
    symbols: List[str],
    chunk_size: int
) -> Generator[List[str], None, None]:
    """
    銘柄リストを chunk に分割

    Yahoo API制限対策
    """

    if not symbols:
        return

    for i in range(0, len(symbols), chunk_size):

        yield symbols[i:i + chunk_size]


# ============================================================
# symbol statistics
# ============================================================

def symbol_count(symbols: Iterable) -> int:
    """
    銘柄数取得（safe）
    """

    try:
        return len(sanitize_symbols(symbols))
    except Exception:
        return 0


# ============================================================
# exports
# ============================================================

__all__ = [
    "sanitize_symbols",
    "ensure_yahoo_symbol",
    "ensure_yahoo_symbols",
    "chunk_symbols",
    "symbol_count",
]