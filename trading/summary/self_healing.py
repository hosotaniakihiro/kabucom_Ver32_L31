# ============================================================
# self_healing.py
# ------------------------------------------------------------
# ✔ 欠損日・銘柄のみ差分 rebuild
# ✔ 全 rebuild を絶対にしない
# ============================================================

import datetime as dt
import logging
from collections import defaultdict

from trading.summary.initial_summary import run_initial_fast_rebuild

logger = logging.getLogger(__name__)

def detect_missing_symbols(df, symbols: list[str]) -> list[str]:
    if df.empty:
        return symbols
    exist = set(df["symbol"].unique())
    return [s for s in symbols if s not in exist]

def detect_stale_symbols(df, today: dt.date) -> list[str]:
    if df.empty:
        return []
    latest_by_symbol = (
        df.groupby("symbol")["datetime"].max().dt.date
    )
    return [
        s for s, d in latest_by_symbol.items()
        if d != today
    ]

def heal_summary_if_needed(
    df,
    *,
    symbols: list[str],
    interval: int,
    today: dt.date | None = None,
):
    """
    欠損 or 古い銘柄のみ rebuild
    """
    today = today or dt.date.today()

    missing = detect_missing_symbols(df, symbols)
    stale = detect_stale_symbols(df, today)

    targets = sorted(set(missing + stale))

    if not targets:
        return

    logger.warning(
        f"🛠 self-healing interval={interval} targets={targets}"
    )

    # ★ 既存 initial_summary を「銘柄限定」で呼べる前提
    run_initial_fast_rebuild(
        symbols=targets,
        interval=interval,
    )
