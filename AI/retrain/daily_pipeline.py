# ============================================================
# AI/retrain/daily_pipeline.py
# ============================================================

import logging
from AI.build_risk_daily_stats import main as build_daily
from AI.optuna_risk_ai import objective
from AI.retrain.param_store import load_params, save_params
from AI.retrain.validator import validate_params
import optuna

logger = logging.getLogger("risk_retrain")


def run_daily_retrain():

    logger.info("🔁 RiskAI daily retrain start")

    # ① 日次集計
    build_daily()

    # ② 現在パラメータ
    old_data = load_params()
    old_params = old_data.get("params", {})

    # ③ Optuna
    study = optuna.create_study(direction="maximize")
    study.optimize(objective, n_trials=50)

    best_params = study.best_params
    best_score = study.best_value

    # ④ バリデーション
    if not validate_params(best_params, old_params):
        logger.warning("❌ RiskAI retrain rejected (validator)")
        return

    # ⑤ 保存
    save_params(best_params, best_score)

    logger.info(f"✅ RiskAI retrain success {best_params}")


if __name__ == "__main__":
    run_daily_retrain()
