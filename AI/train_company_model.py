# ============================================================
# AI/train_company_model.py
# ------------------------------------------------------------
# ✔ company_info 特徴量を用いた静的モデル学習
# ✔ 銘柄属性（規模・業種）専用
# ✔ paths.py 前提（Y:/ 直書き禁止）
# ============================================================

import logging
from pathlib import Path
import joblib
import pandas as pd
from lightgbm import LGBMClassifier
from sklearn.model_selection import train_test_split

from config.paths import get_path
from AI.company_info_loader import load_company_features

logger = logging.getLogger(__name__)

# ============================================================
# paths.py 経由
# ============================================================

MODEL_DIR: Path = get_path("ai_models") / "company"
MODEL_DIR.mkdir(parents=True, exist_ok=True)

MODEL_PATH: Path = MODEL_DIR / "company_model.pkl"

TEST_SIZE = 0.2
RANDOM_STATE = 42


# ============================================================
def main():
    df = load_company_features()

    if df.empty:
        logger.warning("⚠ company features empty")
        return

    # --------------------------------------------------------
    # 目的変数（仮：存在フラグ）
    # ※ 将来 pnl / 勝率等に差し替え可能
    # --------------------------------------------------------
    df["y"] = 1

    X = df.drop(columns=["symbol", "y"])
    y = df["y"]

    X_train, X_test, y_train, y_test = train_test_split(
        X,
        y,
        test_size=TEST_SIZE,
        random_state=RANDOM_STATE,
    )

    model = LGBMClassifier(
        n_estimators=200,
        learning_rate=0.05,
        max_depth=5,
        random_state=RANDOM_STATE,
    )

    model.fit(X_train, y_train)

    acc = model.score(X_test, y_test)
    logger.info(f"✅ company model trained acc={acc:.3f}")

    payload = {
        "model": model,
        "features": list(X.columns),
        "meta": {
            "rows": len(df),
            "accuracy": float(acc),
        },
    }

    joblib.dump(payload, MODEL_PATH)
    logger.info(f"💾 saved: {MODEL_PATH}")


# ============================================================
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
