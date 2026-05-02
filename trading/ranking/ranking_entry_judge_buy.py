# ============================================================
# File   : trading/ranking/ranking_entry_judge_buy.py
# Ver    : 1.3.0-FINAL-RANKING-BUY-AI-PERSISTENCE-INTEGRATED
# ------------------------------------------------------------
# ✔ 1.2.0 全機能完全保持（削除ゼロ）
# ✔ AI最終ゲート維持
# ✔ None / NaN / 0 完全耐性
# ✔ ログ理由完全保持
# ✔ bulk判定保持
# ✔ RankingEntryEvent 保存統合
# ✔ 🔥 NightAI Block Zone 統合（バランス型）
# ✔ 🔥 rank_persistence をAI特徴量へ追加
# ✔ 保存失敗してもENTRY判定は継続（安全設計）
# ============================================================

from __future__ import annotations

import logging
from typing import Dict, Tuple

from utils_common import safe_float
from trading.summary.position_filter import can_entry_symbol
from AI.entry_gate import ai_final_entry_check

# NightAI
from trading.entry.ranking_entry_event_saver import save_ranking_entry_event
from AI.config.ranking_block_zone_loader import is_block_zone

logger = logging.getLogger(__name__)


# ============================================================
# 設定（ランキング BUY 専用）
# ============================================================

MIN_VOLUME_1M = 3000
MIN_VWAP_DEVIATION_PCT = 0.10
MIN_MA_DEVIATION_PCT = 0.15
MAX_PRICE_CHANGE_PCT = 15.0


# ============================================================
# メイン判定
# ============================================================

def judge_ranking_entry_buy(
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

    # --------------------------------------------------------
    # 1. ポジション・クールダウン判定
    # --------------------------------------------------------
    if not can_entry_symbol(symbol, side="BUY"):
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

    # 🔥 追加：ランキング特徴量
    rank_persistence = summary_row.get("rank_persistence")
    volume_speed     = summary_row.get("volume_speed")

    if close_price is None or close_price <= 0:
        return False, "close_price_invalid"

    # --------------------------------------------------------
    # 🔥 NightAI ブロック判定（AI前に入れる）
    # --------------------------------------------------------
    try:
        if is_block_zone(rank_persistence, volume_speed):
            return False, "night_ai_block_zone"
    except Exception:
        logger.exception("[RANKING BUY] block zone check failed")

    # --------------------------------------------------------
    # 3. 出来高チェック
    # --------------------------------------------------------
    if volume is not None and volume < MIN_VOLUME_1M:
        return False, f"volume_too_low:{int(volume)}"

    # --------------------------------------------------------
    # 4. 異常値ガード
    # --------------------------------------------------------
    if open_price and open_price > 0:
        change_pct = abs((close_price - open_price) / open_price) * 100.0
        if change_pct > MAX_PRICE_CHANGE_PCT:
            return False, f"price_change_too_large:{change_pct:.2f}%"

    # --------------------------------------------------------
    # 5. VWAP乖離（上抜け方向）
    # --------------------------------------------------------
    if vwap and vwap > 0:
        vwap_dev_pct = (close_price - vwap) / vwap * 100.0
        if vwap_dev_pct < MIN_VWAP_DEVIATION_PCT:
            return False, f"vwap_deviation_small:{vwap_dev_pct:.3f}%"

    # --------------------------------------------------------
    # 6. MA乖離（上方向）
    # --------------------------------------------------------
    ma_devs = []

    if ma25 and ma25 > 0:
        ma_devs.append((close_price - ma25) / ma25 * 100.0)

    if ma75 and ma75 > 0:
        ma_devs.append((close_price - ma75) / ma75 * 100.0)

    if ma_devs:
        max_dev = max(ma_devs)
        if max_dev < MIN_MA_DEVIATION_PCT:
            return False, f"ma_deviation_small:{max_dev:.3f}%"

    # --------------------------------------------------------
    # 7. AI 最終ゲート（特徴量追加）
    # --------------------------------------------------------
    ai_row = dict(summary_row)
    ai_row.update({
        "symbol": symbol,
        "side": "BUY",
        "entry_decision": "BUY",
        "source": source,
        "interval": summary_row.get("interval", 1),

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
            "[RANKING BUY] ai_final_entry_check exception symbol=%s",
            symbol,
        )
        return False, "ai_exception"

    if not ai_result.get("allow", False):
        return False, f"ai_reject:{ai_result.get('reason')}"

    # --------------------------------------------------------
    # 8. RankingEntryEvent 保存
    # --------------------------------------------------------
    try:
        save_ranking_entry_event(
            symbol=symbol,
            symbolname=summary_row.get("symbolname"),
            side="BUY",
            interval=summary_row.get("interval", 1),
            summary_row=summary_row,
            ranking_row=summary_row,
            ai_reason=ai_result.get("reason"),
        )
    except Exception:
        logger.exception("[RANKING BUY] ranking_entry_event save failed")

    # --------------------------------------------------------
    # 9. 最終OK
    # --------------------------------------------------------
    return True, "ranking_buy_ok"


# ============================================================
# 補助：バルク判定用
# ============================================================

def judge_ranking_entry_buy_bulk(
    summaries: Dict[str, Dict],
    source: str = "RANKING",
) -> Dict[str, Tuple[bool, str]]:

    results: Dict[str, Tuple[bool, str]] = {}

    for symbol, row in summaries.items():
        try:
            results[symbol] = judge_ranking_entry_buy(
                symbol=symbol,
                summary_row=row,
                source=source,
            )
        except Exception:
            logger.exception(
                "[RANKING BUY] judge failed symbol=%s",
                symbol,
            )
            results[symbol] = (False, "exception")

    return results