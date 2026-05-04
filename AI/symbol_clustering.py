# ============================================================
# pj/ai/symbol_clustering.py
# 銘柄別クラスタリング（TONOSAMA 用）
# ------------------------------------------------------------
# ・TosamaTradeLog から銘柄の癖を抽出
# ・KMeans で自動クラスタリング
# ・symbol → cluster_id を保存
# ============================================================

import pandas as pd
import joblib
from pathlib import Path
from sklearn.cluster import KMeans
from database.session import Session_position
from database.models import TosamaTradeLog

MODEL_PATH = Path("AI/model/tonosama_symbol_cluster.pkl")
MAP_PATH   = Path("AI/model/symbol_cluster_map.pkl")

N_CLUSTERS = 6


def build_symbol_features() -> pd.DataFrame:
    """
    銘柄ごとの統計特徴量を作成
    """
    session = Session_position()
    rows = session.query(TosamaTradeLog).all()
    session.close()

    df = pd.DataFrame([{
        "symbol": r.symbol,
        "volume_speed": r.volume_speed,
        "fast_ret": r.fast_ret,
        "pnl_pct": (r.exit_price - r.entry_price) / r.entry_price
                   if r.entry_price and r.exit_price else 0,
        "hold_seconds": r.hold_seconds or 0,
        "entry_hour": r.entry_time.hour if r.entry_time else 9,
    } for r in rows])

    if df.empty:
        raise RuntimeError("TosamaTradeLog is empty")

    g = df.groupby("symbol")

    agg = g.agg(
        avg_volume_speed=("volume_speed", "mean"),
        avg_fast_ret=("fast_ret", "mean"),
        win_rate=("pnl_pct", lambda x: (x > 0).mean()),
        avg_pnl=("pnl_pct", "mean"),
        avg_hold_seconds=("hold_seconds", "mean"),
        entry_hour_mean=("entry_hour", "mean"),
        trade_count=("pnl_pct", "count"),
    ).reset_index()

    return agg


def main():
    df = build_symbol_features()

    FEATURES = [
        "avg_volume_speed",
        "avg_fast_ret",
        "win_rate",
        "avg_pnl",
        "avg_hold_seconds",
        "entry_hour_mean",
    ]

    X = df[FEATURES].fillna(0)

    model = KMeans(
        n_clusters=N_CLUSTERS,
        random_state=42,
        n_init=10,
    )

    df["cluster_id"] = model.fit_predict(X)

    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, MODEL_PATH)

    symbol_cluster = dict(zip(df["symbol"], df["cluster_id"]))
    joblib.dump(symbol_cluster, MAP_PATH)

    print("✅ symbol clustering completed")
    print(df[["symbol", "cluster_id", "trade_count"]].head())


if __name__ == "__main__":
    main()
