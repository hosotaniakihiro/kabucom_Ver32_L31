# ============================================================
# File: AI/nightly_retrain_tonosama.py
# ------------------------------------------------------------
# 殿様イナゴ（BUY）専用 日次自動再学習スクリプト
#
# ✔ 市場クローズ後のみ実行
# ✔ 学習CSV生成 → LightGBM再学習 を自動実行
# ✔ 日中トレードには一切影響しない
# ✔ 失敗しても既存モデルは保持（安全設計）
# ============================================================

from __future__ import annotations

import subprocess
import datetime as dt
import sys
import os
from pathlib import Path


# ============================================================
# 設定
# ============================================================

# Python 実行コマンド（仮想環境対応）
PYTHON_BIN = os.environ.get("PYTHON_BIN", sys.executable)

# 学習CSV生成スクリプト
BUILD_TRAIN_SCRIPT = Path(
    "AI/tonosama_build_train_csv.py"
)

# 学習スクリプト
TRAIN_SCRIPT = Path(
    "AI/tonosama_train_lgbm.py"
)

# ログ
LOG_DIR = Path("logs")
LOG_FILE = LOG_DIR / "nightly_retrain_tonosama.log"


# ============================================================
# ユーティリティ
# ============================================================

def _log(msg: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(f"[{ts}] {msg}\n")


def _run(cmd: list[str]) -> None:
    """
    サブプロセス実行（失敗時は例外）
    """
    _log(f"RUN: {' '.join(cmd)}")
    subprocess.run(cmd, check=True)


def _is_market_closed(now: dt.datetime) -> bool:
    """
    市場クローズ後かを判定
    """
    # 平日 15:30 以降のみ許可
    if now.weekday() >= 5:  # 土日
        return False

    close_time = now.replace(
        hour=15, minute=30, second=0, microsecond=0
    )
    return now >= close_time


# ============================================================
# メイン処理
# ============================================================

def nightly_retrain() -> None:
    now = dt.datetime.now()

    # --------------------------------------------------------
    # 時間ガード
    # --------------------------------------------------------
    if not _is_market_closed(now):
        _log("SKIP: market is open or not closed yet")
        return

    _log("START nightly retrain")

    try:
        # ----------------------------------------------------
        # ① 学習CSV生成
        # ----------------------------------------------------
        if not BUILD_TRAIN_SCRIPT.exists():
            raise FileNotFoundError(
                f"build script not found: {BUILD_TRAIN_SCRIPT}"
            )

        _run([
            PYTHON_BIN,
            str(BUILD_TRAIN_SCRIPT),
        ])

        # ----------------------------------------------------
        # ② モデル再学習
        # ----------------------------------------------------
        if not TRAIN_SCRIPT.exists():
            raise FileNotFoundError(
                f"train script not found: {TRAIN_SCRIPT}"
            )

        _run([
            PYTHON_BIN,
            str(TRAIN_SCRIPT),
        ])

        _log("SUCCESS nightly retrain")

    except Exception as e:
        # 失敗しても既存モデルは残す
        _log(f"ERROR nightly retrain: {e}")
        _log("KEEP existing model")


# ============================================================
# エントリーポイント
# ============================================================

if __name__ == "__main__":
    nightly_retrain()