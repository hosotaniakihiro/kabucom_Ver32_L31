# ============================================================
# TONOSAMA ENTRY AI 学習（LightGBM）
# ------------------------------------------------------------
# ・短期勝率最優先
# ・ランキング × 初動火力 特化
# ・推論側 tonosama_entry_lgbm.py / ai_boost.py と互換
# ============================================================

from pathlib import Path
import pandas as pd
import lightgbm as lgb
import joblib
from sklearn.model_selection import train_test_split
from sklearn.metrics import roc_auc_score, precision_score

# ============================================================
# BASE DIR
# ============================================================
BASE_DIR = Path(__file__).resolve().parents[2]

# ============================================================
# PATH
# ============================================================
DATA_PATH = BASE_DIR / "train" / "tonosama_train.csv"
MODEL_PATH = BASE_DIR / "model" / "tonosama_entry_lgbm.pkl"

# ============================================================
# FEATURES / TARGET
# ============================================================
FEATURES = [
    "volume_speed",    # 出来高速度
    "fast_ret",        # 初動火力 [%]
    "rank_position",   # ランキング順位
    "price",           # 価格
    "spread",          # スプレッド
    "entry_second",    # エントリー秒（0-59 → 正規化）
]

TARGET = "label"  # 勝ち=1 / 負け=0

# ============================================================
# MAIN
# ============================================================
def main():

    # --------------------------------------------------------
    # Load
    # --------------------------------------------------------
    if not DATA_PATH.exists():
        raise FileNotFoundError(f"❌ training csv not found: {DATA_PATH}")

    df = pd.read_csv(DATA_PATH)

    # --------------------------------------------------------
    # Cleaning
    # --------------------------------------------------------
    df = df.dropna(subset=FEATURES + [TARGET])

    # 異常値除外（殿様イナゴ想定）
    df = df[
        (df["volume_speed"] > 1_000) &
        (df["volume_speed"] < 200_000) &
        (df["fast_ret"] > -1.0) &
        (df["fast_ret"] < 3.0)
    ]

    # entry_second 正規化（リーク防止）
    df["entry_second"] = df["entry_second"] / 59.0

    X = df[FEATURES]
    y = df[TARGET]

    # --------------------------------------------------------
    # Split
    # --------------------------------------------------------
    X_train, X_val, y_train, y_val = train_test_split(
        X,
        y,
        test_size=0.2,
        random_state=42,
        stratify=y,
    )

    # --------------------------------------------------------
    # Model
    # --------------------------------------------------------
    model = lgb.LGBMClassifier(
        objective="binary",
        boosting_type="gbdt",
        n_estimators=500,
        learning_rate=0.05,
        max_depth=6,
        num_leaves=31,
        subsample=0.8,
        colsample_bytree=0.8,
        min_child_samples=30,
        class_weight="balanced",
        random_state=42,
        n_jobs=-1,
    )

    model.fit(
        X_train,
        y_train,
        eval_set=[(X_val, y_val)],
        eval_metric="auc",
        verbose=False,
    )

    # --------------------------------------------------------
    # Evaluation
    # --------------------------------------------------------
    prob = model.predict_proba(X_val)[:, 1]

    auc = roc_auc_score(y_val, prob)

    # 実運用想定 Precision（高確度エントリー）
    THRESHOLD = 0.72
    pred_label = (prob >= THRESHOLD).astype(int)
    precision = precision_score(y_val, pred_label, zero_division=0)

    print("===================================")
    print("[TONOSAMA ENTRY AI]")
    print(f"AUC        = {auc:.4f}")
    print(f"Precision  = {precision:.4f} (thr={THRESHOLD})")
    print("===================================")

    # --------------------------------------------------------
    # Save
    # --------------------------------------------------------
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, MODEL_PATH)

    print(f"✅ model saved -> {MODEL_PATH}")


if __name__ == "__main__":
    main()
