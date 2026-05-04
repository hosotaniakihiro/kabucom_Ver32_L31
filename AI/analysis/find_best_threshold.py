# ============================================================
# AI/analysis/find_best_threshold.py
# Ver3.0-FINAL-AUTO-WRITE
# ------------------------------------------------------------
# ✔ pred 列を自動検出
# ✔ 期待値最大の threshold を算出
# ✔ ai_thresholds.json に自動反映
# ============================================================

import pandas as pd
import numpy as np
import json
from pathlib import Path
from datetime import datetime

LOG_PATH = Path("AI/logs/entry_exit_log.csv")
CFG_PATH = Path("config/ai_thresholds.json")

MIN_TRADES = 100
QUANTILES = np.arange(0.6, 0.91, 0.05)

# ============================================================
# load log
# ============================================================
df = pd.read_csv(LOG_PATH)

# ------------------------------------------------------------
# pnl 計算（BUY / SELL 両対応）
# ------------------------------------------------------------
def calc_pnl(row):
    if row.get("side", "BUY") == "SELL":
        return row["entry_price"] / row["exit_price"] - 1
    return row["exit_price"] / row["entry_price"] - 1

df["pnl_pct"] = df.apply(calc_pnl, axis=1)

# ============================================================
# pred 列検出
# ============================================================
pred_cols = [c for c in df.columns if c.startswith("ai_pred_")]
if not pred_cols:
    raise RuntimeError("❌ ai_pred_* column not found")

# ============================================================
# 既存 config 読み込み
# ============================================================
if CFG_PATH.exists():
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
else:
    cfg = {"version": "auto", "by_pred": {}}

cfg["updated_at"] = datetime.now().isoformat()

# ============================================================
# threshold 探索
# ============================================================
for pred_col in pred_cols:
    best_ev = -1e9
    best_th = None

    for q in QUANTILES:
        th = df[pred_col].quantile(q)
        sub = df[df[pred_col] >= th]

        if len(sub) < MIN_TRADES:
            continue

        ev = sub["pnl_pct"].mean()
        if ev > best_ev:
            best_ev = ev
            best_th = th

    if best_th is None:
        print(f"⚠ skip {pred_col} (not enough data)")
        continue

    cfg.setdefault("by_pred", {})
    cfg["by_pred"][pred_col] = {
        "threshold": float(best_th),
        "expected_value": float(best_ev),
        "min_trades": MIN_TRADES,
    }

    print(
        f"[UPDATED] {pred_col} "
        f"TH={best_th:.6f} EV={best_ev:.5%}"
    )

# ============================================================
# write config
# ============================================================
CFG_PATH.parent.mkdir(parents=True, exist_ok=True)
CFG_PATH.write_text(
    json.dumps(cfg, indent=2, ensure_ascii=False),
    encoding="utf-8",
)

print(f"\n✅ threshold auto-updated → {CFG_PATH}")
