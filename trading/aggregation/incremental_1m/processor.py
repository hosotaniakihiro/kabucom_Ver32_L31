"""
============================================================
processor.py
Incremental1MEngine Tick Processor
------------------------------------------------------------
✔ PUSH tick 処理
✔ 1分バー生成
✔ open/high/low/close 更新
✔ volume 加算
✔ unconfirmed bar 保存
✔ finalize トリガー
✔ 異常価格防御
✔ datetime 安全化
✔ tick validation guard（NEW）
✔ price None guard（NEW）
✔ price <=0 guard（NEW）
✔ bar integrity guard（NEW）
✔ volume sanitize（NEW）
✔ debug logging 強化（NEW）
✔ HFT 本番安定版
============================================================
"""

from __future__ import annotations

import logging
import datetime as dt

from .utils import (
    safe_dt,
    safe_float,
    is_abnormal_price,
)

from trading.aggregation.unconfirmed_store import (
    upsert as save_unconfirmed,
)

from core.state.last_state_manager import last_state


logger = logging.getLogger(__name__)


# ============================================================
# INTERNAL HELPERS
# ============================================================

def _sanitize_price(price):

    try:

        if price is None:
            return None

        price = float(price)

        if price <= 0:
            return None

        return price

    except Exception:

        return None


def _sanitize_volume(v):

    try:

        v = float(v)

        if v < 0:
            return 0.0

        return v

    except Exception:

        return 0.0


def _validate_bar(bar):

    """
    bar integrity guard
    """

    try:

        o = _sanitize_price(bar.get("open_price"))
        h = _sanitize_price(bar.get("high_price"))
        l = _sanitize_price(bar.get("low_price"))
        c = _sanitize_price(bar.get("close_price"))

        if o is None or h is None or l is None or c is None:
            return False

        return True

    except Exception:

        return False


# ============================================================
# PROCESS ROW
# ============================================================

def process_row(engine, row: dict):
    """
    PUSH tick 処理

    Parameters
    ----------
    engine : Incremental1MEngine
    row : dict
        push_stream から渡される tick データ
    """

    try:

        symbol = str(row.get("symbol") or "").strip()

        if not symbol:
            return

        # --------------------------------------------------
        # datetime
        # --------------------------------------------------

        dt_tick = safe_dt(row.get("datetime"))

        if dt_tick is None:
            return

        minute = dt_tick.replace(
            second=0,
            microsecond=0
        )

        # --------------------------------------------------
        # price
        # --------------------------------------------------

        raw_price = (
            row.get("close_price")
            if row.get("close_price") is not None
            else row.get("price")
        )

        price = _sanitize_price(
            safe_float(raw_price)
        )

        if price is None:

            logger.debug(
                "[1M] invalid price skip %s %s",
                symbol,
                raw_price
            )

            return

        volume = _sanitize_volume(
            safe_float(row.get("volume"), 0.0)
        )

        # --------------------------------------------------
        # 異常価格防止
        # --------------------------------------------------

        if is_abnormal_price(price):

            logger.debug(
                "[1M] abnormal price skip %s %s",
                symbol,
                price
            )

            return

        # --------------------------------------------------
        # current bar
        # --------------------------------------------------

        cache = engine.current_bar_cache.get(symbol)

        # --------------------------------------------------
        # NEW BAR
        # --------------------------------------------------

        if not cache or cache.get("minute") != minute:

            # 旧バー finalize

            if cache:

                try:

                    if _validate_bar(cache):

                        engine.safe_finalize(symbol, cache)

                    else:

                        logger.debug(
                            "[1M] invalid bar skipped finalize %s",
                            symbol
                        )

                except Exception:

                    logger.exception(
                        "[1M] finalize trigger failed"
                    )

            new_bar = {
                "minute": minute,
                "open_price": price,
                "high_price": price,
                "low_price": price,
                "close_price": price,
                "volume": volume,
            }

            engine.current_bar_cache[symbol] = new_bar

            try:

                save_unconfirmed(
                    symbol,
                    new_bar
                )

            except Exception:

                logger.exception(
                    "[1M] unconfirmed save failed"
                )

            logger.debug(
                "[1M] new bar %s %s price=%s",
                symbol,
                minute,
                price
            )

        # --------------------------------------------------
        # UPDATE BAR
        # --------------------------------------------------

        else:

            try:

                cache_high = _sanitize_price(
                    cache.get("high_price")
                )

                cache_low = _sanitize_price(
                    cache.get("low_price")
                )

                if cache_high is None:
                    cache_high = price

                if cache_low is None:
                    cache_low = price

                cache["high_price"] = max(
                    cache_high,
                    price
                )

                cache["low_price"] = min(
                    cache_low,
                    price
                )

                cache["close_price"] = price

                cache["volume"] = (
                    _sanitize_volume(
                        cache.get("volume")
                    )
                    + volume
                )

            except Exception:

                logger.exception(
                    "[1M] bar update failed"
                )

            try:

                save_unconfirmed(
                    symbol,
                    cache
                )

            except Exception:

                logger.exception(
                    "[1M] unconfirmed update failed"
                )

        # --------------------------------------------------
        # last push 更新
        # --------------------------------------------------

        try:

            last_state.update_push(
                dt_tick
            )

        except Exception:

            logger.exception(
                "[1M] last_state update failed"
            )

    except Exception:

        logger.exception(
            "[1M] processor crashed"
        )