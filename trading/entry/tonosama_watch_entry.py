# ============================================================
# tonosama_watch_entry.py
# 監視銘柄 → ENTRY 再昇格ロジック
# ============================================================

import datetime as dt
import logging

from global_state import global_data
from trading.entry.ignition.ai_boost import infer_tonosama_entry
from trading.entry.ignition.holdtime_ai import predict_hold_seconds
from trading.ranking.ranking_trigger import trigger_ranking_entry

logger = logging.getLogger(__name__)

# ENTRY 再昇格条件（ranking より弱いが watch より強い）
MIN_VOLUME_SPEED = 4000
MIN_FAST_RET = 0.18
AI_THRESHOLD = 0.70
MAX_WATCH_AGE_SEC = 120


def run_watch_entry():
    """
    tonosama_watch から ENTRY を再生成する
    """
    now = dt.datetime.now()

    watch = getattr(global_data, "tonosama_watch", {})
    open_positions = getattr(global_data, "open_positions", {})
    inflight = getattr(global_data, "entry_inflight", set())

    for symbol, info in list(watch.items()):

        # -----------------------------------------
        # 寿命チェック
        # -----------------------------------------
        if (now - info["first_seen"]).total_seconds() > MAX_WATCH_AGE_SEC:
            continue

        if symbol in open_positions or symbol in inflight:
            continue

        volume_speed = info["volume_speed"]
        fast_ret = info["fast_ret"]
        price = info["price"]

        # -----------------------------------------
        # 再昇格条件
        # -----------------------------------------
        if volume_speed < MIN_VOLUME_SPEED:
            continue
        if fast_ret < MIN_FAST_RET:
            continue

        # -----------------------------------------
        # AI 再チェック（重要）
        # -----------------------------------------
        ai = infer_tonosama_entry(
            symbol=symbol,
            fast_ret=fast_ret,
            volume_speed=volume_speed,
        )

        if not ai.get("ok"):
            continue

        ai_conf = ai.get("ai_confidence", 0.0)
        if ai_conf < AI_THRESHOLD:
            continue

        # -----------------------------------------
        # HOLD 秒数 AI
        # -----------------------------------------
        hold_limit_sec = predict_hold_seconds({
            "volume_speed": volume_speed,
            "fast_ret": fast_ret,
            "rank_position": 5,   # ranking なしのため固定
            "price": price,
            "spread": 0.0,
            "entry_second": now.second,
        })

        # -----------------------------------------
        # ENTRY 登録
        # -----------------------------------------
        trigger_ranking_entry(
            symbol=symbol,
            symbolname="",
            type_name="WATCH_REENTRY",
            ranking_strength=5,
            volume_speed=volume_speed,
            reason=(
                f"WATCH_REENTRY "
                f"vol={int(volume_speed)} "
                f"fast_ret={fast_ret:.2f}% "
                f"ai={ai_conf:.2f}"
            ),
            market="ALL",
            extra={
                "hold_limit_sec": hold_limit_sec,
                "fast_ret": fast_ret,
                "ai_confidence": ai_conf,
                "from_watch": True,
            }
        )

        logger.info(
            f"👑 WATCH→ENTRY {symbol} "
            f"fast_ret={fast_ret:.2f}% "
            f"AI={ai_conf:.2f} "
            f"HOLD={hold_limit_sec}s"
        )

        # ★ 一度 ENTRY したら watch から外す
        del global_data.tonosama_watch[symbol]
