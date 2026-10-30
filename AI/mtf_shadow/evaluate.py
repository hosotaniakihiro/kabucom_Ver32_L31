# ============================================================
# AI/mtf_shadow/evaluate.py
# ------------------------------------------------------------
# 場中の記録 (runtime.py) を集計する。
#
#   python -m AI.mtf_shadow.evaluate            # 記録のある全日
#   python -m AI.mtf_shadow.evaluate --days 20261102 20261104
#
# 見るもの: 向きを合わせたスコア (BUY は p_up - p_down、SELL は逆) の五分位ごとの
#   「向きを合わせた 15 分後リターン」。上位がスプレッド (≒0.1%) を超え、
#   日ごとに見ても崩れないなら判定へ入れる候補になる。
# ============================================================
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from AI.mtf_shadow.runtime import TFS, shadow_dir  # noqa: E402


def load(days: list[str] | None) -> pd.DataFrame:
    files = sorted(shadow_dir().glob("mtf_shadow_*.db"))
    if days:
        files = [f for f in files if f.stem.split("_")[-1] in set(days)]
    parts = []
    for f in files:
        with sqlite3.connect(f"file:{f}?mode=ro", uri=True) as c:
            d = pd.read_sql_query("SELECT * FROM records WHERE fwd_ret IS NOT NULL", c)
        d["day"] = f.stem.split("_")[-1]
        parts.append(d)
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def report(df: pd.DataFrame) -> None:
    sign = np.where(df["side"] == "BUY", 1.0, -1.0)
    df = df.assign(ret_side=df["fwd_ret"] * sign)
    print(f"records={len(df):,} days={df['day'].nunique()} "
          f"BUY={int((df['side'] == 'BUY').sum()):,} SELL={int((df['side'] == 'SELL').sum()):,} "
          f"ret_side_mean={df['ret_side'].mean() * 100:+.3f}%")
    scores = {}
    for tf in TFS:
        u, d = df.get(f"p{tf}_up"), df.get(f"p{tf}_down")
        if u is not None and u.notna().any():
            scores[f"{tf}m"] = (u - d) * sign
    if scores:
        scores["avg"] = pd.concat(scores.values(), axis=1).mean(axis=1)
    for name, s in scores.items():
        m = s.notna()
        if m.sum() < 50:
            print(f"\n[{name}] n={int(m.sum())} (少なすぎて見ない)")
            continue
        sub = df[m].assign(score=s[m])
        sub["q"] = pd.qcut(sub["score"].rank(method="first"), 5, labels=False) + 1
        print(f"\n[{name}] n={len(sub):,}")
        t = sub.groupby("q").agg(n=("ret_side", "size"), score=("score", "mean"),
                                 ret=("ret_side", "mean"), win=("ret_side", lambda x: (x > 0).mean()))
        for q, r in t.iterrows():
            print(f"  Q{q} score={r['score']:+.3f} ret={r['ret'] * 100:+.3f}% win={r['win']:.1%} n={r['n']:,.0f}")
        top = sub[sub["q"] == 5].groupby("day")["ret_side"].mean() * 100
        print("  上位20%の日別: " + " ".join(f"{k[4:]}:{v:+.2f}" for k, v in top.items()))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", nargs="*")
    a = ap.parse_args(argv)
    df = load(a.days)
    if df.empty:
        print(f"記録がありません ({shadow_dir()})")
        return
    report(df)


if __name__ == "__main__":
    main()
