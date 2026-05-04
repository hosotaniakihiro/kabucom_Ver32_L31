# ============================================================
# retrain_daily.py
# AI 日次自動再学習
# ============================================================

import subprocess
import sys

TASKS = [
    "AI/train/holdtime/build_holdtime_train_csv.py",
    "AI/train/holdtime/train_holdtime_lgbm.py",
    "AI/train/horizon/build_horizon_train_csv.py",
    "AI/train/horizon/train_horizon_lgbm.py",
]

def run(cmd):
    print(f"▶ {cmd}")
    subprocess.run([sys.executable, cmd], check=True)

def main():
    for t in TASKS:
        run(t)
    print("✅ DAILY RETRAIN COMPLETE")

if __name__ == "__main__":
    main()
