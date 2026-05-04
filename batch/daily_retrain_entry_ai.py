# ============================================================
# batch/daily_retrain_entry_ai.py
# ------------------------------------------------------------
# ✔ 日次で ENTRY方式選択AIを再学習
# ✔ 完全自動（cron / TaskScheduler）
# ============================================================

import subprocess
import sys

PY = sys.executable


def run(cmd):
    print(f"▶ {cmd}")
    subprocess.run([PY, cmd], check=True)


def main():
    print("🚀 DAILY ENTRY AI RETRAIN START")

    run("AI/build_train_csv_entry_mode_selector.py")
    run("AI/train_lightgbm_mode_selector.py")

    print("✅ DAILY ENTRY AI RETRAIN DONE")


if __name__ == "__main__":
    main()
