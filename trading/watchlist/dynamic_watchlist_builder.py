# ============================================================
# dynamic_watchlist_builder.py
# ------------------------------------------------------------
# 20分ランキング履歴 + analyzer を用いて
# 動的監視銘柄100を構築
# ============================================================

from global_state import global_data

from trading.ranking.history import get_symbols_appeared_within
from trading.ranking.analyzer import analyze_all_markets
from trading.ranking.yahoo_symbol_selector import load_rise_fall_symbols

MAX_WATCH = 100
MIN_KEEP_MINUTES = 5      # 最低保持時間
MAX_ROTATION = 20         # 1回の最大入替数


def build_dynamic_watchlist() -> list[str]:
    """
    動的監視銘柄100を構築（安全装置付き）
    """
    now = global_data.now()
    current = set(global_data.symbols_active or [])

    # --------------------------------------------------
    # 20分以内にランキング登場した銘柄
    # --------------------------------------------------
    active_20m = get_symbols_appeared_within(20)

    # --------------------------------------------------
    # 最低保持時間チェック
    # --------------------------------------------------
    force_keep = set()
    for sym in current:
        t = global_data.symbol_active_since.get(sym)
        if t and (now - t).total_seconds() < MIN_KEEP_MINUTES * 60:
            force_keep.add(sym)

    # --------------------------------------------------
    # analyzer による「勢いあり」判定
    # --------------------------------------------------
    trend_keep = set()
    for sym in current:
        try:
            res = analyze_all_markets(
                sym,
                type_name="値上がり率",
                notify=False
            )
            if any(r.get("consecutive_up") for r in res):
                trend_keep.add(sym)
        except Exception:
            pass

    # --------------------------------------------------
    # 継続監視銘柄
    # --------------------------------------------------
    keep = {
        s for s in current
        if s in active_20m
    }

    keep |= force_keep
    keep |= trend_keep

    # --------------------------------------------------
    # 不足分を値上がり率ランキングから補充
    # --------------------------------------------------
    need = MAX_WATCH - len(keep)
    add = []

    if need > 0:
        rise_candidates = load_rise_fall_symbols(
            kind="rise",
            top_n=50,
            exclude=keep
        )
        add = list(rise_candidates)[:need]

    # --------------------------------------------------
    # 回転数制限
    # --------------------------------------------------
    removed = list(current - keep)
    if len(removed) > MAX_ROTATION:
        keep |= set(removed[:len(removed) - MAX_ROTATION])

    final = list(keep) + add
    final = final[:MAX_WATCH]

    return final
