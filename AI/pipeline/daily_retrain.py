# ============================================================
# AI/pipeline/daily_retrain.py
# Ver1.0-FINAL
# ------------------------------------------------------------
# ✔ 学習CSV更新
# ✔ LightGBM 再学習
# ✔ threshold 自動更新
# ============================================================

import subprocess
import sys
from datetime import datetime

def run(cmd):
    print("▶", " ".join(cmd))
    subprocess.run(cmd, check=True)

def main():
    print("=" * 60)
    print("🚀 DAILY RETRAIN START", datetime.now())
    print("=" * 60)

    # --------------------------------------------------------
    # ① 学習CSV生成
    # --------------------------------------------------------
    run([
        sys.executable,
        "AI/build_train_df_from_tv_csv.py",
    ])

    # --------------------------------------------------------
    # ② モデル再学習
    # --------------------------------------------------------
    run([
        sys.executable,
        "AI/train_lgbm_by_timeframe.py",
    ])

    # --------------------------------------------------------
    # ③ threshold 自動更新
    # --------------------------------------------------------
    run([
        sys.executable,
        "AI/analysis/find_best_threshold.py",
    ])

    print("=" * 60)
    print("✅ DAILY RETRAIN DONE", datetime.now())
    print("=" * 60)

if __name__ == "__main__":
    main()
