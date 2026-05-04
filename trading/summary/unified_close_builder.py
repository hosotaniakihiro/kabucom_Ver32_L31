# ============================================================
# trading/summary/unified_close_builder.py
# ------------------------------------------------------------
# ✔ PUSH / Ranking / Yahoo を時間軸で合成
# ✔ close のみ使用（疑似1分足）
# ✔ 列名差異を完全吸収
# ✔ MA計算の土台
# ✔ ENTRY / EXIT では使用禁止
# ============================================================

import pandas as pd
from typing import Optional

# ------------------------------------------------------------
# PUSH close 列候補（将来変更耐性）
# ------------------------------------------------------------
PUSH_CLOSE_CANDIDATES = [
    "close_price",
    "close",
    "price",
    "last_price",
    "CurrentPrice",
]

def _detect_push_close_column(df: pd.DataFrame) -> Optional[str]:
    for c in PUSH_CLOSE_CANDIDATES:
        if c in df.columns:
            return c
    return None


def build_unified_close_1min(
    df_push: pd.DataFrame | None,
    df_ranking: pd.DataFrame | None,
    df_yahoo: pd.DataFrame | None,
) -> pd.DataFrame:
    """
    symbol / datetime 単位で close を統合
    優先順位: PUSH > RANKING > YAHOO
    """

    frames: list[pd.DataFrame] = []

    # ========================================================
    # PUSH
    # ========================================================
    if df_push is not None and not df_push.empty:
        if {"symbol", "datetime"}.issubset(df_push.columns):
            close_col = _detect_push_close_column(df_push)
            if close_col:
                p = df_push[["symbol", "datetime", close_col]].copy()
                p["symbol"] = p["symbol"].astype(str)
                p["datetime"] = pd.to_datetime(
                    p["datetime"], errors="coerce"
                ).dt.floor("T")
                p["close"] = pd.to_numeric(
                    p[close_col], errors="coerce"
                )
                p["source"] = "PUSH"
                frames.append(p[["symbol", "datetime", "close", "source"]])

    # ========================================================
    # RANKING（snapshot）
    # ========================================================
    if df_ranking is not None and not df_ranking.empty:
        if {"symbol", "snapshot_time", "current_price"}.issubset(df_ranking.columns):
            r = df_ranking.copy()
            r["symbol"] = r["symbol"].astype(str)
            r["datetime"] = pd.to_datetime(
                r["snapshot_time"], errors="coerce"
            ).dt.floor("T")
            r["close"] = pd.to_numeric(
                r["current_price"], errors="coerce"
            )
            r["source"] = "RANKING"
            frames.append(r[["symbol", "datetime", "close", "source"]])

    # ========================================================
    # YAHOO
    # ========================================================
    if df_yahoo is not None and not df_yahoo.empty:
        if {"symbol", "datetime", "close"}.issubset(df_yahoo.columns):
            y = df_yahoo.copy()
            y["symbol"] = y["symbol"].astype(str)
            y["datetime"] = pd.to_datetime(
                y["datetime"], errors="coerce"
            ).dt.floor("T")
            y["close"] = pd.to_numeric(
                y["close"], errors="coerce"
            )
            y["source"] = "YAHOO"
            frames.append(y[["symbol", "datetime", "close", "source"]])

    # ========================================================
    # 統合
    # ========================================================
    if not frames:
        return pd.DataFrame()

    df = pd.concat(frames, ignore_index=True)

    # 無効行除去
    df = df.dropna(subset=["symbol", "datetime", "close"])
    if df.empty:
        return pd.DataFrame()

    # 優先順位解決
    priority = {"PUSH": 0, "RANKING": 1, "YAHOO": 2}
    df["prio"] = df["source"].map(priority).fillna(9)

    df = (
        df.sort_values(["symbol", "datetime", "prio"])
          .groupby(["symbol", "datetime"], as_index=False)
          .first()
    )

    return df[["symbol", "datetime", "close", "source"]]
