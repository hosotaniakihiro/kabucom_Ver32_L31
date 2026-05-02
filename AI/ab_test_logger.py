# ============================================================
# File: AI/ab_test_logger.py
# ------------------------------------------------------------
# 殿様イナゴ ABテスト用 ロガー
#
# ✔ ENTRY 判断時の確率・スコアを保存
# ✔ 実トレード結果（勝敗）と後付けで突合可能
# ✔ モデル・閾値調整は「後処理のみ」で実施
# ✔ 本番ロジックに一切の副作用を与えない
# ============================================================

from __future__ import annotations

import csv
import datetime as dt
from pathlib import Path
from typing import Optional


# ============================================================
# 設定
# ============================================================

LOG_DIR = Path("logs")
LOG_FILE = LOG_DIR / "tonosama_ab_log.csv"

FIELDS = [
    "timestamp",
    "symbol",
    "side",
    "prob",
    "score",
    "volume_speed",
    "dominant_ratio",
    "spread_ratio",
    "decision",
    "result",   # 後から埋める（1=win / 0=loss / -1=unknown）
]


# ============================================================
# 初期化
# ============================================================

def _ensure_file():
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    if not LOG_FILE.exists():
        with LOG_FILE.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS)
            writer.writeheader()


# ============================================================
# メイン API
# ============================================================

def log_ab_decision(
    *,
    symbol: str,
    side: str,
    prob: float,
    score: float,
    volume_speed: float,
    dominant_ratio: float,
    spread_ratio: float,
    decision: bool,
    result: int = -1,
    timestamp: Optional[dt.datetime] = None,
) -> None:
    """
    ENTRY 判断時点の情報を AB テスト用に保存する

    Parameters
    ----------
    symbol : str
        銘柄コード
    side : str
        "BUY" or "SELL"
    prob : float
        LightGBM の出力確率
    score : float
        prob × 勢い の統合スコア
    volume_speed : float
        出来高スピード
    dominant_ratio : float
        買い/売り板優勢度
    spread_ratio : float
        スプレッド比率
    decision : bool
        ENTRY 判定（True=入った / False=見送った）
    result : int
        トレード結果
        1=勝ち / 0=負け / -1=未確定
    timestamp : datetime, optional
        指定がなければ現在時刻
    """

    _ensure_file()

    if timestamp is None:
        timestamp = dt.datetime.now()

    row = {
        "timestamp": timestamp.isoformat(),
        "symbol": symbol,
        "side": side,
        "prob": f"{prob:.6f}",
        "score": f"{score:.6f}",
        "volume_speed": f"{volume_speed:.4f}",
        "dominant_ratio": f"{dominant_ratio:.4f}",
        "spread_ratio": f"{spread_ratio:.6f}",
        "decision": int(decision),
        "result": int(result),
    }

    with LOG_FILE.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writerow(row)