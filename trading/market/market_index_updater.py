# ============================================================
# trading/market/market_index_updater.py
# ------------------------------------------------------------
# 市場指数・騰落・出来高のリアルタイム更新
# index_shock_detector 用データ供給
# ============================================================

import logging
from collections import deque

from global_state import global_data

logger = logging.getLogger(__name__)

# ============================================================
# 設定
# ============================================================

MAX_LEN = 10        # series の最大保持本数
DEFAULT_VOL_RATIO = 1.0


# ============================================================
# 初期化（global_data 拡張）
# ============================================================

def init_market_index():
    """
    起動時に global_data.market_index を初期化
    """
    if hasattr(global_data, "market_index"):
        return

    global_data.market_index = {
        "nikkei_change_pct_series": deque(maxlen=MAX_LEN),
        "advance_series": deque(maxlen=MAX_LEN),
        "decline_series": deque(maxlen=MAX_LEN),
        "market_volume_ratio": DEFAULT_VOL_RATIO,
    }

    logger.info("[MARKET_INDEX] initialized")


# ============================================================
# 更新API（外部公開）
# ============================================================

def update_market_index(
    *,
    nikkei_change_pct: float | None = None,
    advance: int | None = None,
    decline: int | None = None,
    volume_ratio: float | None = None,
):
    """
    市場指数情報を1ティック分更新する

    呼び出し元：
    - kabutan / yahoo / kabu API
    - 30秒 / 1分 scheduler
    """

    init_market_index()

    idx = global_data.market_index

    if nikkei_change_pct is not None:
        try:
            idx["nikkei_change_pct_series"].append(float(nikkei_change_pct))
        except Exception:
            pass

    if advance is not None:
        idx["advance_series"].append(int(advance))

    if decline is not None:
        idx["decline_series"].append(int(decline))

    if volume_ratio is not None:
        try:
            idx["market_volume_ratio"] = float(volume_ratio)
        except Exception:
            pass

    logger.debug(
        "[MARKET_INDEX_UPDATE] "
        f"nikkei={nikkei_change_pct} "
        f"adv={advance} dec={decline} "
        f"vol_ratio={idx['market_volume_ratio']}"
    )
