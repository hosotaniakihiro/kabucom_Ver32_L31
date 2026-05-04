import pandas as pd

CSV_PATH = "AI/logs/ai_pass_log.csv"

def main():
    df = pd.read_csv(CSV_PATH)

    funnel = (
        df.groupby(["stage", "result"])
        .size()
        .unstack(fill_value=0)
    )

    funnel["total"] = funnel.sum(axis=1)
    funnel["pass_rate"] = (funnel.get("PASS", 0) / funnel["total"] * 100).round(1)

    print("\n🧠 AI 判定ファネル")
    print("=" * 60)
    print(funnel.to_string())

if __name__ == "__main__":
    main()
