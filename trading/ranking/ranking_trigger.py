# ============================================================
# trading/ranking/ranking_trigger.py
# Ver28.2-FINAL-RANKING-PENDING-MANAGER-FRESHNESS-GUARD
# ------------------------------------------------------------
# ✔ Ver28.1 完全保持（削除ゼロ）
# ✔ ranking_ma_1min ベース trigger
# ✔ AI 昇格判定（promotion AI）
# ✔ pending_manager 完全準拠
# ✔ ENTRY は行わない（5秒足確定は entry_controller）
# ✔ expire_at source限定破棄（SUMMARY_AI 保護）
# ✔ 学習ログ安定（promotion_proba 保存）
# ✔ 🔥 PUSH freshness ガード追加（新鮮銘柄のみ）
# ============================================================

from __future__ import annotations

import datetime as dt
import threading
import time
import logging
import pandas as pd

from websocket_handlers.ws_subscribe import register_symbol

# ★ pending 管理（唯一の入口）
from trading.entry.pending_manager import (
    add_pending,
    has_source,
    get_bucket,
    replace_bucket,
)

# ★ AI 昇格判定
from AI.inference.predict_ranking_promotion import (
    predict_ranking_promotion_proba,
)

# ★ 最新 summary 取得
from global_state import global_data

logger = logging.getLogger(__name__)

# ============================================================
# 定数
# ============================================================
PENDING_EXPIRE_SEC = 180
PROMOTION_AI_THRESHOLD = 0.55
FRESHNESS_MINUTES = 2  # 🔥 新鮮判定


# ============================================================
# 🔥 PUSH freshness 判定
# ============================================================
def _is_push_fresh(symbol: str) -> bool:

    try:
        df_1m = global_data.latest_summary_by_interval.get(1)

        if df_1m is None or df_1m.empty:
            return False

        df_symbol = df_1m[
            (df_1m["symbol"] == symbol)
            & (df_1m.get("source") == "push")
        ]

        if df_symbol.empty:
            return False

        df_symbol = df_symbol.sort_values("datetime")

        last_dt = pd.to_datetime(
            df_symbol.iloc[-1]["datetime"],
            errors="coerce"
        )

        if pd.isna(last_dt):
            return False

        now = pd.Timestamp.now().floor("min")
        threshold = now - pd.Timedelta(minutes=FRESHNESS_MINUTES)

        return last_dt >= threshold

    except Exception:
        logger.exception("[FRESH_CHECK_ERROR] %s", symbol)
        return False


# ============================================================
# メイン：ランキング由来 ENTRY 候補登録
# ============================================================
def trigger_ranking_entry(
    *,
    symbol: str,
    symbolname: str,
    entry_decision: str,
    trend_score: int,
    volume_speed: float,
    reason: str,
    market: str = "ALL",
):
    """
    ranking MA ベース ENTRY 候補を pending_manager に登録
    ENTRY 実行はしない（確定は entry_controller 側）
    """

    now = dt.datetime.now()

    # --------------------------------------------------------
    # 🔥 PUSH freshness ガード
    # --------------------------------------------------------
    if not _is_push_fresh(symbol):
        logger.debug(
            "[RANK_SKIP_FRESHNESS] %s not fresh push",
            symbol,
        )
        return

    # --------------------------------------------------------
    # BUY / SELL 正規化
    # --------------------------------------------------------
    if entry_decision not in ("BUY", "SELL"):
        logger.debug("[RANK_TRIGGER_SKIP] invalid decision: %s", symbol)
        return

    # --------------------------------------------------------
    # ★ AI 昇格判定
    # --------------------------------------------------------
    ai_row = {
        "symbol": symbol,
        "market": market,
        "entry_decision": entry_decision,
        "trend_score": int(trend_score),
        "volume_speed": float(volume_speed),
        "hour": now.hour,
        "minute": now.minute,
    }

    try:
        promotion_proba = float(
            predict_ranking_promotion_proba(ai_row)
        )
    except Exception:
        logger.exception("[RANK_AI_ERROR] %s", symbol)
        return

    if promotion_proba < PROMOTION_AI_THRESHOLD:
        logger.info(
            "[RANK_AI_BLOCK] %s decision=%s proba=%.2f < %.2f",
            symbol,
            entry_decision,
            promotion_proba,
            PROMOTION_AI_THRESHOLD,
        )
        return

    # --------------------------------------------------------
    # 多重登録防止（source 単位）
    # --------------------------------------------------------
    if has_source(symbol, "RANKING_5S"):
        logger.debug("[RANK_PENDING_SKIP] already exists: %s", symbol)
        return

    # --------------------------------------------------------
    # ENTRY 条件
    # --------------------------------------------------------
    expire_at = now + dt.timedelta(seconds=PENDING_EXPIRE_SEC)

    entry = {
        "symbol": symbol,
        "side": entry_decision,
        "source": "RANKING_5S",

        # ranking MA
        "trend_score": int(trend_score),
        "volume_speed": float(volume_speed),

        # AI
        "promotion_proba": promotion_proba,

        # ENTRY 制御条件
        "entry_conditions": {
            "need_push": True,
            "min_volume_speed": max(volume_speed * 0.8, 1.0),
            "expire_at": expire_at,
        },

        # 管理
        "market": market,
        "created_at": now,
        "reason": reason,
    }

    if not add_pending(entry):
        return

    logger.info(
        "[RANK_PENDING_AI] %s %s decision=%s trend=%d "
        "vol=%.0f proba=%.2f expire=%ds",
        symbol,
        symbolname,
        entry_decision,
        trend_score,
        volume_speed,
        promotion_proba,
        PENDING_EXPIRE_SEC,
    )

    # --------------------------------------------------------
    # PUSH 購読
    # --------------------------------------------------------
    register_symbol(symbol)

    # --------------------------------------------------------
    # 有効期限監視（source限定削除）
    # --------------------------------------------------------
    threading.Thread(
        target=_expire_watch,
        args=(symbol, expire_at),
        daemon=True,
    ).start()


# ============================================================
# 有効期限監視（RANKING_5S のみ削除）
# ============================================================
def _expire_watch(symbol: str, expire_at: dt.datetime):

    sleep_sec = 2

    while True:
        time.sleep(sleep_sec)

        if dt.datetime.now() < expire_at:
            continue

        bucket = get_bucket(symbol)
        if not bucket:
            return

        new_bucket = [
            e for e in bucket
            if e.get("source") != "RANKING_5S"
        ]

        replace_bucket(symbol, new_bucket)

        logger.info(
            "[RANK_PENDING_EXPIRE] %s expired at %s",
            symbol,
            expire_at.strftime("%H:%M:%S"),
        )
        return
