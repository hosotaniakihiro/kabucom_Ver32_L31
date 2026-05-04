# ============================================================
# File   : trading/backtest/analyze_ma_conf.py
# ------------------------------------------------------------
# ✔ MA 信頼度（ma*_conf）が PnL / 勝率 / PF に与える影響を検証
# ✔ conf 無し / 閾値カット / スコア減衰 の比較が可能
# ✔ ENTRY ログ / TradeHistory / CSV どれでも対応
# ✔ 可視化 + 数値サマリーを同時出力
# ============================================================

import logging
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt

logger = logging.getLogger(__name__)


# ============================================================
# 基本統計
# ============================================================
def _basic_stats(df: pd.DataFrame) -> dict:
    if df.empty:
        return {}

    wins = df[df["pnl"] > 0]
    losses = df[df["pnl"] <= 0]

    gross_profit = wins["pnl"].sum()
    gross_loss = losses["pnl"].sum()

    pf = (
        gross_profit / abs(gross_loss)
        if gross_loss != 0 else np.nan
    )

    return {
        "trades": len(df),
        "win_rate": len(wins) / len(df) if len(df) else 0.0,
        "avg_pnl": df["pnl"].mean(),
        "total_pnl": df["pnl"].sum(),
        "profit_factor": pf,
        "max_drawdown": _max_drawdown(df["pnl"].cumsum()),
    }


def _max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    peak = equity.cummax()
    dd = equity - peak
    return float(dd.min())


# ============================================================
# conf 条件別に分割
# ============================================================
def split_by_conf(
    df: pd.DataFrame,
    *,
    conf_col: str = "ma75_conf",
    thresholds=(0.0, 0.6, 0.7, 0.8),
):
    """
    conf 閾値ごとに DF を分割
    """
    out = {}
    for t in thresholds:
        out[f"conf>={t}"] = df[df[conf_col] >= t]
    return out


# ============================================================
# メイン解析
# ============================================================
def analyze_ma_conf(
    df: pd.DataFrame,
    *,
    conf_col: str = "ma75_conf",
    title: str = "MA Confidence Analysis",
    show_plot: bool = True,
):
    """
    MA 信頼度がトレード成績に与える影響を解析する

    Parameters
    ----------
    df : pd.DataFrame
        必須列:
          - pnl
          - ma75_conf（または指定 conf_col）
    conf_col : str
        使用する信頼度列
    title : str
        グラフタイトル
    show_plot : bool
        matplotlib 表示
    """

    if df is None or df.empty:
        logger.warning("analyze_ma_conf: empty dataframe")
        return {}

    if conf_col not in df.columns or "pnl" not in df.columns:
        raise ValueError(
            f"required columns missing: {conf_col}, pnl"
        )

    df = df.copy()
    df = df.dropna(subset=[conf_col, "pnl"])

    # --------------------------------------------------------
    # 1) 散布図：conf vs pnl
    # --------------------------------------------------------
    if show_plot:
        plt.figure(figsize=(8, 5))
        plt.scatter(df[conf_col], df["pnl"], alpha=0.6)
        plt.axvline(0.6, linestyle="--")
        plt.axvline(0.7, linestyle="--")
        plt.axvline(0.8, linestyle="--")
        plt.xlabel(conf_col)
        plt.ylabel("PnL")
        plt.title(title)
        plt.grid(True)
        plt.show()

    # --------------------------------------------------------
    # 2) 閾値別成績
    # --------------------------------------------------------
    stats = {}
    split = split_by_conf(df, conf_col=conf_col)

    for label, sdf in split.items():
        stats[label] = _basic_stats(sdf)

    # 表形式で表示
    summary = pd.DataFrame(stats).T
    print("\n=== MA Confidence Performance Summary ===")
    print(summary.round(4))

    return summary


# ============================================================
# スコア減衰方式との比較
# ============================================================
def compare_conf_strategies(
    df: pd.DataFrame,
    *,
    conf_col: str = "ma75_conf",
    score_col: str = "raw_score",
    final_score_col: str = "final_score",
):
    """
    conf 無視 / 閾値カット / 減衰スコア の比較

    必須列:
      - pnl
      - raw_score
      - final_score
    """

    required = {"pnl", score_col, final_score_col, conf_col}
    if not required.issubset(df.columns):
        raise ValueError(
            f"required columns missing: {required - set(df.columns)}"
        )

    cases = {
        "A_raw_all": df,
        "B_conf_cut": df[df[conf_col] >= 0.7],
        "C_conf_decay": df[df[final_score_col] > 0],
    }

    results = {}

    for name, sdf in cases.items():
        results[name] = _basic_stats(sdf)

    summary = pd.DataFrame(results).T
    print("\n=== CONF Strategy Comparison ===")
    print(summary.round(4))

    return summary
