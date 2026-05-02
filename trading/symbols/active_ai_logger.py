# ============================================================
# trading/symbols/active_ai_logger.py
# Ver1.0-FINAL-ACTIVE-AI-LOGGER
# ------------------------------------------------------------
# ✔ ACTIVE_AI 推論結果を CSV に保存
# ✔ 確率分布の可視化・異常検知用
# ✔ STEP9 対応
# ============================================================

from __future__ import annotations

import csv
import datetime as dt
from pathlib import Path


# ============================================================
# ログ保存先
# ============================================================

LOG_DIR = Path("logs/active_ai")
LOG_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# API
# ============================================================

def log_active_ai(
    *,
    symbol: str,
    prob: float,
    allow: bool,
    ranking_score: float,
    summary_count: int,
    turnover: float,
    ai_score: float,
):
    """
    ACTIVE_AI の推論結果を CSV に追記
    """
    today = dt.date.today().strftime("%Y%m%d")
    path = LOG_DIR / f"active_ai_{today}.csv"

    write_header = not path.exists()

    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)

        if write_header:
            writer.writerow([
                "time",
                "symbol",
                "prob",
                "allow",
                "ranking_score",
                "summary_count",
                "turnover",
                "ai_score",
            ])

        writer.writerow([
            dt.datetime.now().isoformat(),
            symbol,
            round(prob, 6),
            int(allow),
            round(ranking_score, 6),
            summary_count,
            round(turnover, 6),
            round(ai_score, 6),
        ])