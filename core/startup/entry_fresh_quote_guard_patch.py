# ============================================================
# File   : core/startup/entry_fresh_quote_guard_patch.py
# Version: V1.0-FRESH-QUOTE-BEFORE-ORDER
# ------------------------------------------------------------
# SUMMARY計算後にエントリーまで遅延し、古いPUSH板/価格で
# 指値注文を出して約定しない問題を防ぐ runtime patch。
#
# 目的:
#   - 発注直前に global_data.push_df から最新Bid/Askを再取得
#   - quote datetime の鮮度を確認
#   - 古い板なら注文を出さずスキップ
#   - 新鮮な板なら BUY=ASK / SELL=BID に必ず差し替えて発注
#
# 環境変数:
#   ENTRY_FRESH_QUOTE_GUARD_ENABLED=1
#   ENTRY_MAX_QUOTE_AGE_SEC=8.0
#   ENTRY_FRESH_QUOTE_REQUIRE_LIMIT=1
#   ENTRY_FRESH_QUOTE_REQUIRE_MARKET=1
#
# 注意:
#   PUSHは50銘柄ローテーションのため、最大5秒程度の遅れは許容。
#   8秒超は株価とかけ離れた注文になりやすいため止める。
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)
_INSTALLED = False
_ORIG_BSE_GET_LATEST_BID_ASK = None


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok"}
    except Exception:
        return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _norm_symbol(v: Any) -> str:
    try:
        s = str(v or "").strip().upper()
        if s.endswith(".T"):
            s = s[:-2]
        if s.endswith(".0") and s[:-2].isdigit():
            s = s[:-2]
        return s
    except Exception:
        return ""


def _to_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        x = float(v)
        return float(default) if x != x else x
    except Exception:
        return float(default)


def _parse_dt(v: Any) -> Optional[dt.datetime]:
    if v is None:
        return None
    try:
        import pandas as pd
        t = pd.to_datetime(v, errors="coerce")
        if pd.isna(t):
            return None
        # pandas Timestamp -> python datetime
        if hasattr(t, "to_pydatetime"):
            t = t.to_pydatetime()
        if isinstance(t, dt.datetime):
            # timezone awareの場合はnaiveに揃える
            if t.tzinfo is not None:
                t = t.astimezone().replace(tzinfo=None)
            return t
    except Exception:
        pass
    return None


def _latest_push_quote(symbol: str) -> Optional[dict[str, Any]]:
    symbol = _norm_symbol(symbol)
    if not symbol:
        return None
    try:
        from global_state import global_data

        dfp = global_data.get_push_df()
        if dfp is None or getattr(dfp, "empty", True):
            return None

        df = dfp.copy()
        rename_map = {
            "Symbol": "symbol",
            "BidPrice": "bid_price",
            "AskPrice": "ask_price",
            "BidQty": "bid_qty",
            "AskQty": "ask_qty",
            "CurrentPrice": "current_price",
            "CurrentPriceTime": "datetime",
            "time": "datetime",
            "DateTime": "datetime",
            "datetime": "datetime",
        }
        for old, new in rename_map.items():
            if old in df.columns and new not in df.columns:
                df[new] = df[old]

        if "symbol" not in df.columns:
            return None
        try:
            df["_sym_norm"] = df["symbol"].map(_norm_symbol)
            df_sym = df[df["_sym_norm"] == symbol]
        except Exception:
            df_sym = df[df["symbol"].astype(str) == symbol]

        if df_sym.empty:
            return None

        dt_col = None
        for c in ("datetime", "CurrentPriceTime", "time", "timestamp", "updated_at"):
            if c in df_sym.columns:
                dt_col = c
                break

        row = None
        quote_dt = None
        if dt_col:
            import pandas as pd
            x = df_sym.copy()
            x["_quote_dt"] = pd.to_datetime(x[dt_col], errors="coerce")
            x2 = x.dropna(subset=["_quote_dt"])
            if not x2.empty:
                row = x2.sort_values("_quote_dt").iloc[-1]
                quote_dt = _parse_dt(row.get("_quote_dt"))
        if row is None:
            row = df_sym.iloc[-1]
            quote_dt = None

        ask = _to_float(row.get("ask_price"), 0.0)
        bid = _to_float(row.get("bid_price"), 0.0)
        cur = _to_float(row.get("current_price") or row.get("price") or row.get("CurrentPrice"), 0.0)
        if ask <= 0 or bid <= 0:
            return None

        now = dt.datetime.now()
        age_sec = None
        if quote_dt is not None:
            # 日付が無い時刻だけの場合、古い年になることがあるので当日補正
            if quote_dt.year < 2000:
                quote_dt = now.replace(hour=quote_dt.hour, minute=quote_dt.minute, second=quote_dt.second, microsecond=quote_dt.microsecond)
            age_sec = max(0.0, (now - quote_dt).total_seconds())

        return {
            "symbol": symbol,
            "ask_price": ask,
            "bid_price": bid,
            "ask_qty": int(_to_float(row.get("ask_qty"), 0.0)),
            "bid_qty": int(_to_float(row.get("bid_qty"), 0.0)),
            "current_price": cur,
            "quote_dt": quote_dt,
            "age_sec": age_sec,
            "source": "push_df_fresh_guard",
        }
    except Exception as e:
        logger.warning("[ENTRY FRESH QUOTE] read failed symbol=%s err=%s", symbol, e, exc_info=False)
        return None


def _fresh_quote(symbol: str, *, side: str) -> Optional[dict[str, Any]]:
    q = _latest_push_quote(symbol)
    max_age = _env_float("ENTRY_MAX_QUOTE_AGE_SEC", 8.0)
    if not q:
        logger.warning("[ENTRY FRESH QUOTE] SKIP no_quote symbol=%s side=%s", symbol, side)
        return None
    age = q.get("age_sec")
    # datetimeが取れない場合は危険なので、デフォルトでは古い扱いにする
    if age is None:
        if _env_bool("ENTRY_ALLOW_QUOTE_WITHOUT_TIMESTAMP", False):
            logger.warning("[ENTRY FRESH QUOTE] timestamp missing but allowed symbol=%s side=%s q=%s", symbol, side, q)
            return q
        logger.warning("[ENTRY FRESH QUOTE] SKIP no_timestamp symbol=%s side=%s q=%s", symbol, side, q)
        return None
    if float(age) > max_age:
        logger.warning(
            "[ENTRY FRESH QUOTE] SKIP stale_quote symbol=%s side=%s age=%.3fs max=%.3fs bid=%s ask=%s current=%s quote_dt=%s",
            symbol, side, float(age), max_age, q.get("bid_price"), q.get("ask_price"), q.get("current_price"), q.get("quote_dt"),
        )
        return None
    return q


def install() -> bool:
    global _INSTALLED, _ORIG_BSE_GET_LATEST_BID_ASK
    if _INSTALLED:
        return True
    if not _env_bool("ENTRY_FRESH_QUOTE_GUARD_ENABLED", True):
        logger.warning("[ENTRY FRESH QUOTE] disabled by env")
        return False

    try:
        import kabu_api.buy_sell_entry as bse
    except Exception as e:
        logger.warning("[ENTRY FRESH QUOTE] import buy_sell_entry failed err=%s", e, exc_info=False)
        return False

    _ORIG_BSE_GET_LATEST_BID_ASK = getattr(bse, "get_latest_bid_ask", None)

    def patched_get_latest_bid_ask(symbol: str):
        q = _fresh_quote(symbol, side="UNKNOWN")
        if not q:
            return None
        return {
            "symbol": q.get("symbol"),
            "ask_price": q.get("ask_price"),
            "bid_price": q.get("bid_price"),
            "ask_qty": q.get("ask_qty", 0),
            "bid_qty": q.get("bid_qty", 0),
            "current_price": q.get("current_price", 0.0),
            "age_sec": q.get("age_sec"),
            "quote_dt": q.get("quote_dt"),
            "source": q.get("source"),
        }

    # buy_sell_entry.py は `from utils_common import get_latest_bid_ask` で束縛しているため、
    # bse側の参照を直接差し替える。
    bse.get_latest_bid_ask = patched_get_latest_bid_ask

    _INSTALLED = True
    logger.warning(
        "[ENTRY FRESH QUOTE] installed max_age_sec=%.3f require_limit=%s require_market=%s",
        _env_float("ENTRY_MAX_QUOTE_AGE_SEC", 8.0),
        _env_bool("ENTRY_FRESH_QUOTE_REQUIRE_LIMIT", True),
        _env_bool("ENTRY_FRESH_QUOTE_REQUIRE_MARKET", True),
    )
    return True

try:
    install()
except Exception as e:
    logger.warning("[ENTRY FRESH QUOTE] auto install failed err=%s", e, exc_info=False)

__all__ = ["install"]
