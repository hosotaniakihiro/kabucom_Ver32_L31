import pandas as pd

CSV_PATH = "AI/logs/ai_pass_log.csv"

def main():
    df = pd.read_csv(CSV_PATH)

    # 時間帯 × stage × result
    grp = (
        df.groupby(["hour", "stage", "result"])
        .size()
        .unstack(fill_value=0)
        .reset_index()
    )

    grp["total"] = grp.get("PASS", 0) + grp.get("BLOCK", 0)
    grp["pass_rate"] = (grp.get("PASS", 0) / grp["total"] * 100).round(1)

    print("\n📊 時間帯別 AI 通過率")
    print("=" * 70)
    print(grp.sort_values(["stage", "hour"]).to_string(index=False))

if __name__ == "__main__":
    main()
