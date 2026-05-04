# AI/train_tonosama_model.py
import lightgbm as lgb
import pandas as pd
from sklearn.model_selection import train_test_split
from sklearn.metrics import roc_auc_score
import joblib

df = pd.read_csv("AI/tonosama_train.csv")

X = df.drop(columns=["label"])
y = df["label"]

X_train, X_val, y_train, y_val = train_test_split(
    X, y, test_size=0.2, shuffle=True, random_state=42
)

model = lgb.LGBMClassifier(
    n_estimators=300,
    max_depth=5,
    learning_rate=0.03,
    subsample=0.8,
    colsample_bytree=0.8,
)

model.fit(X_train, y_train)

pred = model.predict_proba(X_val)[:, 1]
auc = roc_auc_score(y_val, pred)
print("AUC:", auc)

joblib.dump(model, "AI/tonosama_model.pkl")
