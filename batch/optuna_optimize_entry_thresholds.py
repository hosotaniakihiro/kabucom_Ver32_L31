# ============================================================
# File   : batch/optuna_optimize_entry_thresholds.py
# ------------------------------------------------------------
# ✔ Optuna による ENTRY 閾値の重探索
# ✔ 市場終了後 / 週末実行前提
# ✔ backtest を何度も回すため trading 実行系とは完全分離
# ✔ 最良パラメータを entry_thresholds.json に反映
# ============================================================

import optuna
import json
import logging
from pathlib import Path

# ============================================================
# 設定
# ============================================================

CFG_PATH = Path("config/entry_thresholds.json")

# backtest 関数は「与えた閾値でスコアを返す」ことだけを保証すればOK
# 例:
# result = backtest(
#     min_ai_confidence=0.72,
#     min_volume_speed=6500,
# )
# return {"score": 1.234}
from backtest.run_backtest import backtest


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


# ============================================================
# Optuna 目的関数
# ============================================================
def objective(trial: optuna.Trial) -> float:
    """
    Optuna が探索する目的関数
    戻り値は「大きいほど良いスコア」
    """

    # ------------------------------
    # 探索パラメータ
    # ------------------------------
    min_ai_conf = trial.suggest_float(
        "MIN_AI_CONFIDENCE",
        0.60,
        0.85,
    )

    min_volume_speed = trial.suggest_int(
        "MIN_VOLUME_SPEED",
        3000,
        12000,
        step=500,
    )

    # ------------------------------
    # バックテスト実行
    # ------------------------------
    try:
        result = backtest(
            min_ai_confidence=min_ai_conf,
            min_volume_speed=min_volume_speed,
        )
    except Exception as e:
        # backtest が落ちた trial は最悪評価
        logger.error(f"[BACKTEST ERROR] {e}")
        return -1e9

    score = result.get("score")
    if score is None:
        return -1e9

    return float(score)


# ============================================================
# メイン
# ============================================================
def main():

    logger.info("🚀 Optuna ENTRY threshold optimization START")

    # ------------------------------
    # Study 作成
    # ------------------------------
    study = optuna.create_study(
        direction="maximize",
        study_name="entry_threshold_optimization",
    )

    # ------------------------------
    # 最適化実行
    # ------------------------------
    study.optimize(
        objective,
        n_trials=100,
        show_progress_bar=True,
    )

    best_params = study.best_params
    best_value = study.best_value

    logger.info(f"✅ BEST SCORE = {best_value}")
    logger.info(f"✅ BEST PARAMS = {best_params}")

    # ------------------------------
    # 既存 config 読み込み
    # ------------------------------
    if CFG_PATH.exists():
        with CFG_PATH.open("r", encoding="utf-8") as f:
            cfg = json.load(f)
    else:
        cfg = {}

    # ------------------------------
    # 更新（Optuna が決めた値のみ）
    # ------------------------------
    for k, v in best_params.items():
        cfg[k] = v

    # ------------------------------
    # 保存
    # ------------------------------
    CFG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CFG_PATH.open("w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)

    logger.info(f"💾 entry_thresholds.json updated: {CFG_PATH}")
    logger.info("🎉 Optuna optimization DONE")


# ============================================================
if __name__ == "__main__":
    main()
