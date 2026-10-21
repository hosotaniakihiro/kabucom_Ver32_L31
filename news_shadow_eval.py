# ============================================================
# File   : news_shadow_eval.py
# ------------------------------------------------------------
# 検証先行(シャドー)の集計。読み取り専用・発注には無関係。
#
#   news_shadow_log(建て時 as-of の news_score) と
#   positions(約定済みの entry/exit 価格) を hold_id で突き合わせ、
#   news_score の帯域別に 勝率 / 平均損益率 / 件数 を出す。
#   さらに news_score と損益率の相関係数を出す。
#
#   狙い: 「news_score が高い建玉ほど成績が良いか？」を数値で見る。
#         良い相関が数週間で安定して初めて、実際の発注に配線する。
# ============================================================

from __future__ import annotations

import math
import logging
import datetime as dt
from typing import Optional

logger = logging.getLogger(__name__)

# news_score の帯域
BANDS = [
    ("悪材料 (<40)", lambda s: s < 40),
    ("中立 (40-60)", lambda s: 40 <= s <= 60),
    ("好材料 (>60)", lambda s: s > 60),
]


def _side_sign(side: Optional[str]) -> int:
    """side を +1(ロング) / -1(ショート) に正規化。不明はロング扱い。"""
    if side is None:
        return 1
    s = str(side).strip().upper()
    # "SELL_CREDIT"(信用売り=ショート) 等の接頭辞にも対応（完全一致では取りこぼす）
    if s.startswith("SELL") or s.startswith("SHORT") or s in {"S", "2", "-1"}:
        return -1
    return 1


def _load_shadow():
    import news_pipeline as npl
    from database.models import NewsShadowLog

    session = npl._news_session()
    try:
        rows = session.query(
            NewsShadowLog.hold_id,
            NewsShadowLog.symbol,
            NewsShadowLog.news_score,
            NewsShadowLog.entry_time,
        ).all()
        return {str(r.hold_id): r for r in rows}
    finally:
        session.close()


class _Row:
    __slots__ = ("hold_id", "symbol", "side", "avg_price", "exit_price")

    def __init__(self, hold_id, symbol, side, avg_price, exit_price):
        self.hold_id = hold_id
        self.symbol = symbol
        self.side = side
        self.avg_price = avg_price
        self.exit_price = exit_price


def _load_closed_positions():
    """exit 済み(exit_price あり)のポジションを positions.db から生SQLで読む。"""
    from sqlalchemy import text
    import news_shadow

    eng = news_shadow._positions_engine()
    if eng is None:
        return []
    try:
        with eng.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT hold_id, symbol, side, avg_price, exit_price "
                    "FROM positions WHERE exit_price IS NOT NULL"
                )
            ).all()
        return [_Row(r[0], r[1], r[2], r[3], r[4]) for r in rows]
    except Exception:
        logger.exception("[SHADOW-EVAL] positions 読み取り失敗")
        return []
    finally:
        eng.dispose()


def _pnl_pct(side, avg_price, exit_price) -> Optional[float]:
    try:
        if not avg_price or avg_price <= 0 or exit_price is None:
            return None
        raw = (float(exit_price) - float(avg_price)) / float(avg_price)
        return raw * _side_sign(side) * 100.0  # %
    except Exception:
        return None


def _pearson(xs, ys) -> Optional[float]:
    n = len(xs)
    if n < 2:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    if sxx <= 0 or syy <= 0:
        return None
    return sxy / math.sqrt(sxx * syy)


def build_report() -> dict:
    shadow = _load_shadow()
    closed = _load_closed_positions()

    paired = []  # (news_score, pnl_pct)
    for pos in closed:
        key = str(pos.hold_id) if pos.hold_id is not None else None
        sh = shadow.get(key) if key else None
        if sh is None:
            continue
        p = _pnl_pct(pos.side, pos.avg_price, pos.exit_price)
        if p is None:
            continue
        paired.append((float(sh.news_score), p))

    report = {"matched_trades": len(paired), "bands": [], "correlation": None}
    if not paired:
        return report

    for name, pred in BANDS:
        sel = [p for s, p in paired if pred(s)]
        if sel:
            wins = sum(1 for p in sel if p > 0)
            report["bands"].append({
                "band": name,
                "n": len(sel),
                "win_rate": wins / len(sel) * 100.0,
                "avg_pnl_pct": sum(sel) / len(sel),
            })
        else:
            report["bands"].append({"band": name, "n": 0, "win_rate": None, "avg_pnl_pct": None})

    report["correlation"] = _pearson([s for s, _ in paired], [p for _, p in paired])
    return report


def print_report(report: dict) -> None:
    print("=" * 56)
    print(f"news_score シャドー検証  (突合トレード数={report['matched_trades']})")
    print("=" * 56)
    if report["matched_trades"] == 0:
        print("※ まだ突き合わせできるデータがありません。")
        print("  news_shadow.run_loop() を常駐させ、建玉が約定(exit)するのを待ってください。")
        return

    print(f"{'帯域':<14}{'件数':>6}{'勝率%':>9}{'平均損益%':>11}")
    print("-" * 56)
    for b in report["bands"]:
        wr = f"{b['win_rate']:.1f}" if b["win_rate"] is not None else "-"
        ap = f"{b['avg_pnl_pct']:+.3f}" if b["avg_pnl_pct"] is not None else "-"
        print(f"{b['band']:<14}{b['n']:>6}{wr:>9}{ap:>11}")
    print("-" * 56)
    corr = report["correlation"]
    print(f"news_score × 損益率 の相関係数: {corr:+.3f}" if corr is not None else "相関係数: 算出不可(件数不足)")
    print("")
    print("見方: 好材料帯の勝率/平均損益が中立・悪材料帯より明確に高く、")
    print("      相関が安定して正(目安 +0.1 以上)なら、発注への配線を検討する価値あり。")


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    print_report(build_report())
