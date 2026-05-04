# ============================================================
# trading/signals/conditions_runner.py
# Ver24-FINAL-FLAG-EXPAND-STABLE
# ------------------------------------------------------------
# ✔ BUY / SELL 条件を正しいコンテキストで評価
# ✔ prev / recent を必ず渡す
# ✔ dict / list 両対応
# ✔ conditions(list[str]) を付与
# ✔ 条件名を 0/1 フラグ列として自動展開
# ✔ dtype / 初期化 / 例外耐性を強化
# ============================================================

import pandas as pd
import logging

from trading.signals.conditions_long_trend import conditions_long_trend
from trading.signals.conditions_long_patterns import conditions_long_patterns
from trading.signals.conditions_short_patterns import conditions_short_patterns

logger = logging.getLogger(__name__)


# ============================================================
def _normalize_conditions(obj):
    """
    conditions 定義を dict[name -> func] に正規化
    """
    if isinstance(obj, dict):
        return obj
    if isinstance(obj, (list, tuple)):
        return {func.__name__: func for func in obj}
    return {}


# ============================================================
def apply_all_conditions(df: pd.DataFrame, side: str) -> pd.DataFrame:
    """
    side: BUY / SELL
    戻り値: df + conditions(list[str]) + flag columns
    """
    if df is None or df.empty:
        return df

    # ソート・コピー
    df = (
        df.sort_values(["symbol", "datetime"])
        .reset_index(drop=True)
        .copy()
    )

    # --------------------------------------------------------
    # 条件統合
    # --------------------------------------------------------
    all_conditions = {}

    if side == "BUY":
        all_conditions.update(_normalize_conditions(conditions_long_trend))
        all_conditions.update(_normalize_conditions(conditions_long_patterns))

    elif side == "SELL":
        all_conditions.update(_normalize_conditions(conditions_short_patterns))

    else:
        df["conditions"] = [[] for _ in range(len(df))]
        return df

    condition_names = list(all_conditions.keys())

    # --------------------------------------------------------
    # 初期化（conditions + flag）
    # --------------------------------------------------------
    df["conditions"] = [[] for _ in range(len(df))]

    for name in condition_names:
        # ★ 必ず列を作り、int 0 で初期化
        if name not in df.columns:
            df[name] = 0
        else:
            df[name] = 0

    # --------------------------------------------------------
    # 条件評価（symbol 単位）
    # --------------------------------------------------------
    for symbol, g in df.groupby("symbol", sort=False):
        g = g.reset_index()
        idx_map = g["index"].tolist()

        for i in range(len(g)):
            curr = g.loc[i]
            prev = g.loc[i - 1] if i > 0 else None
            recent = g.loc[: i - 1] if i >= 2 else None

            matched = []

            for name, func in all_conditions.items():
                try:
                    result = func(curr, prev, recent)

                    # bool / numpy.bool_ を True として扱う
                    if result is True:
                        matched.append(name)
                        df.at[idx_map[i], name] = 1

                except Exception as e:
                    # 条件1個の例外で全体を止めない
                    logger.debug(
                        "[CONDITION ERROR] %s %s index=%s error=%s",
                        side,
                        name,
                        idx_map[i],
                        e,
                    )
                    continue

            df.at[idx_map[i], "conditions"] = matched

    return df
