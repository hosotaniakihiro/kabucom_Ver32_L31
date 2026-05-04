# ============================================================
# AI/monitoring/ai_performance_monitor.py
# ------------------------------------------------------------
# STEP3-① AI 精度モニタリング & 劣化検知
#
# ・ENTRY / EXIT 実績から AI 精度を日次集計
# ・劣化を自動検知（NORMAL / WARNING / CRITICAL）
# ・他AIが参照できる状態JSONを生成
# ============================================================

import sqlite3
import json
import math
import logging
from pathlib import Path
from datetime import datetime, timedelta

import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)

# ============================================================
# PATH
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parents[2]

ENTRY_DB = PROJECT_ROOT / "AI" / "data" / "ai_entry_events.db"
STATE_DIR = PROJECT_ROOT / "AI" / "monitor"
STATE_DIR.mkdir(parents=True, exist_ok=True)

STATE_FILE = STATE_DIR / "ai_health_state.json"

# ============================================================
# PARAMS（調整しやすい）
# ============================================================

LOOKBACK_TRADES = 50
WINRATE_DROP_PCT = 0.15        # 15% 勝率低下
RMSE_MULTIPLIER = 1.5          # HOLDTIME 劣化判定
MIN_TRADES = 20

# ============================================================
# UTIL
# ============================================================

def _rmse(y_true, y_pred):
    return math.sqrt(np.mean((y_true - y_pred) ** 2))

def _mae(y_true, y_pred):
    return np.mean(np.abs(y_true - y_pred))

# ============================================================
# LOAD ENTRY EVENTS
# ============================================================

def load_entry_events() -> pd.DataFrame:
    if not ENTRY_DB.exists():
        return pd.DataFrame()

    with sqlite3.connect(ENTRY_DB) as con:
        df = pd.read_sql("SELECT * FROM entry_events", con)

    if df.empty:
        return df

    df["datetime"] = pd.to_datetime(df["datetime"])
    return df

# ============================================================
# LOAD EXIT LOG（TradeHistory / ExitLog 結果）
# ※ positions DB を参照（最低限 pnl / holding_seconds）
# ============================================================

def load_exit_results() -> pd.DataFrame:
    # 環境依存のため最小構成
    # → ai_entry_events 側に pnl を後追いで入れる設計も可
    # 今回は features_json 内に pnl が入っている前提で処理

    return pd.DataFrame()

# ============================================================
# METRIC CALC
# ============================================================

def calc_metrics(df: pd.DataFrame) -> dict:

    metrics = {}

    if len(df) < MIN_TRADES:
        metrics["status"] = "INSUFFICIENT_DATA"
        return metrics

    # --------------------------------------------------------
    # 勝敗
    # --------------------------------------------------------
    df["win"] = df.get("pnl", 0) > 0

    winrate = df["win"].mean()
    avg_pnl = df.get("pnl", pd.Series()).mean()

    metrics["winrate"] = round(float(winrate), 4)
    metrics["avg_pnl"] = round(float(avg_pnl), 4)

    # --------------------------------------------------------
    # AI通過後成績
    # --------------------------------------------------------
    if "ai_pass" in df:
        passed = df[df["ai_pass"] == 1]
        if not passed.empty:
            metrics["ai_pass_winrate"] = round(float(passed["win"].mean()), 4)
            metrics["ai_pass_count"] = int(len(passed))

    # --------------------------------------------------------
    # HOLDTIME 精度
    # --------------------------------------------------------
    if {"pred_hold_sec", "holding_seconds"}.issubset(df.columns):
        y_true = df["holding_seconds"].astype(float)
        y_pred = df["pred_hold_sec"].astype(float)

        metrics["hold_mae"] = round(_mae(y_true, y_pred), 2)
        metrics["hold_rmse"] = round(_rmse(y_true, y_pred), 2)

    return metrics

# ============================================================
# DEGRADATION CHECK
# ============================================================

def judge_health(
    recent: dict,
    baseline: dict | None,
) -> str:
    """
    return: NORMAL / WARNING / CRITICAL
    """

    if not baseline:
        return "NORMAL"

    # 勝率劣化
    if (
        "winrate" in recent
        and "winrate" in baseline
        and recent["winrate"] < baseline["winrate"] * (1 - WINRATE_DROP_PCT)
    ):
        return "WARNING"

    # HOLDTIME RMSE 劣化
    if (
        "hold_rmse" in recent
        and "hold_rmse" in baseline
        and recent["hold_rmse"] > baseline["hold_rmse"] * RMSE_MULTIPLIER
    ):
        return "CRITICAL"

    return "NORMAL"

# ============================================================
# MAIN
# ============================================================

def run_ai_performance_monitor():

    logger.info("🧠 AI PERFORMANCE MONITOR START")

    df = load_entry_events()
    if df.empty:
        logger.warning("No entry events")
        return

    # 直近Nトレード
    recent_df = df.sort_values("datetime").tail(LOOKBACK_TRADES)

    recent_metrics = calc_metrics(recent_df)

    # ベースライン（過去30日）
    cutoff = datetime.now() - timedelta(days=30)
    baseline_df = df[df["datetime"] >= cutoff]

    baseline_metrics = (
        calc_metrics(baseline_df)
        if len(baseline_df) >= MIN_TRADES
        else None
    )

    health = judge_health(recent_metrics, baseline_metrics)

    state = {
        "timestamp": datetime.now().isoformat(),
        "health": health,
        "recent": recent_metrics,
        "baseline": baseline_metrics,
    }

    STATE_FILE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    logger.info(f"AI HEALTH = {health}")
    logger.info(f"STATE SAVED → {STATE_FILE}")

    return state


# ============================================================
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_ai_performance_monitor()
