# ============================================================
# backtest16/analytics/walk_forward.py
# ------------------------------------------------------------
# 時系列3分割(§11)。最終評価期間を見た後にパラメータを変えないための固定分割。
#   前半3か月=学習 / 中間1.5か月=検証 / 後半1.5か月=最終評価
# 標準条件のトレードを期間別に集計し、過学習/偶然の高勝率を点検する。
# ============================================================

from __future__ import annotations

import datetime as dt
from typing import List

from backtest16 import config as C
from backtest16.analytics.metrics import strategy_metrics


def period_of(date_str: str) -> str:
    d = dt.date.fromisoformat(date_str[:10])
    if d <= C.SPLIT_TRAIN_END:
        return "train"
    if d <= C.SPLIT_VALID_END:
        return "valid"
    return "test"


def split_metrics(trades: List[dict], strategy_id: str = None) -> dict:
    rows = [t for t in trades if (strategy_id is None or t.get("strategy_id") == strategy_id)]
    out = {}
    for period in ("train", "valid", "test"):
        sub = [t for t in rows if period_of(str(t["date"])) == period]
        out[period] = strategy_metrics(sub)
    # 一貫性の簡易判定: 3期間すべてで期待値>0 なら robust、符号反転があれば warn
    exps = [out[p].get("expectancy", 0) for p in ("train", "valid", "test")]
    signs = {1 if e > 0 else (-1 if e < 0 else 0) for e in exps}
    out["consistency"] = "robust" if signs == {1} else ("negative" if signs == {-1} else "mixed")
    return out
