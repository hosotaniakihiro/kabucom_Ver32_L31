# ============================================================
# File   : trading/push/allocator/selector.py
# Version: Ver1.0-PRODUCTION-PUSH-ALLOCATOR-SELECTOR
# ------------------------------------------------------------
# ✔ final push symbol selector
# ✔ 50 symbol limit
# ✔ churn prevention
# ✔ state integration
# ✔ priority selection
# ✔ production safe
# ============================================================

from __future__ import annotations

import logging
from typing import Set

import pandas as pd

from trading.push.allocator.state.state import AllocatorState

logger = logging.getLogger(__name__)


# ============================================================
# select symbols
# ============================================================

def select_symbols(
    scored_df: pd.DataFrame,
    config,
    state: AllocatorState,
) -> Set[str]:
    """
    scoring後のDataFrameから最終push銘柄選択
    """

    if scored_df is None or scored_df.empty:
        return set()

    max_symbols = config.max_push_symbols

    selected: Set[str] = set()

    # --------------------------------------------------------
    # priority順
    # --------------------------------------------------------

    df = scored_df.sort_values(
        "final_score",
        ascending=False,
        ignore_index=True,
    )

    # --------------------------------------------------------
    # 強制保持（ポジション）
    # --------------------------------------------------------

    if "flag_position" in df.columns:

        pos_df = df[df["flag_position"] == 1]

        for symbol in pos_df["symbol"]:

            selected.add(symbol)

            if len(selected) >= max_symbols:
                return selected

    # --------------------------------------------------------
    # 強制保持（注文）
    # --------------------------------------------------------

    if "flag_order" in df.columns:

        order_df = df[df["flag_order"] == 1]

        for symbol in order_df["symbol"]:

            selected.add(symbol)

            if len(selected) >= max_symbols:
                return selected

    # --------------------------------------------------------
    # priority追加
    # --------------------------------------------------------

    for symbol in df["symbol"]:

        if symbol in selected:
            continue

        if len(selected) >= max_symbols:
            break

        selected.add(symbol)

    # --------------------------------------------------------
    # churn prevention
    # --------------------------------------------------------

    current_symbols = state.current_symbols()

    final_set: Set[str] = set(selected)

    for symbol in current_symbols:

        if symbol in final_set:
            continue

        if not state.can_remove(symbol, config.min_hold_seconds):
            final_set.add(symbol)

    # --------------------------------------------------------
    # limit再調整
    # --------------------------------------------------------

    if len(final_set) > max_symbols:

        df2 = df[df["symbol"].isin(final_set)]

        df2 = df2.sort_values(
            "final_score",
            ascending=False,
        )

        final_set = set(df2["symbol"].head(max_symbols))

    logger.info(
        "[allocator selector] "
        f"selected={len(final_set)}"
    )

    return final_set