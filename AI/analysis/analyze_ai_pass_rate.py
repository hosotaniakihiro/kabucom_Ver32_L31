import pandas as pd

df = pd.read_csv("AI/logs/ai_pass_log.csv")

summary = (
    df
    .groupby(["time_bucket", "stage", "side"])
    .agg(
        total=("passed", "count"),
        passed=("passed", "sum"),
    )
)

summary["pass_rate"] = summary["passed"] / summary["total"]
print(summary.sort_index())
