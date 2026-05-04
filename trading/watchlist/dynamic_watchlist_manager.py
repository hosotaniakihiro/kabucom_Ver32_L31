# trading/watchlist/dynamic_watchlist_manager.py
import datetime as dt
import logging

from global_state import global_data

logger = logging.getLogger(__name__)

WINDOW_MINUTES = 20
TARGET_SIZE = 100


def _extract_recent_ranking_symbols():
    """
    latest_ranking から直近ランキングに出現した銘柄集合を作る
    """
    if not hasattr(global_data, "latest_ranking"):
        return set()

    symbols = set()
    for key, df in global_data.latest_ranking.items():
        if df is None or df.empty:
            continue
        symbols |= set(df["symbol"].astype(str))
    return symbols


def _extract_rise_top_symbols(limit=100):
    """
    値上がり率ランキング上位を取得（rank列は使わない）
    """
    if not hasattr(global_data, "latest_ranking"):
        return []

    out = []
    for key, df in global_data.latest_ranking.items():
        if not key.startswith("値上がり率"):
            continue
        if df is None or df.empty:
            continue
        out.extend(df["symbol"].astype(str).tolist())

    # 重複排除しつつ先頭から
    seen = set()
    uniq = []
    for s in out:
        if s not in seen:
            uniq.append(s)
            seen.add(s)
        if len(uniq) >= limit:
            break
    return uniq


def update_dynamic_watchlist():
    """
    ★ メイン関数
    """
    now = dt.datetime.now()

    # 初回用
    if not hasattr(global_data, "symbol_last_seen"):
        global_data.symbol_last_seen = {}

    # ランキング出現銘柄
    recent_symbols = _extract_recent_ranking_symbols()

    # 出現時刻更新
    for s in recent_symbols:
        global_data.symbol_last_seen[s] = now

    # 20分以内に出た銘柄だけ残す
    alive = {
        s for s, t in global_data.symbol_last_seen.items()
        if (now - t).total_seconds() <= WINDOW_MINUTES * 60
    }

    # まず生存銘柄
    final = list(alive)

    # 足りなければ値上がり率上位で補充
    if len(final) < TARGET_SIZE:
        rise_add = _extract_rise_top_symbols()
        for s in rise_add:
            if s not in final:
                final.append(s)
            if len(final) >= TARGET_SIZE:
                break

    # それでも足りなければ symbols_light
    if len(final) < TARGET_SIZE:
        for s in global_data.symbols_light:
            if s not in final:
                final.append(s)
            if len(final) >= TARGET_SIZE:
                break

    global_data.symbols_active = set(final[:TARGET_SIZE])

    logger.info(
        f"🔄 動的監視銘柄更新: {len(global_data.symbols_active)}"
    )
