# ============================================================
# trading/ai/optimize_ranking_thresholds.py
# Ver: RANKING-THRESHOLD-OPTIMIZER-FINAL
# ------------------------------------------------------------
# ✔ ranking_ai_events から ENTRY 判定閾値を最適化
# ✔ OK / NG 両方の教師データを使用
# ✔ Precision / Recall / F1 を評価指標に採用
# ✔ Optuna による自動探索
# ============================================================

import sqlite3
import logging
from typing import List, Tuple

import optuna

from config.paths import get_path

logger = logging.getLogger(__name__)

# ============================================================
# DB
# ============================================================

DB_PATH = get_path("ai_entry_events_db")
TABLE = "ranking_ai_events"


# ============================================================
# データロード
# ============================================================

def load_ranking_ai_events() -> List[Tuple[int, int]]:
    """
    Returns
    -------
    list of (score, entry_ok)
    """
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute(
        f"""
        SELECT score, entry_ok
        FROM {TABLE}
        WHERE score IS NOT NULL
          AND entry_ok IS NOT NULL
        """
    )

    rows = cur.fetchall()
    conn.close()

    return [(int(score), int(ok)) for score, ok in rows]


# ============================================================
# 評価関数
# ============================================================

def evaluate_threshold(
    data: List[Tuple[int, int]],
    *,
    min_score: int,
) -> float:
    """
    F1-score を返す
    """

    tp = fp = fn = 0

    for score, ok in data:
        pred = int(score >= min_score)

        if pred == 1 and ok == 1:
            tp += 1
        elif pred == 1 and ok == 0:
            fp += 1
        elif pred == 0 and ok == 1:
            fn += 1

    precision = tp / max(tp + fp, 1)
    recall = tp / max(tp + fn, 1)

    if precision + recall == 0:
        return 0.0

    f1 = 2 * precision * recall / (precision + recall)
    return f1


# ============================================================
# Optuna objective
# ============================================================

def objective(trial: optuna.Trial) -> float:
    min_score = trial.suggest_int("min_score", 2, 8)

    data = load_ranking_ai_events()
    if not data:
        return 0.0

    f1 = evaluate_threshold(
        data,
        min_score=min_score,
    )

    return f1


# ============================================================
# 実行
# ============================================================

def run_optimize(
    *,
    n_trials: int = 50,
):
    logger.info("[OPTUNA] ranking threshold optimization started")

    study = optuna.create_study(direction="maximize")
    study.optimize(objective, n_trials=n_trials)

    best = study.best_params
    best_value = study.best_value

    logger.info("[OPTUNA] finished")
    logger.info(f"[OPTUNA] BEST PARAMS = {best}")
    logger.info(f"[OPTUNA] BEST F1     = {best_value:.4f}")

    print("====================================")
    print(" BEST RANKING THRESHOLD")
    print("====================================")
    print(f"min_score : {best['min_score']}")
    print(f"F1-score  : {best_value:.4f}")
    print("====================================")

    return best


# ============================================================
# CLI
# ============================================================

if __name__ == "__main__":
    run_optimize(n_trials=50)
