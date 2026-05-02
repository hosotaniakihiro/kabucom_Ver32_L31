# ============================================================
# File   : trading/exit/feature_cache.py
# Version: V32-FINAL-EXIT-FEATURE-CACHE
# ------------------------------------------------------------
# ✔ symbol単位キャッシュ
# ✔ TTL制御
# ✔ multi-timeframe統合
# ✔ ranking / daily統合
# ✔ NaN安全
# ✔ スレッド安全
# ✔ 例外安全
# ✔ 将来sequence化対応可能
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
from threading import Lock
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


# ============================================================
# FeatureCache
# ============================================================

class ExitFeatureCache:

    def __init__(self, ttl_seconds: int = 3):
        self._ttl = ttl_seconds
        self._lock = Lock()
        self._cache: Dict[str, Dict[str, Any]] = {}

    # ========================================================
    # Public
    # ========================================================

    def get(
        self,
        symbol: str,
        builder_fn
    ) -> Dict[str, Any]:
        """
        builder_fn(symbol) -> feature dict
        """

        try:
            now = dt.datetime.utcnow()

            with self._lock:

                item = self._cache.get(symbol)

                if item:
                    ts = item.get("timestamp")
                    if ts and (now - ts).total_seconds() < self._ttl:
                        return item["features"]

                # 再生成
                features = builder_fn(symbol)

                if not isinstance(features, dict):
                    features = {}

                features = self._sanitize(features)

                self._cache[symbol] = {
                    "timestamp": now,
                    "features": features
                }

                return features

        except Exception:
            logger.exception("ExitFeatureCache.get failed")
            return {}

    # ========================================================
    # Invalidate
    # ========================================================

    def invalidate(self, symbol: Optional[str] = None):

        with self._lock:
            if symbol:
                self._cache.pop(symbol, None)
            else:
                self._cache.clear()

    # ========================================================
    # Sanitize
    # ========================================================

    def _sanitize(self, features: Dict[str, Any]) -> Dict[str, float]:

        safe = {}

        for k, v in features.items():
            try:
                if v is None:
                    safe[k] = 0.0
                else:
                    val = float(v)
                    if val != val:  # NaN
                        safe[k] = 0.0
                    else:
                        safe[k] = val
            except Exception:
                safe[k] = 0.0

        return safe


# ============================================================
# Default Builder（統合用）
# ============================================================

def build_exit_features_default(
    symbol: str,
    summary_1m,
    summary_3m,
    summary_5m,
    daily,
    ranking
) -> Dict[str, float]:

    try:
        f = {}

        # 1min
        if summary_1m is not None:
            f["ma75_slope"] = summary_1m.get("ma75_slope")
            f["atr_ratio"] = summary_1m.get("atr_ratio")
            f["volume_decay"] = summary_1m.get("volume_decay")

        # ranking
        if ranking is not None:
            f["ranking_delta"] = ranking.get("delta")
            f["ranking_persistence"] = ranking.get("persistence")

        # daily
        if daily is not None:
            f["daily_range"] = daily.get("range")

        # 5min
        if summary_5m is not None:
            f["trend_5m"] = summary_5m.get("trend_score")

        # 初期値補完
        for key in [
            "ma75_slope",
            "ranking_delta",
            "ranking_persistence",
            "atr_ratio",
            "volume_decay",
            "daily_range",
            "trend_5m",
        ]:
            if key not in f:
                f[key] = 0.0

        return f

    except Exception:
        logger.exception("build_exit_features_default failed")
        return {}