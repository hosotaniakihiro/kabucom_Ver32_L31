import pandas as pd

CSV_PATH = "AI/logs/ai_pass_log.csv"

def main():
    df = pd.read_csv(CSV_PATH)

    # BLOCK かつ score >= 5
    df_ng = df[
        (df["result"] == "BLOCK") &
        (df["score"] >= 5)
    ]

    if df_ng.empty:
        print("✅ score高いのにBLOCKされたケースなし")
        return

    print("\n🚨 score高いのに BLOCK された銘柄 TOP20")
    print("=" * 70)

    summary = (
        df_ng.groupby(["symbol", "side", "stage"])
        .size()
        .reset_index(name="block_count")
        .sort_values("block_count", ascending=False)
        .head(20)
    )

    print(summary.to_string(index=False))

if __name__ == "__main__":
    main()
