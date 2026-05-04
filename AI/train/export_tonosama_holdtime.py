# ============================================================
# pj/AI/train/export_tonosama_holdtime.py
# 2025-12-31
# ------------------------------------------------------------
# TONOSAMA 最適 holding 秒数 学習用 CSV 生成
# ============================================================

import pandas as pd
from database.session import Session_position
from database.models import TosamaTradeLog

def export_holdtime_csv(path="AI/train/tosama_holdtime.csv"):

    session = Session_position()
    rows = session.query(TosamaTradeLog).all()
    data = []

    for r in rows:
        if not r.entry_price or not r.exit_price:
            continue

        pnl_pct = (r.exit_price - r.entry_price) / r.entry_price

        # 勝ちトレードのみ学習対象
        if pnl_pct <= 0:
            continue

        data.append({
            "volume_speed": r.volume_speed,
            "fast_ret": r.fast_ret,
            "rank_position": r.rank_position,
            "price": r.entry_price,
            "spread": r.spread,
            "entry_second": r.entry_time.second if r.entry_time else 0,
            "hold_seconds": r.hold_seconds,  # ← ★ 回帰ターゲット
        })

    df = pd.DataFrame(data)
    df.to_csv(path, index=False, encoding="utf-8-sig")
    session.close()

    print(f"[HOLDTIME CSV] exported -> {path}")
