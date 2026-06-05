from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)
_INSTALLED = False


def _csv_env(name: str, default: str) -> list[str]:
    try:
        raw = os.getenv(name, default)
        return [x.strip() for x in str(raw).split(',') if x.strip()]
    except Exception:
        return [x.strip() for x in default.split(',') if x.strip()]


def _int_env(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, str(default))))
    except Exception:
        return int(default)


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return float(default)


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        import trading.ranking.collectors as c

        # 429対策: 取引中の毎分収集はエントリーに効く上昇/下落ランキングを優先する。
        # 旧: 7 types x 4 markets = 28 calls/min
        # 新既定: 2 types x 3 markets = 6 calls/min
        type_ids = []
        for x in _csv_env("RANKING_API_ENABLED_TYPE_IDS", "1,2"):
            try:
                type_ids.append(int(float(x)))
            except Exception:
                pass
        if not type_ids:
            type_ids = [1, 2]

        markets = _csv_env("RANKING_API_ENABLED_MARKETS", "ALL,TP,TS")
        market_master = {
            "ALL": "全市場",
            "TP": "東証プライム",
            "TS": "東証スタンダード",
            "TG": "東証グロース",
        }
        type_master = {
            1: "値上がり率",
            2: "値下がり率",
            3: "売買高上位",
            4: "売買代金",
            5: "TICK回数",
            6: "売買高急増",
            7: "売買代金急増",
        }
        c.TYPE_TO_NAME = {i: type_master[i] for i in type_ids if i in type_master}
        c.EXCHANGE_DIVISIONS = {m: market_master[m] for m in markets if m in market_master}
        c.API_CALL_SLEEP_SEC = max(0.05, _float_env("RANKING_API_CALL_SLEEP_SEC", 0.25))

        os.environ.setdefault("RANKING_API_ENABLED_TYPE_IDS", ",".join(str(i) for i in c.TYPE_TO_NAME.keys()))
        os.environ.setdefault("RANKING_API_ENABLED_MARKETS", ",".join(c.EXCHANGE_DIVISIONS.keys()))
        os.environ.setdefault("RANKING_API_CALL_SLEEP_SEC", str(c.API_CALL_SLEEP_SEC))
        os.environ.setdefault("RANKING_API_429_COOLDOWN_SEC", "60")
        os.environ.setdefault("RANKING_ENTRY_TIMEOUT_COOLDOWN_SEC", "20")
        os.environ.setdefault("RANKING_ENTRY_TIMEOUT_COOLDOWN_MAX_SEC", "60")

        _INSTALLED = True
        logger.warning(
            "[RANKING API CALL BUDGET] installed type_ids=%s markets=%s calls_per_cycle=%s sleep=%.3fs old_calls=28 purpose=avoid_429",
            list(c.TYPE_TO_NAME.keys()),
            list(c.EXCHANGE_DIVISIONS.keys()),
            len(c.TYPE_TO_NAME) * len(c.EXCHANGE_DIVISIONS),
            c.API_CALL_SLEEP_SEC,
        )
        return True
    except Exception:
        logger.exception("[RANKING API CALL BUDGET] install failed")
        return False

try:
    install()
except Exception:
    logger.exception("[RANKING API CALL BUDGET] auto install failed")

__all__ = ["install"]