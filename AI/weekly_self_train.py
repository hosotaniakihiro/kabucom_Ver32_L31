# ============================================================
# pj/ai/weekly_self_train.py
# ------------------------------------------------------------
# 🧠 TONOSAMA 自己進化・週末完全自動再学習
# ------------------------------------------------------------
# ✔ 学習ETL / クラスタ再計算 / AI再学習 / Optuna最適化
# ✔ 本番トレードロジックと完全分離
# ✔ 週末・市場停止中のバッチ実行専用
# ============================================================

import subprocess
import logging
from pathlib import Path
from datetime import datetime

# ============================================================
# 設定
# ============================================================

PYTHON = "python"

TASKS = [
    # --------------------------------------------------------
    # ① 学習CSV生成（TONOSAMAトレードログ → CSV）
    # --------------------------------------------------------
    "AI/train/export_tonosama_training_csv.py",

    # --------------------------------------------------------
    # ② 銘柄クラスタ再計算
    # --------------------------------------------------------
    "pj/ai/cluster_symbols.py",

    # --------------------------------------------------------
    # ③ Entry AI 再学習（即益・エントリー判定）
    # --------------------------------------------------------
    "pj/ai/train_tonosama_entry.py",

    # --------------------------------------------------------
    # ④ HoldTime AI 再学習（クラスタ別）
    # --------------------------------------------------------
    "pj/ai/train_holdtime_cluster.py",

    # --------------------------------------------------------
    # ⑤ Optuna によるクラスタ別閾値最適化
    # --------------------------------------------------------
    "pj/ai/optimize_cluster_params.py",
]

# ============================================================
# ログ設定
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("TONOSAMA_SELF_TRAIN")


# ============================================================
def run():
    start = datetime.now()
    logger.warning("🧠 TONOSAMA WEEKLY SELF TRAIN START")

    for cmd in TASKS:
        try:
            path = Path(cmd)
            if not path.exists():
                logger.error(f"❌ NOT FOUND: {cmd}")
                continue

            logger.warning(f"▶ RUN: {cmd}")
            subprocess.run(
                [PYTHON, cmd],
                check=True,
            )
            logger.warning(f"✅ DONE: {cmd}")

        except subprocess.CalledProcessError as e:
            logger.error(
                f"❌ FAILED (returncode={e.returncode}): {cmd}",
                exc_info=True,
            )

        except Exception as e:
            logger.error(
                f"❌ ERROR: {cmd} ({e})",
                exc_info=True,
            )

    elapsed = (datetime.now() - start).total_seconds()
    logger.warning(
        f"🎉 TONOSAMA SELF TRAIN FINISHED "
        f"(elapsed={elapsed:.1f}s)"
    )


# ============================================================
if __name__ == "__main__":
    run()
