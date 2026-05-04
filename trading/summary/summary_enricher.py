# ============================================================
# File   : trading/summary/summary_enricher.py
# ------------------------------------------------------------
# ✔ summary 1行（dict / Series）に MA を付与
# ✔ MA5 / MA25 / MA75 + 信頼度（conf）
# ✔ PUSH 非依存（DB 常駐 MA）
# ✔ ranking / fallback 補完を自動評価
# ✔ summary_controller から呼ばれる前提
# ============================================================

import logging
from typing import Dict, Any

from trading.indicators.ma_builder import build_all_ma

logger = logging.getLogger(__name__)


# ============================================================
# 単一 row（dict）に MA を付与
# ============================================================
def enrich_ma_row(
    row: Dict[str, Any],
    *,
    interval: int,
) -> Dict[str, Any]:
    """
    summary の1行（dict）に MA を付与する

    Parameters
    ----------
    row : dict
        summary 1行分（symbol / datetime / close_price 等を含む）
    interval : int
        1 / 3 / 5（分足）

    Returns
    -------
    dict
        MA・信頼度を追加した row
    """

    symbol = row.get("symbol")
    dt = row.get("datetime")

    if not symbol or dt is None:
        logger.debug(
            "[enrich_ma_row] skip (symbol or datetime missing): %s",
            row
        )
        return row

    try:
        ma_info = build_all_ma(
            symbol=str(symbol),
            interval=interval,
            end_time=dt,
        )
    except Exception:
        logger.exception(
            "[enrich_ma_row] MA build failed: symbol=%s interval=%s",
            symbol,
            interval,
        )
        return row

    # MA / conf を row に反映
    row.update(ma_info)
    return row


# ============================================================
# DataFrame 全体に MA を付与
# ============================================================
def enrich_ma_df(
    df,
    *,
    interval: int,
):
    """
    summary DataFrame に MA を付与する
    ※ 内部で 1 行ずつ DB 参照するため、diff_update 用

    Parameters
    ----------
    df : pd.DataFrame
        summary DataFrame
    interval : int
        1 / 3 / 5

    Returns
    -------
    pd.DataFrame
        MA 列を追加した DataFrame
    """

    if df is None or df.empty:
        return df

    rows = []

    for _, r in df.iterrows():
        row = r.to_dict()
        row = enrich_ma_row(row, interval=interval)
        rows.append(row)

    # DataFrame 再構築（列欠損防止）
    import pandas as pd
    return pd.DataFrame(rows)


# ============================================================
# latest_features 用の軽量 MA 抽出
# ============================================================
def extract_ma_features(row: Dict[str, Any]) -> Dict[str, Any]:
    """
    latest_features / AI 用に MA 系だけ抜き出す
    """
    keys = (
        "ma5", "ma25", "ma75",
        "ma5_conf", "ma25_conf", "ma75_conf",
    )

    return {k: row.get(k) for k in keys}
