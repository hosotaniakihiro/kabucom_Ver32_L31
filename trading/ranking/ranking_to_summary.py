# ============================================================
# File   : trading/ranking/ranking_to_summary.py
# ------------------------------------------------------------
# ✔ ランキングデータを summary 互換バーに変換
# ✔ PUSH が無い銘柄の MA / AI 学習用データ補完
# ✔ OHLC = close 扱い（MA 用として十分）
# ✔ source='ranking' を明示
# ✔ DB 保存 or メモリ利用 両対応
# ============================================================

import logging
import datetime as dt
from typing import Dict, Any

logger = logging.getLogger(__name__)


# ============================================================
# ランキング1行 → summary互換 dict
# ============================================================
def ranking_to_summary_row(
    ranking_row: Dict[str, Any],
    *,
    interval: int,
    now: dt.datetime | None = None,
) -> Dict[str, Any]:
    """
    ランキング由来データを summary 1行分に変換する

    Parameters
    ----------
    ranking_row : dict
        ranking の 1 行（symbol / price / volume 等を含む）
    interval : int
        1 / 3 / 5（分足）
    now : datetime, optional
        datetime 指定（省略時は現在時刻）

    Returns
    -------
    dict
        summary 互換 row
    """

    symbol = ranking_row.get("symbol")
    price = ranking_row.get("price") or ranking_row.get("close")

    if symbol is None or price is None:
        raise ValueError(f"invalid ranking_row: {ranking_row}")

    if now is None:
        now = dt.datetime.now()

    # --------------------------------------------------------
    # 時刻丸め（interval 単位）
    # --------------------------------------------------------
    minute = (now.minute // interval) * interval
    bar_time = now.replace(
        minute=minute,
        second=0,
        microsecond=0,
    )

    # --------------------------------------------------------
    # summary 互換 row
    # --------------------------------------------------------
    row = {
        # identity
        "symbol": str(symbol),
        "datetime": bar_time,
        "date": bar_time.date(),
        "time": bar_time.time(),
        "start_time": bar_time.time(),
        "end_time": (bar_time + dt.timedelta(minutes=interval)).time(),

        # OHLCV（close 一本足）
        "open": float(price),
        "high": float(price),
        "low": float(price),
        "close": float(price),
        "volume": float(ranking_row.get("volume", 0) or 0),
        "turnover": float(ranking_row.get("turnover", 0) or 0),
        "vwap": float(ranking_row.get("vwap", price) or price),

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
        "is_limit_up": ranking_row.get("is_limit_up"),
        "is_limit_down": ranking_row.get("is_limit_down"),

        # meta
        "source": "ranking",
    }

    return row


# ============================================================
# DataFrame 用ラッパー
# ============================================================
def ranking_df_to_summary_rows(
    df,
    *,
    interval: int,
    now: dt.datetime | None = None
):
    """
    ranking DataFrame → summary rows（list[dict]）
    """
    if df is None or df.empty:
        return []

    rows = []

    for _, r in df.iterrows():
        try:
            row = ranking_to_summary_row(
                r.to_dict(),
                interval=interval,
                now=now,
            )
            rows.append(row)
        except Exception:
            logger.debug(
                "[ranking_to_summary] skip row: %s",
                r.to_dict(),
            )

    return rows
