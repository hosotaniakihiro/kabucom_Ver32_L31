# ============================================================
# trading/summary/score_trace_printer.py
# ------------------------------------------------------------
# ✔ 銘柄別 スコア内訳表示
# ✔ 1min / 3min / 5min 共通
# ============================================================

import logging

logger = logging.getLogger("score_trace")


def print_score_trace(df, interval: int, limit: int = 20):
    """
    df: scoring 後の DataFrame
    """
    if df is None or df.empty:
        return

    shown = 0

    for _, r in df.iterrows():
        if shown >= limit:
            break

        symbol = r.get("symbol")
        name = r.get("symbolname", "")
        score = r.get("score_total")
        entry = r.get("entry_decision")
        reasons = r.get("score_reasons", {})

        if not reasons:
            continue

        logger.info(
            "📊 %s %s [%dmin] score=%d entry=%s",
            symbol,
            name,
            interval,
            score,
            entry,
        )

        for k, v in reasons.items():
            logger.info("    %+d %s", v, k)

        shown += 1
