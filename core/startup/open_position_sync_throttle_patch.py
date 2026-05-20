# ============================================================
# File   : core/startup/open_position_sync_throttle_patch.py
# Version: V1.0-BROKER-EMPTY-THROTTLE
# ------------------------------------------------------------
# broker authoritative 有効時、建玉なし状態で
# sync_open_positions_from_db() が1秒ごとに broker API を叩き、
# 長い WARNING ログを出し続けて起動/場中処理が止まって見える問題を抑制する。
#
# 目的:
#   - broker_read_ok=True / broker_count=0 / symbols=[] の空結果は短時間キャッシュ
#   - 同じ空建玉状態では broker API 再読取を間引く
#   - 建玉あり/force_log=True/API失敗時は通常動作
#
# 環境変数:
#   OPEN_POSITION_EMPTY_THROTTLE_SEC   default=5.0
#   OPEN_POSITION_THROTTLE_ENABLED     default=1
# ============================================================

from __future__ import annotations

import copy
import logging
import os
import time
from typing import Any, Dict

logger = logging.getLogger(__name__)

_INSTALLED = False
_ORIGINAL_SYNC = None
_CACHE: Dict[str, Any] = {
    "ts": 0.0,
    "result": None,
    "empty_authoritative": False,
}


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on", "ok"}:
            return True
        if s in {"0", "false", "no", "n", "off", "ng"}:
            return False
        return bool(default)
    except Exception:
        return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _is_broker_authoritative_empty(result: Any) -> bool:
    """global_data 状態と戻り値から broker authoritative empty を判定。"""
    try:
        if isinstance(result, dict) and len(result) > 0:
            return False

        from global_state import global_data

        mode = str(getattr(global_data, "open_positions_source_mode", "") or "")
        read_ok = bool(getattr(global_data, "open_positions_broker_read_ok", False))
        cnt = int(getattr(global_data, "open_positions_synced_count", 0) or 0)

        if read_ok and cnt == 0 and mode.startswith("broker_credit_authoritative_empty"):
            return True
        return False
    except Exception:
        return False


def _clone_result(result: Any) -> Any:
    try:
        return copy.deepcopy(result)
    except Exception:
        return result


def install() -> bool:
    global _INSTALLED, _ORIGINAL_SYNC

    if _INSTALLED:
        return True

    if not _env_bool("OPEN_POSITION_THROTTLE_ENABLED", True):
        logger.warning("[OPEN POSITION THROTTLE] disabled by env")
        return False

    try:
        import trading.position.open_position_sync as target
    except Exception:
        logger.warning("[OPEN POSITION THROTTLE] import target failed", exc_info=False)
        return False

    original = getattr(target, "sync_open_positions_from_db", None)
    if not callable(original):
        logger.warning("[OPEN POSITION THROTTLE] target sync function unavailable")
        return False

    _ORIGINAL_SYNC = original

    def throttled_sync_open_positions_from_db(*, force_log: bool = False):
        now = time.monotonic()
        throttle_sec = max(0.0, _env_float("OPEN_POSITION_EMPTY_THROTTLE_SEC", 5.0))

        # force_log時は必ず実読取。
        if not force_log and _CACHE.get("empty_authoritative") and _CACHE.get("result") is not None:
            age = now - float(_CACHE.get("ts") or 0.0)
            if age < throttle_sec:
                if age < 0.1:
                    # 異常な連続呼び出しだけ見える化。
                    logger.debug("[OPEN POSITION THROTTLE] cached empty hit age=%.3fs", age)
                return _clone_result(_CACHE.get("result") or {})

        result = original(force_log=force_log) or {}
        empty_authoritative = _is_broker_authoritative_empty(result)

        if empty_authoritative:
            _CACHE["ts"] = now
            _CACHE["result"] = _clone_result(result or {})
            _CACHE["empty_authoritative"] = True
            logger.info(
                "[OPEN POSITION THROTTLE] cache broker authoritative empty ttl=%.1fs",
                throttle_sec,
            )
        else:
            _CACHE["ts"] = now
            _CACHE["result"] = None
            _CACHE["empty_authoritative"] = False

        return result

    target.sync_open_positions_from_db = throttled_sync_open_positions_from_db
    _INSTALLED = True
    logger.warning(
        "[OPEN POSITION THROTTLE] installed empty_throttle_sec=%.1f",
        _env_float("OPEN_POSITION_EMPTY_THROTTLE_SEC", 5.0),
    )
    return True


__all__ = ["install"]
