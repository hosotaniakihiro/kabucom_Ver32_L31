# ============================================================
# File   : trading/push/push_stream/dataframe.py
# Version: Ver2.0-PUSH-STREAM-DATAFRAME-O1-APPEND
# ------------------------------------------------------------
# 2026-09-02 P0 修正 (PUSH リアルタイム停止 / RECONNECT STORM)
#
# 旧 Ver1.0 は on_message の中で
#     state._push_df = pd.concat([state._push_df, 1行])
#     _sync_push_df_to_global()      # ここで更に 8 回 full copy + 2 回 sort
# を **1 tick ごと** に実行していた。1 tick のコストが累積行数に比例するため
# 受信スループットが行数に反比例して落ちる。
#
#   本番 push_receiver 実測 (2026-09-02, PUSH MONITOR 5 秒間隔):
#       df   2,175 行 -> 21.7 msg/s
#       df   4,951 行 -> 11.5 msg/s
#       df  12,423 行 ->  1.1 msg/s
#
# kabu Station は register 直後に登録銘柄ぶんのスナップショットを
# バーストで送る。読み取りスレッドがそれを捌けないと TCP 受信バッファが
# 詰まり、サーバ側から切られる。実際 uptime=3.0-4.0s の即死が 55 連続し、
# [WS RECONNECT STORM] となって PUSH の event datetime が進まなくなった
# (rowid は伸びるのに datetime が 20 分前で止まる、の正体)。
#
# 対策:
#   - append は dict を deque へ積むだけ (O(1))
#   - DataFrame 化は参照時に遅延生成し、seq が変わるまでキャッシュ
#   - global_data への同期は専用スレッドが間引いて実行 (runtime.py)
#
# stale 判定・閾値・fail-closed の条件は一切変更していない。
# ============================================================

from __future__ import annotations

import logging
import numpy as np
import pandas as pd

from .constants import MAX_DF_ROWS
from . import state

logger = logging.getLogger(__name__)

try:
    from trading.push.push_ring_buffer import PushRingBuffer
except Exception:
    PushRingBuffer = None


def _materialize_locked() -> pd.DataFrame:
    """state._df_lock を保持した状態で呼ぶこと。

    deque の中身から DataFrame を作る。seq が前回と同じならキャッシュを返す。
    """
    if state._push_df_cache is not None and state._push_df_cache_seq == state._push_rows_seq:
        return state._push_df_cache

    rows = list(state._push_rows)
    if not rows:
        df = pd.DataFrame()
    else:
        try:
            df = pd.DataFrame(rows)
        except Exception:
            logger.exception("[push_stream] materialize push df failed rows=%d", len(rows))
            df = pd.DataFrame()

    state._push_df_cache = df
    state._push_df_cache_seq = state._push_rows_seq
    # 旧コード互換: state._push_df を直接読む経路が残っていても壊れないようにする。
    state._push_df = df
    return df


def get_push_dataframe() -> pd.DataFrame:
    with state._df_lock:
        df = _materialize_locked()
        # 呼び出し側が書き換えても内部リングに影響しないよう、浅いコピーを返す。
        return df.copy(deep=False) if isinstance(df, pd.DataFrame) and not df.empty else pd.DataFrame()


def push_rows_count() -> int:
    """リングに積まれている tick 行数 (materialize せずに数える)。"""
    try:
        return len(state._push_rows)
    except Exception:
        return 0


def clear_push_dataframe() -> None:
    with state._df_lock:
        state._push_rows.clear()
        state._push_rows_seq += 1
        state._push_df_cache = None
        state._push_df_cache_seq = -1
        state._push_df = pd.DataFrame()

    # 明示クリアは低頻度なので同期で反映してよい。
    from .runtime import _sync_push_df_to_global
    _sync_push_df_to_global(reason="clear")
    logger.info("[push_stream] push dataframe cleared")


def _align_all_na_dtypes(base: "pd.DataFrame", add_df: "pd.DataFrame") -> "pd.DataFrame":
    """追加行の all-NA 列の dtype を揃えてから concat させる。

    Ver2.0 では on_message から concat 自体を外したのでホットパスからは
    外れたが、外部から呼ばれても壊れないよう残す。
    """
    try:
        cast = {}
        for c in add_df.columns:
            col = add_df[c]
            if not col.isna().all():
                continue
            want = base[c].dtype if c in base.columns else np.dtype("float64")
            if col.dtype != want:
                cast[c] = want
        if cast:
            return add_df.astype(cast)
    except Exception:
        logger.debug("[push_stream] all-NA dtype align failed", exc_info=True)
    return add_df


def _append_df(row: dict) -> None:
    """PUSH 1 tick をリングへ積む。O(1)。

    ここで DataFrame 化も global_data 同期もしない。
    WS 読み取りスレッドを絶対に待たせないことが目的。
    """
    try:
        if not isinstance(row, dict):
            return
        with state._df_lock:
            state._push_rows.append(row)
            state._push_rows_seq += 1
            # maxlen 付き deque なので MAX_DF_ROWS を超えたぶんは自動で落ちる。
    except Exception:
        logger.exception("[push_stream] dataframe append failed")


def _init_ring_buffer():
    try:
        if PushRingBuffer is not None:
            return PushRingBuffer(maxlen=MAX_DF_ROWS)
    except Exception:
        logger.exception("[push_stream] PushRingBuffer init failed")
    return None
