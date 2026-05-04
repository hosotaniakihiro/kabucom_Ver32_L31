# ============================================================
# pj/trading/entry/tonosama_watch.py
# Ver1.0-TONOSAMA-WATCH-PROMOTION
# ------------------------------------------------------------
# ✔ tonosama_watch を定期監視
# ✔ 初動 WATCH → AI 判定 → ENTRY 昇格
# ✔ ranking / summary 非依存
# ============================================================

import datetime as dt
import logging

from global_state import global_data
from trading.handlers.entry_handler import place_entry_buy
from trading.entry.ignition.ai_boost import infer_tonosama_entry

logger = logging.getLogger(__name__)

# ============================================================
# パラメータ
# ============================================================
WATCH_EXPIRE_SEC = 30
AI_THRESHOLD = 0.65


# ============================================================
# メイン監視ループ
# ============================================================
def run_tonosama_watch():

    if not hasattr(global_data, "tosama_watch"):
        return

    now = dt.datetime.now()

    for sym, info in list(global_data.tosama_watch.items()):

        # -----------------------------
        # ① 時間切れ → 除外
        # -----------------------------
        if (now - info["first_seen"]).total_seconds() > WATCH_EXPIRE_SEC:
            del global_data.tosama_watch[sym]
            continue

        # -----------------------------
        # ② AI 判定（再評価）
        # -----------------------------
        ai_result = infer_tonosama_entry(
            symbol=sym,
            fast_ret=info["fast_ret"],
            volume_speed=info["volume_speed"],
        )

        if not ai_result.get("ok"):
            continue

        ai_prob = ai_result.get("ai_confidence", 0.0)

        if ai_prob < AI_THRESHOLD:
            continue

        # -----------------------------
        # ③ ENTRY 昇格（成行）
        # -----------------------------
        order_id = place_entry_buy(
            sym,
            "",
            None,
            "TONOSAMA_INIT"
        )

        if order_id:
            logger.warning(
                f"👑 TONOSAMA WATCH→ENTRY {sym} AI={ai_prob:.2f}"
            )

        # WATCH から削除（成功・失敗問わず）
        del global_data.tosama_watch[sym]
