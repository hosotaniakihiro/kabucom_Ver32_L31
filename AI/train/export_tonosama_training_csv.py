# ============================================================
# File   : AI/train/export_tonosama_training_csv.py
# ------------------------------------------------------------
# ✔ TONOSAMA 学習用 CSV 生成
# ✔ 本番トレード結果 → 教師データ変換（ETL）
# ✔ 市場終了後バッチ専用
# ✔ trading / inference からは一切呼ばない
# ============================================================

import logging
from pathlib import Path

import pandas as pd

from database.session import Session_position
from database.models import TosamaTradeLog

# ============================================================
# ログ設定
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


# ============================================================
def export_tonosama_training_csv(
    path: str = "AI/train/tosama_train.csv",
    *,
    min_profit_pct: float = 0.005,
    max_hold_sec: int = 30,
):
    """
    TONOSAMA 学習用 CSV を生成する

    label 定義:
      1 = 即益成功（短時間・十分な利益）
      0 = その他
    """

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    session = Session_position()

    try:
        rows = session.query(TosamaTradeLog).all()
        logger.info(f"[TONOSAMA TRAIN] loaded rows={len(rows)}")

        data = []

        for r in rows:
            # -----------------------------
            # 必須項目チェック
            # -----------------------------
            if not r.entry_price or not r.exit_price:
                continue

            if not r.entry_time or r.hold_seconds is None:
                continue

            # -----------------------------
            # 損益率
            # -----------------------------
            try:
                pnl_pct = (r.exit_price - r.entry_price) / r.entry_price
            except Exception:
                continue

            # -----------------------------
            # ラベル生成（最重要）
            # -----------------------------
            label = 1 if (
                pnl_pct >= min_profit_pct
                and r.hold_seconds <= max_hold_sec
            ) else 0

            # -----------------------------
            # 1レコード生成
            # -----------------------------
            data.append({
                # --- 識別 ---
                "symbol": str(r.symbol),

                # --- 特徴量 ---
                "volume_speed": r.volume_speed,
                "fast_ret": r.fast_ret,
                "rank_position": r.rank_position,
                "price": r.entry_price,
                "spread": r.spread,
                "entry_second": r.entry_time.second,

                # --- 結果 ---
                "hold_seconds": r.hold_seconds,
                "pnl_pct": pnl_pct,

                # --- 教師 ---
                "label": label,
            })

        if not data:
            logger.warning("[TONOSAMA TRAIN] no valid records")
            return

        df = pd.DataFrame(data)

        # -----------------------------
        # 欠損処理（安全）
        # -----------------------------
        df = df.dropna().reset_index(drop=True)

        # -----------------------------
        # CSV 出力
        # -----------------------------
        df.to_csv(
            path,
            index=False,
            encoding="utf-8-sig",
        )

        logger.info(
            f"[TONOSAMA TRAIN] CSV exported -> {path} "
            f"rows={len(df)}"
        )

    finally:
        session.close()


# ============================================================
# 単体実行（市場終了後）
# ============================================================
if __name__ == "__main__":
    export_tonosama_training_csv()
