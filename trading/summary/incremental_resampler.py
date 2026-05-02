# ============================================================
# trading/summary/incremental_resampler.py
# Ver2.0-STREAM-3M-5M-DIFF-UNIFIED-PRODUCTION
# ------------------------------------------------------------
# ✔ 1分足 → 3分足差分生成（DataFrame版）
# ✔ 1分足 → 5分足差分生成（DataFrame版）
# ✔ オブジェクト配列版 build_3m 追加
# ✔ 全履歴resample不要
# ✔ O(1)生成
# ✔ intraday専用
# ✔ 欠損耐性
# ✔ 本番安全設計
# ============================================================

from __future__ import annotations
import pandas as pd
from typing import List, Dict, Any


# ============================================================
# DataFrame版（既存互換）
# ============================================================

def build_3m_from_last_3(df1m: pd.DataFrame) -> pd.DataFrame:
    """
    直近3本の1分足から3分足を1本生成（DataFrame版）
    """

    if df1m is None or len(df1m) < 3:
        return pd.DataFrame()

    last3 = df1m.tail(3)

    try:
        row = {
            "symbol": last3.iloc[-1]["symbol"],
            "datetime": last3.iloc[-1]["datetime"],
            "open_price": last3.iloc[0]["open_price"],
            "high_price": last3["high_price"].max(),
            "low_price": last3["low_price"].min(),
            "close_price": last3.iloc[-1]["close_price"],
            "volume": last3["volume"].sum(),
        }
    except Exception:
        return pd.DataFrame()

    return pd.DataFrame([row])


def build_5m_from_last_5(df1m: pd.DataFrame) -> pd.DataFrame:
    """
    直近5本の1分足から5分足を1本生成（DataFrame版）
    """

    if df1m is None or len(df1m) < 5:
        return pd.DataFrame()

    last5 = df1m.tail(5)

    try:
        row = {
            "symbol": last5.iloc[-1]["symbol"],
            "datetime": last5.iloc[-1]["datetime"],
            "open_price": last5.iloc[0]["open_price"],
            "high_price": last5["high_price"].max(),
            "low_price": last5["low_price"].min(),
            "close_price": last5.iloc[-1]["close_price"],
            "volume": last5["volume"].sum(),
        }
    except Exception:
        return pd.DataFrame()

    return pd.DataFrame([row])


# ============================================================
# 高速ストリーム用（オブジェクト配列版）
# ============================================================

def build_3m(last3: List[Any]) -> Dict[str, Any]:
    """
    オブジェクト3本から3分足を生成（超軽量版）
    last3: dequeやlistなどを想定
    各要素は .open .high .low .close .volume 属性を持つ想定
    """

    if last3 is None or len(last3) < 3:
        return {}

    try:
        return {
            "open_price": last3[0].open,
            "high_price": max(x.high for x in last3),
            "low_price": min(x.low for x in last3),
            "close_price": last3[-1].close,
            "volume": sum(x.volume for x in last3),
        }
    except Exception:
        return {}


def build_5m(last5: List[Any]) -> Dict[str, Any]:
    """
    オブジェクト5本から5分足を生成（超軽量版）
    """

    if last5 is None or len(last5) < 5:
        return {}

    try:
        return {
            "open_price": last5[0].open,
            "high_price": max(x.high for x in last5),
            "low_price": min(x.low for x in last5),
            "close_price": last5[-1].close,
            "volume": sum(x.volume for x in last5),
        }
    except Exception:
        return {}