# ============================================================
# trading/summary/pseudo_ema_builder.py
# Ver1.0-FINAL-PSEUDO-1M-EMA-BUILDER
# ------------------------------------------------------------
# ✔ RANKING_PSEUDO 疑似1m 専用
# ✔ EMA5 / EMA13 / EMA21 のみ
# ✔ summary / indicator 系と完全分離
# ✔ 副作用ゼロ（列追加のみ）
# ============================================================

import pandas as pd
import logging

logger = logging.getLogger(__name__)

EMA_WINDOWS = (5, 13, 21)


def build_pseudo_ema_1m(df_1m: pd.DataFrame) -> pd.DataFrame:
    """
    疑似1m（RANKING_PSEUDO）専用 EMA builder

    Parameters
    ----------
    df_1m : DataFrame
        confirmed_bar_builder が生成した 1m DataFrame
        source == "RANKING_PSEUDO" のみ対象

    Returns
    -------
    DataFrame
        EMA列を追加した DataFrame（コピー）
    """

    if df_1m is None or df_1m.empty:
        return df_1m

    if "source" not in df_1m.columns:
        return df_1m

    df = df_1m.copy()

    # 疑似1mのみ
    mask = df["source"] == "RANKING_PSEUDO"
    if not mask.any():
        return df

    try:
        df.sort_values(["symbol", "end_time"], inplace=True)

        for w in EMA_WINDOWS:
            col = f"pseudo_ema{w}"
            df.loc[mask, col] = (
                df[mask]
                .groupby("symbol")["close_price"]
                .transform(lambda s: s.ewm(span=w, adjust=False).mean())
            )

        logger.debug(
            "[PSEUDO_EMA] built EMA for symbols=%d rows=%d",
            df.loc[mask, "symbol"].nunique(),
            mask.sum(),
        )

    except Exception:
        logger.exception("[PSEUDO_EMA] build failed")

    return df
