# ============================================================
# AI/mtf_shadow/train.py
# ------------------------------------------------------------
# build_dataset の出力 → LightGBM 3 クラス (down / flat / up) → AI/models_shadow/mtf_{tf}m.pkl
#
#   python -m AI.mtf_shadow.train --tf 1 3 5
#
# - 日付で分ける (古い順に 学習 / 早期停止用 / 検証 = 70 / 10 / 20%)。
#   同じ日の足が学習と検証に混ざると、隣の足どうしで答えが漏れる。
# - 検証は「p_up - p_down の十分位ごとの 15 分後平均リターン」を主に見る。
#   AUC が 0.5 を少し超えても、上位の平均リターンがスプレッド (≒0.1%) 未満なら使い物にならない。
# - 保存形式: {"model", "features", "classes", "tf", "horizon_min", "trained_at", "data", "metrics"}
# ============================================================
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

import joblib
import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.metrics import log_loss, roc_auc_score

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from AI.mtf_shadow.build_dataset import default_out_dir  # noqa: E402
from AI.mtf_shadow.features import CLASSES, FEATURES, HORIZON_MIN  # noqa: E402

MODEL_DIR = _REPO / "AI" / "models_shadow"
MAX_TRAIN_ROWS = 4_000_000


def split_by_date(df: pd.DataFrame):
    days = np.sort(df["datetime"].dt.normalize().unique())
    n = len(days)
    d_val = days[int(n * 0.70)]
    d_test = days[int(n * 0.80)]
    day = df["datetime"].dt.normalize()
    return df[day < d_val], df[(day >= d_val) & (day < d_test)], df[day >= d_test]


def decile_table(score: np.ndarray, fwd: np.ndarray) -> list[dict]:
    q = pd.qcut(pd.Series(score).rank(method="first"), 10, labels=False)
    t = pd.DataFrame({"q": q, "score": score, "fwd": fwd}).groupby("q").agg(
        n=("fwd", "size"), score=("score", "mean"), fwd_mean=("fwd", "mean"), up_rate=("fwd", lambda s: (s > 0).mean()))
    return [{"decile": int(k) + 1, **{c: float(v) for c, v in r.items()}} for k, r in t.iterrows()]


def train_tf(tf: int, data_dir: Path, seed: int = 42) -> dict:
    df = pd.read_pickle(data_dir / f"dataset_{tf}m.pkl")
    tr, va, te = split_by_date(df)
    if len(tr) > MAX_TRAIN_ROWS:
        tr = tr.sample(MAX_TRAIN_ROWS, random_state=seed)
    print(f"[tf={tf}] train={len(tr):,} val={len(va):,} test={len(te):,} "
          f"test_days={te['datetime'].dt.normalize().nunique()}", flush=True)

    model = lgb.LGBMClassifier(
        objective="multiclass", num_class=3, n_estimators=3000, learning_rate=0.03,
        num_leaves=63, min_child_samples=2000, subsample=0.7, subsample_freq=1,
        colsample_bytree=0.8, reg_lambda=1.0, random_state=seed, n_jobs=6, verbose=-1,
    )
    model.fit(tr[FEATURES], tr["y"].astype(int), eval_set=[(va[FEATURES], va["y"].astype(int))],
              callbacks=[lgb.early_stopping(100, verbose=False), lgb.log_evaluation(200)])

    p = model.predict_proba(te[FEATURES])
    y = te["y"].astype(int).to_numpy()
    fwd = te["fwd_ret"].to_numpy()
    prior = np.bincount(tr["y"].astype(int), minlength=3) / len(tr)
    score = p[:, 2] - p[:, 0]
    metrics = {
        "best_iteration": int(model.best_iteration_ or 0),
        "logloss": float(log_loss(y, p, labels=[0, 1, 2])),
        "logloss_prior": float(log_loss(y, np.tile(prior, (len(y), 1)), labels=[0, 1, 2])),
        "auc_up": float(roc_auc_score(y == 2, p[:, 2])),
        "auc_down": float(roc_auc_score(y == 0, p[:, 0])),
        "class_rate_test": {c: float((y == i).mean()) for i, c in enumerate(CLASSES)},
        "share_p_up_ge_055": float((p[:, 2] >= 0.55).mean()),
        "share_p_down_ge_055": float((p[:, 0] >= 0.55).mean()),
        "fwd_mean_all": float(np.nanmean(fwd)),
        "deciles_up_minus_down": decile_table(score, fwd),
        "importance": dict(sorted(
            ((f, int(v)) for f, v in zip(FEATURES, model.booster_.feature_importance("gain"))),
            key=lambda kv: -kv[1])),
    }
    data = {
        "rows": int(len(df)), "symbols": int(df["symbol"].nunique()),
        "start": str(df["datetime"].min()), "end": str(df["datetime"].max()),
        "test_start": str(te["datetime"].min()),
    }
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    out = MODEL_DIR / f"mtf_{tf}m.pkl"
    joblib.dump({
        "model": model, "features": list(FEATURES), "classes": list(CLASSES), "tf": tf,
        "horizon_min": HORIZON_MIN, "trained_at": dt.datetime.now().isoformat(timespec="seconds"),
        "data": data, "metrics": metrics,
    }, out)
    (MODEL_DIR / f"mtf_{tf}m_metrics.json").write_text(
        json.dumps({"data": data, "metrics": metrics}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[tf={tf}] saved {out}", flush=True)
    return {"tf": tf, "data": data, "metrics": metrics}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--tf", type=int, nargs="+", default=[1, 3, 5])
    ap.add_argument("--data", default=str(default_out_dir()))
    a = ap.parse_args(argv)
    for tf in a.tf:
        r = train_tf(tf, Path(a.data))
        m = r["metrics"]
        print(f"[tf={tf}] logloss={m['logloss']:.4f} (prior {m['logloss_prior']:.4f}) "
              f"auc_up={m['auc_up']:.3f} auc_down={m['auc_down']:.3f} "
              f"p_up>=0.55: {m['share_p_up_ge_055']:.1%}", flush=True)
        for d in m["deciles_up_minus_down"]:
            print(f"   D{d['decile']:>2} score={d['score']:+.3f} fwd={d['fwd_mean'] * 100:+.3f}% "
                  f"up={d['up_rate']:.1%} n={d['n']:,.0f}", flush=True)


if __name__ == "__main__":
    main()
