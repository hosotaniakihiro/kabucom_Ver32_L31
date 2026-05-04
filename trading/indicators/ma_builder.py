# ============================================================
# trading/indicators/ma_builder.py
# Ver1.1-FINAL-MA-CONFIDENCE-ORM-SAFE
# ------------------------------------------------------------
# ✔ MA5 / MA25 / MA75 を DB から構築
# ✔ PUSH依存なし（summary DB 常駐）
# ✔ ranking / fallback 補完対応
# ✔ 信頼度（confidence）を必ず返す
# ✔ ORM(datetime無し)完全対応
# ✔ ローテーション耐性・AI学習耐性
# ============================================================

from typing import Tuple, Optional
import datetime as dt

from database import Session_summary
from database.models import (
    StockSummary1Min,
    StockSummary3Min,
    StockSummary5Min,
)

# ------------------------------------------------------------
# interval → ORM model
# ------------------------------------------------------------
MODEL_MAP = {
    1: StockSummary1Min,
    3: StockSummary3Min,
    5: StockSummary5Min,
}

# ------------------------------------------------------------
# source 優先度（大きいほど強い）
# push > ranking > fallback
# ------------------------------------------------------------
SOURCE_PRIORITY = {
    "push": 3,
    "ranking": 2,
    "fallback": 1,
}

# ------------------------------------------------------------
# MA builder（ORM安全）
# ------------------------------------------------------------
def build_ma_from_db(
    *,
    symbol: str,
    interval: int,
    ma_window: int,
    end_time: Optional[dt.datetime] = None,
) -> Tuple[Optional[float], float]:
    """
    summary DB から MA を構築する（ORM安全版）

    Parameters
    ----------
    symbol : str
        銘柄コード
    interval : int
        1 / 3 / 5（分足）
    ma_window : int
        MA本数（5 / 25 / 75）
    end_time : datetime, optional
        この時刻以前のデータのみ使用（DF由来）

    Returns
    -------
    ma_value : float | None
        MA値（算出不能時は None）
    confidence : float
        信頼度（0.0 ～ 1.0）
    """

    if interval not in MODEL_MAP:
        raise ValueError(f"invalid interval: {interval}")

    Model = MODEL_MAP[interval]

    # --------------------------------------------------------
    # 時刻条件（datetime → date + end_time）
    # --------------------------------------------------------
    end_date = None
    end_time_only = None

    if end_time is not None:
        end_date = end_time.date()
        end_time_only = end_time.time()

    # --------------------------------------------------------
    # DB 取得
    # --------------------------------------------------------
    with Session_summary() as session:
        q = session.query(Model).filter(Model.symbol == symbol)

        if end_date is not None:
            q = q.filter(
                (Model.date < end_date) |
                (
                    (Model.date == end_date) &
                    (Model.end_time <= end_time_only)
                )
            )

        # 最新順で多めに取得（補完混在対策）
        rows = (
            q.order_by(
                Model.date.desc(),
                Model.end_time.desc()
            )
            .limit(ma_window * 2)
            .all()
        )

    if not rows:
        return None, 0.0

    # --------------------------------------------------------
    # close_price を source 優先で抽出
    # --------------------------------------------------------
    closes = []
    used_sources = []

    for r in rows:
        if r.close_price is None:
            continue

        closes.append(float(r.close_price))
        used_sources.append(getattr(r, "source", "push"))

        if len(closes) >= ma_window:
            break

    used = len(closes)

    if used == 0:
        return None, 0.0

    # --------------------------------------------------------
    # MA 計算
    # --------------------------------------------------------
    ma_value = sum(closes) / used

    # --------------------------------------------------------
    # 信頼度計算（本数ベース）
    # --------------------------------------------------------
    confidence = min(1.0, used / ma_window)

    # --------------------------------------------------------
    # source による補正
    # --------------------------------------------------------
    if used_sources:
        weakest_source = min(
            used_sources,
            key=lambda s: SOURCE_PRIORITY.get(s, 0)
        )

        if weakest_source == "ranking":
            confidence *= 0.95
        elif weakest_source == "fallback":
            confidence *= 0.5

    # 上下限ガード
    confidence = max(0.0, min(confidence, 1.0))

    return ma_value, confidence


# ------------------------------------------------------------
# 複数 MA をまとめて取得（便利関数）
# ------------------------------------------------------------
def build_all_ma(
    *,
    symbol: str,
    interval: int,
    end_time: Optional[dt.datetime] = None,
):
    """
    MA5 / MA25 / MA75 をまとめて返す
    """
    result = {}

    for w in (5, 25, 75):
        ma, conf = build_ma_from_db(
            symbol=symbol,
            interval=interval,
            ma_window=w,
            end_time=end_time,
        )
        result[f"ma{w}"] = ma
        result[f"ma{w}_conf"] = conf

    return result
