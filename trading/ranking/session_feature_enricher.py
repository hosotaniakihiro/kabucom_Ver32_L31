# ============================================================
# File   : ranking/session_feature_enricher.py
# Version: Ver1.0.0-FINAL-RANKING-SESSION-FEATURE-ENRICHER
# ------------------------------------------------------------
# ✔ ranking_session に summary 乖離特徴量を付与
# ✔ MA25 / MA75 / VWAP / close との差分
# ✔ 欠損・NaN・ゼロ割 完全耐性
# ✔ pandas 単体 / 副作用なし
# ============================================================

import pandas as pd
import numpy as np


def attach_summary_gaps(
    df_sessions: pd.DataFrame,
    df_summary_latest: pd.DataFrame,
) -> pd.DataFrame:
    """
    ランキングセッションに summary 乖離特徴量を付与する

    Parameters
    ----------
    df_sessions : pd.DataFrame
        ranking/session_builder.py の出力
        必須カラム:
          - symbol
          - rank_close

    df_summary_latest : pd.DataFrame
        銘柄ごとの最新 summary（1分足）
        必須カラム:
          - symbol
          - ma25
          - ma75
          - vwap
          - close_price

    Returns
    -------
    pd.DataFrame
        summary乖離特徴量が追加された df_sessions
    """

    if df_sessions is None or df_sessions.empty:
        return df_sessions

    if df_summary_latest is None or df_summary_latest.empty:
        # summary が無ければ何も付けずに返す
        return df_sessions

    out = df_sessions.copy()

    # --------------------------------------------------------
    # summary 側を index 化
    # --------------------------------------------------------
    s = (
        df_summary_latest
        .copy()
        .assign(symbol=lambda x: x["symbol"].astype(str))
        .set_index("symbol")
    )

    # --------------------------------------------------------
    # 安全取得関数
    # --------------------------------------------------------
    def _safe_get(symbol: str, col: str):
        try:
            v = s.at[symbol, col]
            if v is None:
                return np.nan
            return float(v)
        except Exception:
            return np.nan

    # --------------------------------------------------------
    # summary 値を付与
    # --------------------------------------------------------
    for col in ["ma25", "ma75", "vwap", "close_price"]:
        out[col] = (
            out["symbol"]
            .astype(str)
            .map(lambda sym: _safe_get(sym, col))
        )

    # --------------------------------------------------------
    # 乖離計算（ゼロ割・NaN完全耐性）
    # --------------------------------------------------------
    def _safe_gap(price, base):
        try:
            if price is None or base is None:
                return np.nan
            if base == 0:
                return np.nan
            return price / base - 1.0
        except Exception:
            return np.nan

    out["d_ma25"] = out.apply(
        lambda r: _safe_gap(r["rank_close"], r["ma25"]),
        axis=1,
    )

    out["d_ma75"] = out.apply(
        lambda r: _safe_gap(r["rank_close"], r["ma75"]),
        axis=1,
    )

    out["d_vwap"] = out.apply(
        lambda r: _safe_gap(r["rank_close"], r["vwap"]),
        axis=1,
    )

    out["d_close"] = out.apply(
        lambda r: _safe_gap(r["rank_close"], r["close_price"]),
        axis=1,
    )

    return out