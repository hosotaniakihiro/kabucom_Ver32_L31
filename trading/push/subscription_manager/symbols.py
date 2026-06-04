# ============================================================
# File   : trading/push/subscription_manager/symbols.py
# Version: V1.1-ALLOW-3DIGIT-ALPHA-SYMBOLS
# Function:
#   - symbol 正規化
#   - 重複除去
#   - symbol flatten / chunk 分割
#   - 明示入力から symbol 一覧へ変換
# ------------------------------------------------------------
# Notes:
#   - kabu Station / 東証の英字入り銘柄コードに対応する。
#   - 例: 280A, 286A, 147A, 201A, 280A.T
# ============================================================

from __future__ import annotations

from typing import Any, List, Optional, Sequence, Set


def safe_str(v: Any) -> str:
    try:
        return str(v).strip()
    except Exception:
        return ""


def normalize_symbol(x: Any) -> Optional[str]:
    if x is None:
        return None

    if isinstance(x, dict):
        for key in ("symbol", "code", "ticker", "security_code", "stock_code", "Symbol"):
            if key in x:
                return normalize_symbol(x.get(key))
        return None

    try:
        s = str(x).strip()
    except Exception:
        return None

    if not s:
        return None

    # Yahoo形式や表示名付き形式を登録用コードへ寄せる。
    # 例: 7203.T -> 7203 / 280A.T -> 280A / "7203 トヨタ" -> 7203
    if "." in s:
        s = s.split(".", 1)[0].strip()

    if " " in s:
        parts = [p for p in s.split() if p]
        if parts:
            s = parts[0]

    alnum = "".join(ch for ch in s if ch.isalnum()).upper()

    # 通常の4桁数字コード。
    if len(alnum) == 4 and alnum.isdigit():
        return alnum

    # 東証の英字入りコード。
    # 例: 280A, 286A など「数字3桁 + 英字1文字」を落とさない。
    if len(alnum) == 4 and alnum[:3].isdigit() and alnum[3].isalpha():
        return alnum

    # 既存互換: 4桁数字 + 英字1文字。
    if len(alnum) == 5 and alnum[:4].isdigit() and alnum[4].isalpha():
        return alnum

    # 既存互換: 余分な英字が付いた場合は先頭1文字だけ残す。
    if len(alnum) > 4 and alnum[:4].isdigit() and alnum[4:].isalpha():
        return (alnum[:4] + alnum[4:5]).upper()

    digits = "".join(ch for ch in s if ch.isdigit())

    if s.isdigit() and len(s) == 4:
        return s
    if len(digits) == 4:
        return digits

    return None


def dedupe_keep_order(symbols: Sequence[str]) -> List[str]:
    seen: Set[str] = set()
    out: List[str] = []
    for s in symbols:
        if not s:
            continue
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def limit_symbols(symbols: Sequence[str], max_symbols: int) -> List[str]:
    if max_symbols <= 0:
        return list(symbols)
    return list(symbols[:max_symbols])


def chunked(items: Sequence[str], size: int) -> List[List[str]]:
    if size <= 0:
        size = 50
    return [list(items[i:i + size]) for i in range(0, len(items), size)]


def flatten_symbols(value: Any) -> List[Any]:
    out: List[Any] = []

    if value is None:
        return out

    if isinstance(value, dict):
        for k, v in value.items():
            lk = str(k).lower()
            if lk in (
                "symbols",
                "items",
                "data",
                "rows",
                "active_symbols",
                "watch_symbols",
                "monitor_symbols",
                "codes",
                "target_symbols",
                "payload",
                "candidates",
            ):
                out.extend(flatten_symbols(v))
            else:
                out.append(k)
                out.extend(flatten_symbols(v))
        return out

    if isinstance(value, (list, tuple, set)):
        for x in value:
            out.extend(flatten_symbols(x))
        return out

    out.append(value)
    return out


def collect_symbols_from_explicit(symbols: Any) -> List[str]:
    raw = flatten_symbols(symbols)
    normalized = [normalize_symbol(x) for x in raw]
    return [s for s in normalized if s]
