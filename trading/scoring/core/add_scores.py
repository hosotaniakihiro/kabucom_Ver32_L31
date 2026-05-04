# ============================================================
# trading/scoring/core/add_scores.py
# Ver27.6-ABSOLUTE-PRODUCTION-HARDENED
# ------------------------------------------------------------
# ✔ Ver27.5 全機能完全保持（削除ゼロ）
# ✔ ini(score_config.ini) 駆動（SCORE_TABLE）
# ✔ row[flag] == 1 / True / "1" / float 完全対応
# ✔ if 文による個別条件分岐を完全排除
# ✔ BUY / SELL 共通
# ✔ 重複加点完全防止（1キー1回）
# ✔ 未実装 flag は DEBUG ログで静かに無視
# ✔ scoring_core 完全互換（DataFrame in / out）
# ✔ score_total 正式列
# ✔ score 列を後方互換として自動生成
# ✔ NaN / None / 型安全
# ✔ 高速ベクトル処理
# ✔ FLAG 自動生成
# ✔ 列名ゆらぎ吸収
# ✔ slope_atr_scaled 対応
# ✔ flag 上書き防止
# ✔ index非連番完全対応
# ✔ SCORE_TABLEキー大小ゆらぎ対応
# ✔ PRO FLAG GENERATOR 統合
# ✔ 巨大DataFrame耐性
# ✔ bool / int / float flag 完全対応
# ✔ mask高速化
# ✔ reason dict安全化
# ============================================================

from __future__ import annotations

import logging
import pandas as pd
import numpy as np

from trading.scoring.config.score_table import SCORE_TABLE
from trading.scoring.flags.flag_generator import generate_all_flags

logger = logging.getLogger(__name__)


# ============================================================
# 列名ゆらぎ吸収
# ============================================================

def _col(df: pd.DataFrame, *names):

    lower_map = {c.lower(): c for c in df.columns}

    for n in names:

        if n in df.columns:
            return n

        if n.lower() in lower_map:
            return lower_map[n.lower()]

    return None


# ============================================================
# 基本FLAG生成
# ============================================================

def _generate_scoring_flags(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    df = df.copy()

    close_col = _col(df, "close", "close_price")
    ma75_col = _col(df, "ma75")
    vwap_col = _col(df, "vwap")

    slope_col = _col(
        df,
        "slope_atr_scaled",
        "ma75_slope",
        "slope",
    )

    mtf_col = _col(df, "mtf_score", "mtf")

    atr_col = _col(df, "atr")

    # --------------------------------------------------------
    # slope
    # --------------------------------------------------------

    if slope_col and "flag_slope_positive" not in df.columns:

        s = pd.to_numeric(df[slope_col], errors="coerce").fillna(0)

        df["flag_slope_positive"] = (s > 0).astype(int)
        df["flag_slope_strong"] = (s > 1).astype(int)

    # --------------------------------------------------------
    # mtf
    # --------------------------------------------------------

    if mtf_col and "flag_mtf_positive" not in df.columns:

        m = pd.to_numeric(df[mtf_col], errors="coerce").fillna(0)

        df["flag_mtf_positive"] = (m > 0).astype(int)

    # --------------------------------------------------------
    # MA75
    # --------------------------------------------------------

    if close_col and ma75_col and "flag_above_ma75" not in df.columns:

        c = pd.to_numeric(df[close_col], errors="coerce")
        m = pd.to_numeric(df[ma75_col], errors="coerce")

        df["flag_above_ma75"] = (c > m).astype(int)

    # --------------------------------------------------------
    # VWAP
    # --------------------------------------------------------

    if close_col and vwap_col and "flag_above_vwap" not in df.columns:

        c = pd.to_numeric(df[close_col], errors="coerce")
        v = pd.to_numeric(df[vwap_col], errors="coerce")

        df["flag_above_vwap"] = (c > v).astype(int)

    # --------------------------------------------------------
    # ATR volatility
    # --------------------------------------------------------

    if atr_col and "flag_volatility_high" not in df.columns:

        a = pd.to_numeric(df[atr_col], errors="coerce").fillna(0)

        median = a.median() if len(a) else 0

        df["flag_volatility_high"] = (a > median).astype(int)

    return df


# ============================================================
# MAIN
# ============================================================

def add_all_scores(df: pd.DataFrame) -> pd.DataFrame:
    print("FLAGS:", [c for c in df.columns if c.startswith("flag_")])
    print("SCORE_TABLE:", SCORE_TABLE)
    if df is None or df.empty:
        return df

    #logger.info(f"[FLAG COLS] {list(df.columns)}")
    #logger.info(f"[SCORE TABLE] {list(SCORE_TABLE.keys())}")

    df_out = df.copy().reset_index(drop=True)

    # ========================================================
    # PRO FLAG GENERATOR
    # ========================================================

    try:
        df_out = generate_all_flags(df_out)
    except Exception:
        logger.exception("[FLAG GEN] failed")

    # ========================================================
    # BASIC FLAGS
    # ========================================================

    df_out = _generate_scoring_flags(df_out)

    # ========================================================
    # 初期化
    # ========================================================

    n = len(df_out)

    df_out["score_total"] = np.zeros(n, dtype="int32")
    df_out["score_reasons"] = [{} for _ in range(n)]

    # ========================================================
    # SCORE_TABLE 正規化
    # ========================================================

    table = {str(k).lower(): v for k, v in SCORE_TABLE.items()}

    col_map = {c.lower(): c for c in df_out.columns}

    # ========================================================
    # FLAG → SCORE
    # ========================================================

    for key_lower, value in table.items():

        if key_lower not in col_map:

            logger.debug(f"[SCORING] flag not in df: {key_lower}")

            continue

        real_col = col_map[key_lower]

        try:

            weight = int(value)

            col = df_out[real_col]

            # ------------------------------------------------
            # 型安全化
            # ------------------------------------------------

            numeric = pd.to_numeric(col, errors="coerce")

            mask = (
                (numeric == 1)
                | (numeric > 0)
                | (col == True)
                | (col == "1")
            )

            if not mask.any():
                continue

            df_out.loc[mask, "score_total"] += weight

            idx_list = df_out.index[mask]

            for idx in idx_list:

                r = df_out.at[idx, "score_reasons"]

                if real_col not in r:
                    r[real_col] = weight

        except Exception:

            logger.exception(f"[SCORING] error flag: {real_col}")

    # ========================================================
    # 型保証
    # ========================================================

    df_out["score_total"] = (
        pd.to_numeric(df_out["score_total"], errors="coerce")
        .replace([np.inf, -np.inf], 0)
        .fillna(0)
        .astype(int)
    )

    # 後方互換

    df_out["score"] = df_out["score_total"]

    return df_out