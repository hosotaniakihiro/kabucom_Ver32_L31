# ============================================================
# File   : trading/execution/execution_loop.py
# Version: Ver1.0.0-PRO-ASYNC-EXECUTION
# ------------------------------------------------------------
# ✔ score_cache 消費
# ✔ ENTRY/EXIT 完全分離
# ✔ 重複発火防止
# ✔ クールダウン管理
# ✔ AI拡張可能
# ✔ scheduler絶対停止しない
# ============================================================

from __future__ import annotations

import time
import logging
import datetime as dt
import pandas as pd

from global_state import global_data

# 既存エントリー関数（あなたの構成に合わせて）
from trading.handlers.entry_controller import run_entry_pipeline
from trading.exit.exit_loop import exit_loop

logger = logging.getLogger(__name__)

POLL_INTERVAL = 0.2
COOLDOWN_SECONDS = 10

# symbol別最終発火時刻
_last_action_time: dict[str, dt.datetime] = {}


# ============================================================
# 内部：クールダウン判定
# ============================================================

def _cooldown_ok(symbol: str) -> bool:

    now = dt.datetime.now()

    last = _last_action_time.get(symbol)

    if last is None:
        return True

    return (now - last).total_seconds() >= COOLDOWN_SECONDS


def _mark_action(symbol: str):
    _last_action_time[symbol] = dt.datetime.now()


# ============================================================
# メインループ
# ============================================================

def execution_loop():

    logger.info("🟢 execution_loop started")

    while True:

        try:

            score_df = getattr(global_data, "score_cache", None)

            if score_df is None or not isinstance(score_df, pd.DataFrame):
                time.sleep(POLL_INTERVAL)
                continue

            if score_df.empty:
                global_data.score_cache = None
                time.sleep(POLL_INTERVAL)
                continue

            # ------------------------------------------------
            # ENTRY候補抽出
            # ------------------------------------------------
            # ここはあなたのスコア設計に合わせて調整可能
            entry_candidates = score_df[
                score_df.get("decision") == "BUY"
            ]

            # ------------------------------------------------
            # SELL候補抽出（EXIT）
            # ------------------------------------------------
            exit_candidates = score_df[
                score_df.get("decision") == "SELL"
            ]

            # ------------------------------------------------
            # ENTRY実行
            # ------------------------------------------------
            for _, row in entry_candidates.iterrows():

                symbol = str(row["symbol"])

                if not _cooldown_ok(symbol):
                    continue

                try:
                    run_entry_pipeline(
                        pipeline_source="ASYNC",
                        interval=1,
                        forced_symbol=symbol
                    )
                    _mark_action(symbol)

                except Exception:
                    logger.exception(
                        "❌ ENTRY execution failed symbol=%s",
                        symbol
                    )

            # ------------------------------------------------
            # EXIT実行
            # ------------------------------------------------
            for _, row in exit_candidates.iterrows():

                symbol = str(row["symbol"])

                if not _cooldown_ok(symbol):
                    continue

                try:
                    exit_loop(
                        forced_symbol=symbol
                    )
                    _mark_action(symbol)

                except Exception:
                    logger.exception(
                        "❌ EXIT execution failed symbol=%s",
                        symbol
                    )

            global_data.score_cache = None

        except Exception:
            logger.exception("❌ execution_loop unexpected error")

        time.sleep(POLL_INTERVAL)