# ============================================================
# limit_price_utils.py（Ver1.0）
# ------------------------------------------------------------
# ・東証の値幅制限ルールに基づいてストップ高/ストップ安を計算
# ・前日終値(prev_close) から limit_up, limit_down を返す
# ・calculator.py / summary_controller.py / entry_controller.py で使用
# ============================================================

def calc_limit_prices(prev_close):
    """
    前日終値（prev_close）から東証の値幅制限に基づき
    ストップ高（limit_up）・ストップ安（limit_down）を計算する。

    Parameters
    ----------
    prev_close : float
        前営業日の終値

    Returns
    -------
    (limit_up, limit_down) : tuple(float, float)
    """

    try:
        price = float(prev_close)
    except Exception:
        return None, None

    # ================================
    # 東証値幅制限表（2025年現在）
    # ================================
    if price < 100:
        w = 30
    elif price < 200:
        w = 50
    elif price < 500:
        w = 80
    elif price < 700:
        w = 100
    elif price < 1000:
        w = 150
    elif price < 1500:
        w = 300
    elif price < 2000:
        w = 400
    elif price < 3000:
        w = 500
    elif price < 5000:
        w = 700
    elif price < 7000:
        w = 1000
    elif price < 10000:
        w = 1500
    elif price < 15000:
        w = 3000
    elif price < 20000:
        w = 4000
    elif price < 30000:
        w = 5000
    elif price < 50000:
        w = 7000
    else:
        w = 10000

    limit_up = price + w
    limit_down = price - w

    return limit_up, limit_down


# ============================================================
# DataFrame に limit_up / limit_down を一括付与
# ============================================================

def attach_limit_prices(df, prev_col="prev_close"):
    """
    DataFrame に limit_up / limit_down 列を追加する。
    prev_close（前日終値）を基準に自動計算。

    Parameters
    ----------
    df : pd.DataFrame
    prev_col : str
        前日終値を示すカラム名（デフォルト: "prev_close"）

    Returns
    -------
    df : pd.DataFrame
        limit_up / limit_down が追加された DataFrame
    """
    import pandas as pd

    if df is None or df.empty:
        return df

    if prev_col not in df.columns:
        raise KeyError(f"'{prev_col}' 列が DataFrame に存在しません")

    df["limit_up"], df["limit_down"] = zip(
        *df[prev_col].apply(calc_limit_prices)
    )

    return df
