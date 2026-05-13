# ============================================================
# File   : trading/entry/unfilled_order_cancel_watch.py
# Version: V1.0-ENTRY-UNFILLED-CANCEL-WATCH
# ------------------------------------------------------------
# エントリー注文後、一定秒数たっても建玉が確認できない場合に
# kabuステーションの注文取消APIを呼ぶ。
#
# 目的:
#   - 指値が刺さらないまま資金・銘柄ロックが残る状態を防ぐ
#   - 10秒未約定なら取消し、次の銘柄へ移れるようにする
# ============================================================

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

from kabu_api.cancel_order import cancel_order_common
from utils_common import normalize_symbol

logger = logging.getLogger(__name__)

_DEFAULT_CANCEL_SECONDS = 10.0
_STARTED: set[str] = set()
_LOCK = threading.RLock()


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return bool(default)
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        if v is None or v == "":
            return default
        return int(float(v))
    except Exception:
        return default


def _pos_symbol(p: Any) -> str:
    try:
        if isinstance(p, dict):
            return normalize_symbol(
                p.get("symbol")
                or p.get("Symbol")
                or p.get("銘柄コード")
                or p.get("code")
                or p.get("stock_code")
            )
    except Exception:
        pass
    return ""


def _pos_qty(p: Any) -> int:
    try:
        if isinstance(p, dict):
            return _safe_int(
                p.get("qty")
                or p.get("Qty")
                or p.get("quantity")
                or p.get("Quantity")
                or p.get("LeavesQty")
                or p.get("HoldQty")
                or p.get("hold_qty"),
                0,
            )
    except Exception:
        pass
    return 0


def _has_open_position_symbol(symbol: str) -> bool:
    """
    約定済みで建玉ができているかを確認する。
    建玉があれば取消しない。
    """
    sym = normalize_symbol(symbol)
    if not sym:
        return False

    try:
        from global_state import global_data

        positions = getattr(global_data, "open_positions", None)
        if isinstance(positions, dict):
            for k, v in positions.items():
                if normalize_symbol(k) == sym:
                    return True
                if isinstance(v, dict) and _pos_symbol(v) == sym:
                    q = _pos_qty(v)
                    if q <= 0:
                        return True
                    return q > 0

        try:
            rows = global_data.get_positions()
        except Exception:
            rows = []

        if isinstance(rows, dict):
            rows = list(rows.values())

        if hasattr(rows, "to_dict"):
            try:
                rows = rows.to_dict("records")
            except Exception:
                rows = []

        if isinstance(rows, (list, tuple, set)):
            for p in rows:
                if not isinstance(p, dict):
                    continue
                if _pos_symbol(p) == sym:
                    q = _pos_qty(p)
                    if q <= 0:
                        return True
                    return q > 0

        try:
            p = global_data.get_position(sym)
            if p:
                return True
        except Exception:
            pass

    except Exception:
        logger.exception("[ENTRY CANCEL WATCH] open position check failed symbol=%s", symbol)

    return False


def _is_entry_inflight(symbol: str) -> bool:
    try:
        from global_state import global_data
        return bool(global_data.is_entry_inflight(symbol))
    except Exception:
        return True


def _release_entry_inflight(symbol: str) -> None:
    try:
        from global_state import global_data
        global_data.remove_entry_inflight(symbol)
    except Exception:
        pass


def schedule_cancel_unfilled_entry_order(
    *,
    symbol: str,
    order_id: str,
    side: str,
    seconds: float | None = None,
) -> None:
    """
    エントリー発注後の未約定取消監視を開始する。

    環境変数:
      ENTRY_UNFILLED_CANCEL_ENABLED=1/0
      ENTRY_UNFILLED_CANCEL_SECONDS=10
    """
    if not _env_bool("ENTRY_UNFILLED_CANCEL_ENABLED", True):
        return

    sym = normalize_symbol(symbol)
    oid = str(order_id or "").strip()
    side_u = str(side or "").upper().strip()
    wait_sec = float(seconds if seconds is not None else _env_float("ENTRY_UNFILLED_CANCEL_SECONDS", _DEFAULT_CANCEL_SECONDS))

    if not sym or not oid or wait_sec <= 0:
        logger.warning(
            "[ENTRY CANCEL WATCH] skip invalid args symbol=%s order_id=%s side=%s seconds=%s",
            symbol,
            order_id,
            side,
            seconds,
        )
        return

    key = f"{sym}|{oid}"
    with _LOCK:
        if key in _STARTED:
            logger.info(
                "[ENTRY CANCEL WATCH] already scheduled symbol=%s side=%s order_id=%s",
                sym,
                side_u,
                oid,
            )
            return
        _STARTED.add(key)

    def _worker() -> None:
        try:
            time.sleep(wait_sec)

            if _has_open_position_symbol(sym):
                logger.info(
                    "[ENTRY CANCEL WATCH] filled/open position detected -> no cancel symbol=%s side=%s order_id=%s wait=%.1fs",
                    sym,
                    side_u,
                    oid,
                    wait_sec,
                )
                _release_entry_inflight(sym)
                return

            if not _is_entry_inflight(sym):
                logger.info(
                    "[ENTRY CANCEL WATCH] inflight already released -> no cancel symbol=%s side=%s order_id=%s wait=%.1fs",
                    sym,
                    side_u,
                    oid,
                    wait_sec,
                )
                return

            ok = cancel_order_common(
                oid,
                symbol=sym,
                reason=f"ENTRY_UNFILLED_TIMEOUT_{int(wait_sec)}S_{side_u}",
            )

            _release_entry_inflight(sym)

            if ok:
                logger.warning(
                    "[ENTRY CANCEL WATCH] canceled unfilled entry order symbol=%s side=%s order_id=%s wait=%.1fs",
                    sym,
                    side_u,
                    oid,
                    wait_sec,
                )
            else:
                logger.warning(
                    "[ENTRY CANCEL WATCH] cancel failed or already filled symbol=%s side=%s order_id=%s wait=%.1fs",
                    sym,
                    side_u,
                    oid,
                    wait_sec,
                )

        except Exception:
            logger.exception(
                "[ENTRY CANCEL WATCH] worker failed symbol=%s side=%s order_id=%s",
                sym,
                side_u,
                oid,
            )
        finally:
            with _LOCK:
                _STARTED.discard(key)

    th = threading.Thread(
        target=_worker,
        name=f"entry-cancel-watch-{sym}-{oid}",
        daemon=True,
    )
    th.start()

    logger.warning(
        "[ENTRY CANCEL WATCH] scheduled symbol=%s side=%s order_id=%s cancel_after=%.1fs",
        sym,
        side_u,
        oid,
        wait_sec,
    )


__all__ = ["schedule_cancel_unfilled_entry_order"]
