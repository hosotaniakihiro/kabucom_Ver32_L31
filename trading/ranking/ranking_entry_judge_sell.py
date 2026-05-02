# ============================================================
# File   : trading/ranking/ranking_entry_judge_sell.py
# Ver    : 1.4.0-FINAL-RANKING-SELL-AI-PERSISTENCE-INTEGRATED
# ------------------------------------------------------------
# ✔ 1.3.0 全機能完全保持（削除ゼロ）
# ✔ ranking_entry_config 準拠維持
# ✔ strict_time ロジック維持
# ✔ AI最終ゲート維持
# ✔ None / NaN / 0 完全耐性
# ✔ bulk判定維持
# ✔ RankingEntryEvent 保存統合
# ✔ 🔥 NightAI Block Zone 統合（バランス型）
# ✔ 🔥 rank_persistence をAI特徴量へ追加
# ✔ 保存失敗してもENTRY判定は継続（安全設計）
# ============================================================

from __future__ import annotations

import logging
import datetime as dt
from typing import Dict, Tuple

from utils_common import safe_float
from trading.summary.position_filter import can_entry_symbol
from AI.entry_gate import ai_final_entry_check

from config.ranking_entry_config import (
    RANKING_ENTRY_CONFIG as C,
    is_time_allowed,
    is_strict_time,
)

# 🔥 NightAI
from trading.entry.ranking_entry_event_saver import save_ranking_entry_event
from AI.config.ranking_block_zone_loader import is_block_zone

logger = logging.getLogger(__name__)


# ============================================================
# メイン判定
# ============================================================

def judge_ranking_entry_sell(
    *,
    symbol: str,
    summary_row: Dict,
    source: str = "RANKING",
) -> Tuple[bool, str]:

    # --------------------------------------------------------
    # 0. 基本ガード
    # --------------------------------------------------------
    if not symbol:
        return False, "symbol_empty"

    if not isinstance(summary_row, dict):
        return False, "summary_invalid"

    now_t = dt.datetime.now().time()

    if not is_time_allowed(now_t):
        return False, "time_guard_blocked"

    strict_time = is_strict_time(now_t)

    # --------------------------------------------------------
    # 1. ポジション・クールダウン判定
    # --------------------------------------------------------
    if not can_entry_symbol(symbol, side="SELL"):
        return False, "cooldown_or_position_blocked"

    # --------------------------------------------------------
    # 2. 数値取得（完全耐性）
    # --------------------------------------------------------
    close_price = safe_float(summary_row.get("close_price"))
    open_price  = safe_float(summary_row.get("open_price"))
    volume      = safe_float(summary_row.get("volume"))
    vwap        = safe_float(summary_row.get("vwap"))
    ma25        = safe_float(summary_row.get("ma25"))
    ma75        = safe_float(summary_row.get("ma75"))
    ma75_conf   = safe_float(summary_row.get("ma75_conf"))
    ma75_hard_ng = bool(summary_row.get("ma75_hard_ng", False))

    # 🔥 ランキング特徴量
    rank_persistence = summary_row.get("rank_persistence")
    volume_speed     = summary_row.get("volume_speed")

    if close_price is None or close_price <= 0:
        return False, "close_price_invalid"

    # --------------------------------------------------------
    # 🔥 NightAI ブロック判定（AI前）
    # --------------------------------------------------------
    try:
        if is_block_zone(rank_persistence, volume_speed):
            return False, "night_ai_block_zone"
    except Exception:
        logger.exception("[RANKING SELL] block zone check failed")

    # --------------------------------------------------------
    # 3. 価格帯フィルタ
    # --------------------------------------------------------
    if not (C["PRICE"]["MIN"] <= close_price <= C["PRICE"]["MAX"]):
        return False, f"price_out_of_range:{close_price}"

    # --------------------------------------------------------
    # 4. 出来高チェック
    # --------------------------------------------------------
    if volume is not None:
        min_vol = 1000 if not strict_time else 1500
        if volume < min_vol:
            return False, f"volume_too_low:{int(volume)}"

    # --------------------------------------------------------
    # 5. 異常値ガード
    # --------------------------------------------------------
    if open_price and open_price > 0:
        change_pct = abs((close_price - open_price) / open_price) * 100.0
        if change_pct > 15.0:
            return False, f"price_change_too_large:{change_pct:.2f}%"

    # --------------------------------------------------------
    # 6. VWAP乖離（下抜け方向）
    # --------------------------------------------------------
    if vwap and vwap > 0:
        vwap_dev_pct = (vwap - close_price) / vwap * 100.0
        min_vwap_dev = 0.10 if not strict_time else 0.20
        if vwap_dev_pct < min_vwap_dev:
            return False, f"vwap_deviation_small:{vwap_dev_pct:.3f}%"

    # --------------------------------------------------------
    # 7. MA乖離（下方向）
    # --------------------------------------------------------
    ma_devs = []

    if ma25 and ma25 > 0:
        ma_devs.append((ma25 - close_price) / ma25 * 100.0)

    if ma75 and ma75 > 0:
        ma_devs.append((ma75 - close_price) / ma75 * 100.0)

    if ma_devs:
        max_dev = max(ma_devs)
        min_ma_dev = 0.15 if not strict_time else 0.25
        if max_dev < min_ma_dev:
            return False, f"ma_deviation_small:{max_dev:.3f}%"

    # --------------------------------------------------------
    # 8. MA75_conf フィルタ
    # --------------------------------------------------------
    if ma75_conf is not None:
        if ma75_conf < C["MA75"]["MIN_CONF_SELL"]:
            return False, f"ma75_conf_low:{ma75_conf:.3f}"

    if C["MA75"]["USE_HARD_NG"] and ma75_hard_ng:
        return False, "ma75_hard_ng"

    # --------------------------------------------------------
    # 9. AI 最終ゲート（特徴量追加）
    # --------------------------------------------------------
    ai_row = dict(summary_row)
    ai_row.update({
        "symbol": symbol,
        "side": "SELL",
        "entry_decision": "SELL",
        "source": source,
        "interval": summary_row.get("interval", 1),
        "strict_time": strict_time,

        # 🔥 AI強化特徴量
        "rank_persistence": rank_persistence,
        "rank_persistence_norm": (
            rank_persistence / 5.0 if rank_persistence else 0
        ),
        "volume_speed": volume_speed,
    })

    try:
        ai_result = ai_final_entry_check(ai_row)
    except Exception:
        logger.exception(
            "[RANKING SELL] ai_final_entry_check exception symbol=%s",
            symbol,
        )
        return False, "ai_exception"

    if not ai_result.get("allow", False):
        return False, f"ai_reject:{ai_result.get('reason')}"

    # --------------------------------------------------------
    # 10. RankingEntryEvent 保存
    # --------------------------------------------------------
    try:
        save_ranking_entry_event(
            symbol=symbol,
            symbolname=summary_row.get("symbolname"),
            side="SELL",
            interval=summary_row.get("interval", 1),
            summary_row=summary_row,
            ranking_row=summary_row,
            ai_reason=ai_result.get("reason"),
        )
    except Exception:
        logger.exception("[RANKING SELL] ranking_entry_event save failed")

    # --------------------------------------------------------
    # 11. 最終OK
    # --------------------------------------------------------
    return True, "ranking_sell_ok"


# ============================================================
# 補助：バルク判定用
# ============================================================

def judge_ranking_entry_sell_bulk(
    summaries: Dict[str, Dict],
    source: str = "RANKING",
) -> Dict[str, Tuple[bool, str]]:

    results: Dict[str, Tuple[bool, str]] = {}

    for symbol, row in summaries.items():
        try:
            results[symbol] = judge_ranking_entry_sell(
                symbol=symbol,
                summary_row=row,
                source=source,
            )
        except Exception:
            logger.exception(
                "[RANKING SELL] judge failed symbol=%s",
                symbol,
            )
            results[symbol] = (False, "exception")

    return results