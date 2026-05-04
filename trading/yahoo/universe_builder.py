# trading/summary/yahoo_universe_builder.py

import logging

from global_state import global_data
from trading.ranking.ranking_loader import (
    load_market_rise_rank,
    load_market_fall_rank,
    load_value_rank_by_market,
)

logger = logging.getLogger(__name__)


def build_yahoo_target_symbols():
    """
    Yahoo 補完用の銘柄ユニバースを構築する
    """

    # ==================================================
    # ① 監視銘柄（100）
    # ==================================================
    watch_symbols = set(global_data.symbols_light)
    logger.info(f"Yahoo base watch symbols = {len(watch_symbols)}")

    target = set(watch_symbols)

    # ==================================================
    # ② 全市場 値上がり・値下がり
    # ==================================================
    rise = load_market_rise_rank(limit=50)   # List[str]
    fall = load_market_fall_rank(limit=50)

    # 監視銘柄を除外
    rise_filtered = [s for s in rise if s not in watch_symbols][:30]
    fall_filtered = [s for s in fall if s not in watch_symbols][:30]

    target |= set(rise_filtered)
    target |= set(fall_filtered)

    logger.info(
        f"Yahoo rise/fall added: rise={len(rise_filtered)} "
        f"fall={len(fall_filtered)}"
    )

    # ==================================================
    # ③ 売買代金（グロース・スタンダード）
    # ==================================================
    excluded = set(target)

    value_growth = load_value_rank_by_market(
        market="GROWTH",
        limit=50
    )
    value_standard = load_value_rank_by_market(
        market="STANDARD",
        limit=50
    )

    growth_filtered = [s for s in value_growth if s not in excluded][:20]
    standard_filtered = [s for s in value_standard if s not in excluded][:20]

    target |= set(growth_filtered)
    target |= set(standard_filtered)

    logger.info(
        f"Yahoo value added: growth={len(growth_filtered)} "
        f"standard={len(standard_filtered)}"
    )

    symbols = sorted(target)

    logger.info(f"Yahoo final target symbols = {len(symbols)}")

    return symbols
