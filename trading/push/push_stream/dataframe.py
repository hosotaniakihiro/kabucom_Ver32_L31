# ============================================================
# File   : trading/push/push_stream/dataframe.py
# Version: Ver1.0-PUSH-STREAM-DATAFRAME
# ============================================================

from __future__ import annotations

import logging
import numpy as np
import pandas as pd

from .constants import MAX_DF_ROWS
from . import state
from .runtime import _sync_push_df_to_global

logger = logging.getLogger(__name__)

try:
    from trading.push.push_ring_buffer import PushRingBuffer
except Exception:
    PushRingBuffer = None


def get_push_dataframe() -> pd.DataFrame:
    with state._df_lock:
        return state._push_df.copy() if state._push_df is not None and not state._push_df.empty else pd.DataFrame()


def clear_push_dataframe() -> None:
    with state._df_lock:
        state._push_df = pd.DataFrame()

    _sync_push_df_to_global()
    logger.info("[push_stream] push dataframe cleared")



def _align_all_na_dtypes(base: "pd.DataFrame", add_df: "pd.DataFrame") -> "pd.DataFrame":
    """追加行の all-NA 列の dtype を揃えてから concat させる。

    PUSH のメッセージは板つき/板なしで届くキーが変わり、値が来ない項目は
    None になる。1 行の DataFrame ではその列が「全部 NA」になるため、
    pandas が
      FutureWarning: The behavior of DataFrame concatenation with empty or
      all-NA entries is deprecated.
    を出す (本番で 1 日 300 件超)。将来の pandas では all-NA 列も dtype 判定に
    参加するようになるので、いま明示して結果を固定しておく。

    - base に同じ列があれば base の dtype へ寄せる
    - base に無い新しい列は float64 (欠損を表せる数値型) にする
    値は全て NA のままなので、結果の値は変わらない
    (実測: 警告 12 件 -> 2 件、列集合・全列の値ともに一致)。

    add_df は 1 行なので走査コストは列数ぶんだけで、毎tickのホットパスでも軽い。
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
    try:
        with state._df_lock:
            add_df = pd.DataFrame([row])
            if state._push_df is None or state._push_df.empty:
                state._push_df = add_df
            else:
                state._push_df = pd.concat(
                    [state._push_df, _align_all_na_dtypes(state._push_df, add_df)],
                    ignore_index=True,
                )

            if len(state._push_df) > MAX_DF_ROWS:
                state._push_df = state._push_df.tail(MAX_DF_ROWS).reset_index(drop=True)

        _sync_push_df_to_global()

    except Exception:
        logger.exception("[push_stream] dataframe append failed")


def _init_ring_buffer():
    try:
        if PushRingBuffer is not None:
            return PushRingBuffer(maxlen=MAX_DF_ROWS)
    except Exception:
        logger.exception("[push_stream] PushRingBuffer init failed")
    return None