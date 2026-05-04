# ============================================================
# File   : crash_short_exit.py
# Created: 2026-01-02
# ------------------------------------------------------------
# 市場クラッシュ SHORT（SELL）専用 EXIT 判定
#
# 方針:
# ・利益確定は早め（クラッシュ反発に巻き込まれない）
# ・指数反発 / 地合い改善 / 出来高減衰で即 EXIT
# ・TONOSAMA / イナゴ地合いに転じたら強制 EXIT
#
# return:
#   True  = EXIT（成行/即時）
#   False = HOLD
# ============================================================

from global_state import global_data
from AI.market_regime import detect_market_regime
from AI.index_shock_detector import detect_index_shock   # ★ 追加


# ============================================================
# 設定（経験則）
# ============================================================

# 利確・損切り（指数ベース）
TAKE_PROFIT_PCT = 0.6     # +0.6% 分の下落を取れたら十分
STOP_LOSS_PCT   = -0.3    # -0.3% 逆行したら即撤退

# 反発検知
REBOUND_NIKKEI_PCT = 0.25   # 直近反発幅
REBOUND_BREADTH    = 0.45   # 騰落改善

# 出来高減衰（投げが終わった兆候）
MIN_VOLUME_RATIO = 0.9

# 最大保持秒（クラッシュ SHORT は長く持たない）
MAX_HOLD_SEC = 120


# ============================================================
# メイン
# ============================================================

def should_exit_crash_short(
    entry_price: float,
    current_price: float,
    entry_time,
    features: dict | None = None,
) -> bool:
    """
    crash_short の EXIT 判定

    entry_price:
        SHORT 約定価格
    current_price:
        現在値
    entry_time:
        SHORT 約定時刻（datetime）
    features:
        市場特徴量（None の場合は global_data から取得）
    """

    # --------------------------------------------------------
    # 🚨 0. 指数ショック（急反発）最優先 EXIT
    # --------------------------------------------------------
    # 上方向ショック検知時は即逃げる
    if detect_index_shock() == 2:
        return True

    # --------------------------------------------------------
    # ① 地合い改善チェック
    # --------------------------------------------------------
    regime = detect_market_regime()

    # 通常以上に回復 → 即 EXIT
    if regime >= 2:
        return True

    # --------------------------------------------------------
    # ② 特徴量構築
    # --------------------------------------------------------
    if features is None:
        features = _build_feature_dict()

    nikkei_pct = features.get("nikkei_change_pct", 0.0)
    breadth = features.get("advance_ratio", 0.5)
    vol_ratio = features.get("market_volume_ratio", 1.0)

    # --------------------------------------------------------
    # ③ PnL 判定（SHORT）
    # --------------------------------------------------------
    pnl_pct = (entry_price - current_price) / entry_price * 100.0

    # 利確
    if pnl_pct >= TAKE_PROFIT_PCT:
        return True

    # 損切り
    if pnl_pct <= STOP_LOSS_PCT:
        return True

    # --------------------------------------------------------
    # ④ 指数反発検知（補助）
    # --------------------------------------------------------
    if nikkei_pct >= REBOUND_NIKKEI_PCT and breadth >= REBOUND_BREADTH:
        return True

    # --------------------------------------------------------
    # ⑤ 出来高減衰（投げ終了）
    # --------------------------------------------------------
    if vol_ratio <= MIN_VOLUME_RATIO:
        return True

    # --------------------------------------------------------
    # ⑥ 最大保持時間
    # --------------------------------------------------------
    try:
        hold_sec = (global_data.now() - entry_time).total_seconds()
        if hold_sec >= MAX_HOLD_SEC:
            return True
    except Exception:
        # 時刻不整合時は安全側
        return True

    # --------------------------------------------------------
    # HOLD
    # --------------------------------------------------------
    return False


# ============================================================
# 特徴量構築
# ============================================================

def _build_feature_dict() -> dict:
    """
    global_data から市場特徴量を生成
    """

    idx = getattr(global_data, "market_index", {})

    nikkei_pct = idx.get("nikkei_change_pct")
    adv = idx.get("advance", 0)
    dec = idx.get("decline", 0)

    volume_ratio = idx.get("market_volume_ratio", 1.0)

    # 旧構造 fallback
    nikkei = getattr(global_data, "market_nikkei", None)
    if nikkei_pct is None and nikkei:
        nikkei_pct = nikkei.get("change_pct", 0.0)

    nikkei_pct = nikkei_pct or 0.0
    advance_ratio = adv / max(adv + dec, 1)

    return {
        "nikkei_change_pct": nikkei_pct,
        "advance_ratio": advance_ratio,
        "market_volume_ratio": volume_ratio,
    }
