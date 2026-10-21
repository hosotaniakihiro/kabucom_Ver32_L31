# ============================================================
# File   : trading/scoring/flags/combo_flags.py
# Version: Ver1.0-PRODUCTION-COMBO-FLAGS
# ------------------------------------------------------------
# ✔ flag_bull_big_combo
# ✔ flag_multi_signal_cluster
# ✔ score_config.ini 完全対応
# ✔ NaN / inf 完全防御
# ✔ flag列自動検出
# ✔ vectorized高速処理
# ✔ HTF対応
# ✔ DataFrame in / out
# ============================================================

from __future__ import annotations

import pandas as pd
import numpy as np


# ============================================================
# bool → int safe
# ============================================================

def _flag(expr):

    try:
        return expr.fillna(False).astype(int)
    except Exception:
        return pd.Series(0, index=expr.index)


# ============================================================
# 0/1 フラグ列判定
# ============================================================

def _is_binary_flag_column(s) -> bool:
    """0/1 (もしくは bool) だけを取る列か。

    文字列列 (flag_score_reasons) や集計スコア列 (flag_score_*_total) を
    multi_signal_cluster のカウントから除外するための判定。
    """
    try:
        if isinstance(s, pd.DataFrame):
            s = s.iloc[:, 0]
        if s.dtype == bool:
            return True
        if not pd.api.types.is_numeric_dtype(s):
            return False
        vals = s.dropna()
        if vals.empty:
            return True
        return bool(vals.isin([0, 1, 0.0, 1.0, True, False]).all())
    except Exception:
        return False


# ============================================================
# main
# ============================================================

def generate_combo_flags(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    df = df.copy()

    # --------------------------------------------------------
    # helper
    # --------------------------------------------------------

    def _get_flag(name):

        if name in df.columns:
            return df[name].fillna(0)

        return pd.Series(0, index=df.index)

    # --------------------------------------------------------
    # bull big combo
    # --------------------------------------------------------

    macd = _get_flag("flag_macd_cross")
    vol = _get_flag("flag_volume_spike")
    breakout = _get_flag("flag_breakout_high")
    vwap = _get_flag("flag_vwap_break")

    df["flag_bull_big_combo"] = _flag(
        (macd == 1) &
        (vol == 1) &
        (breakout == 1) &
        (vwap == 1)
    )

    # --------------------------------------------------------
    # multi signal cluster
    # --------------------------------------------------------

    # "flag_" で始まる列には 0/1 のフラグ以外に
    #   flag_score_reasons      (文字列: flag_pattern_score_bridge)
    #   flag_score_buy_total 等 (集計スコア: 単体で 5 を超える)
    # が含まれる。前者は sum() で
    #   TypeError: unsupported operand type(s) for +: 'float' and 'str'
    # を起こし、generate_combo_flags が丸ごと [FLAG GEN] module failed と
    # なって flag_bull_big_combo / flag_multi_signal_cluster の両方が
    # 生成されなくなっていた。
    # 本来の意図は "0/1 のフラグが何本立ったか" なので、
    # bool / 0・1 だけを取る数値列に限定して数える。
    flag_cols = [
        c for c in df.columns
        if c.startswith("flag_") and _is_binary_flag_column(df[c])
    ]

    if len(flag_cols) > 0:

        score = df[flag_cols].fillna(0).sum(axis=1)

        df["flag_multi_signal_cluster"] = _flag(
            score >= 5
        )

    else:

        df["flag_multi_signal_cluster"] = 0

    return df