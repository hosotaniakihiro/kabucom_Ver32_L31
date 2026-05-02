# ============================================================
# File   : ranking/session_builder.py
# Version: Ver1.0.0-FINAL-RANKING-SESSION-BUILDER
# ------------------------------------------------------------
# ✔ ranking_raw_1min から連続出現セッションを生成
# ✔ symbol × ranking_type × session_id
# ✔ ランキング価格の OHLC を生成
# ✔ 順位推移・改善度・勢いを算出
# ✔ pandas 単体 / 副作用なし
# ============================================================

import pandas as pd


def build_ranking_sessions(
    df_rank: pd.DataFrame,
    gap_allow: int = 0,
) -> pd.DataFrame:
    """
    ランキング連続出現セッションを生成する

    Parameters
    ----------
    df_rank : pd.DataFrame
        必須カラム:
          - dt            : datetime (timestamp)
          - symbol        : str
          - ranking_type  : str
          - rank          : int
          - price         : float

    gap_allow : int, default 0
        許容する欠損分数
        0 = 1分連続のみ同一セッション
        1 = 1分欠損までは同一セッション扱い

    Returns
    -------
    pd.DataFrame
        1行 = 1セッション
    """

    if df_rank is None or df_rank.empty:
        return pd.DataFrame()

    # --------------------------------------------------------
    # 前処理
    # --------------------------------------------------------
    df = df_rank.copy()

    # 型安全
    df["dt"] = pd.to_datetime(df["dt"], errors="coerce")
    df = df.dropna(subset=["dt"])

    df["symbol"] = df["symbol"].astype(str)
    df["ranking_type"] = df["ranking_type"].astype(str)

    # 並び替え（最重要）
    df = df.sort_values(
        ["symbol", "ranking_type", "dt"]
    )

    # --------------------------------------------------------
    # セッション切り判定
    # --------------------------------------------------------
    # 前回出現時刻
    df["prev_dt"] = (
        df.groupby(["symbol", "ranking_type"])["dt"]
        .shift(1)
    )

    # 分差
    df["gap_min"] = (
        df["dt"] - df["prev_dt"]
    ).dt.total_seconds().div(60)

    # 新セッション判定
    df["new_session"] = (
        df["prev_dt"].isna()
        | (df["gap_min"] > (1 + gap_allow))
    )

    # セッションID（symbol×type内の連番）
    df["session_id"] = (
        df.groupby(["symbol", "ranking_type"])["new_session"]
        .cumsum()
    )

    # --------------------------------------------------------
    # セッション集計
    # --------------------------------------------------------
    agg = (
        df.groupby(["symbol", "ranking_type", "session_id"])
        .agg(
            start_dt=("dt", "min"),
            end_dt=("dt", "max"),
            minutes=("dt", "count"),

            # 順位
            rank_first=("rank", "first"),
            rank_last=("rank", "last"),
            rank_best=("rank", "min"),
            rank_worst=("rank", "max"),

            # ランキング価格 OHLC
            rank_open=("price", "first"),
            rank_close=("price", "last"),
            rank_high=("price", "max"),
            rank_low=("price", "min"),
        )
        .reset_index()
    )

    if agg.empty:
        return agg

    # --------------------------------------------------------
    # 派生指標
    # --------------------------------------------------------
    # セッションリターン
    agg["rank_ret"] = (
        agg["rank_close"] / agg["rank_open"] - 1.0
    )

    # セッション内レンジ
    agg["rank_range"] = (
        agg["rank_high"] / agg["rank_low"] - 1.0
    )

    # 順位改善幅（正なら改善）
    agg["rank_improve"] = (
        agg["rank_first"] - agg["rank_best"]
    )

    # 順位の傾き（改善/悪化の速さ）
    agg["rank_slope"] = (
        (agg["rank_last"] - agg["rank_first"])
        / agg["minutes"].clip(lower=1)
    )

    return agg