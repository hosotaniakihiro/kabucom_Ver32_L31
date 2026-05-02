# ============================================================
# File   : trading/ranking/summary/display.py
# Ver    : PRODUCTION-STABLE-REV1.0-RANKING-SUMMARY-DISPLAY
# ------------------------------------------------------------
# 【概要】
#   ランキング由来サマリーの簡易TOP10表示
#   正式なPUSH風表示とDiscord通知は announce.py に委譲
# ============================================================

from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

from trading.ranking.summary.score import ensure_score_columns
from trading.ranking.summary.utils import fmt2, fmt_int, fmt_price, safe_str

logger = logging.getLogger(__name__)


def display_ranking_summary_top10(
    df: pd.DataFrame,
    *,
    interval: int,
    title: Optional[str] = None,
    top_n: int = 10,
) -> None:
    if df is None or df.empty:
        logger.info(
            "========== 📊 RANKING SUMMARY TOP10 (%smin) ==========\n"
            "データなし",
            interval,
        )
        return

    work = ensure_score_columns(df)

    score_col = "display_score"
    if score_col not in work.columns:
        score_col = "final_score" if "final_score" in work.columns else "score"

    if score_col not in work.columns:
        work["score"] = 0.0
        score_col = "score"

    work[score_col] = pd.to_numeric(work[score_col], errors="coerce").fillna(0)
    work = work.sort_values(score_col, ascending=False).head(int(top_n))

    heading = title or f"========== 📊 RANKING SUMMARY TOP10 ({interval}min) =========="

    lines = [heading]

    lines.append(
        "No  Symbol  Name              Price    Score    RSI     MACD    Hist    Chg%    Rank  Type"
    )
    lines.append(
        "--  ------  ----------------  -------  -------  ------  ------  ------  ------  ----  ----"
    )

    for i, row in enumerate(work.itertuples(index=False), start=1):
        d = row._asdict()

        symbol = safe_str(d.get("symbol"))[:6]
        name = safe_str(d.get("symbolname"))[:16]
        price = fmt_price(d.get("current_price", d.get("close")))
        score = fmt2(d.get(score_col))
        rsi = fmt2(d.get("rsi"))
        macd = fmt2(d.get("macd"))
        hist = fmt2(d.get("macd_hist", d.get("hist")))
        chg = fmt2(d.get("change_percentage"))
        rank = fmt_int(d.get("rank", d.get("best_rank_position")))
        rtype = safe_str(d.get("ranking_type"))[:8]

        lines.append(
            f"{i:>2}  {symbol:<6}  {name:<16}  "
            f"{price:>7}  {score:>7}  {rsi:>6}  "
            f"{macd:>6}  {hist:>6}  {chg:>6}  {rank:>4}  {rtype}"
        )

    logger.info("\n%s", "\n".join(lines))


def announce_if_requested(
    *,
    interval: int,
    display: bool,
    top_n: int,
    use_discord: bool,
) -> bool:
    if not display:
        return False

    try:
        from trading.ranking.summary.announce import announce_ranking_summary

        ok = announce_ranking_summary(
            interval=interval,
            topn=top_n,
            use_discord=use_discord,
        )

        logger.info(
            "[RANKING SUMMARY RUNNER] announce done interval=%s ok=%s use_discord=%s",
            interval,
            ok,
            use_discord,
        )

        return bool(ok)

    except Exception:
        logger.exception(
            "[RANKING SUMMARY RUNNER] announce failed interval=%s -> fallback display",
            interval,
        )
        return False