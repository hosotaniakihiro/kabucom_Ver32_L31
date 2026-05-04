def build_ranking_features(
    df_rank: pd.DataFrame,
    now: pd.Timestamp,
    window_min: int = 15,
    max_rank: int = 50,
):
    df = df_rank.copy()

    df["rank_score"] = (max_rank + 1 - df["rank"]).clip(lower=0)

    features = {}

    features["rank_cnt"] = len(df)
    features["rank_best"] = df["rank"].min()
    features["rank_mean"] = df["rank"].mean()
    features["rank_score_sum"] = df["rank_score"].sum()

    return features
