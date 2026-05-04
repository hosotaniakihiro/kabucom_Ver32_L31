import pandas as pd
from ai_fusion import compute_fusion_score
from predict_mtf import predict_mtf

def backtest_fusion(df_all):
    results = []

    for i, row in df_all.iterrows():
        prob = predict_mtf(
            row["m1"], row["m3"], row["m5"],
            row["daily"]
        )

        mtf_score = row["mtf_score"]
        news = row["news_score"]
        sns = row["sns_score"]

        fus = compute_fusion_score(prob, mtf_score, news, sns,
                                   row["m1"], row["m3"], row["daily"], row["ranking"])

        pred = 1 if fus >= 80 else 0

        results.append({
            "fusion_score": fus,
            "prob_up": prob,
            "true": row["future_close"]
        })

    df = pd.DataFrame(results)

    df["correct"] = (df["fusion_score"] >= 80).eq(df["true"])
    acc = df["correct"].mean()

    print(f"📈 Fusion-AI Accuracy = {acc:.3f}")

    return df
