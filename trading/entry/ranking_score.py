# trading/entry/ranking_score.py
import logging
from trading.ranking.analyzer import analyze_all_markets

logger = logging.getLogger(__name__)

# =====================================
# ランキングスコア（entry_controller 用）
# =====================================
def calc_ranking_score(symbol: str, type_name_list=None):
    """
    entry_controller 用: ランキングをスコア化して返す
    BUY / SELL 共通で使用可能
    戻り値: (score, reason_list)
    """
    if type_name_list is None:
        # 代表的なランキングを採用
        type_name_list = ["値上がり率", "売買代金急増", "出来高急増"]

    total_score = 0
    reasons = []

    for type_name in type_name_list:
        try:
            results = analyze_all_markets(symbol, type_name, notify=False)
        except Exception as e:
            logger.error(f"ランキング解析失敗: {symbol} {type_name} {e}")
            continue

        for r in results:
            if r.get("status") != "OK":
                continue

            # 初登場 TOP20
            if r.get("first_time_topN"):
                total_score += 2
                reasons.append(f"{type_name} TOP20初登場(+2)")

            # 連続 Rank UP
            if r.get("consecutive_up"):
                total_score += 2
                reasons.append(f"{type_name} 連続順位上昇(+2)")

            # 順位改善幅（ΔRank）
            latest_rank = r.get("rank_latest")
            prev_rank = r.get("rank_prev")
            if latest_rank and prev_rank:
                delta = prev_rank - latest_rank
                if delta >= 20:
                    total_score += 2
                    reasons.append(f"{type_name} ΔRank={delta}(+2)")
                elif delta >= 10:
                    total_score += 1
                    reasons.append(f"{type_name} ΔRank={delta}(+1)")

    return total_score, reasons
