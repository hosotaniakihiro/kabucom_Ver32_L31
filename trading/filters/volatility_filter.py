# ==========================================================
# trading/filters/volatility_filter.py
# ----------------------------------------------------------
# ✔ ENTRY前専用・副作用ゼロ
# ✔ ATR(1m) + 直近5分値幅 で「動く銘柄だけ」通す
# ✔ SUMMARY / RANKING 共通
# ✔ 説明可能（数値・閾値・理由を必ず返す）
# ==========================================================

import pandas as pd
from utils_common import safe_float


# ==========================================================
# ATR(1分) ベース 実効ボラフィルタ（唯一の正）
# ==========================================================
def atr_1m_filter(
    *,
    df_1m: pd.DataFrame,
    symbol: str,
    min_ratio: float = 0.0025,  # 0.25%
):
    """
    1分足 ATR ベースの実効ボラティリティ判定

    Returns
    -------
    (ng: bool, detail: dict)

    detail keys:
        - reason
        - atr
        - atr_ratio
        - min_ratio
        - price
        - bars
    """

    # --------------------------------------------------
    # DF チェック
    # --------------------------------------------------
    if df_1m is None or df_1m.empty:
        return True, {
            "reason": "1m未生成",
            "atr": None,
            "atr_ratio": None,
            "min_ratio": min_ratio,
            "price": None,
            "bars": 0,
        }

    d = df_1m[df_1m["symbol"] == symbol]
    bars = len(d)

    if bars < 15:
        return True, {
            "reason": "1m本数不足",
            "atr": None,
            "atr_ratio": None,
            "min_ratio": min_ratio,
            "price": None,
            "bars": bars,
        }

    # --------------------------------------------------
    # ATR(14) 計算
    # --------------------------------------------------
    highs = d["high_price"].values
    lows = d["low_price"].values
    closes = d["close_price"].values

    tr = []
    for i in range(1, bars):
        tr.append(
            max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
        )

    if len(tr) < 14:
        return True, {
            "reason": "ATR計算不可",
            "atr": None,
            "atr_ratio": None,
            "min_ratio": min_ratio,
            "price": None,
            "bars": bars,
        }

    atr = sum(tr[-14:]) / 14
    price = safe_float(closes[-1], 0)

    if atr <= 0 or price <= 0:
        return True, {
            "reason": "価格異常",
            "atr": atr,
            "atr_ratio": None,
            "min_ratio": min_ratio,
            "price": price,
            "bars": bars,
        }

    atr_ratio = atr / price

    # --------------------------------------------------
    # 判定
    # --------------------------------------------------
    if atr_ratio < min_ratio:
        return True, {
            "reason": "ATR不足",
            "atr": atr,
            "atr_ratio": atr_ratio,
            "min_ratio": min_ratio,
            "price": price,
            "bars": bars,
        }

    # OK
    return False, {
        "reason": "OK",
        "atr": atr,
        "atr_ratio": atr_ratio,
        "min_ratio": min_ratio,
        "price": price,
        "bars": bars,
    }


# ==========================================================
# 直近5分 高安幅 ボラフィルタ
# ==========================================================
def range_5m_filter(
    *,
    df_5m: pd.DataFrame,
    symbol: str,
    min_pct: float = 0.008,
):
    """
    直近5分足の高安値幅による実効ボラ判定

    Returns
    -------
    (ng: bool, detail: dict)

    detail keys:
        - reason
        - range
        - ratio
        - min_pct
        - price
    """

    if df_5m is None or df_5m.empty:
        return True, {
            "reason": "5m未生成",
            "range": None,
            "ratio": None,
            "min_pct": min_pct,
            "price": None,
        }

    d = df_5m[df_5m["symbol"] == symbol]
    if d.empty:
        return True, {
            "reason": "5mデータなし",
            "range": None,
            "ratio": None,
            "min_pct": min_pct,
            "price": None,
        }

    high = safe_float(d.iloc[-1].get("high_price"), 0)
    low = safe_float(d.iloc[-1].get("low_price"), 0)
    close = safe_float(d.iloc[-1].get("close_price"), 0)

    if high <= 0 or low <= 0 or close <= 0:
        return True, {
            "reason": "価格異常",
            "range": None,
            "ratio": None,
            "min_pct": min_pct,
            "price": close,
        }

    r = high - low
    ratio = r / close

    if ratio < min_pct:
        return True, {
            "reason": "RANGE不足",
            "range": r,
            "ratio": ratio,
            "min_pct": min_pct,
            "price": close,
        }

    return False, {
        "reason": "OK",
        "range": r,
        "ratio": ratio,
        "min_pct": min_pct,
        "price": close,
    }
