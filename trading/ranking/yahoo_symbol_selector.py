# ============================================================
# yahoo_symbol_selector.py（Ver24-FINAL-ULTRA）
# ------------------------------------------------------------
# ✔ ACTIVE / LIGHT を最優先で Yahoo 補完
# ✔ ranking は「拡張ソース」
# ✔ ATS / ENTRY / ranking と完全分離
# ============================================================

from typing import Set, List
from global_state import global_data
import logging

logger = logging.getLogger(__name__)


# ------------------------------------------------------------
# latest_ranking から抽出
# ------------------------------------------------------------
def _extract_from_latest(
    type_name: str,
    market: str,
    sort_col: str,
    top_n: int,
    exclude: Set[str]
) -> Set[str]:

    if not hasattr(global_data, "latest_ranking"):
        return set()

    key = f"{type_name}_{market}"
    df = global_data.latest_ranking.get(key)

    if df is None or df.empty or sort_col not in df.columns:
        return set()

    df = df.sort_values(sort_col, ascending=False)

    out = set()
    for sym in df["symbol"]:
        sym = str(sym)
        if sym not in exclude:
            out.add(sym)
        if len(out) >= top_n:
            break

    return out


# ------------------------------------------------------------
# Yahoo 補完対象（最終確定）
# ------------------------------------------------------------
def build_yahoo_target_symbols() -> List[str]:
    """
    Yahoo 補完対象銘柄

    優先順位：
      1. ACTIVE（ATS監視中）
      2. LIGHT（ローテ待機）
      3. ranking 由来（補助）
    """

    symbols: Set[str] = set()

    # ========================================================
    # ① ACTIVE / LIGHT（最重要）
    # ========================================================
    active = getattr(global_data, "symbols_active", set()) or set()
    light  = getattr(global_data, "symbols_light", set()) or set()

    symbols |= set(active)
    symbols |= set(light)

    # ========================================================
    # ② ranking 由来（拡張）
    # ========================================================
    symbols |= _extract_from_latest(
        type_name="値上がり率",
        market="ALL",
        sort_col="change_percentage",
        top_n=30,
        exclude=symbols,
    )

    symbols |= _extract_from_latest(
        type_name="値下がり率",
        market="ALL",
        sort_col="change_percentage",
        top_n=30,
        exclude=symbols,
    )

    symbols |= _extract_from_latest(
        type_name="売買代金",
        market="TG",
        sort_col="trading_volume",
        top_n=20,
        exclude=symbols,
    )

    symbols |= _extract_from_latest(
        type_name="売買代金",
        market="TS",
        sort_col="trading_volume",
        top_n=20,
        exclude=symbols,
    )

    # ========================================================
    # 正規化 & ログ
    # ========================================================
    symbols = {s for s in symbols if s and isinstance(s, str)}

    logger.info(
        f"📡 Yahoo補完対象: {len(symbols)} "
        f"(ACTIVE={len(active)}, LIGHT={len(light)})"
    )

    return sorted(symbols)
