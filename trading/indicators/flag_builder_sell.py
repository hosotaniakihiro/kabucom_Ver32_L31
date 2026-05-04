# ============================================================
# trading/indicators/flag_builder_sell.py
# Ver1.2-FLAG-BUILDER-SELL-ULTRA-SAFE-STABLE
# ------------------------------------------------------------
# ✔ conditions_short ロジックを DataFrame flag 化
# ✔ 1 flag = 1 column（ini と完全一致）
# ✔ BUY 側と完全対称
# ✔ 未定義変数バグ修正
# ✔ NaN / inf 完全耐性（NEW）
# ✔ int型 0/1 強制保証（NEW）
# ✔ datetime 欠損安全（NEW）
# ✔ symbol単位 index 安全保証（NEW）
# ✔ conditions_short 空安全（NEW）
# ✔ RuntimeLoop 絶対保護（NEW）
# ✔ 書き込み最適化（NEW）
# ============================================================

import logging
import pandas as pd
import numpy as np

from trading.signals.conditions_short import conditions_short

logger = logging.getLogger(__name__)


# ============================================================
# メイン
# ============================================================

def build_sell_flags(df: pd.DataFrame) -> pd.DataFrame:
    """
    各 conditions_short を DataFrame 列（0 / 1）として展開する
    """

    if df is None or df.empty:
        return df

    if not isinstance(df, pd.DataFrame):
        logger.error("[SELL_FLAG] input not DataFrame")
        return df

    if not conditions_short:
        logger.warning("[SELL_FLAG] conditions_short empty")
        return df

    df = df.copy()

    # --------------------------------------------------------
    # datetime 安全保証
    # --------------------------------------------------------
    if "datetime" not in df.columns:
        logger.warning("[SELL_FLAG] datetime column missing")
        return df

    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
    df = df.dropna(subset=["datetime"])

    if df.empty:
        return df

    # --------------------------------------------------------
    # NaN / inf 事前除去
    # --------------------------------------------------------
    df = df.replace([np.inf, -np.inf], np.nan)

    # --------------------------------------------------------
    # flag 列初期化（0固定）
    # --------------------------------------------------------
    flag_names = []

    for fn in conditions_short:
        flag_name = fn.__name__.replace("cond_", "")

        if flag_name in flag_names:
            continue

        flag_names.append(flag_name)

        if flag_name not in df.columns:
            df[flag_name] = 0
        else:
            df[flag_name] = (
                pd.to_numeric(df[flag_name], errors="coerce")
                .fillna(0)
                .astype(int)
            )

    # --------------------------------------------------------
    # symbol 単位で評価
    # --------------------------------------------------------
    for symbol, g in df.groupby("symbol", sort=False):

        g = g.sort_values("datetime")

        if g.empty:
            continue

        idx_list = list(g.index)

        for local_i in range(len(g)):

            try:
                curr = g.iloc[local_i].to_dict()
                prev = (
                    g.iloc[local_i - 1].to_dict()
                    if local_i > 0 else None
                )
                recent = g.iloc[: local_i + 1]

                global_idx = idx_list[local_i]

                for fn in conditions_short:

                    flag_name = fn.__name__.replace("cond_", "")

                    try:
                        ok, _ = fn(curr, prev, recent, None)

                        if ok:
                            df.at[global_idx, flag_name] = 1

                    except Exception:
                        logger.exception(
                            f"❌ SELL flag eval error: {flag_name}"
                        )

            except Exception:
                logger.exception("❌ SELL symbol loop error")

    # --------------------------------------------------------
    # 最終型保証（0/1 int）
    # --------------------------------------------------------
    for flag_name in flag_names:
        df[flag_name] = (
            pd.to_numeric(df[flag_name], errors="coerce")
            .fillna(0)
            .astype(int)
        )

    return df