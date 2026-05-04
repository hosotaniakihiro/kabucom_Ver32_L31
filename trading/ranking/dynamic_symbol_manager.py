# ============================================================
# trading/ranking/dynamic_symbol_manager.py
# ------------------------------------------------------------
# 動的監視銘柄マネージャ（RANKING 正式版）
#
# ✔ 20分ランキング非登場銘柄を除外
# ✔ 値上がり率 上位50 ∩ 売買高 上位25 を最優先
# ✔ 常に MAX_ACTIVE 銘柄を維持
# ✔ ランキング反映時に価格フィルタ（150〜10000円）
# ✔ GlobalData 正式 API 完全準拠
# ✔ ranking_snapshot_1min を唯一の正本とする
# ✔ ★ rank_position 無しを完全救済（NEW / SAFE）
# ✔ ★ GuardedSet 警告を出さない正式更新（NEW）
# ============================================================

import datetime as dt
import logging
import pandas as pd

from global_state import global_data

logger = logging.getLogger(__name__)

# ============================================================
# 設定
# ============================================================

MAX_ACTIVE = 100
RANKING_LOOKBACK_MIN = 20

PRICE_MIN = 150
PRICE_MAX = 10000

GAIN_TOP_N = 50       # 値上がり率 上位
VOLUME_TOP_N = 25     # 売買高 上位（値上がり率内）

# ============================================================
# 内部ユーティリティ
# ============================================================

def _now() -> dt.datetime:
    return dt.datetime.now()


def _recent_limit_time() -> dt.datetime:
    return _now() - dt.timedelta(minutes=RANKING_LOOKBACK_MIN)


def _get_latest_snapshot() -> pd.DataFrame:
    """
    ranking_snapshot_1min を取得（正式 API）
    """
    try:
        return global_data.get_latest_ranking_snapshot()
    except Exception:
        return pd.DataFrame()


def _ensure_rank_position(df: pd.DataFrame) -> pd.DataFrame:
    """
    ranking snapshot に rank_position が無い場合の正規化
    - 既存列があればそれを使用
    - 無ければ index 順で生成
    """
    if df is None or df.empty:
        return df

    if "rank_position" in df.columns:
        return df

    for col in (
        "rank",
        "順位",
        "順位_値上がり率",
        "順位_売買代金",
        "順位_売買高",
    ):
        if col in df.columns:
            out = df.copy()
            out["rank_position"] = pd.to_numeric(out[col], errors="coerce")
            return out

    # 最終フォールバック（index 順）
    out = df.copy()
    out["rank_position"] = range(1, len(out) + 1)
    return out


def _extract_recent_ranking_symbols(df: pd.DataFrame) -> set[str]:
    """
    snapshot_time が直近 RANKING_LOOKBACK_MIN 以内の symbol を抽出
    """
    if df is None or df.empty:
        return set()

    if "snapshot_time" not in df.columns or "symbol" not in df.columns:
        return set()

    df = df.copy()
    df["snapshot_time"] = pd.to_datetime(df["snapshot_time"], errors="coerce")
    df = df.dropna(subset=["snapshot_time"])

    recent_df = df[df["snapshot_time"] >= _recent_limit_time()]
    return set(recent_df["symbol"].astype(str).unique())


def _filter_by_price(df: pd.DataFrame) -> pd.DataFrame:
    """
    ランキング → 監視銘柄に入れる前の価格フィルタ
    """
    if df is None or df.empty:
        return df

    price_col = None
    for c in ("current_price", "price", "close_price", "last_price"):
        if c in df.columns:
            price_col = c
            break

    if price_col is None:
        return df.iloc[0:0]

    out = df.copy()
    out[price_col] = pd.to_numeric(out[price_col], errors="coerce")

    return out[
        (out[price_col] >= PRICE_MIN) &
        (out[price_col] <= PRICE_MAX)
    ]


def _pick_gain_volume_combo(
    df_snapshot: pd.DataFrame,
    exclude: set[str],
) -> list[str]:
    """
    値上がり率 上位50 の中から
    売買高 上位25 を抽出
    """
    if df_snapshot is None or df_snapshot.empty:
        return []

    df_snapshot = _ensure_rank_position(df_snapshot)

    df_gain = df_snapshot[df_snapshot["rank_type"] == "値上がり率"]
    df_vol  = df_snapshot[df_snapshot["rank_type"] == "売買高"]

    if df_gain.empty or df_vol.empty:
        return []

    df_gain = _filter_by_price(df_gain)
    df_vol  = _filter_by_price(df_vol)

    if df_gain.empty or df_vol.empty:
        return []

    gain_syms = (
        df_gain
        .sort_values("rank_position")
        ["symbol"]
        .astype(str)
        .drop_duplicates()
        .head(GAIN_TOP_N)
        .tolist()
    )

    if not gain_syms:
        return []

    df_vol2 = df_vol[df_vol["symbol"].astype(str).isin(gain_syms)]

    picked: list[str] = []
    for sym in df_vol2.sort_values("rank_position")["symbol"].astype(str):
        if sym in exclude:
            continue
        picked.append(sym)
        exclude.add(sym)
        if len(picked) >= VOLUME_TOP_N:
            break

    return picked


def _pick_from_snapshot(
    df_snapshot: pd.DataFrame,
    priority_types: list[str],
    exclude: set[str],
    limit: int,
) -> list[str]:
    """
    フォールバック用：ranking_snapshot から優先順で補充
    """
    picked: list[str] = []

    df_snapshot = _ensure_rank_position(df_snapshot)

    for rtype in priority_types:
        df = df_snapshot[df_snapshot["rank_type"] == rtype]
        if df.empty:
            continue

        df = _filter_by_price(df)
        if df.empty:
            continue

        for sym in df.sort_values("rank_position")["symbol"].astype(str):
            if sym in exclude:
                continue
            picked.append(sym)
            exclude.add(sym)
            if len(picked) >= limit:
                return picked

    return picked


# ============================================================
# メイン関数
# ============================================================

def update_active_symbols(force: bool = False):
    """
    動的監視銘柄を更新する（ranking 主導）
    """

    df_snapshot = _get_latest_snapshot()

    if df_snapshot.empty:
        logger.warning("active_symbol_manager: ranking_snapshot empty → skip")
        return

    # --------------------------------------------------------
    # 現在の ACTIVE
    # --------------------------------------------------------
    prev_active = set(getattr(global_data, "symbols_active", set()) or [])

    # --------------------------------------------------------
    # ① 最近ランキングに出た銘柄
    # --------------------------------------------------------
    recent_symbols = _extract_recent_ranking_symbols(df_snapshot)

    # --------------------------------------------------------
    # ② 生存判定（20分以内に1回でも登場）
    # --------------------------------------------------------
    alive = prev_active & recent_symbols
    exclude = set(alive)

    # --------------------------------------------------------
    # ③ 最優先：値上がり率50 ∩ 売買高25
    # --------------------------------------------------------
    primary = _pick_gain_volume_combo(df_snapshot, exclude)

    # --------------------------------------------------------
    # ④ 不足分は従来ランキングから補充
    # --------------------------------------------------------
    need = MAX_ACTIVE - (len(alive) + len(primary))
    secondary: list[str] = []

    if need > 0:
        priority_types = [
            "値上がり率",
            "売買代金",
            "売買高",
            "TICK回数",
        ]
        secondary = _pick_from_snapshot(
            df_snapshot,
            priority_types,
            exclude,
            need,
        )

    # --------------------------------------------------------
    # ⑤ 更新（GuardedSet 正式ルート）
    # --------------------------------------------------------
    updated = list(alive) + primary + secondary
    updated = updated[:MAX_ACTIVE]

    global_data.symbols_active = set(updated)

    with global_data.latest_ranking_symbols_lock:
        gs = global_data.latest_ranking_symbols
        gs._silent = True
        gs.clear()
        gs.update(updated)
        gs._silent = False

    logger.info(
        "🔄 ACTIVE更新: prev=%d → now=%d "
        "(alive=%d, gain∩vol=%d, add=%d)",
        len(prev_active),
        len(updated),
        len(alive),
        len(primary),
        len(secondary),
    )
