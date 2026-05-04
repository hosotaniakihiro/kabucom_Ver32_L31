import pandas as pd
import json

CSV_PATH = "AI/logs/ai_pass_log.csv"
OUT_PATH = "AI/config/timeband_ai_threshold.json"

TARGET_PASS_RATE = 0.30   # ← 理想の通過率
MIN_SAMPLES = 30

def main():
    df = pd.read_csv(CSV_PATH)

    # FINAL_AI だけ使う
    df = df[df["stage"] == "final_ai"]

    results = {}

    for hour in sorted(df["hour"].unique()):
        d = df[df["hour"] == hour]
        if len(d) < MIN_SAMPLES:
            continue

        d = d.sort_values("confidence", ascending=False)

        # 上から何%通せばいいか
        cutoff_idx = int(len(d) * TARGET_PASS_RATE)
        cutoff_idx = max(1, cutoff_idx)

        th = float(d.iloc[cutoff_idx]["confidence"])

        results[int(hour)] = round(th, 3)

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    print("✅ 時間帯別 AI 閾値生成完了")
    print(results)

if __name__ == "__main__":
    main()
