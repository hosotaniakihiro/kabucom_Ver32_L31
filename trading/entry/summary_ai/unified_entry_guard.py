# ============================================================
# File   : trading/entry/summary_ai/unified_entry_guard.py
# Version: PRODUCTION-STABLE-REV1.0-ENTRY-GUARD
# Purpose:
#   4ルート統合後の安全フィルタ
#
# Important:
#   - ランキング単体では発注しない
#   - 殿様イナゴ単体でも原則発注しない
#   - PUSH/Yahooの本物テクニカル確認を優先
# ============================================================

from __future__ import annotations

from .unified_candidate import UnifiedEntryCandidate


def can_send_to_ai_gate(
    c: UnifiedEntryCandidate,
    *,
    require_real_technical_for_entry: bool = True,
    min_slope_atr_scaled: float = 0.02,
    min_buy_score: float = 0.0,
    max_sell_score: float = 99.0,
) -> tuple[bool, str]:
    """
    AI gateへ渡してよいか。

    注意:
      ここで直接エントリー可否を決めるのではなく、
      明らかに危険な候補を落とす。
    """

    if not c.symbol:
        return False, "empty symbol"

    if c.close <= 0:
        return False, f"invalid close={c.close}"

    if c.score_sell > max_sell_score:
        return False, f"sell score too high sell={c.score_sell:.2f}"

    # PUSH/Yahooなら本物slopeを見る
    if c.is_push_like() or c.is_yahoo_like():
        if c.slope_atr_scaled < min_slope_atr_scaled:
            return False, (
                f"weak real slope "
                f"slope_atr_scaled={c.slope_atr_scaled:.4f} "
                f"< {min_slope_atr_scaled:.4f}"
            )

        if c.score_buy < min_buy_score:
            return False, (
                f"weak buy score buy={c.score_buy:.2f} "
                f"< {min_buy_score:.2f}"
            )

        return True, "real technical candidate"

    # ランキング単体はAIには渡してもよいが、
    # 最終発注にはPUSH/Yahoo確認が必要。
    if c.is_ranking_like():
        if (
            c.ranking_score <= 0
            and c.ranking_momentum <= 0
            and c.price_delta_pct <= 0
            and c.rank_improve <= 0
            and c.volume_delta <= 0
        ):
            return False, "weak ranking candidate"

        if require_real_technical_for_entry and not c.has_real_technical():
            return True, "ranking discovery only; needs real technical confirmation"

        return True, "ranking candidate"

    # 殿様イナゴも単体発注禁止寄り
    if c.is_tonosama_like():
        if require_real_technical_for_entry and not c.has_real_technical():
            return True, "tonosama trigger only; needs real technical confirmation"

        return True, "tonosama candidate with technical"

    return True, "unknown source candidate"