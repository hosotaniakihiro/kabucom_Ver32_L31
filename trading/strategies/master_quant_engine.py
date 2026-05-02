# ============================================================
# File   : trading/strategy/master_quant_engine.py
# Version: FINAL-MASTER-QUANT-V1
# ------------------------------------------------------------
# ✔ AIスコア
# ✔ MTF強度
# ✔ 指数補正
# ✔ セクター回転
# ✔ RL攻撃度
# ✔ 可変サイズ
# ✔ ポートフォリオ最適化前処理
# ============================================================

from trading.regime.index_correlation import aggressive_index_multiplier
from trading.regime.sector_rotation import sector_boost_multiplier
from trading.ai.online_rl_engine import OnlineAggressionRL


rl_engine = OnlineAggressionRL()


def master_decision(
    symbol,
    summary_1,
    summary_3,
    summary_5,
    index_row,
    sector_map,
    sector_state,
    regime,
):

    row1 = summary_1.get(symbol)
    if not row1:
        return None

    ai_score = row1.get("score", 0)
    mtf_strength = (
        summary_3.get(symbol, {}).get("ma75_slope", 0) * 0.6 +
        summary_5.get(symbol, {}).get("ma75_slope", 0) * 0.4
    )

    idx_mult = aggressive_index_multiplier(
        row1,
        index_row,
        regime
    )

    sector_mult = sector_boost_multiplier(
        symbol,
        sector_map,
        sector_state
    )

    aggression = rl_engine.get()

    final_score = ai_score * idx_mult * sector_mult * aggression

    size_multiplier = min(
        3.0,
        1 + abs(mtf_strength) * 2 * aggression
    )

    return {
        "final_score": final_score,
        "size_multiplier": size_multiplier,
        "mtf_strength": mtf_strength,
        "index_mult": idx_mult,
        "sector_mult": sector_mult,
    }