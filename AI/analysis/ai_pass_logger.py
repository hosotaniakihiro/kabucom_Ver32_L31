# ============================================================
# AI/analytics/ai_pass_logger.py
# ------------------------------------------------------------
# ✔ TONOSAMA → FINAL_AI 通過ログ
# ✔ 時間帯別 / 銘柄別 / BUY-SELL 別 集計用
# ✔ CSV（人間可読・将来DB化前提）
# ✔ entry_controller から安全に呼び出し可能
# ============================================================

import csv
import os
from datetime import datetime

# ============================================================
# 保存先
# ============================================================
BASE_DIR = "AI/logs"
os.makedirs(BASE_DIR, exist_ok=True)

CSV_PATH = os.path.join(BASE_DIR, "ai_pass_log.csv")

# ============================================================
# CSV ヘッダ
# ============================================================
_HEADER = [
    "datetime",     # 判定時刻
    "hour",         # 時間帯（0-23）
    "symbol",       # 銘柄
    "side",         # BUY / SELL
    "stage",        # tonosama_select / final_ai
    "result",       # PASS / BLOCK
    "score",        # score_total
    "confidence",   # AI信頼度
    "reason",       # BLOCK理由（任意）
]

# ============================================================
# ログ記録
# ============================================================
def log_ai_pass(
    symbol: str,
    side: str,
    stage: str,
    passed: bool,
    score: float = 0.0,
    confidence: float = 0.0,
    reason: str = "",
):
    """
    AI 判定ログを1行追記する
    - entry_controller から呼び出される前提
    - 例外は握りつぶさず、そのまま上位に返す
    """

    now = datetime.now()

    row = [
        now.strftime("%Y-%m-%d %H:%M:%S"),
        now.hour,
        str(symbol),
        str(side),
        str(stage),
        "PASS" if passed else "BLOCK",
        float(score),
        float(confidence),
        str(reason) if reason else "",
    ]

    write_header = not os.path.exists(CSV_PATH)

    with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow(_HEADER)
        writer.writerow(row)
