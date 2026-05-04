import pandas as pd

CSV_PATH = "AI/logs/ai_pass_log.csv"
OUT_PATH = "AI/train/block_reason_features.csv"

REASON_MAP = {
    "rule score too low": "block_rule_low",
    "final_ai_ng": "block_final_ai",
    "timeband": "block_timeband",
    "tonosama_select_ng": "block_tonosama",
}

def main():
    df = pd.read_csv(CSV_PATH)

    df_block = df[df["result"] == "BLOCK"].copy()

    for k in REASON_MAP.values():
        df_block[k] = 0

    for idx, r in df_block.iterrows():
        for key, col in REASON_MAP.items():
            if key in str(r["reason"]):
                df_block.at[idx, col] = 1

    df_block.to_csv(OUT_PATH, index=False)
    print("✅ BLOCK reason 特徴量生成完了")

if __name__ == "__main__":
    main()
