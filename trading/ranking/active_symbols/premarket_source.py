# ============================================================
# File   : trading/ranking/active_symbols/premarket_source.py
# Version: Ver1.0-ACTIVE-SYMBOLS-PREMARKET-SOURCE
# ============================================================
from __future__ import annotations
import datetime as dt, logging
from typing import Iterable, List, Optional, Set
import pandas as pd
from .config import ENABLE_PREMARKET_SBI, MIN_PRICE, PREMARKET_ALLOW_NO_PRICE, PREMARKET_END_HOUR, PREMARKET_END_MINUTE, PREMARKET_START_HOUR, PREMARKET_START_MINUTE, PRICE_COLUMNS
from .normalize import dedupe_keep_order, first_existing_col, normalize_symbol, today_ymd, to_float

logger = logging.getLogger(__name__)


def is_premarket_time(now: Optional[dt.datetime] = None) -> bool:
    n = now or dt.datetime.now()
    cur = n.hour * 60 + n.minute
    start = PREMARKET_START_HOUR * 60 + PREMARKET_START_MINUTE
    end = PREMARKET_END_HOUR * 60 + PREMARKET_END_MINUTE
    return start <= cur < end


def _normalize_premarket_df(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    if "symbol" not in out.columns:
        for c in ("code", "銘柄コード", "Symbol"):
            if c in out.columns:
                out["symbol"] = out[c]
                break
    if "symbol" not in out.columns:
        return pd.DataFrame()
    out["symbol"] = out["symbol"].map(normalize_symbol)
    out = out[out["symbol"].notna()].copy()
    price_col = first_existing_col(out, PRICE_COLUMNS)
    if price_col and "current_price" not in out.columns:
        out["current_price"] = pd.to_numeric(out[price_col], errors="coerce").fillna(0.0)
    return out


def load_premarket_dataframe(now: Optional[dt.datetime] = None) -> pd.DataFrame:
    if not ENABLE_PREMARKET_SBI:
        return pd.DataFrame()
    try:
        from trading.ranking.premarket_sbi_provider import load_premarket_sbi_dataframe
        ymd = today_ymd(now)
        df = load_premarket_sbi_dataframe(target_date=ymd)
        df = _normalize_premarket_df(df)
        logger.info("[ACTIVE PREMARKET] loaded SBI dataframe ymd=%s rows=%d cols=%s head=%s", ymd, len(df), list(df.columns), df["symbol"].head(20).tolist() if "symbol" in df.columns else [])
        return df
    except Exception:
        logger.debug("[ACTIVE PREMARKET] dataframe load failed, fallback to candidates", exc_info=True)
        return pd.DataFrame()


def load_premarket_symbols(now: Optional[dt.datetime] = None) -> List[str]:
    if not ENABLE_PREMARKET_SBI:
        return []
    df = load_premarket_dataframe(now=now)
    if not df.empty and "symbol" in df.columns:
        return dedupe_keep_order(df["symbol"].tolist())
    try:
        from trading.ranking.premarket_sbi_provider import load_premarket_sbi_candidates
        ymd = today_ymd(now)
        symbols = load_premarket_sbi_candidates(target_date=ymd)
        symbols = dedupe_keep_order(symbols)
        logger.info("[ACTIVE PREMARKET] loaded SBI symbols ymd=%s total=%d head=%s", ymd, len(symbols), symbols[:20])
        return symbols
    except ModuleNotFoundError as e:
        logger.warning("[ACTIVE PREMARKET] provider not found: %s", e)
        return []
    except Exception:
        logger.exception("[ACTIVE PREMARKET] load failed")
        return []


def filter_premarket_min_price(symbols: Iterable[str], *, now: dt.datetime, protected: Set[str]) -> List[str]:
    cleaned = dedupe_keep_order(symbols)
    if not cleaned:
        return []
    df = load_premarket_dataframe(now=now)
    if df.empty or "symbol" not in df.columns:
        if PREMARKET_ALLOW_NO_PRICE:
            logger.warning("[ACTIVE PREMARKET PRICE] no dataframe/price info -> allow symbols count=%d set ACTIVE_PREMARKET_ALLOW_NO_PRICE=0 to drop", len(cleaned))
            return cleaned
        logger.warning("[ACTIVE PREMARKET PRICE] no dataframe/price info -> drop all count=%d", len(cleaned))
        return []
    price_col = first_existing_col(df, ("current_price", "price", "現在値", "close", "close_price"))
    if not price_col:
        if PREMARKET_ALLOW_NO_PRICE:
            logger.warning("[ACTIVE PREMARKET PRICE] price column missing cols=%s -> allow symbols count=%d", list(df.columns), len(cleaned))
            return cleaned
        logger.warning("[ACTIVE PREMARKET PRICE] price column missing cols=%s -> drop all count=%d", list(df.columns), len(cleaned))
        return []
    work = df.copy()
    work["symbol"] = work["symbol"].map(normalize_symbol)
    work = work[work["symbol"].notna()].copy()
    work["__price__"] = pd.to_numeric(work[price_col], errors="coerce").fillna(0.0)
    price_map = dict(zip(work["symbol"], work["__price__"]))
    kept, removed = [], []
    for s in cleaned:
        if s in protected:
            kept.append(s)
            continue
        price = to_float(price_map.get(s), 0.0)
        if price >= MIN_PRICE:
            kept.append(s)
        else:
            removed.append(s)
    logger.info("[ACTIVE PREMARKET PRICE] before=%d after=%d removed=%d min_price=%.1f price_col=%s removed_head=%s", len(cleaned), len(kept), len(removed), MIN_PRICE, price_col, removed[:30])
    return kept
