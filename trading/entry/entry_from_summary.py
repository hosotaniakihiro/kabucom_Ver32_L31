# ============================================================
# trading/entry/entry_from_summary.py
# Ver25.1-FINAL-PENDING-MANAGER-ONLY
# ------------------------------------------------------------
# ✔ 定時サマリー由来 ENTRY候補登録
# ✔ 実発注はしない（pending_manager に積むだけ）
# ✔ global_data.pending_entries 直アクセス完全排除
# ✔ dict/list 混入事故を構造的に遮断
# ✔ BUY / SELL 両対応（将来拡張安全）
# ✔ スレッド安全
# ============================================================

from __future__ import annotations

import logging
import pandas as pd
from datetime import datetime, timedelta
from typing import Optional

from trading.entry.pending_manager import add_pending

logger = logging.getLogger(__name__)


# ============================================================
# SUMMARY → pending 登録
# ============================================================
def register_entry_from_summary(
    df: pd.DataFrame,
    interval: int,
    *,
    top_n: int = 5,
    score_threshold: float = 3.0,
    expire_minutes: Optional[int] = None,
):
    """
    定時サマリーで確定した score を元に
    ENTRY候補を pending_manager に登録するだけ

    - 最終判断・発注は entry_controller
    - pending_entries には一切触らない
    """

    # --------------------------------------------------------
    # ガード
    # --------------------------------------------------------
    if df is None or df.empty:
        return

    required_cols = {"symbol", "score_total", "datetime"}
    if not required_cols.issubset(df.columns):
        logger.debug("[SUMMARY→PENDING] required columns missing")
        return

    # --------------------------------------------------------
    # 最新バーのみ
    # --------------------------------------------------------
    latest_dt = df["datetime"].max()
    df_latest = df[df["datetime"] == latest_dt].copy()
    if df_latest.empty:
        return

    # --------------------------------------------------------
    # スコア抽出
    # --------------------------------------------------------
    targets = (
        df_latest[df_latest["score_total"] >= score_threshold]
        .sort_values("score_total", ascending=False)
        .head(top_n)
    )

    if targets.empty:
        return

    now = datetime.now()

    # interval に応じた expire
    if expire_minutes is None:
        expire_minutes = max(1, interval)

    expire_at = now + timedelta(minutes=expire_minutes)

    # --------------------------------------------------------
    # pending_manager 経由で登録
    # --------------------------------------------------------
    for _, row in targets.iterrows():
        sym = str(row.get("symbol"))
        if not sym:
            continue

        # BUY / SELL 判定（将来安全）
        side = (
            row.get("entry_decision")
            or row.get("dominant_side")
            or "BUY"
        )

        if side not in ("BUY", "SELL"):
            continue

        entry = {
            # ---- 必須 ----
            "symbol": sym,
            "side": side,

            # ---- 由来 ----
            "source": "SUMMARY_AI",
            "interval": interval,

            # ---- スコア ----
            "score": float(row.get("score_total", 0)),
            "dominant_ratio": row.get("dominant_ratio"),

            # ---- 時刻 ----
            "datetime": row.get("datetime"),
            "created_at": now,

            # ---- ENTRY 条件 ----
            "entry_conditions": {
                "expire_at": expire_at,
            },

            # ---- 理由（学習・ログ用） ----
            "reason_scores": row.get("reason_scores", {}),
            "entry_reason": row.get("entry_reason", ""),
        }

        if not add_pending(entry):
            continue

        logger.info(
            "[SUMMARY→PENDING] %s %s interval=%d score=%.1f",
            sym,
            side,
            interval,
            entry["score"],
        )
