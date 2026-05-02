# ============================================================
# File   : trading/summary/realtime_summary_update.py
# Created: 2026-01-21
# Ver    : 1.1-REALTIME-CACHE-ONLY-FINAL-SAFE
# ------------------------------------------------------------
# ✔ DB には一切触れない（cache 専用）
# ✔ confirmed ではない「進行中 1min」を構築
# ✔ push / tick / 5sec bar からリアルタイム反映
# ✔ summary_cache[1] を唯一の正本として更新
# ✔ incremental / initial と完全非干渉
# ✔ AI / scoring / entry が即時参照可能
# ✔ cache 欠損・破損を自己修復
# ============================================================

import logging
import pandas as pd
import datetime as dt

from global_state import global_data

logger = logging.getLogger(__name__)


# ============================================================
# 内部 util
# ============================================================

def _floor_minute(ts: dt.datetime) -> dt.datetime:
    """
    秒以下切り捨て（1min 未確定足）
    """
    return ts.replace(second=0, microsecond=0)


def _ensure_summary_cache():
    """
    summary_cache 構造を保証（自己修復）
    """
    if not hasattr(global_data, "summary_cache"):
        global_data.summary_cache = {}

    if 1 not in global_data.summary_cache:
        global_data.summary_cache[1] = {
            "hist": pd.DataFrame(),
            "latest": pd.DataFrame(),
            "last_dt": None,
        }

    cache = global_data.summary_cache[1]

    # hist 必須カラム補正
    required_cols = [
        "symbol",
        "datetime",
        "open_price",
        "high_price",
        "low_price",
        "close_price",
        "volume",
        "source",
        "interval",
        "interval_name",
        "confirmed",
    ]

    if not cache["hist"].empty:
        for c in required_cols:
            if c not in cache["hist"].columns:
                cache["hist"][c] = (
                    False if c == "confirmed" else None
                )

    return cache


# ============================================================
# realtime summary update（1min 未確定）
# ============================================================

def realtime_summary_update(
    *,
    symbol: str,
    price: float,
    volume: float | None,
    ts: dt.datetime,
):
    """
    秒足 / tick / 5sec bar から
    「進行中の 1min summary（未確定）」を更新する

    DB: ❌
    cache: ✅
    """

    # --------------------------------------------------------
    # 基本ガード
    # --------------------------------------------------------
    if not symbol:
        return
    if price is None or price <= 0:
        return
    if ts is None:
        return

    cache = _ensure_summary_cache()

    minute_dt = _floor_minute(ts)

    # --------------------------------------------------------
    # hist 取得
    # --------------------------------------------------------
    df_hist = cache["hist"]

    # --------------------------------------------------------
    # 既存行探索（symbol × minute）
    # --------------------------------------------------------
    if not df_hist.empty:
        mask = (
            (df_hist["symbol"] == symbol)
            & (df_hist["datetime"] == minute_dt)
        )
    else:
        mask = None

    # --------------------------------------------------------
    # 既存行あり → update
    # --------------------------------------------------------
    if mask is not None and mask.any():

        idx = df_hist[mask].index[-1]

        df_hist.at[idx, "high_price"] = max(
            float(df_hist.at[idx, "high_price"]),
            price,
        )
        df_hist.at[idx, "low_price"] = min(
            float(df_hist.at[idx, "low_price"]),
            price,
        )
        df_hist.at[idx, "close_price"] = price

        if volume is not None and not pd.isna(volume):
            df_hist.at[idx, "volume"] = (
                float(df_hist.at[idx, "volume"] or 0.0)
                + float(volume)
            )

    # --------------------------------------------------------
    # 新規 1min 行 → insert
    # --------------------------------------------------------
    else:
        new_row = {
            "symbol": symbol,
            "datetime": minute_dt,
            "open_price": price,
            "high_price": price,
            "low_price": price,
            "close_price": price,
            "volume": float(volume) if volume else 0.0,
            "source": "realtime",
            "interval": 1,
            "interval_name": "1min",
            "confirmed": False,   # ★ 未確定を明示
        }

        df_hist = pd.concat(
            [df_hist, pd.DataFrame([new_row])],
            ignore_index=True,
        )

    # --------------------------------------------------------
    # latest 更新（未確定含む）
    # --------------------------------------------------------
    df_latest = (
        df_hist
        .sort_values("datetime")
        .groupby("symbol")
        .tail(1)
        .reset_index(drop=True)
    )

    cache["hist"] = df_hist
    cache["latest"] = df_latest
    cache["last_dt"] = df_hist["datetime"].max()

    logger.debug(
        "[REALTIME] 1min updated symbol=%s price=%.2f dt=%s",
        symbol,
        price,
        minute_dt,
    )
