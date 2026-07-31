# ============================================================
# File   : core/startup/entry_summary_retry_rotation_runtime_patch.py
# Version: V2-EXECUTE-BEST-CANDIDATE-WRAP-REMOVED
# ------------------------------------------------------------
# SUMMARY_AIでAI_OKになった候補を、未約定2秒キャンセル後に
# pendingへ戻す。
#
# V2:
#   - _execute_best_candidate / global_data.add_entry_inflight の
#     monkeypatchラップ (item を threading.local() 経由で受け渡す方式) を撤去。
#     entry_execute_timeout_guard_patch が発注本体を別スレッド
#     (threading.Thread) で実行するため、その内部から呼ばれる
#     add_entry_inflight のラップでは _CURRENT.item が別スレッドの
#     thread-local に見えず、記憶できないことがあるバグを内包していた。
#   - trading/handlers/entry_controller.py の _execute_best_candidate 本体
#     (発注成功直後) から remember_order_for_retry() を直接呼ぶ形に変更し、
#     item を直接渡すことで解消した。
#
# V1.1:
#   - RESTフル板エントリー有効時は、再queueを原則1回だけにする。
#   - 再queue時にもう一度 build_entry_order() を通るため、REST板で
#     価格を再計算できる。
#   - 1回再挑戦しても約定しない場合は、同銘柄に粘らず次候補へ進む。
#
# V1.0:
#   - 最大3巡まで循環。
# ============================================================

from __future__ import annotations

import copy
import datetime as dt
import logging
import os
import threading
from typing import Any, Dict

logger = logging.getLogger(__name__)
_INSTALLED = False
_LOCK = threading.RLock()
_ORDER_ENTRY_MAP: Dict[str, Dict[str, Any]] = {}


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return bool(default)
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        return int(default) if v is None or str(v).strip() == "" else int(float(v))
    except Exception:
        return int(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        return float(default) if v is None or str(v).strip() == "" else float(v)
    except Exception:
        return float(default)


def _sym(v: Any) -> str:
    s = str(v or "").strip()
    return s[:-2] if s.endswith(".0") else s


def _rest_board_reprice_mode() -> bool:
    return _env_bool("ENTRY_REST_FULL_BOARD_ENABLED", True) and _env_bool("ENTRY_REST_REPRICE_RETRY_ONCE", True)


def _retry_max_rounds_default() -> int:
    return 1 if _rest_board_reprice_mode() else 3


def _retry_cooldown_default() -> float:
    return 4.0 if _rest_board_reprice_mode() else 2.5


def _is_summary_ai(entry: Dict[str, Any]) -> bool:
    src = str(entry.get("source") or "").strip().upper()
    typ = str(entry.get("entry_type") or "").strip().upper()
    return src == "SUMMARY" or typ == "SUMMARY_AI"


def _set_short_cooldown(symbol: str) -> None:
    try:
        from global_state import global_data
        sec = _env_float("ENTRY_SUMMARY_RETRY_SYMBOL_COOLDOWN_SEC", _retry_cooldown_default())
        if sec <= 0:
            return
        until = dt.datetime.now() + dt.timedelta(seconds=sec)
        if not hasattr(global_data, "trade_restricted") or not isinstance(global_data.trade_restricted, dict):
            global_data.trade_restricted = {}
        global_data.trade_restricted[_sym(symbol)] = until
        logger.warning("[SUMMARY RETRY ROTATION] short cooldown symbol=%s until=%s sec=%.2f rest_reprice=%s", symbol, until, sec, _rest_board_reprice_mode())
    except Exception:
        logger.exception("[SUMMARY RETRY ROTATION] cooldown failed symbol=%s", symbol)


def remember_order_for_retry(order_id: str, symbol: str, side: str, entry: Any) -> None:
    """trading/handlers/entry_controller.py の _execute_best_candidate 本体から、
    発注成功直後に item["entry"] を直接渡して呼ばれる。"""
    try:
        if not order_id or not isinstance(entry, dict) or not _is_summary_ai(entry):
            return
        saved = copy.deepcopy(entry)
        saved["symbol"] = _sym(saved.get("symbol") or symbol)
        saved["side"] = str(saved.get("side") or side).upper()
        saved["source"] = saved.get("source") or "SUMMARY"
        saved["entry_type"] = saved.get("entry_type") or "SUMMARY_AI"
        saved["summary_retry_round"] = int(saved.get("summary_retry_round") or 0)
        saved["summary_retry_original_order_id"] = str(order_id)
        saved["rest_board_reprice_retry"] = bool(_rest_board_reprice_mode())
        with _LOCK:
            _ORDER_ENTRY_MAP[str(order_id)] = saved
        logger.warning(
            "[SUMMARY RETRY ROTATION] remember order_id=%s symbol=%s side=%s round=%s rest_reprice=%s",
            order_id, symbol, side, saved.get("summary_retry_round"), saved.get("rest_board_reprice_retry"),
        )
    except Exception:
        logger.exception("[SUMMARY RETRY ROTATION] remember failed order_id=%s", order_id)


def on_unfilled_order_cancelled(symbol: str, order_id: str) -> bool:
    if not _env_bool("ENTRY_SUMMARY_RETRY_ENABLED", True):
        return False
    try:
        with _LOCK:
            entry = _ORDER_ENTRY_MAP.pop(str(order_id), None)
        if not isinstance(entry, dict) or not _is_summary_ai(entry):
            return False

        max_rounds = _env_int("ENTRY_SUMMARY_RETRY_MAX_ROUNDS", _retry_max_rounds_default())
        new_round = int(entry.get("summary_retry_round") or 0) + 1
        if new_round > max_rounds:
            logger.warning(
                "[SUMMARY RETRY ROTATION] max rounds reached symbol=%s order_id=%s round=%s max=%s rest_reprice=%s -> move next candidate",
                symbol, order_id, new_round, max_rounds, entry.get("rest_board_reprice_retry"),
            )
            return False

        entry["summary_retry_round"] = new_round
        entry["summary_retry_prev_order_id"] = str(order_id)
        entry["created_at"] = dt.datetime.now()
        entry["symbol"] = _sym(entry.get("symbol") or symbol)
        entry["source"] = entry.get("source") or "SUMMARY"
        entry["entry_type"] = entry.get("entry_type") or "SUMMARY_AI"
        entry["retry_reprice_reason"] = "unfilled_cancel_rest_board_reprice" if _rest_board_reprice_mode() else "unfilled_cancel_retry"

        from trading.entry.pending_manager import add_pending, snapshot_root
        ok = add_pending(entry)
        _set_short_cooldown(entry["symbol"])
        logger.warning(
            "[SUMMARY RETRY ROTATION] requeue symbol=%s side=%s round=%s/%s ok=%s root=%s rest_reprice=%s reason=%s",
            entry.get("symbol"), entry.get("side"), new_round, max_rounds, ok, snapshot_root(), entry.get("rest_board_reprice_retry"), entry.get("retry_reprice_reason"),
        )
        return bool(ok)
    except Exception:
        logger.exception("[SUMMARY RETRY ROTATION] requeue failed symbol=%s order_id=%s", symbol, order_id)
        return False


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    _INSTALLED = True
    logger.warning(
        "[SUMMARY RETRY ROTATION] installed (execute_best_candidate wrap removed, called directly from entry_controller) max_rounds=%s cooldown_sec=%.2f rest_reprice_once=%s",
        _env_int("ENTRY_SUMMARY_RETRY_MAX_ROUNDS", _retry_max_rounds_default()),
        _env_float("ENTRY_SUMMARY_RETRY_SYMBOL_COOLDOWN_SEC", _retry_cooldown_default()),
        _rest_board_reprice_mode(),
    )
    return True

try:
    install()
except Exception:
    logger.exception("[SUMMARY RETRY ROTATION] auto install failed")

__all__ = ["install", "on_unfilled_order_cancelled", "remember_order_for_retry"]
