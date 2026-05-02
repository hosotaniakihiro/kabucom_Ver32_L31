# ==========================================================
# File   : trading/signals/factors/tonosama.py
# Version: Ver1.0-PRODUCTION-TONOSAMA-DETECTOR
# ----------------------------------------------------------
# ✔ 殿様イナゴ検出
# ✔ volume / turnover / RSI / slope 判定
# ✔ DataFrame / dict 両対応
# ✔ NaN / inf 完全防御
# ✔ symbolname互換
# ✔ 高速・本番安定
# ==========================================================

from __future__ import annotations

import logging
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)

# ==========================================================
# 検出パラメータ
# ==========================================================

RSI_THRESHOLD = 80

VOLUME_THRESHOLD = 1_000_000

TURNOVER_THRESHOLD = 1_000_000_000

SLOPE_THRESHOLD = 0.001


# ==========================================================
# safe number
# ==========================================================

def _num(v, default=0):

    try:

        v = pd.to_numeric(v, errors="coerce")

        if pd.isna(v):
            return default

        if np.isinf(v):
            return default

        return float(v)

    except Exception:

        return default


# ==========================================================
# symbol表示
# ==========================================================

def _symbol_display(row):

    try:

        symbol = str(row.get("symbol", ""))

        name = row.get("symbolname", symbol)

        if name is None:
            name = symbol

        name = str(name).strip()

        if name == "" or name == "nan":
            name = symbol

        return symbol, name

    except Exception:

        return "UNKNOWN", "UNKNOWN"


# ==========================================================
# 殿様イナゴ判定
# ==========================================================

def is_tonosama(row):

    try:

        rsi = _num(row.get("rsi"))

        volume = _num(row.get("volume"))

        turnover = _num(row.get("turnover"))

        slope = _num(row.get("slope_atr_scaled"))

        if (
            rsi >= RSI_THRESHOLD
            and volume >= VOLUME_THRESHOLD
            and turnover >= TURNOVER_THRESHOLD
            and slope >= SLOPE_THRESHOLD
        ):
            return True

        return False

    except Exception:

        return False


# ==========================================================
# DataFrame検出
# ==========================================================

def detect_tonosama(df):

    """
    DataFrameから殿様イナゴ銘柄を抽出
    """

    results = []

    try:

        if df is None:
            return results

        if isinstance(df, pd.DataFrame) is False:
            return results

        if df.empty:
            return results

        for _, r in df.iterrows():

            if is_tonosama(r):

                symbol, name = _symbol_display(r)

                results.append({
                    "symbol": symbol,
                    "symbolname": name,
                    "rsi": _num(r.get("rsi")),
                    "volume": int(_num(r.get("volume"))),
                    "turnover": int(_num(r.get("turnover"))),
                    "close": _num(r.get("close")),
                })

        return results

    except Exception:

        logger.exception("detect_tonosama failed")

        return results


# ==========================================================
# シングル判定
# ==========================================================

def detect_row(row):

    """
    dict / series 1行判定
    """

    try:

        if row is None:
            return None

        if is_tonosama(row):

            symbol, name = _symbol_display(row)

            return {
                "symbol": symbol,
                "symbolname": name,
                "rsi": _num(row.get("rsi")),
                "volume": int(_num(row.get("volume"))),
                "turnover": int(_num(row.get("turnover"))),
                "close": _num(row.get("close")),
            }

        return None

    except Exception:

        logger.exception("detect_row failed")

        return None