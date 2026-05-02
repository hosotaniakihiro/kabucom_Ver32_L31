# ============================================================
# File: trading/summary/summary_confirm_runner.py
# Ver1.0-FINAL-SCHEDULED-CONFIRM-SAFE
# ------------------------------------------------------------
# ✔ 定時サマリー（3min / 5min）で
#   PUSH × RANKING confirm_pr を付与
# ✔ summary_cache 再代入なし（キー単位更新）
# ✔ self_heal / initial / runtime 全フェーズ対応
# ✔ None / empty / 欠損完全耐性
# ✔ ログ最小・運用安全
# ============================================================

from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

from global_state import global_data
from trading.summary.summary_confirm import mark_push_rank_confirm

logger = logging.getLogger(__name__)


# ============================================================
# 内部ユーティリティ
# ============================================================
def _count_confirm(df: Optional[pd.DataFrame], flag_col: str = "confirm_pr") -> int:
    """
    confirm_pr=True の件数を安全にカウント
    """
    if df is None or df.empty:
        return 0

    if flag_col not in df.columns:
        return 0

    try:
        return int(df[flag_col].fillna(False).sum())
    except Exception:
        return 0


# ============================================================
# メインAPI
# ============================================================
def apply_confirm_flag(interval: int) -> None:
    """
    定時サマリーで PUSH × RANKING の confirm_pr を付与する

    Parameters
    ----------
    interval : int
        3 または 5（分）
    """

    if interval not in (3, 5):
        logger.warning(f"[confirm] invalid interval={interval} → skip")
        return

    push_key = f"{interval}min_push"
    rank_key = f"{interval}min_rank"

    # --------------------------------------------------------
    # cache 取得
    # --------------------------------------------------------
    df_push = global_data.summary_cache.get(push_key)
    df_rank = global_data.summary_cache.get(rank_key)

    if df_push is None:
        logger.debug(f"[confirm] {push_key} not found → skip")
        return

    # --------------------------------------------------------
    # confirm_pr 付与（PUSH 側のみ）
    # --------------------------------------------------------
    df_push_confirmed = mark_push_rank_confirm(
        df_push=df_push,
        df_rank=df_rank,
    )

    # --------------------------------------------------------
    # cache 更新（キー単位・安全）
    # --------------------------------------------------------
    global_data.summary_cache[push_key] = df_push_confirmed

    # --------------------------------------------------------
    # ログ（最小）
    # --------------------------------------------------------
    confirmed_cnt = _count_confirm(df_push_confirmed)

    logger.info(
        f"[confirm] {interval}min "
        f"PUSH={0 if df_push is None else len(df_push)} "
        f"RANK={0 if df_rank is None else len(df_rank)} "
        f"CONFIRM={confirmed_cnt}"
    )