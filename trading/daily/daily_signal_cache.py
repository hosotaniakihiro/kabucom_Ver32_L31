# ============================================================
# File   : trading/daily/daily_signal_cache.py
# Version: PRODUCTION-STABLE-REV1.0-STARTUP-CACHE
# ------------------------------------------------------------
# 目的:
#   daily_signal_loader.py で読んだ日足DB情報を
#   起動時に1回だけメモリへキャッシュする。
#
# 重要:
#   - 場中のAI gate / entry / exitではDBを読まない
#   - 起動時に stock_analysis_latest を1回読む
#   - 参照は dict lookup のみ
# ============================================================

from __future__ import annotations

import logging
import threading
from typing import Dict, Optional, Iterable, Any

import pandas as pd

from trading.daily.daily_signal_loader import (
    DAILY_DB_PATH,
    DailyDecision,
    build_daily_decision_map,
    _to_symbol4,
)

logger = logging.getLogger(__name__)


_LOCK = threading.RLock()
_DAILY_CACHE: Dict[str, DailyDecision] = {}
_CACHE_READY: bool = False
_CACHE_DB_PATH: str = DAILY_DB_PATH


def warmup_daily_signal_cache(
    db_path: str = DAILY_DB_PATH,
    symbols: Optional[Iterable[str]] = None,
    force: bool = False,
) -> int:
    """
    起動時に1回呼ぶ。

    symbols=None の場合:
      stock_analysis_latest 全銘柄を読む

    symbols指定ありの場合:
      指定銘柄だけ読む
    """

    global _DAILY_CACHE, _CACHE_READY, _CACHE_DB_PATH

    with _LOCK:
        if _CACHE_READY and not force:
            logger.info(
                "[DAILY CACHE] already ready symbols=%s db=%s",
                len(_DAILY_CACHE),
                _CACHE_DB_PATH,
            )
            return len(_DAILY_CACHE)

        logger.info("[DAILY CACHE] warmup start db=%s", db_path)

        try:
            dmap = build_daily_decision_map(
                symbols=symbols,
                db_path=db_path,
            )

            _DAILY_CACHE = dict(dmap)
            _CACHE_READY = True
            _CACHE_DB_PATH = db_path

            logger.info(
                "[DAILY CACHE] warmup done symbols=%s db=%s",
                len(_DAILY_CACHE),
                db_path,
            )

            return len(_DAILY_CACHE)

        except Exception as e:
            logger.exception("[DAILY CACHE] warmup failed db=%s err=%s", db_path, e)
            _DAILY_CACHE = {}
            _CACHE_READY = False
            return 0


def refresh_daily_signal_cache(
    db_path: str = DAILY_DB_PATH,
    symbols: Optional[Iterable[str]] = None,
) -> int:
    """
    手動更新用。
    日足DBを更新した後に、必要なら呼ぶ。
    """
    return warmup_daily_signal_cache(
        db_path=db_path,
        symbols=symbols,
        force=True,
    )


def is_daily_cache_ready() -> bool:
    with _LOCK:
        return bool(_CACHE_READY)


def get_daily_cache_size() -> int:
    with _LOCK:
        return len(_DAILY_CACHE)


def get_daily_decision(symbol: Any) -> Optional[DailyDecision]:
    sym = _to_symbol4(symbol)
    with _LOCK:
        return _DAILY_CACHE.get(sym)


def get_daily_decision_map_copy() -> Dict[str, DailyDecision]:
    with _LOCK:
        return dict(_DAILY_CACHE)


def attach_daily_decision_from_cache(
    df: pd.DataFrame,
    symbol_col: str = "symbol",
    fallback_load_if_empty: bool = True,
    filter_buy: bool = False,
) -> pd.DataFrame:
    """
    候補DFへ日足判定を付与する。
    DBは読まず、基本はメモリキャッシュだけを見る。

    fallback_load_if_empty=True:
      万一起動時キャッシュが未実行なら、その場で1回だけwarmupする。
    """

    if df is None or df.empty:
        return df

    if fallback_load_if_empty and not is_daily_cache_ready():
        logger.warning("[DAILY CACHE] not ready. fallback warmup now.")
        warmup_daily_signal_cache()

    out = df.copy()

    if symbol_col not in out.columns:
        if "stock_code" in out.columns:
            symbol_col = "stock_code"
        elif "code" in out.columns:
            symbol_col = "code"
        else:
            logger.warning("[DAILY CACHE] symbol column not found")
            return out

    out["daily_score"] = 0.0
    out["daily_buy_score"] = 0.0
    out["daily_sell_score"] = 0.0
    out["daily_ok_buy"] = False
    out["daily_ok_sell"] = False
    out["daily_exit_warn"] = False
    out["daily_reason"] = ""
    out["daily_date"] = ""

    hit = 0

    with _LOCK:
        cache = _DAILY_CACHE

        for idx, row in out.iterrows():
            sym = _to_symbol4(row.get(symbol_col, ""))
            dec = cache.get(sym)

            if dec is None:
                continue

            hit += 1
            out.at[idx, "daily_score"] = dec.daily_score
            out.at[idx, "daily_buy_score"] = dec.daily_buy_score
            out.at[idx, "daily_sell_score"] = dec.daily_sell_score
            out.at[idx, "daily_ok_buy"] = dec.daily_ok_buy
            out.at[idx, "daily_ok_sell"] = dec.daily_ok_sell
            out.at[idx, "daily_exit_warn"] = dec.daily_exit_warn
            out.at[idx, "daily_reason"] = dec.reason
            out.at[idx, "daily_date"] = dec.date

    logger.info(
        "[DAILY CACHE] attached rows=%s hit=%s cache_size=%s",
        len(out),
        hit,
        get_daily_cache_size(),
    )

    if filter_buy:
        before = len(out)
        out = out[out["daily_ok_buy"] == True].copy()
        logger.info("[DAILY CACHE] filter_buy before=%s after=%s", before, len(out))

    return out


def debug_daily_cache_sample(limit: int = 20) -> None:
    with _LOCK:
        items = list(_DAILY_CACHE.items())[:limit]

    logger.info("[DAILY CACHE DEBUG] ready=%s size=%s", _CACHE_READY, len(_DAILY_CACHE))

    for sym, dec in items:
        logger.info(
            "[DAILY CACHE DEBUG] %s %s date=%s score=%.2f buy=%.2f sell=%.2f ok_buy=%s exit_warn=%s reason=%s",
            sym,
            dec.name,
            dec.date,
            dec.daily_score,
            dec.daily_buy_score,
            dec.daily_sell_score,
            dec.daily_ok_buy,
            dec.daily_exit_warn,
            dec.reason,
        )