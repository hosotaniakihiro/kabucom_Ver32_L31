# ============================================================
# File   : AI/tools/plot_exit_transition.py
# Ver1.0-FINAL-PLOT-EXIT-TRANSITION
# ------------------------------------------------------------
# ✔ 未EXIT（HOLD）→ EXIT の判断遷移を可視化
# ✔ takeprofit / collapse / hold_ratio の時系列表示
# ✔ ai_entry_events.db を直接参照
# ✔ 1銘柄・最新トレードを対象
# ============================================================

import sqlite3
import json
from pathlib import Path
import matplotlib.pyplot as plt


# ============================================================
# PATH
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DB_FILE = PROJECT_ROOT / "AI" / "data" / "ai_entry_events.db"


# ============================================================
# CORE
# ============================================================

def plot_exit_transition(symbol: str):
    """
    指定銘柄の最新トレードについて
    EXIT判断スナップショットの遷移を可視化する
    """

    if not DB_FILE.exists():
        print("DB not found:", DB_FILE)
        return

    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()

    # 最新トレード取得（EXIT済み or 未EXITでも可）
    cur.execute(
        """
        SELECT
            exit_check_snapshots,
            exit_reason
        FROM entry_events
        WHERE symbol = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (symbol,),
    )

    row = cur.fetchone()
    con.close()

    if not row:
        print("No entry found for symbol:", symbol)
        return

    raw_snapshots, exit_reason = row

    if not raw_snapshots:
        print("No exit_check_snapshots found")
        return

    try:
        snapshots = json.loads(raw_snapshots)
    except Exception as e:
        print("Invalid snapshot JSON:", e)
        return

    if not isinstance(snapshots, list) or len(snapshots) == 0:
        print("Empty snapshot list")
        return

    # --------------------------------------------------------
    # データ展開
    # --------------------------------------------------------

    steps = list(range(len(snapshots)))

    takeprofit_probs = [
        s.get("takeprofit_prob") for s in snapshots
    ]
    collapse_probs = [
        s.get("collapse_prob") for s in snapshots
    ]
    hold_ratios = [
        s.get("hold_ratio") for s in snapshots
    ]

    decisions = [
        s.get("decision", "HOLD") for s in snapshots
    ]

    # --------------------------------------------------------
    # PLOT
    # --------------------------------------------------------

    plt.figure(figsize=(12, 6))

    plt.plot(
        steps,
        takeprofit_probs,
        label="takeprofit_prob",
        linewidth=2,
    )
    plt.plot(
        steps,
        collapse_probs,
        label="collapse_prob",
        linewidth=2,
    )
    plt.plot(
        steps,
        hold_ratios,
        label="hold_ratio",
        linewidth=2,
        linestyle="--",
    )

    # 閾値ライン
    plt.axhline(0.70, color="gray", linestyle=":", alpha=0.7)
    plt.axhline(0.80, color="gray", linestyle=":", alpha=0.7)
    plt.axhline(1.15, color="gray", linestyle=":", alpha=0.4)

    # EXITポイント強調
    for i, d in enumerate(decisions):
        if d != "HOLD":
            plt.scatter(
                i,
                takeprofit_probs[i]
                if takeprofit_probs[i] is not None
                else collapse_probs[i],
                color="red",
                s=80,
                zorder=5,
                label="EXIT" if i == decisions.index(d) else None,
            )
            plt.annotate(
                d,
                (i, takeprofit_probs[i] or 0),
                textcoords="offset points",
                xytext=(5, 5),
            )
