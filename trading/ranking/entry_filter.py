# =========================================================
# trading/ranking/entry_filter.py
# Ver28-RANKING-ENTRY-FILTER
# ---------------------------------------------------------
# ✔ ランキング専用 ENTRY 最終ゲート
# ✔ volume × technical × AI の三段階
# =========================================================

import logging
from global_state import global_data

logger = logging.getLogger(__name__)

# -----------------------------
# 閾値（ここだけ調整すればOK）
# -----------------------------
MIN_VOLUME_SPEED = 3000       # 出来高速度
MIN_AI_PROB = 0.55            # AI確信度（0〜1）
ALLOW_NO_AI = True            # AI無い場合は通すか


def filter_ranking_entries(df_summary):
    """
    summary_df + pending_entries を突き合わせて
    ENTRY 可能銘柄だけ返す
    """

    if df_summary is None or df_summary.empty:
        return []

    pending = getattr(global_data, "pending_entries", {})
    if not pending:
        return []

    entry_symbols = []

    for _, row in df_summary.iterrows():
        sym = row["symbol"]

        if sym not in pending:
            continue

        p = pending[sym]

        # -------------------------
        # ① 出来高フィルタ
        # -------------------------
        vol_speed = p.get("volume_speed", 0)
        if vol_speed < MIN_VOLUME_SPEED:
            continue

        # -------------------------
        # ② テクニカル判定
        # -------------------------
        decision = row.get("entry_decision", "NONE")
        if decision not in ("BUY", "SELL"):
            continue

        # -------------------------
        # ③ AI フィルタ
        # -------------------------
        ai_prob = p.get("ai_prob", 0.0)

        if ai_prob > 0:
            if ai_prob < MIN_AI_PROB:
                continue
        else:
            if not ALLOW_NO_AI:
                continue

        # -------------------------
        # 通過
        # -------------------------
        logger.info(
            f"[RANK ENTRY PASS] {sym} "
            f"dir={decision} "
            f"vol={vol_speed:.0f} "
            f"ai={ai_prob:.2f}"
        )

        entry_symbols.append(sym)

    return entry_symbols
