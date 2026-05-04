# ============================================================
# daily_retrain.py
# AI 日次自動再学習パイプライン（STEP2-⑤ FINAL）
# ------------------------------------------------------------
# ✔ 引け後 / バッチ専用
# ✔ CSV生成 → 学習 を安全に順序実行
# ✔ 途中失敗しても他工程に影響しない
# ✔ タイムアウト付き（暴走防止）
# ✔ 学習後にモデルキャッシュを安全クリア
# ✔ SELL / HOLDTIME / HORIZON / FINAL 全対応
# ============================================================

import sys
import subprocess
import logging
from pathlib import Path
from datetime import datetime
import time

# ============================================================
# PATH / LOG
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parent
AI_DIR = PROJECT_ROOT / "AI"

LOG_DIR = AI_DIR / "logs"
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

logger = logging.getLogger("daily_retrain")

# ============================================================
# 実行タスク定義（順序厳守）
# ============================================================

TASKS = [

    # ========================================================
    # SELL / EXIT 系
    # ========================================================
    "AI/train/sell/build_sell_train_csv.py",
    "AI/train/sell/train_sell_lgbm_stop.py",
    "AI/train/sell/train_sell_lgbm_tp.py",
    "AI/train/sell/train_sell_lgbm_trail.py",

    # ========================================================
    # HOLDTIME
    # ========================================================
    "AI/train/holdtime/build_holdtime_train_csv.py",
    "AI/train/holdtime/train_holdtime_lgbm.py",

    # ========================================================
    # HORIZON
    # ========================================================
    "AI/train/horizon/build_horizon_train_csv.py",
    "AI/train/horizon/train_horizon_lgbm.py",

    # ========================================================
    # FINAL DECISION
    # ========================================================
    "AI/train/final/build_final_train_csv.py",
    "AI/train/final/train_final_decision_lgbm.py",
]

# ============================================================
# 実行制御パラメータ
# ============================================================

SCRIPT_TIMEOUT_SEC = 60 * 30   # 30分（1本あたり）

# ============================================================
# ユーティリティ
# ============================================================

def run_script(script_path: str) -> str:
    """
    単一スクリプト実行

    Returns
    -------
    str : "success" | "skipped" | "failed"
    """

    path = PROJECT_ROOT / script_path

    if not path.exists():
        logger.warning(f"⚠ SKIP (not found): {script_path}")
        return "skipped"

    logger.info(f"▶ START: {script_path}")
    start = time.time()

    try:
        subprocess.run(
            [sys.executable, str(path)],
            check=True,
            timeout=SCRIPT_TIMEOUT_SEC,
        )

        elapsed = time.time() - start
        logger.info(f"✅ SUCCESS: {script_path} ({elapsed:.1f}s)")
        return "success"

    except subprocess.TimeoutExpired:
        logger.error(f"⏰ TIMEOUT ({SCRIPT_TIMEOUT_SEC}s): {script_path}")
        return "failed"

    except subprocess.CalledProcessError as e:
        logger.error(f"❌ FAILED: {script_path}")
        logger.error(str(e))
        return "failed"

    except Exception:
        logger.exception(f"❌ ERROR: {script_path}")
        return "failed"


# ============================================================
# 学習後処理：モデルキャッシュクリア
# ============================================================

def clear_model_cache_safe():
    """
    学習後に推論用モデルキャッシュをクリア
    Runtime が起動していても安全
    """
    try:
        from AI.inference.model_loader import clear_model_cache
        clear_model_cache()
        logger.info("🧹 AI model cache cleared")
    except Exception as e:
        logger.warning(f"⚠ model cache clear skipped: {e}")


# ============================================================
# メイン
# ============================================================

def main():

    logger.info("=" * 80)
    logger.info("🚀 DAILY AI RETRAIN START")
    logger.info(f"   ROOT : {PROJECT_ROOT}")
    logger.info("=" * 80)

    stat = {
        "success": 0,
        "failed": 0,
        "skipped": 0,
    }

    for task in TASKS:
        result = run_script(task)
        stat[result] += 1

    # --------------------------------------------------------
    # 学習後処理
    # --------------------------------------------------------
    clear_model_cache_safe()

    logger.info("=" * 80)
    logger.info("🏁 DAILY AI RETRAIN FINISHED")
    logger.info(f"   SUCCESS : {stat['success']}")
    logger.info(f"   SKIPPED : {stat['skipped']}")
    logger.info(f"   FAILED  : {stat['failed']}")
    logger.info(f"   LOG     : {log_file}")
    logger.info("=" * 80)


# ============================================================
if __name__ == "__main__":
    main()
