# ============================================================
# daily_retrain.py
# AI 日次自動再学習パイプライン
# ------------------------------------------------------------
# ・引け後 / バッチ専用
# ・CSV生成 → 学習 → モデル更新 を順番に実行
# ・途中失敗しても次工程に影響しない
# ============================================================

import sys
import subprocess
import logging
from pathlib import Path
from datetime import datetime

# ============================================================
# LOG 設定
# ============================================================
LOG_DIR = Path("AI/logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)

log_file = LOG_DIR / f"daily_retrain_{datetime.now():%Y%m%d}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(log_file, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)

logger = logging.getLogger(__name__)

# ============================================================
# 実行タスク定義（順序厳守）
# ============================================================
TASKS = [
    # ----------------------------
    # SELL / EXIT 系
    # ----------------------------
    "AI/train/sell/build_sell_train_csv.py",
    "AI/train/sell/train_sell_lgbm_stop.py",
    "AI/train/sell/train_sell_lgbm_tp.py",
    "AI/train/sell/train_sell_lgbm_trail.py",

    # ----------------------------
    # HOLDTIME
    # ----------------------------
    "AI/train/holdtime/build_holdtime_train_csv.py",
    "AI/train/holdtime/train_holdtime_lgbm.py",

    # ----------------------------
    # HORIZON
    # ----------------------------
    "AI/train/horizon/build_horizon_train_csv.py",
    "AI/train/horizon/train_horizon_lgbm.py",

    # ----------------------------
    # FINAL DECISION
    # ----------------------------
    "AI/train/final/build_final_train_csv.py",
    "AI/train/final/train_final_decision_lgbm.py",
]

# ============================================================
# ユーティリティ
# ============================================================
def run_script(script_path: str) -> bool:
    """
    1スクリプト実行
    Returns:
        bool: 成功=True / 失敗=False
    """
    logger.info(f"▶ START: {script_path}")

    try:
        subprocess.run(
            [sys.executable, script_path],
            check=True,
        )
        logger.info(f"✅ SUCCESS: {script_path}")
        return True

    except subprocess.CalledProcessError as e:
        logger.error(f"❌ FAILED: {script_path}")
        logger.error(str(e))
        return False

    except Exception:
        logger.exception(f"❌ ERROR: {script_path}")
        return False


# ============================================================
# メイン
# ============================================================
def main():

    logger.info("=" * 80)
    logger.info("🚀 DAILY RETRAIN START")
    logger.info("=" * 80)

    success = 0
    failed = 0

    for task in TASKS:
        path = Path(task)
        if not path.exists():
            logger.warning(f"⚠ SKIP (not found): {task}")
            continue

        ok = run_script(task)
        if ok:
            success += 1
        else:
            failed += 1

    logger.info("=" * 80)
    logger.info("🏁 DAILY RETRAIN FINISHED")
    logger.info(f"   SUCCESS: {success}")
    logger.info(f"   FAILED : {failed}")
    logger.info(f"   LOG    : {log_file}")
    logger.info("=" * 80)


# ============================================================
if __name__ == "__main__":
    main()
