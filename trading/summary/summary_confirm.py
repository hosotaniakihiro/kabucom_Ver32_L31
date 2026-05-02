# ============================================================
# File: trading/summary/summary_confirm.py
# Ver1.0-FINAL-PUSH-RANKING-CONFIRM-SAFE
# ------------------------------------------------------------
# ✔ PUSH / RANKING 両立銘柄の一致判定
# ✔ DataFrame 非破壊（copy）
# ✔ None / empty / 型ズレ完全耐性
# ✔ 表示（summary_printer）・ENTRY 両用
# ✔ PUSH 起点設計（confirm_pr は PUSH 側に付与）
# ============================================================

from __future__ import annotations

import pandas as pd
import logging

logger = logging.getLogger(__name__)


# ============================================================
# メインAPI
# ============================================================
def mark_push_rank_confirm(
    df_push: pd.DataFrame | None,
    df_rank: pd.DataFrame | None,
    *,
    symbol_col: str = "symbol",
    flag_col: str = "confirm_pr",
) -> pd.DataFrame | None:
    """
    PUSH / RANKING 両方に存在する銘柄に confirm_pr=True を付与する

    Parameters
    ----------
    df_push : pd.DataFrame | None
        PUSHベースのサマリー（主データ）
    df_rank : pd.DataFrame | None
        RANKINGベースのサマリー（参照用）
    symbol_col : str
        銘柄コード列名（default: symbol）
    flag_col : str
        付与する一致フラグ列名（default: confirm_pr）

    Returns
    -------
    pd.DataFrame | None
        confirm_pr 列を付与した PUSH サマリー（copy）
        df_push が None の場合は None を返す
    """

    # --------------------------------------------------------
    # PUSH が無い場合は何もできない
    # --------------------------------------------------------
    if df_push is None:
        return None

    if df_push.empty:
        # 空DFでも列だけは保証
        df = df_push.copy()
        if flag_col not in df.columns:
            df[flag_col] = False
        return df

    df = df_push.copy()

    # --------------------------------------------------------
    # フラグ列を必ず初期化
    # --------------------------------------------------------
    df[flag_col] = False

    # --------------------------------------------------------
    # RANKING が無い場合はそのまま返す
    # --------------------------------------------------------
    if df_rank is None or df_rank.empty:
        return df

    if symbol_col not in df.columns or symbol_col not in df_rank.columns:
        logger.warning(
            f"[summary_confirm] symbol column not found "
            f"(push={symbol_col in df.columns}, rank={symbol_col in df_rank.columns})"
        )
        return df

    # --------------------------------------------------------
    # 銘柄集合の作成（str正規化）
    # --------------------------------------------------------
    try:
        push_syms = set(df[symbol_col].astype(str))
        rank_syms = set(df_rank[symbol_col].astype(str))
    except Exception as e:
        logger.exception(f"[summary_confirm] symbol normalize failed: {e}")
        return df

    confirmed_syms = push_syms & rank_syms
    if not confirmed_syms:
        return df

    # --------------------------------------------------------
    # confirm_pr 付与
    # --------------------------------------------------------
    df.loc[
        df[symbol_col].astype(str).isin(confirmed_syms),
        flag_col
    ] = True

    return df


# ============================================================
# 補助API（ENTRY用：単銘柄判定）
# ============================================================
def is_push_rank_confirm(
    symbol: str | int,
    df_rank: pd.DataFrame | None,
    *,
    symbol_col: str = "symbol",
) -> bool:
    """
    単一銘柄が RANKING サマリーにも存在するかを判定する
    ENTRY直前・軽量チェック用
    """

    if df_rank is None or df_rank.empty:
        return False

    if symbol_col not in df_rank.columns:
        return False

    try:
        sym = str(symbol)
        return sym in set(df_rank[symbol_col].astype(str))
    except Exception:
        return False