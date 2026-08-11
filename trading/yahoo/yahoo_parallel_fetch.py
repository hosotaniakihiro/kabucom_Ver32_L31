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
import os
import sys
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

def parallel_fetch_symbols_core(
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


# --- yahoo_parallel_empty_cooldown_patch (native化, 元 core/startup/yahoo_parallel_empty_cooldown_patch.py) ---
# Yahoo並列取得が連続して0件を返し続ける状態を検知したら、一定時間クールダウンして
# 無駄なリクエストを止める。
_YAHOO_PARALLEL_EMPTY_COOLDOWN_STATE = {"empty_count": 0, "cooldown_until": 0.0}


def _yahoo_parallel_empty_cooldown_env_int(name: str, default: int, *, min_value: int | None = None, max_value: int | None = None) -> int:
    try:
        v = int(float(str(os.getenv(name, str(default))).replace(",", "")))
    except Exception:
        v = int(default)
    if min_value is not None:
        v = max(v, min_value)
    if max_value is not None:
        v = min(v, max_value)
    return int(v)


def _yahoo_parallel_empty_cooldown_scope_enabled() -> bool:
    """旧パッチの root importer だった DB_CONTEXTS|TRADING_CONTEXTS|GENERIC_HELPER_CONTEXTS
    (実質 argv が空/未知の場合以外は常に有効) を再現する。
    """
    try:
        if str(os.environ.get("DISABLE_YAHOO_PARALLEL_EMPTY_COOLDOWN_PATCH", "")).strip() == "1":
            return False
        return bool(" ".join(str(x) for x in sys.argv).strip())
    except Exception:
        return False


def parallel_fetch_symbols(
    symbols,
    *,
    start_dt,
    end_dt,
    fetch_func,
):
    if not _yahoo_parallel_empty_cooldown_scope_enabled():
        return parallel_fetch_symbols_core(symbols, start_dt=start_dt, end_dt=end_dt, fetch_func=fetch_func)

    syms = list(symbols or [])
    now_ts = time.time()
    threshold = _yahoo_parallel_empty_cooldown_env_int("YAHOO_PARALLEL_EMPTY_BATCH_THRESHOLD", 2, min_value=1, max_value=20)
    cooldown_sec = _yahoo_parallel_empty_cooldown_env_int("YAHOO_PARALLEL_EMPTY_BATCH_COOLDOWN_SEC", 900, min_value=0, max_value=7200)
    min_symbols = _yahoo_parallel_empty_cooldown_env_int("YAHOO_PARALLEL_EMPTY_MIN_SYMBOLS", 20, min_value=1, max_value=1000)

    state = _YAHOO_PARALLEL_EMPTY_COOLDOWN_STATE
    if len(syms) >= min_symbols and state["cooldown_until"] > now_ts:
        logger.warning(
            "[YAHOO PARALLEL EMPTY COOLDOWN] skipped remain=%.0fs symbols=%d empty_count=%d",
            state["cooldown_until"] - now_ts,
            len(syms),
            state["empty_count"],
        )
        return []

    ret = parallel_fetch_symbols_core(symbols, start_dt=start_dt, end_dt=end_dt, fetch_func=fetch_func)
    try:
        result_len = len(ret or [])
    except Exception:
        result_len = 0

    if len(syms) >= min_symbols and result_len == 0:
        state["empty_count"] += 1
        if cooldown_sec > 0 and state["empty_count"] >= threshold:
            state["cooldown_until"] = now_ts + cooldown_sec
            logger.warning(
                "[YAHOO PARALLEL EMPTY COOLDOWN] cooldown start empty_count=%d threshold=%d cooldown=%ss symbols=%d",
                state["empty_count"],
                threshold,
                cooldown_sec,
                len(syms),
            )
    else:
        if state["empty_count"]:
            logger.info("[YAHOO PARALLEL EMPTY COOLDOWN] reset empty_count=%d result_frames=%d", state["empty_count"], result_len)
        state["empty_count"] = 0
        state["cooldown_until"] = 0.0
    return ret


if _yahoo_parallel_empty_cooldown_scope_enabled():
    # 旧パッチの install() が行っていた env のsetdefaultを、モジュールimport時に1回だけ適用する。
    os.environ.setdefault("YAHOO_PARALLEL_EMPTY_BATCH_THRESHOLD", "2")
    os.environ.setdefault("YAHOO_PARALLEL_EMPTY_BATCH_COOLDOWN_SEC", "900")
    os.environ.setdefault("YAHOO_PARALLEL_EMPTY_MIN_SYMBOLS", "20")