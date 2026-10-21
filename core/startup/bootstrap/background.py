# ============================================================
# File   : core/startup/background_bootstrap.py
# Version: PRODUCTION-STABLE-REV5-SUMMARY-AI-ASYNC-ENTRY-PATCH
# ------------------------------------------------------------
# 【概要】
#   background 系常駐処理の起動
#
# 【主な機能】
#   ✔ flush loop 起動
#   ✔ reconnect monitor 起動
#   ✔ StreamDBWriter 起動
#   ✔ 新 push_stream.runner.start_push_stream 起動
#   ✔ ATS 登録ループ起動
#   ✔ position sync 起動
#   ✔ pending monitor 起動
#   ✔ StreamOrchestrator 起動
#   ✔ SUMMARY AI 実発注の非同期化 patch 起動
#   ✔ 二重起動防止
#   ✔ 永久安定ループ設計
#   ✔ 例外完全吸収
#   ✔ 機能削除ゼロ
#
# 【REV5 修正】
#   ✔ core.startup.summary_ai_async_entry_patch を background 起動時に install
#   ✔ 1分サマリー job の90秒 timeout が発注処理を巻き込む問題を緩和
#   ✔ AI_OK/approved 後の実発注は worker thread 側ログで追跡
# ============================================================

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from typing import Any

from websocket_handlers.dataframe_manager import flush_push_buffer
from websocket_handlers.websocket_reconnect import start_reconnect_monitor

from ats import ats_register_loop
from core.position_sync import start_position_sync_loop
from trading.handlers.pending_monitor import start_pending_monitor
from core.runtime.stream_orchestrator import StreamOrchestrator

from global_state import global_data

logger = logging.getLogger(__name__)


# ------------------------------------------------------------
# internal flags
# ------------------------------------------------------------

_FLUSH_STARTED = False
_STREAM_STARTED = False
_BACKGROUND_STARTED = False
_PENDING_MONITOR_STARTED = False
_STREAMDB_STARTED = False
_PUSH_STREAM_STARTED = False
_SUMMARY_AI_ASYNC_PATCH_STARTED = False

_STREAMDB_INSTANCE: Any | None = None


# ============================================================
# helpers
# ============================================================

def _get_stream_writer_singleton() -> Any | None:
    try:
        from trading.push import push_db_writer as mod

        writer = getattr(mod, "stream_writer", None)
        if writer is not None:
            return writer

        cls = getattr(mod, "StreamDBWriter", None)
        if callable(cls):
            return cls()

    except Exception:
        logger.exception("[background] resolve stream_writer singleton failed")

    return None


def _is_writer_alive(writer: Any) -> bool:
    try:
        th = getattr(writer, "_thread", None)
        return bool(getattr(writer, "_started", False) and th is not None and th.is_alive())
    except Exception:
        return False


def _resolve_ws_url() -> str | None:
    try:
        for attr in ("push_ws_url", "ws_url"):
            v = getattr(global_data, attr, None)
            if v:
                return str(v).strip()
    except Exception:
        pass
    return None


def _resolve_refresh_callable() -> Any | None:
    """
    kabu Station 登録更新 callable を取得する。
    push_bootstrap 側で global_data.push_refresh_callable に入っている想定。
    """
    try:
        fn = getattr(global_data, "push_refresh_callable", None)
        if callable(fn):
            return fn
    except Exception:
        logger.debug("[background] push_refresh_callable resolve failed", exc_info=True)

    return None


# ============================================================
# SUMMARY AI async entry patch
# ============================================================

def _install_summary_ai_async_entry_patch() -> None:
    global _SUMMARY_AI_ASYNC_PATCH_STARTED

    if _SUMMARY_AI_ASYNC_PATCH_STARTED:
        logger.info("[background] summary_ai_async_entry_patch already installed")
        return

    try:
        # summary_ai_async_entry_patch は
        # trading/entry/summary_ai/direct_snapshot.py へ本文化済み
        # (summary_entry.execute_entry_pipeline が委譲する)。
        # ここでは env の調整だけを走らせる。
        from trading.entry.summary_ai import direct_snapshot as p
        ok = bool(p.apply_async_entry_defaults())
        _SUMMARY_AI_ASYNC_PATCH_STARTED = ok
        logger.warning("[background] summary_ai async entry defaults applied=%s", ok)
    except Exception:
        logger.exception("[background] summary_ai async entry defaults failed")


# ============================================================
# flush loop
# ============================================================

def _start_flush_loop(interval_sec: float = 0.2):
    global _FLUSH_STARTED

    if _FLUSH_STARTED:
        logger.info("flush loop already started")
        return

    def _loop():
        logger.info("🔄 flush loop started (%.2fs)", interval_sec)
        while True:
            try:
                flush_push_buffer()
            except Exception:
                logger.error("❌ flush loop error", exc_info=True)
            time.sleep(interval_sec)

    t = threading.Thread(target=_loop, daemon=True, name="legacy-flush-loop")
    t.start()
    _FLUSH_STARTED = True


# ============================================================
# StreamOrchestrator
# ============================================================

def _start_stream():
    global _STREAM_STARTED

    if _STREAM_STARTED:
        logger.info("StreamOrchestrator already started")
        return

    try:
        orchestrator = StreamOrchestrator(sleep_sec=0.2)
        t = threading.Thread(
            target=orchestrator.start,
            daemon=True,
            name="stream-orchestrator",
        )
        t.start()
        _STREAM_STARTED = True
        logger.info("🔥 StreamOrchestrator started")
    except Exception:
        logger.exception("❌ StreamOrchestrator failed")


# ============================================================
# StreamDBWriter
# ============================================================

def _start_stream_db():
    """
    PUSH DB writer を起動する。

    startup.py 側の push_storage_bootstrap ですでに起動済みの場合は、
    singleton を検出して二重起動しない。
    """
    global _STREAMDB_STARTED
    global _STREAMDB_INSTANCE

    if _STREAMDB_STARTED and _STREAMDB_INSTANCE is not None and _is_writer_alive(_STREAMDB_INSTANCE):
        logger.info("StreamDBWriter already started")
        return

    try:
        logger.info("🚀 Starting StreamDBWriter (FULL PUSH STORAGE)")

        writer = _get_stream_writer_singleton()
        if writer is None:
            logger.error("❌ StreamDBWriter resolve failed")
            return

        if not _is_writer_alive(writer):
            start = getattr(writer, "start", None)
            if callable(start):
                start()
            else:
                logger.error("❌ StreamDBWriter.start missing")
                return

        _STREAMDB_INSTANCE = writer
        _STREAMDB_STARTED = True

        try:
            global_data.push_writer_running = True
            global_data.push_storage_running = True
        except Exception:
            pass

        logger.info("✅ StreamDBWriter started alive=%s", _is_writer_alive(writer))

    except Exception:
        logger.exception("❌ StreamDBWriter failed")


# ============================================================
# push_stream runner
# ============================================================

def _is_push_owner_process() -> tuple[bool, str]:
    """このプロセスが PUSH WebSocket の所有者か。

    kabu Station の PUSH は 1 セッションしか張れない。2 本目が繋がると
    サーバ側が古い方を切るため、両者が数秒おきに互いを蹴り合う。

    2026-08-31 の実測:
      10:42 push_receiver_runner(PID 52480) 起動 -> WS は 230 秒維持
      10:46 main.py(PID 51468) 起動 = 2 本目の WS を張る
      10:47 以降 632 回連続で uptime 2〜4 秒の切断が継続 (40 分以上回復せず)
      netstat: 51468 と 52480 が同時に :18080 へ Established
               (main.py 側は一時 5 本同時、TimeWait 約 85 本)
      main.py 側は total_received=0 のまま (配信は勝った方にしか来ない)

    設計上も main.py は PUSH を張らない側で、
    scheduler_jobs/summary/memory_latest_1m._writeback_push_df() の docstring に
    「main.py は自前の PUSH WebSocket を張らないので push_df を書く担当が居ない」
    と明記されている。bootstrap がその設計に反して無条件起動していた。

    所有してよいのは role=push_receiver だけ。判定は
    trading.push.push_stream.ownership に一本化する
    (runner.start_push_stream と必ず同じ結論になること)。
    PUSH_STREAM_FORCE_OWNER=1 で従来動作に戻せる。
    """
    try:
        from trading.push.push_stream.ownership import is_push_owner

        owner, role, reason = is_push_owner()
        return bool(owner), "role=%s %s" % (role, reason)
    except Exception:
        logger.exception("[PUSH WS OWNER] owner 判定に失敗したので安全側 (非所有) に倒す")
        return False, "owner_check_failed"


def _start_push_stream_runner():
    """
    新 push_stream package の WebSocket runner を起動する。

    これが起動しないと:
      - WebSocket opened が出ない
      - on_message が発火しない
      - queue に入らない
      - stream_data に flush されない
    """
    global _PUSH_STREAM_STARTED

    if _PUSH_STREAM_STARTED:
        logger.info("push_stream runner already started")
        return

    is_owner, owner_reason = _is_push_owner_process()
    if not is_owner:
        logger.warning(
            "[PUSH WS OWNER] pid=%s owner=False reason=%s argv=%s "
            "-> WebSocket と subscription rotation を起動しない。"
            "PUSH は data collector 側プロセスが所有し、このプロセスは PUSH DB 経由で読む。"
            "(kabu Station の PUSH は 1 セッションのみ。2 本張ると互いを切り合う)",
            os.getpid(), owner_reason, " ".join(sys.argv[:3]),
        )
        return
    logger.warning(
        "[PUSH WS OWNER] pid=%s owner=True reason=%s argv=%s -> このプロセスが PUSH を所有する",
        os.getpid(), owner_reason, " ".join(sys.argv[:3]),
    )

    try:
        from trading.push.push_stream.runner import start_push_stream, get_status

        writer = _get_stream_writer_singleton()
        refresh_callable = _resolve_refresh_callable()
        ws_url = _resolve_ws_url()

        # rotation は PUSH WebSocket 所有者だけが行う。
        # 所有権が取れない (= 他プロセスが所有している) 状態で
        # enable_rotate=True を渡すと unregister/all を撃ってしまうため、
        # ここで所有状況を見てから決める。
        enable_rotate = True
        try:
            from trading.push.push_stream.ws_owner import owned_elsewhere

            elsewhere, holder = owned_elsewhere()
            if elsewhere:
                logger.error(
                    "[PUSH WS OWNER][MULTI_WS_DETECTED] pid=%s: PUSH WebSocket は別プロセスが所有している holder=%s "
                    "-> rotation を要求しない",
                    os.getpid(), holder,
                )
                enable_rotate = False
        except Exception:
            logger.debug("[PUSH WS OWNER] ownership probe failed", exc_info=True)

        start_push_stream(
            ws_url=ws_url,
            stream_writer=writer,
            order_book_writer=None,
            refresh_callable=refresh_callable,
            enable_rotate=enable_rotate,
        )

        _PUSH_STREAM_STARTED = True

        try:
            status = get_status()
        except Exception:
            status = {}

        logger.info(
            "✅ push_stream runner started ws_url=%s refresh_callable=%s status=%s",
            ws_url,
            callable(refresh_callable),
            status,
        )

    except Exception:
        logger.exception("❌ push_stream runner start failed")


# ============================================================
# background bootstrap
# ============================================================

def bootstrap_background():
    """
    background 常駐系を起動する。

    起動順:
      1. SUMMARY AI async entry patch
      2. legacy flush loop
      3. reconnect monitor
      4. StreamDBWriter
      5. push_stream runner
      6. ATS loop
      7. position sync
      8. pending monitor
      9. StreamOrchestrator
    """

    global _BACKGROUND_STARTED
    global _PENDING_MONITOR_STARTED

    if _BACKGROUND_STARTED:
        logger.info("background already started")
        return

    logger.info("🚀 background bootstrap start")

    # --------------------------------------------------------
    # SUMMARY AI async entry patch
    # --------------------------------------------------------
    _install_summary_ai_async_entry_patch()

    # --------------------------------------------------------
    # legacy flush loop
    # --------------------------------------------------------
    _start_flush_loop(interval_sec=0.2)

    # --------------------------------------------------------
    # WebSocket reconnect monitor
    # --------------------------------------------------------
    try:
        start_reconnect_monitor(initial_connect=True)
    except Exception:
        logger.exception("❌ reconnect monitor failed")

    # --------------------------------------------------------
    # StreamDBWriter
    # --------------------------------------------------------
    _start_stream_db()

    # --------------------------------------------------------
    # push_stream runner
    # --------------------------------------------------------
    _start_push_stream_runner()

    # --------------------------------------------------------
    # ATS register loop
    # --------------------------------------------------------
    try:
        threading.Thread(
            target=ats_register_loop,
            args=(global_data.token_value, 10, 50, 5, 20),
            daemon=True,
            name="ats-register-loop",
        ).start()
    except Exception:
        logger.exception("❌ ATS loop start failed")

    # --------------------------------------------------------
    # position sync
    # --------------------------------------------------------
    try:
        start_position_sync_loop()
    except Exception:
        logger.exception("❌ position sync failed")

    # --------------------------------------------------------
    # pending monitor
    # --------------------------------------------------------
    if not _PENDING_MONITOR_STARTED:
        try:
            start_pending_monitor()
            _PENDING_MONITOR_STARTED = True
        except Exception:
            logger.exception("❌ pending monitor failed")

    # --------------------------------------------------------
    # StreamOrchestrator
    # --------------------------------------------------------
    _start_stream()

    _BACKGROUND_STARTED = True

    logger.info("🚀 background bootstrap complete")


__all__ = [
    "bootstrap_background",
]