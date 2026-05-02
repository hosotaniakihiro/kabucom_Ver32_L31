# ============================================================
# File   : trading/summary/recovery/loaders.py
# Ver    : PRODUCTION-STABLE-REV8.1-LOADERS-COMPAT-SPLIT-PUSH-SAFE-EXPORT
# ------------------------------------------------------------
# 【概要】
#   summary recovery loader 群の互換入口 shim
#
# 【目的】
#   - 旧 import 経路:
#       trading.summary.recovery.loaders
#     との互換を維持する
#
#   - 実体は以下へ分割:
#       loaders_common
#       loaders_summary
#       loaders_ranking
#       loaders_push
#
#   - 既存 bootstrap / preload / orchestrator を壊さず段階移行可能にする
#
# 【REV8.1 修正点】
#   - loaders_push 側の安全SQL対応関数を optional re-export 可能にした
#   - stream_data に tick_time が無いDBでも loaders_push 側で安全に読む設計に対応
#   - このファイル自体は SQL を発行しない
#   - tick_time no such column の直接修正は loaders_push.py 側で行う
#
# 【重要】
#   - 本ファイルは互換 shim
#   - 実処理は各 loaders_xxx.py に委譲
#   - import 互換性を最優先
# ============================================================

from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

# ============================================================
# common loaders
# ============================================================

from .loaders_common import (
    FUTURE_TOLERANCE_MINUTES,
    apply_max_allowed_dt_filter,
    apply_target_date_filter,
    coerce_date_set,
    log_df_date_breakdown,
    normalize_symbols,
    now_naive,
    sanitize_checkpoint_dt,
    sanitize_query_dt,
)

# ============================================================
# push loaders
# ============================================================
# NOTE:
#   tick_time no such column の直接修正は loaders_push.py 側。
#   ここでは旧 import 経路との互換のため、関数を再エクスポートする。
# ============================================================

from .loaders_push import (
    DEFAULT_PUSH_DB_DIR,
    DEFAULT_PUSH_TABLE_CANDIDATES,
    detect_push_table_name,
    filter_push_after,
    load_push_df_for_dates,
    load_runtime_push_df,
    normalize_push_df,
    resolve_push_db_path,
)

# loaders_push.py 側に安全SQL helper が存在する場合だけ re-export する。
# まだ loaders_push.py を更新していない環境でも、この shim 自体が壊れないようにする。
try:
    from .loaders_push import (
        fetch_push_table_columns,
        build_push_time_where_clause,
    )

    _HAS_PUSH_SAFE_SQL_HELPERS = True

except Exception:
    fetch_push_table_columns = None  # type: ignore
    build_push_time_where_clause = None  # type: ignore
    _HAS_PUSH_SAFE_SQL_HELPERS = False


# ============================================================
# ranking loaders
# ============================================================

from .loaders_ranking import (
    load_ranking_symbols_for_dates,
    load_restore_target_symbols,
    load_today_global_ranking_symbols,
)

# ============================================================
# summary loaders
# ============================================================

from .loaders_summary import (
    load_last_summary_datetime,
    load_latest_summary_snapshot,
    load_recent_summary_tail_default,
    load_recent_summary_tail_per_symbol,
    load_summary_df_between,
    load_summary_df_from_datetime,
    read_sqlalchemy_model_to_df,
    resolve_summary_table_name_from_model,
)

logger = logging.getLogger(__name__)


# ============================================================
# compact preload resolver
# ============================================================

def resolve_compact_preload_start() -> tuple[
    Optional[pd.Timestamp],
    Optional[pd.Timestamp],
    Optional[pd.Timestamp],
]:
    """
    3分足 / 5分足の既存サマリー進捗から、
    起動時に最低限読み込むべき PUSH / 1分足範囲を決める。

    戻り値:
        load_start_dt:
            実際に読み込みを開始すべき時刻。
            3分足の最終時刻と、5分足MA75計算用のウォームアップ開始時刻の
            早い方を採用する。

        last_3m_dt:
            3分足 summary の最新 datetime。

        last_5m_dt:
            5分足 summary の最新 datetime。

    設計:
        - 5分足MA75には概算で 5分 * 75 = 375分 の1分足が必要。
        - そのため last_5m_dt が存在する場合は、
          last_5m_dt - 375分 を warmup_5m_start とする。
        - 3分足の差分更新も考慮し、
          min(last_3m_dt, warmup_5m_start) を load_start_dt とする。
    """
    now_ts = now_naive()

    last_3m_dt = load_last_summary_datetime(3)
    last_5m_dt = load_last_summary_datetime(5)

    last_3m_dt = sanitize_checkpoint_dt(
        last_3m_dt,
        label="resolve_compact_preload_start.last_3m_dt",
        interval=3,
    )
    last_5m_dt = sanitize_checkpoint_dt(
        last_5m_dt,
        label="resolve_compact_preload_start.last_5m_dt",
        interval=5,
    )

    warmup_5m_start: Optional[pd.Timestamp] = None
    if last_5m_dt is not None and pd.notna(last_5m_dt):
        warmup_5m_start = pd.to_datetime(last_5m_dt) - pd.Timedelta(minutes=375)

    starts = [
        x
        for x in [
            last_3m_dt,
            warmup_5m_start,
        ]
        if x is not None and not pd.isna(x)
    ]

    load_start_dt = min(starts) if starts else None

    logger.info(
        "[summary.recovery.loaders] compact preload resolved "
        "load_start_dt=%s last_3m_dt=%s last_5m_dt=%s warmup_5m_start=%s now=%s",
        load_start_dt,
        last_3m_dt,
        last_5m_dt,
        warmup_5m_start,
        now_ts,
    )

    return load_start_dt, last_3m_dt, last_5m_dt


# ============================================================
# diagnostics
# ============================================================

def log_loader_compat_status() -> None:
    """
    loader shim の状態をログ出力する診断用関数。

    目的:
        - 分割後にどの安全 helper が読み込めているか確認する。
        - loaders_push.py 側の tick_time 安全SQL対応が入っているかを確認しやすくする。
    """
    logger.info(
        "[summary.recovery.loaders] compat status "
        "push_safe_sql_helpers=%s fetch_push_table_columns=%s build_push_time_where_clause=%s",
        _HAS_PUSH_SAFE_SQL_HELPERS,
        callable(fetch_push_table_columns),
        callable(build_push_time_where_clause),
    )


# ============================================================
# __all__
# ============================================================

__all__ = [
    # common constants / utils
    "FUTURE_TOLERANCE_MINUTES",
    "now_naive",
    "sanitize_checkpoint_dt",
    "sanitize_query_dt",
    "coerce_date_set",
    "normalize_symbols",
    "log_df_date_breakdown",
    "apply_target_date_filter",
    "apply_max_allowed_dt_filter",

    # push constants / utils
    "DEFAULT_PUSH_DB_DIR",
    "DEFAULT_PUSH_TABLE_CANDIDATES",
    "resolve_push_db_path",
    "detect_push_table_name",
    "normalize_push_df",
    "load_push_df_for_dates",
    "load_runtime_push_df",
    "filter_push_after",

    # ranking loaders
    "load_today_global_ranking_symbols",
    "load_ranking_symbols_for_dates",
    "load_restore_target_symbols",

    # summary loaders
    "resolve_summary_table_name_from_model",
    "read_sqlalchemy_model_to_df",
    "load_last_summary_datetime",
    "load_summary_df_from_datetime",
    "load_summary_df_between",
    "load_recent_summary_tail_per_symbol",
    "load_recent_summary_tail_default",
    "load_latest_summary_snapshot",

    # compact preload
    "resolve_compact_preload_start",

    # diagnostics
    "log_loader_compat_status",
]

if _HAS_PUSH_SAFE_SQL_HELPERS:
    __all__.extend(
        [
            "fetch_push_table_columns",
            "build_push_time_where_clause",
        ]
    )