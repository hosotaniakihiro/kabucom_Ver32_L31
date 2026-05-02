# ============================================================
# File   : trading/yahoo/download/download_runner.py
# Version: Ver4.2-PRODUCTION-YAHOO-DOWNLOAD-RUNNER-RANKING-30MIN
# ------------------------------------------------------------
# ✔ complement_scheduler から対象銘柄解決 / download責務を分離
# ✔ periodic / startup 両方の start_map に対応
# ✔ ranking出現銘柄の重複排除
# ✔ trading.ranking.runtime_symbols と連携
# ✔ yahoo_backfill_status.db を正本として利用
# ✔ 場中再起動後も当日取得済み銘柄を復元可能
# ✔ grouped start_map download
# ✔ batch-first loader 前提で最適化
# ✔ Yahoo対象は ranking only
# ✔ 30分以上ランキングに出ていない銘柄を除外
# ✔ 日付付き backfill DB を明示的に使用
# ✔ production hardened
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
from collections import defaultdict
from typing import Callable, Iterable

import pandas as pd

from trading.yahoo.loader import load_multiple_symbols

logger = logging.getLogger(__name__)

# ------------------------------------------------------------
# config
# ------------------------------------------------------------

YAHOO_SYMBOL_CHUNK = 50
INCLUDE_TODAY_ALL_RANKINGS = True
YAHOO_MAX_SYMBOLS: int | None = None

# 互換値。対象抽出では使用しない。ranking_raw_1min 当日全銘柄を優先する。
YAHOO_RANKING_ACTIVE_TIMEOUT_MINUTES = 30


# ------------------------------------------------------------
# optional ranking runtime cache
# ------------------------------------------------------------
try:
    from trading.ranking.runtime_symbols import (
        ensure_ranking_symbol_cache,
        set_ranking_symbols,
        normalize_symbols,
        get_yahoo_active_symbols,
        prune_stale_yahoo_symbols,
    )
    _HAS_RANKING_CACHE = True
except Exception:  # pragma: no cover
    _HAS_RANKING_CACHE = False

    def ensure_ranking_symbol_cache(*args, **kwargs) -> None:
        return None

    def set_ranking_symbols(
        symbols: Iterable[object],
        *,
        filtered: bool = True,
        target_date=None,
        seen_at=None,
    ) -> set[str]:
        out: set[str] = set()
        for s in symbols or []:
            if s is None:
                continue
            ss = str(s).strip()
            if ss.endswith(".0"):
                ss = ss[:-2]
            if ss:
                out.add(ss)
        return out

    def normalize_symbols(symbols: Iterable[object]) -> set[str]:
        out: set[str] = set()
        for s in symbols or []:
            if s is None:
                continue
            ss = str(s).strip()
            if ss.endswith(".0"):
                ss = ss[:-2]
            if ss:
                out.add(ss)
        return out

    def get_yahoo_active_symbols(
        *,
        timeout_minutes: int = 30,
        now_dt=None,
        filtered_only: bool = True,
        exclude_backfilled: bool = False,
        max_symbols: int | None = None,
    ) -> set[str]:
        return set()

    def prune_stale_yahoo_symbols(*args, **kwargs) -> int:
        return 0


# ------------------------------------------------------------
# optional provider
# ------------------------------------------------------------
try:
    from trading.yahoo.symbol.yahoo_symbol_provider import get_yahoo_target_symbols
    _HAS_PROVIDER = True
except Exception:  # pragma: no cover
    _HAS_PROVIDER = False

    def get_yahoo_target_symbols(*args, **kwargs) -> list[str]:
        return []


# ------------------------------------------------------------
# optional backfill status db
# ------------------------------------------------------------
try:
    from trading.yahoo.storage.yahoo_backfill_status import (
        ensure_yahoo_backfill_status_db,
        compute_download_target_symbols,
        restore_backfilled_symbols_to_runtime,
    )
    _HAS_BACKFILL_DB = True
except Exception:  # pragma: no cover
    _HAS_BACKFILL_DB = False

    def ensure_yahoo_backfill_status_db(*args, **kwargs):
        return ""

    def compute_download_target_symbols(
        ranking_symbols,
        *,
        trade_date=None,
        base_dir=None,
    ) -> set[str]:
        out: set[str] = set()
        for s in ranking_symbols or []:
            if s is None:
                continue
            ss = str(s).strip()
            if ss.endswith(".0"):
                ss = ss[:-2]
            if ss:
                out.add(ss)
        return out

    def restore_backfilled_symbols_to_runtime(*args, **kwargs) -> int:
        return 0


# ============================================================
# symbol utility
# ============================================================

def chunk_symbols(symbols: list[str]):
    for i in range(0, len(symbols), YAHOO_SYMBOL_CHUNK):
        yield symbols[i:i + YAHOO_SYMBOL_CHUNK]


def sanitize_symbols(symbols: Iterable[object]) -> list[str]:
    if not symbols:
        return []

    clean: list[str] = []
    seen: set[str] = set()

    for s in symbols:
        if s is None:
            continue

        sym = str(s).strip()
        if sym.endswith(".0"):
            sym = sym[:-2]

        if not sym or sym in seen:
            continue

        seen.add(sym)
        clean.append(sym)

    return clean


# ============================================================
# target resolver
# ============================================================

def _resolve_symbols_from_provider(
    *,
    target_date: dt.date | None = None,
) -> list[str]:
    if not _HAS_PROVIDER:
        return []

    try:
        symbols = get_yahoo_target_symbols(
            max_symbols=YAHOO_MAX_SYMBOLS,
            include_today_all_rankings=INCLUDE_TODAY_ALL_RANKINGS,
            target_date=target_date,
            include_active=False,
            include_light=False,
            include_universe=False,
        )
        symbols = sanitize_symbols(symbols)

        logger.info(
            "[YAHOO TARGET] provider symbols=%d mode=ranking_only max_symbols=%s target_date=%s",
            len(symbols),
            YAHOO_MAX_SYMBOLS,
            target_date,
        )
        return symbols

    except TypeError:
        symbols = get_yahoo_target_symbols(max_symbols=YAHOO_MAX_SYMBOLS)
        symbols = sanitize_symbols(symbols)

        logger.warning(
            "[YAHOO TARGET] provider fallback mode=legacy symbols=%d max_symbols=%s",
            len(symbols),
            YAHOO_MAX_SYMBOLS,
        )
        return symbols

    except Exception:
        logger.exception("[YAHOO TARGET] provider resolve failed")
        return []


def resolve_target_symbols(
    *,
    target_date: dt.date | None = None,
    use_ranking_cache: bool = False,
) -> list[str]:
    """
    Yahoo補完対象銘柄を返す。

    Ver5.0修正:
      - runtime 30分銘柄を優先しない
      - yahoo_backfill_status success 済みでも除外しない
      - ranking_raw_1min の当日全銘柄を正本にする

    理由:
      backfill_status は「Yahoo 1分足取得済み」であり、
      「summary DB反映済み」ではないため、ここで除外すると
      保存済みYahoo 1分足からsummary DBへの再反映が走らない。
    """
    if target_date is None:
        target_date = dt.date.today()

    # ranking_raw_1min 当日全銘柄を最優先
    symbols = _resolve_symbols_from_provider(target_date=target_date)
    symbols = sanitize_symbols(symbols)

    if symbols:
        logger.info(
            "[YAHOO TARGET] resolved from ranking_raw all_day symbols=%d target_date=%s use_ranking_cache=%s status_filter=disabled",
            len(symbols),
            target_date,
            use_ranking_cache,
        )
    else:
        logger.warning(
            "[YAHOO TARGET] provider returned no symbols target_date=%s; trying runtime fallback",
            target_date,
        )

        runtime_symbols: list[str] = []
        if _HAS_RANKING_CACHE:
            try:
                ensure_ranking_symbol_cache(target_date=target_date)
                active_set = get_yahoo_active_symbols(
                    timeout_minutes=24 * 60,
                    now_dt=dt.datetime.now(),
                    filtered_only=True,
                    exclude_backfilled=False,
                    max_symbols=None,
                )
                runtime_symbols = sanitize_symbols(sorted(active_set))
            except Exception:
                logger.exception("[YAHOO TARGET] runtime fallback failed")

        symbols = runtime_symbols

    if not symbols:
        return []

    # runtime cache には登録だけ行う。DL対象からは除外しない。
    if use_ranking_cache and _HAS_RANKING_CACHE:
        try:
            ensure_ranking_symbol_cache(target_date=target_date)
            set_ranking_symbols(
                symbols,
                filtered=True,
                target_date=target_date,
                seen_at=dt.datetime.now(),
            )
        except Exception:
            logger.debug("[YAHOO TARGET] runtime cache register failed", exc_info=True)

    logger.info(
        "[YAHOO TARGET] final symbols=%d target_date=%s source=ranking_raw_all_day backfill_success_exclusion=False sample=%s",
        len(symbols),
        target_date,
        symbols[:20],
    )
    return symbols

# ============================================================
# download
# ============================================================

def download_symbols(
    symbols: list[str],
    start_dt: dt.datetime,
    end_dt: dt.datetime,
) -> pd.DataFrame:
    all_df: list[pd.DataFrame] = []

    symbols = sanitize_symbols(symbols)
    if not symbols:
        return pd.DataFrame()

    for chunk in chunk_symbols(symbols):
        try:
            logger.info(
                "[YAHOO CHUNK] download start=%s end=%s chunk_symbols=%d",
                start_dt,
                end_dt,
                len(chunk),
            )

            df = load_multiple_symbols(
                symbols=chunk,
                start_dt=start_dt,
                end_dt=end_dt,
                interval="1m",
            )

            if df is None:
                continue

            if not isinstance(df, pd.DataFrame):
                try:
                    df = pd.DataFrame(df)
                except Exception:
                    continue

            if df.empty:
                logger.info(
                    "[YAHOO CHUNK] empty result start=%s end=%s chunk_symbols=%d",
                    start_dt,
                    end_dt,
                    len(chunk),
                )
                continue

            logger.info(
                "[YAHOO CHUNK] fetched rows=%d symbols=%d start=%s end=%s",
                len(df),
                df["symbol"].nunique() if "symbol" in df.columns else -1,
                start_dt,
                end_dt,
            )

            all_df.append(df)

        except Exception:
            logger.exception(
                "Yahoo chunk download failed start=%s end=%s size=%d",
                start_dt,
                end_dt,
                len(chunk),
            )

    if not all_df:
        return pd.DataFrame()

    try:
        out = pd.concat(all_df, ignore_index=False)

        try:
            if "symbol" in out.columns and "datetime" in out.columns:
                out = out.drop_duplicates(subset=["symbol", "datetime"], keep="last")
            elif "symbol" in out.columns and "time" in out.columns:
                out = out.drop_duplicates(subset=["symbol", "time"], keep="last")
        except Exception:
            pass

        try:
            if "symbol" in out.columns and "time" in out.columns:
                out = out.sort_values(["symbol", "time"])
            elif "symbol" in out.columns and "datetime" in out.columns:
                out = out.sort_values(["symbol", "datetime"])
        except Exception:
            pass

        return out

    except Exception:
        logger.exception("Yahoo concat failed")
        return pd.DataFrame()


def download_by_start_map(
    symbols: list[str],
    end_dt: dt.datetime,
    start_map_builder: Callable[[list[str], dt.date, dt.datetime], dict[str, dt.datetime]],
    *,
    target_date: dt.date,
    log_prefix: str,
) -> pd.DataFrame:
    symbols = sanitize_symbols(symbols)
    if not symbols:
        return pd.DataFrame()

    start_map = start_map_builder(symbols, target_date, end_dt)

    grouped: dict[str, list[str]] = defaultdict(list)
    for sym, start_dt in start_map.items():
        try:
            if sym is None or start_dt is None:
                continue
            key = start_dt.strftime("%Y-%m-%d %H:%M:%S")
            grouped[key].append(str(sym).strip())
        except Exception:
            logger.debug(
                "[%s] invalid start_map row sym=%s start_dt=%s",
                log_prefix,
                sym,
                start_dt,
                exc_info=True,
            )

    all_df: list[pd.DataFrame] = []

    logger.info(
        "[%s] grouped start_map buckets=%d target_symbols=%d target_date=%s",
        log_prefix,
        len(grouped),
        len(symbols),
        target_date,
    )

    for start_key, group_symbols in grouped.items():
        try:
            start_dt = dt.datetime.strptime(start_key, "%Y-%m-%d %H:%M:%S")
            group_symbols = sanitize_symbols(group_symbols)

            if not group_symbols:
                continue

            if start_dt >= end_dt:
                logger.info(
                    "[%s] skip grouped download start>=end start=%s end=%s symbols=%d",
                    log_prefix,
                    start_dt,
                    end_dt,
                    len(group_symbols),
                )
                continue

            logger.info(
                "[%s] download %s -> %s symbols=%d",
                log_prefix,
                start_dt,
                end_dt,
                len(group_symbols),
            )

            df = download_symbols(group_symbols, start_dt, end_dt)
            if df is None or df.empty:
                logger.info(
                    "[%s] grouped download empty start=%s end=%s symbols=%d",
                    log_prefix,
                    start_dt,
                    end_dt,
                    len(group_symbols),
                )
                continue

            logger.info(
                "[%s] grouped download fetched rows=%d symbols=%d start=%s end=%s",
                log_prefix,
                len(df),
                df["symbol"].nunique() if "symbol" in df.columns else -1,
                start_dt,
                end_dt,
            )

            all_df.append(df)

        except Exception:
            logger.exception(
                "[%s] grouped download failed start=%s symbols=%d",
                log_prefix,
                start_key,
                len(group_symbols),
            )

    if not all_df:
        return pd.DataFrame()

    try:
        out = pd.concat(all_df, ignore_index=False)

        try:
            if "symbol" in out.columns and "datetime" in out.columns:
                out = out.drop_duplicates(subset=["symbol", "datetime"], keep="last")
            elif "symbol" in out.columns and "time" in out.columns:
                out = out.drop_duplicates(subset=["symbol", "time"], keep="last")
        except Exception:
            pass

        try:
            if "symbol" in out.columns and "time" in out.columns:
                out = out.sort_values(["symbol", "time"])
            elif "symbol" in out.columns and "datetime" in out.columns:
                out = out.sort_values(["symbol", "datetime"])
        except Exception:
            pass

        logger.info(
            "[%s] total grouped concat rows=%d symbols=%d",
            log_prefix,
            len(out),
            out["symbol"].nunique() if "symbol" in out.columns else -1,
        )

        return out

    except Exception:
        logger.exception("[%s] concat failed", log_prefix)
        return pd.DataFrame()


__all__ = [
    "resolve_target_symbols",
    "download_symbols",
    "download_by_start_map",
]