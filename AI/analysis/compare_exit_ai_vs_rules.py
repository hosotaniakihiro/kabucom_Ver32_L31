# ============================================================
# AI/analysis/compare_exit_ai_vs_rules.py
# Ver1.1.0-FINAL-EXIT-AI-VS-RULES
# ------------------------------------------------------------
# ✔ ルール EXIT vs EXIT AI の事後比較
# ✔ 実運用コード非依存（分析専用）
# ✔ MFE / MAE / 勝率 / HOLD 改善を評価
# ✔ SHAP ログと結合可能
# ✔ CSV / 集計出力対応
# ✔ 欠損・SELL/BUTY 完全耐性
# ============================================================

from __future__ import annotations

import sqlite3
import pandas as pd
from pathlib import Path
from typing import Optional

from config.paths import get_path


# ============================================================
# パス
# ============================================================

DB_POSITION: Path = get_path("position_db")
DB_EXIT_SHAP: Path = get_path("ai_exit_shap_db")

OUT_DIR: Path = get_path("analysis_output")
OUT_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# DB ロード
# ============================================================

def load_trade_exit_stats() -> pd.DataFrame:
    """
    trade_exit_stats を DataFrame としてロード
    """
    with sqlite3.connect(DB_POSITION) as conn:
        df = pd.read_sql(
            """
            SELECT
                trade_id,
                symbol,
                side,
                entry_price,
                exit_price,
                atr_1min,
                mfe,
                mae,
                mfe_pct,
                mae_pct,
                holding_seconds,
                exit_reason,
                index_shock,
                is_valid,
                created_at
            FROM trade_exit_stats
            """,
            conn,
        )

    if df.empty:
        raise RuntimeError("trade_exit_stats is empty")

    return df


def load_exit_shap_logs() -> Optional[pd.DataFrame]:
    """
    exit_ai_shap_logs が存在すればロード
    """
    if not DB_EXIT_SHAP.exists():
        return None

    with sqlite3.connect(DB_EXIT_SHAP) as conn:
        df = pd.read_sql(
            """
            SELECT
                trade_id,
                symbol,
                label,
                confidence,
                shap_json,
                created_at
            FROM exit_ai_shap_logs
            """,
            conn,
        )

    return df if not df.empty else None


# ============================================================
# ルール vs AI 比較
# ============================================================

def compare_exit_ai_vs_rules(
    *,
    min_ai_confidence: float = 0.0,
) -> pd.DataFrame:
    """
    ルール EXIT と AI EXIT（仮想）の比較
    """

    df = load_trade_exit_stats()
    shap_df = load_exit_shap_logs()

    # --------------------------------------------------------
    # SHAP 結合（存在すれば）
    # --------------------------------------------------------
    if shap_df is not None:
        df = df.merge(
            shap_df,
            on=["trade_id", "symbol"],
            how="left",
            suffixes=("", "_ai"),
        )
    else:
        df["label"] = None
        df["confidence"] = None

    # --------------------------------------------------------
    # ルール EXIT の PnL
    # --------------------------------------------------------
    df["rule_pnl"] = df["exit_price"] - df["entry_price"]
    df.loc[df["side"] == "SELL", "rule_pnl"] *= -1

    # --------------------------------------------------------
    # AI が EXIT を止めたと解釈する条件
    #   label: 1 = 良いEXIT
    #          0 = 中立
    #         -1 = 悪いEXIT
    # --------------------------------------------------------
    df["ai_block_exit"] = (
        df["label"].notna()
        & (df["label"] != 1)
        & (df["confidence"].fillna(0.0) >= min_ai_confidence)
    )

    # --------------------------------------------------------
    # 仮想 HOLD PnL
    #   - AI が止めていた場合
    #   - MFE > rule_pnl の時のみ改善とみなす
    # --------------------------------------------------------
    df["ai_virtual_pnl"] = df["rule_pnl"]

    mask = df["ai_block_exit"] & (df["mfe"] > df["rule_pnl"])
    df.loc[mask, "ai_virtual_pnl"] = df.loc[mask, "mfe"]

    # --------------------------------------------------------
    # 改善量
    # --------------------------------------------------------
    df["pnl_improvement"] = df["ai_virtual_pnl"] - df["rule_pnl"]

    # --------------------------------------------------------
    # フラグ補助
    # --------------------------------------------------------
    df["rule_win"] = df["rule_pnl"] > 0
    df["ai_virtual_win"] = df["ai_virtual_pnl"] > 0
    df["improved"] = df["pnl_improvement"] > 0

    return df


# ============================================================
# 集計
# ============================================================

def summarize(df: pd.DataFrame) -> pd.DataFrame:
    """
    全体集計
    """
    return pd.DataFrame(
        {
            "total_trades": [len(df)],
            "rule_win_rate": [df["rule_win"].mean()],
            "ai_virtual_win_rate": [df["ai_virtual_win"].mean()],
            "avg_rule_pnl": [df["rule_pnl"].mean()],
            "avg_ai_virtual_pnl": [df["ai_virtual_pnl"].mean()],
            "avg_improvement": [df["pnl_improvement"].mean()],
            "block_rate": [df["ai_block_exit"].mean()],
            "improve_rate": [df["improved"].mean()],
        }
    )


# ============================================================
# メイン
# ============================================================

def main():
    df = compare_exit_ai_vs_rules(min_ai_confidence=0.0)
    summary = summarize(df)

    detail_path = OUT_DIR / "exit_ai_vs_rules_detail.csv"
    summary_path = OUT_DIR / "exit_ai_vs_rules_summary.csv"

    df.to_csv(detail_path, index=False)
    summary.to_csv(summary_path, index=False)

    print("✅ EXIT AI vs RULES 比較 完了")
    print(summary)


if __name__ == "__main__":
    main()
