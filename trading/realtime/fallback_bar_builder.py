# ============================================================
# File   : trading/realtime/fallback_bar_builder.py
# ------------------------------------------------------------
# ✔ PUSH 停止時の疑似バー生成（最終安全弁）
# ✔ summary / MA / AI を止めないための最低限データ供給
# ✔ OHLC = last_close（価格固定）
# ✔ volume = 0（出来高なし）
# ✔ source='fallback' を明示
# ✔ ENTRY は別レイヤーで必ずブロックされる前提
# ============================================================

import datetime as dt
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


# ============================================================
# 内部ユーティリティ：時刻丸め
# ============================================================
def _floor_time(now: dt.datetime, interval: int) -> dt.datetime:
    """
    指定 interval（分）で時刻を切り下げ
    """
    minute = (now.minute // interval) * interval
    return now.replace(
        minute=minute,
        second=0,
        microsecond=0,
    )


# ============================================================
# フォールバックバー生成（単一）
# ============================================================
def build_fallback_bar(
    *,
    symbol: str,
    last_close: float,
    interval: int,
    now: Optional[dt.datetime] = None,
) -> Dict[str, Any]:
    """
    PUSH が完全に来ない場合の疑似 summary バーを生成する

    Parameters
    ----------
    symbol : str
        銘柄コード
    last_close : float
        直近の終値（DB / ranking 由来）
    interval : int
        1 / 3 / 5（分足）
    now : datetime, optional
        現在時刻（省略時は now）

    Returns
    -------
    dict
        summary 互換 row
    """

    if last_close is None:
        raise ValueError("last_close is required for fallback bar")

    if now is None:
        now = dt.datetime.now()

    bar_time = _floor_time(now, interval)
    end_time = bar_time + dt.timedelta(minutes=interval)

    row = {
        # identity
        "symbol": str(symbol),
        "datetime": bar_time,
        "date": bar_time.date(),
        "time": bar_time.time(),
        "start_time": bar_time.time(),
        "end_time": end_time.time(),

        # OHLCV（価格固定）
        "open": float(last_close),
        "high": float(last_close),
        "low": float(last_close),
        "close": float(last_close),
        "volume": 0.0,
        "turnover": 0.0,
        "vwap": float(last_close),

        # indicators（未計算）
        "rsi": None,
        "macd": None,
        "macd_signal": None,
        "macd_hist": None,
        "bb_upper": None,
        "bb_middle": None,
        "bb_lower": None,
        "atr": None,

        # flags
        "is_limit_up": False,
        "is_limit_down": False,

        # meta
        "source": "fallback",
    }

    logger.debug(
        "[FALLBACK_BAR] symbol=%s interval=%s close=%s time=%s",
        symbol,
        interval,
        last_close,
        bar_time,
    )

    return row


# ============================================================
# DataFrame / list 用ラッパー（複数銘柄）
# ============================================================
def build_fallback_bars(
    *,
    symbols: list[str],
    last_close_map: Dict[str, float],
    interval: int,
    now: Optional[dt.datetime] = None,
):
    """
    複数銘柄分の fallback bar をまとめて生成する

    Parameters
    ----------
    symbols : list[str]
        対象銘柄
    last_close_map : dict
        symbol -> last_close
    interval : int
        1 / 3 / 5
    now : datetime, optional

    Returns
    -------
    list[dict]
        summary 互換 rows
    """

    rows = []

    for sym in symbols:
        last_close = last_close_map.get(sym)
        if last_close is None:
            continue

        try:
            row = build_fallback_bar(
                symbol=sym,
                last_close=last_close,
                interval=interval,
                now=now,
            )
            rows.append(row)
        except Exception:
            logger.debug(
                "[FALLBACK_BAR] skip symbol=%s", sym
            )

    return rows
