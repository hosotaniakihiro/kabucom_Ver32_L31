# ============================================================
# File   : trading/entry/etf_guard.py
# ------------------------------------------------------------
# 2026-10-26 (ユーザー要望): ETF / ETN / REIT をエントリー候補にしない。
#
# 10/26 は SUMMARY_AI (score=36) の候補に 1366 (ｉＦｒｅｅＥＴＦ 日経ダブルインバース) が
# 出て、候補として PUSH の保護枠に入り続けた (7,000 円超の除外も素通り)。
# summary 側の filter_common_stock_rows は名前で ETF を落とすが、経路によっては通らない。
#
# 判定は銘柄名で行う。銘柄コードの頭 (etf_filter.ETF_PREFIX) は 1332 ニッスイ・
# 1333 マルハニチロなど普通株も含むので使わない。名前は全角を NFKC で半角に寄せてから見る。
# ENTRY_ETF_GUARD_ENABLED=0 で無効。
# ============================================================
from __future__ import annotations

import datetime as dt
import logging
import os
import re
import sqlite3
import unicodedata
from typing import Any, Optional

logger = logging.getLogger(__name__)

_KEYWORDS = ("ETF", "ETN", "REIT", "上場投信", "インデックス", "為替ヘッジ", "投資法人")
# 「リート」は アクリート (4395)・日本コンクリート工業 (5269) に含まれるので前の文字で除く。
# 略称の ETF (ハンセンブル・金２倍ブル・Ｊ４百ベア２) は末尾のブル/ベアで見るが、
# ビーエイブル (604A)・〜ケーブル などの社名は除く。
_PATTERNS = (re.compile(r"(?<![クア])リート"), re.compile(r"(?<![イーケテタ])(ブル|ベア)\d*$"))
_NAME_CACHE: dict[str, str] = {}


def enabled() -> bool:
    return str(os.getenv("ENTRY_ETF_GUARD_ENABLED", "1")).strip().lower() not in ("0", "false", "off", "no")


def is_etf_name(name: Any) -> bool:
    s = unicodedata.normalize("NFKC", str(name or "")).upper()
    return bool(s) and (any(k in s for k in _KEYWORDS) or any(p.search(s) for p in _PATTERNS))


def _name_from_push_db(symbol: str) -> str:
    try:
        from trading.push.push_db_location import push_db_path

        path = push_db_path(dt.date.today())
        if not path.exists():
            return ""
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1.0)
        try:
            row = con.execute(
                "SELECT symbolname FROM stream_data_raw WHERE symbol=? ORDER BY datetime DESC LIMIT 1",
                (symbol,),
            ).fetchone()
        finally:
            con.close()
        return str(row[0] or "") if row else ""
    except Exception:
        logger.debug("[ETF GUARD] push db name lookup failed symbol=%s", symbol, exc_info=True)
        return ""


def resolve_name(symbol: str, entry: Optional[dict] = None) -> str:
    """entry の名前 -> main の銘柄名マップ -> 当日の PUSH DB の順で探す。"""
    if isinstance(entry, dict):
        for k in ("symbolname", "symbol_name", "name"):
            v = entry.get(k)
            if v and str(v).strip() and str(v).strip() != symbol:
                return str(v)
    if symbol in _NAME_CACHE:
        return _NAME_CACHE[symbol]
    name = ""
    try:
        from core.global_context.context import get_symbol_name_map

        name = str((get_symbol_name_map() or {}).get(symbol) or "")
    except Exception:
        name = ""
    if not name:
        name = _name_from_push_db(symbol)
    if name:
        _NAME_CACHE[symbol] = name
    return name


def reject_reason(entry: Any) -> str:
    """ETF なら理由の文字列、そうでなければ空文字。"""
    if not enabled() or not isinstance(entry, dict):
        return ""
    symbol = str(entry.get("symbol") or "").strip()
    if symbol.endswith(".0"):
        symbol = symbol[:-2]
    if not symbol:
        return ""
    name = resolve_name(symbol, entry)
    return f"ETF_FUND:{name}" if is_etf_name(name) else ""
