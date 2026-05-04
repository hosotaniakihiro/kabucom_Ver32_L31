# ============================================================
# AI/optuna_risk_ai.py
# ============================================================

import optuna
import pandas as pd


DATA_PATH = "logs/risk_daily_stats.csv"


def evaluate(df: pd.DataFrame) -> float:
    """
    スコアが高いほど良い
    """

    total_pnl = df["total_pnl"].sum()
    worst_dd = df["max_dd"].min()
    stop_penalty = df["stop_count"].sum() * 0.2

    # 利益 + DD抑制 - STOP多発
    score = total_pnl + worst_dd * 1.5 - stop_penalty
    return score


def run_simulation(params: dict) -> pd.DataFrame:
    """
    ★ここで RiskAI パラメータを差し替えて
      既存バックテスト or Market Replay を流す
    """

    # ---- 擬似：既存 backtest を呼ぶ想定 ----
    # run_backtest(**params)

    df = pd.read_csv(DATA_PATH)
    return df


def objective(trial):

    params = {
        "max_loss_streak": trial.suggest_int("max_loss_streak", 2, 6),
        "max_intraday_dd": trial.suggest_float("max_intraday_dd", -0.05, -0.01),
        "cooldown_minutes": trial.suggest_int("cooldown_minutes", 10, 90),
    }

    df = run_simulation(params)
    return evaluate(df)


if __name__ == "__main__":

    study = optuna.create_study(direction="maximize")
    study.optimize(objective, n_trials=100)

    print("BEST SCORE:", study.best_value)
    print("BEST PARAMS:", study.best_params)
