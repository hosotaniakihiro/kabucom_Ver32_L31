# ============================================================
# AI/train/build_ranking_promotion_train_df.py
# ------------------------------------------------------------
# ✔ ranking 昇格 → ENTRY → EXIT の完全教師データ生成
# ✔ tosama DB（ranking_promotion_label）専用
# ✔ classification / regression 両対応
# ✔ 学習前段の唯一の正規データ生成点
# ============================================================

import logging
import pandas as pd
from sqlalchemy import text

from database.session import tosama_engine

logger = logging.getLogger(__name__)


# ============================================================
# メイン：学習用 DataFrame 構築
# ============================================================
def build_ranking_promotion_train_df(
    *,
    require_exit: bool = True,
) -> pd.DataFrame:
    """
    ranking 昇格学習用 DataFrame を生成

    Parameters
    ----------
    require_exit : bool
        True  : EXIT 完了（pnl 確定）のみ使用（本番学習用）
        False : ENTRY 成否のみ（暫定分析用）

    Returns
    -------
    pd.DataFrame
    """

    # --------------------------------------------------------
    # SQL（ADD ONLY 前提・安全）
    # --------------------------------------------------------
    sql = """
        SELECT
            symbol,
            triggered_at,
            result,              -- ENTRY 成否（0/1）
            reason,

            -- EXIT 結果（NULL 許容）
            exit_price,
            pnl,
            hold_seconds,

            -- ranking 特徴量（保存済み前提）
            ranking_strength,
            volume_speed,
            market,
            rank_type,

            created_at
        FROM ranking_promotion_label
    """

    if require_exit:
        sql += " WHERE pnl IS NOT NULL"

    # --------------------------------------------------------
    # DB 読み込み
    # --------------------------------------------------------
    try:
        with tosama_engine.connect() as conn:
            df = pd.read_sql(text(sql), conn)
    except Exception:
        logger.exception("❌ ranking promotion train df load failed")
        return pd.DataFrame()

    if df.empty:
        logger.warning("⚠ ranking promotion train df is empty")
        return df

    # --------------------------------------------------------
    # 型正規化（学習事故防止）
    # --------------------------------------------------------
    numeric_cols = [
        "ranking_strength",
        "volume_speed",
        "pnl",
        "hold_seconds",
    ]

    for c in numeric_cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")

    # --------------------------------------------------------
    # ラベル生成
    # --------------------------------------------------------
    # classification：儲かったか？
    if "pnl" in df.columns:
        df["label_profit"] = (df["pnl"] > 0).astype(int)

    # ENTRY 成功ラベル（補助）
    df["label_entry"] = df["result"].astype(int)

    # --------------------------------------------------------
    # 欠損補完（LightGBM 安全）
    # --------------------------------------------------------
    df["ranking_strength"] = df["ranking_strength"].fillna(0)
    df["volume_speed"] = df["volume_speed"].fillna(0.0)
    df["hold_seconds"] = df["hold_seconds"].fillna(0)

    # --------------------------------------------------------
    # カテゴリ特徴量（将来 One-Hot / categorical 対応）
    # --------------------------------------------------------
    for col in ("market", "rank_type"):
        if col in df.columns:
            df[col] = df[col].fillna("UNKNOWN")

    # --------------------------------------------------------
    # 並び替え（時系列リーク防止用）
    # --------------------------------------------------------
    if "triggered_at" in df.columns:
        df = df.sort_values("triggered_at").reset_index(drop=True)

    logger.info(
        "✅ ranking promotion train df built rows=%d symbols=%d",
        len(df),
        df["symbol"].nunique(),
    )

    return df


# ============================================================
# 単体実行（デバッグ用）
# ============================================================
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    df = build_ranking_promotion_train_df(require_exit=True)
    print(df.head())
    print("rows =", len(df))
