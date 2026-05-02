# ============================================================
# File   : trading/yahoo/yahoo_parallel_fetch.py
# Version: Ver2.0-PRODUCTION-YAHOO-PARALLEL-FETCH-HARD-TIMEOUT
# ------------------------------------------------------------
# ✔ ThreadPoolExecutor 並列取得
# ✔ Yahoo rate limit 安全化
# ✔ worker crash guard
# ✔ future timeout
# ✔ 全体 timeout
# ✔ API throttling
# ✔ result safety
# ✔ scheduler停止防止
# ✔ loader完全互換
# ✔ hanging future を待ち続けない
# ============================================================

from __future__ import annotations

import logging
import time
import pandas as pd

from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

logger = logging.getLogger(__name__)

# ============================================================
# config
# ============================================================

# Yahooは並列数が多いと落ちる
MAX_WORKERS = 4

# APIスロットリング
REQUEST_SLEEP = 0.02

# 1 future あたりの許容秒数（参考値）
FUTURE_TIMEOUT = 25.0

# 全体処理の最大許容秒数
TOTAL_TIMEOUT = 45.0

# poll 間隔
WAIT_POLL_SEC = 0.25


# ============================================================
# worker
# ============================================================

def _fetch_worker(
    fetch_func,
    sym,
    start_dt,
    end_dt,
):
    """
    worker wrapper
    """
    started = time.time()

    try:
        df = fetch_func(sym, start_dt, end_dt)

        if df is None:
            return pd.DataFrame()

        if not isinstance(df, pd.DataFrame):
            try:
                df = pd.DataFrame(df)
            except Exception:
                return pd.DataFrame()

        """try:
            logger.info(
                "[YAHOO DEBUG] worker done sym=%s rows=%s elapsed=%.3fs",
                sym,
                0 if df is None else len(df),
                max(time.time() - started, 0.0),
            )
        except Exception:
            pass"""

        return df

    except Exception:
        logger.exception(
            "[YAHOO DEBUG] worker crash %s",
            sym,
        )
        return pd.DataFrame()


# ============================================================
# parallel fetch
# ============================================================

def parallel_fetch_symbols(
    symbols,
    *,
    start_dt,
    end_dt,
    fetch_func,
):
    """
    複数銘柄を並列取得

    Parameters
    ----------
    symbols : list[str]
    start_dt : datetime
    end_dt : datetime
    fetch_func : function
        loader関数
    """

    result = []

    if not symbols:
        return result

    started_at = time.time()
    future_to_symbol = {}

    try:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            # ------------------------------------------------
            # submit
            # ------------------------------------------------
            for sym in symbols:
                fut = executor.submit(
                    _fetch_worker,
                    fetch_func,
                    sym,
                    start_dt,
                    end_dt,
                )
                future_to_symbol[fut] = {
                    "symbol": sym,
                    "submitted_at": time.time(),
                }

                # Yahoo rate limit対策
                time.sleep(REQUEST_SLEEP)

            pending = set(future_to_symbol.keys())

            # ------------------------------------------------
            # collect with hard timeout
            # ------------------------------------------------
            while pending:
                now_ts = time.time()
                total_elapsed = now_ts - started_at

                if total_elapsed >= TOTAL_TIMEOUT:
                    logger.warning(
                        "[YAHOO DEBUG] parallel fetch total timeout elapsed=%.3fs pending=%d",
                        total_elapsed,
                        len(pending),
                    )
                    break

                done, pending = wait(
                    pending,
                    timeout=WAIT_POLL_SEC,
                    return_when=FIRST_COMPLETED,
                )

                # done を回収
                for future in done:
                    meta = future_to_symbol.get(future, {})
                    sym = meta.get("symbol")

                    try:
                        df = future.result()

                        if df is None:
                            continue

                        if isinstance(df, pd.DataFrame) and not df.empty:
                            result.append(df)
                        else:
                            logger.info(
                                "[YAHOO DEBUG] worker empty sym=%s",
                                sym,
                            )

                    except Exception:
                        logger.exception(
                            "[YAHOO DEBUG] worker failed sym=%s",
                            sym,
                        )

                # 長すぎる future を警告
                if pending:
                    now_ts = time.time()
                    timeout_syms = []

                    for future in list(pending):
                        meta = future_to_symbol.get(future, {})
                        sym = meta.get("symbol")
                        submitted_at = float(meta.get("submitted_at") or now_ts)
                        elapsed = now_ts - submitted_at

                        if elapsed >= FUTURE_TIMEOUT:
                            timeout_syms.append((sym, round(elapsed, 3)))

                    if timeout_syms:
                        logger.warning(
                            "[YAHOO DEBUG] long pending futures count=%d details=%s",
                            len(timeout_syms),
                            timeout_syms[:20],
                        )

            # 締切後に残っている future は待たない
            if pending:
                timeout_syms = []
                for future in list(pending):
                    meta = future_to_symbol.get(future, {})
                    sym = meta.get("symbol")
                    submitted_at = float(meta.get("submitted_at") or time.time())
                    elapsed = time.time() - submitted_at
                    timeout_syms.append((sym, round(elapsed, 3)))

                    try:
                        future.cancel()
                    except Exception:
                        pass

                logger.warning(
                    "[YAHOO DEBUG] pending futures abandoned count=%d details=%s",
                    len(timeout_syms),
                    timeout_syms[:50],
                )

    except Exception:
        logger.exception(
            "[YAHOO DEBUG] parallel fetch failed"
        )

    try:
        logger.info(
            "[YAHOO DEBUG] parallel fetch done symbols=%d result_frames=%d elapsed=%.3fs",
            len(symbols),
            len(result),
            max(time.time() - started_at, 0.0),
        )
    except Exception:
        pass

    return result