# ============================================================
# position_sync.py
# Ver24.2-FINAL-INFLIGHT-RELEASE-INTEGRATED
# ------------------------------------------------------------
# ✔ Ver24.1 完全保持（機能削除ゼロ）
# ✔ /positions API → DB同期
# ✔ Position DB → global_data.open_positions 更新
# ✔ atr_1min / entry_time は runtime 前提（None-SAFE）
# ✔ EXIT / state_machine 完全対応
# ✔ ENTRY inflight を「建玉成立時」に安全解放（NEW）
# ============================================================

import os
import time
import logging
import datetime as dt
import threading

from global_state import global_data
from kabu_api.positions import sync_positions_from_kabus, get_positions

from database import Session_position
from database.models import Position

logger = logging.getLogger(__name__)


def _load_default_sync_interval():
    raw = os.getenv("POSITION_SYNC_INTERVAL_SECONDS", "10")
    try:
        return float(raw)
    except (TypeError, ValueError):
        logger.warning(
            "PositionSync: invalid POSITION_SYNC_INTERVAL_SECONDS=%r → default 10.0s",
            raw,
        )
        return 10.0


POSITION_SYNC_INTERVAL_SECONDS = _load_default_sync_interval()
POSITION_SYNC_TOKEN_WAIT_SECONDS = 3
POSITION_SYNC_ERROR_WAIT_SECONDS = 5
_POSITION_SYNC_STARTED = False
_POSITION_SYNC_LOCK = threading.Lock()


# ============================================================
# P0-4: positions DB 障害時のリトライストーム防止
# ------------------------------------------------------------
# 旧実装は disk I/O error でも 10 秒ごとに無条件で再試行し続けたため、
# 2026-08-27 15:21:44 以降 NAS へ毎秒級の失敗接続を投げ続けていた
# (実測 fail_count 5 -> 200 が 422 秒 = 0.46 回/秒)。
# 失敗したら bounded exponential backoff で間隔を空ける。
# broker 側の建玉確認は PositionStateService が別途続けるので、
# DB を待つあいだも安全確認は止まらない。
# ============================================================

_DB_BACKOFF_SCHEDULE = (1.0, 2.0, 5.0, 10.0, 30.0)
_db_fail_streak = 0
_db_next_try_at = 0.0
_db_backoff_lock = threading.Lock()


def _db_read_allowed() -> tuple:
    now = time.monotonic()
    with _db_backoff_lock:
        if now >= _db_next_try_at:
            return True, 0.0
        return False, _db_next_try_at - now


def _note_db_ok() -> None:
    global _db_fail_streak, _db_next_try_at
    with _db_backoff_lock:
        if _db_fail_streak:
            logger.warning("[PositionSync] positions DB recovered after %d failures", _db_fail_streak)
        _db_fail_streak = 0
        _db_next_try_at = 0.0


def _note_db_error() -> float:
    global _db_fail_streak, _db_next_try_at
    with _db_backoff_lock:
        idx = min(_db_fail_streak, len(_DB_BACKOFF_SCHEDULE) - 1)
        wait = _DB_BACKOFF_SCHEDULE[idx]
        _db_fail_streak += 1
        _db_next_try_at = time.monotonic() + wait
        return wait


def update_open_positions_from_db():
    """
    Position DB から OPEN の建玉を global_data.open_positions に反映
    EXIT / ENTRY / AI が参照する唯一の正本

    P0-2: 読み取り失敗時に global_data.open_positions を空にしない。
          失敗は例外パスで抜け、既存 dict には一切触れない。
    """
    allowed, remain = _db_read_allowed()
    if not allowed:
        logger.info(
            "[PositionSync] positions DB backoff 中 -> skip (残り %.1fs)。"
            "既存の open_positions は保持する count=%s",
            remain,
            len(getattr(global_data, "open_positions", {}) or {}),
        )
        return

    session = None

    try:
        session = Session_position()
        rows = session.query(Position).filter_by(status="OPEN").all()

        open_positions = {}

        for p in rows:
            symbol = str(p.symbol)

            open_positions[symbol] = {
                "id": p.id,
                "symbol": symbol,
                "symbolname": getattr(p, "symbolname", ""),
                "side": p.side,
                "qty": p.qty,
                "avg_price": p.avg_price,

                # -----------------------------
                # ★ runtime 派生情報（None-SAFE）
                # -----------------------------
                "atr_1min": getattr(p, "atr_1min", None),
                "entry_time": getattr(p, "entry_time", None),
            }

        # ======================================================
        # ★ inflight 解放判定（NEW / 最重要）
        # ------------------------------------------------------
        # 「DB 上で OPEN になった」＝ 建玉成立
        # entry_controller の意思決定とは完全分離
        # ======================================================
        newly_opened = []

        with global_data.entry_inflight_lock:
            for symbol in open_positions.keys():
                if symbol in global_data.entry_inflight:
                    newly_opened.append(symbol)

        for symbol in newly_opened:
            global_data.release_entry_inflight(
                symbol,
                reason="POSITION_OPENED"
            )

        # ======================================================
        # ★ overwrite 防止設計に従う
        # ======================================================
        current = global_data.open_positions

        if not isinstance(current, dict):
            raise TypeError(
                f"open_positions invalid type: {type(current)}"
            )

        current.clear()
        current.update(open_positions)

        _note_db_ok()

        logger.info(
            "[PositionSync] OPEN positions updated: %d (released inflight=%d)",
            len(open_positions),
            len(newly_opened),
        )

    except Exception as e:
        wait = _note_db_error()
        # P0-2: ここで current.clear() は済んでいない (例外は query で出る)。
        # 既存の open_positions には触れず、last-known をそのまま残す。
        logger.error(
            "[PositionSync] open_positions 更新エラー: %s "
            "-> 既存の open_positions を保持 (count=%s)、次の再試行まで %.1fs",
            e,
            len(getattr(global_data, "open_positions", {}) or {}),
            wait,
            exc_info=True,
        )

    finally:
        if session:
            try:
                session.close()
            except Exception:
                logger.debug("[PositionSync] session close failed", exc_info=True)


def _resolve_sync_interval(interval_seconds=None):
    try:
        interval = float(
            POSITION_SYNC_INTERVAL_SECONDS
            if interval_seconds is None
            else interval_seconds
        )
    except (TypeError, ValueError):
        logger.warning(
            "PositionSync: invalid interval=%r → default %.1fs",
            interval_seconds,
            POSITION_SYNC_INTERVAL_SECONDS,
        )
        interval = POSITION_SYNC_INTERVAL_SECONDS

    return max(1.0, interval)


def start_position_sync_loop(interval_seconds=None):
    """
    Position API → DB → global_state を同期する常駐スレッド。

    interval_seconds 未指定時は POSITION_SYNC_INTERVAL_SECONDS（既定10秒）で同期する。
    """

    global _POSITION_SYNC_STARTED

    interval = _resolve_sync_interval(interval_seconds)

    with _POSITION_SYNC_LOCK:
        if _POSITION_SYNC_STARTED:
            logger.info(
                "PositionSync thread already started (interval=%.1fs)",
                interval,
            )
            return False
        _POSITION_SYNC_STARTED = True

    def loop():
        logger.info("PositionSyncLoop started interval=%.1fs", interval)

        while True:

            # token 待ち
            if not getattr(global_data, "token_value", None):
                logger.warning(
                    "PositionSync: token 未セット → %.1fs待機",
                    POSITION_SYNC_TOKEN_WAIT_SECONDS,
                )
                time.sleep(POSITION_SYNC_TOKEN_WAIT_SECONDS)
                continue

            # ==================================================
            # API 取得
            # ==================================================
            api_positions = get_positions()

            if api_positions is None:
                logger.error(
                    "PositionSync: /positions = None → %.1fs後に再試行",
                    POSITION_SYNC_ERROR_WAIT_SECONDS,
                )
                time.sleep(POSITION_SYNC_ERROR_WAIT_SECONDS)
                continue

            # ==================================================
            # API → DB 同期
            # ==================================================
            try:
                sync_positions_from_kabus()
            except Exception as e:
                logger.error(
                    "[PositionSync] sync_positions_from_kabus Error: %s",
                    e,
                    exc_info=True,
                )

            # ==================================================
            # DB → global_state 反映（最重要）
            # ==================================================
            update_open_positions_from_db()

            time.sleep(interval)

    threading.Thread(
        target=loop,
        daemon=True,
        name="position-sync-loop",
    ).start()
    logger.info("PositionSync thread started interval=%.1fs", interval)
    return True
