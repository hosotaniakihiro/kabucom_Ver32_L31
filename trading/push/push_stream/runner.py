# ============================================================
# File   : trading/push/push_stream/runner.py
# Version: Ver1.8-PRODUCTION-PUSH-STREAM-CORE-VENDOR-ROTATION
# ------------------------------------------------------------
# 【概要】
#   kabu Station PUSH WebSocket runner
#
# 【2026-10-29】
#   ✔ 再接続の待ちを場中と場外で分ける (場中 0.3,0.5,1,2 秒・上限 2 秒)。
#     受信のあった接続・monitor の自己切断は「続いた回数」に数えない。場が開いたら回数を 0 に戻す。
#   ✔ 切断の理由 (10054 / close code / 自己切断) と接続中の受信件数を "reconnect after" の 1 行に出す。
#   ✔ PUSH_STREAM_RECONNECT_SESSION_AWARE=0 で従来の待ち方に戻る。
#
# 【REV1.8】
#   ✔ vendor sample compatible run_forever() を本体実装
#   ✔ ローテーションREST登録中はWS再接続を一時停止
#   ✔ PUSH reconnect stability patch の runner 差し替えを不要化
#
# 【REV1.7】
#   ✔ main.py / main_database.py 分離運用時、main.py側では
#     push_stream DB writer / order_book writer / flush worker を強制OFF
#   ✔ main.py側は WebSocket受信・latest price cache・5秒足・push_df 更新だけを担当
#   ✔ stream_data へのflushログ/DB保存は main_database.py 側だけに寄せる
# ============================================================

from __future__ import annotations

import datetime as dt
import importlib
import logging
import os
import sys
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

import websocket

from . import state
from .runtime import (
    _ensure_runtime_flags,
    _resolve_ws_url,
    _safe_get_runtime,
    _safe_iso,
    _safe_set_runtime,
)
from .transport import (
    _clear_sender,
    _is_ws_alive,
    refresh_subscriptions,
    set_refresh_callable,
)
from .dataframe import _init_ring_buffer
from .writers import _init_stream_writer, _init_order_book_writer, _flush_worker
from .monitor import _monitor_worker
from .rotation_core import _rotation_worker, enable_rotation
from .ws_callbacks import on_open, on_message, on_error, on_close
from .constants import RECONNECT_WAIT_SEC
from .ws_owner import (
    acquire_push_ws_owner,
    active_ws_instance_count,
    is_push_ws_owner,
    owned_elsewhere,
    ownership_snapshot,
    register_ws_instance,
    unregister_ws_instance,
)
from . import start_trace

logger = logging.getLogger(__name__)

VERSION = "Ver1.8-PRODUCTION-PUSH-STREAM-CORE-VENDOR-ROTATION"


def _env_bool(name: str, default: bool) -> bool:
    try:
        v = os.environ.get(name)
        if v is None:
            return bool(default)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}:
            return True
        if s in {"0", "false", "no", "n", "off", "disable", "disabled"}:
            return False
        return bool(default)
    except Exception:
        return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _should_disable_push_db_write_in_this_process() -> bool:
    """
    main.py側ではpush_streamのDB flush workerを起動しない。

    data_collectors.split_mode.should_skip_data_collector_work_in_main() が True の場合、
    このプロセスは main.py 側なので、PUSH DB保存は main_database.py 側だけに任せる。
    """
    try:
        from data_collectors.split_mode import should_skip_data_collector_work_in_main
        return bool(should_skip_data_collector_work_in_main())
    except Exception:
        return False


def _get_existing_refresh_callable() -> Any:
    try:
        return getattr(state, "_refresh_callable", None)
    except Exception:
        return None


def _is_refresh_callable_alive() -> bool:
    try:
        return callable(_get_existing_refresh_callable())
    except Exception:
        return False


def _is_self_refresh_callable(fn: Any) -> bool:
    try:
        return fn is refresh_subscriptions
    except Exception:
        return False


def _auto_resolve_subscription_refresh_callable() -> Optional[Callable[..., Any]]:
    candidates: Tuple[Tuple[str, str], ...] = (
        ("trading.push.subscription_manager.core", "refresh_subscriptions"),
        ("trading.push.subscription_manager.core", "refresh_subscription_symbols"),
        ("trading.push.subscription_manager.core", "refresh_register_symbols"),
        ("trading.push.subscription_manager", "refresh_subscriptions"),
        ("trading.push.subscription_manager", "refresh_subscription_symbols"),
        ("trading.push.subscription_manager", "refresh_register_symbols"),
        ("trading.push.subscription_manager.core", "register_symbols"),
        ("trading.push.subscription_manager", "register_symbols"),
    )

    for module_name, func_name in candidates:
        try:
            mod = importlib.import_module(module_name)
            fn = getattr(mod, func_name, None)
        except Exception:
            logger.debug(
                "[push_stream] auto resolve refresh callable import/getattr failed module=%s func=%s",
                module_name,
                func_name,
                exc_info=True,
            )
            continue

        if not callable(fn):
            continue

        if _is_self_refresh_callable(fn):
            logger.warning(
                "[push_stream] auto resolve refresh callable skipped self module=%s func=%s",
                module_name,
                func_name,
            )
            continue

        logger.info(
            "[push_stream] auto resolved refresh_callable module=%s func=%s",
            module_name,
            func_name,
        )
        return fn

    logger.warning(
        "[push_stream] auto resolve refresh_callable failed: subscription_manager refresh/register function not found"
    )
    return None


def _set_refresh_callable_preserve(refresh_callable: Optional[Any]) -> bool:
    existing = _get_existing_refresh_callable()

    if refresh_callable is None:
        if callable(existing):
            logger.info(
                "[push_stream] preserve refresh_callable existing=True fn=%s",
                getattr(existing, "__name__", type(existing).__name__),
            )
            return True

        auto_fn = _auto_resolve_subscription_refresh_callable()
        if callable(auto_fn):
            try:
                set_refresh_callable(auto_fn)
                installed = _get_existing_refresh_callable()
                ok = callable(installed)
                logger.info(
                    "[push_stream] refresh_callable auto-installed ok=%s fn=%s",
                    ok,
                    getattr(auto_fn, "__name__", type(auto_fn).__name__),
                )
                return ok
            except Exception:
                logger.exception("[push_stream] auto set_refresh_callable failed")
                return False

        logger.warning("[push_stream] refresh_callable missing and auto resolve failed")
        return False

    if _is_self_refresh_callable(refresh_callable):
        logger.warning(
            "[push_stream] start_push_stream received self refresh callable -> ignore and keep existing existing=%s",
            callable(existing),
        )
        return callable(existing)

    if not callable(refresh_callable):
        logger.warning(
            "[push_stream] start_push_stream refresh_callable rejected: not callable type=%s existing=%s",
            type(refresh_callable).__name__,
            callable(existing),
        )
        return callable(existing)

    try:
        set_refresh_callable(refresh_callable)
        installed = _get_existing_refresh_callable()
        ok = callable(installed)
        logger.info(
            "[push_stream] refresh_callable installed by runner ok=%s fn=%s",
            ok,
            getattr(refresh_callable, "__name__", type(refresh_callable).__name__),
        )
        return ok
    except Exception:
        logger.exception("[push_stream] set_refresh_callable failed; keep existing=%s", callable(existing))
        return callable(existing)


def _safe_ppid() -> int:
    try:
        return int(os.getppid())
    except Exception:
        return -1


def _resolve_ws_owner_role() -> str:
    """このプロセスの PUSH 所有者ロール名 (ログ・ロックメタ用)。"""
    explicit = os.environ.get("PUSH_WS_OWNER_ROLE")
    if explicit:
        return str(explicit).strip()
    try:
        argv0 = os.path.basename(str(sys.argv[0] or "")).lower()
    except Exception:
        argv0 = ""
    if "push_receiver" in argv0:
        return "push_receiver"
    if "main_database" in argv0:
        return "main_database"
    if argv0.startswith("main"):
        return "main"
    return argv0 or "unknown"


def _rotation_disabled_by_env() -> bool:
    """rotation を env で明示的に禁止しているか。

    main.py の memory-only fallback は PUSH_ROTATION_ENABLED=0 /
    PUSH_STREAM_ENABLE_ROTATION=0 を立ててから WS を張るが、
    呼び出し側が enable_rotate=True を渡していたため env が無視され、
    所有者でない main.py でも rotation worker が起動して
    unregister/all を 7 秒おきに撃っていた (2026-08-31 実測)。
    env の明示 OFF は引数より強くする。
    """
    for name in ("PUSH_ROTATION_ENABLED", "PUSH_STREAM_ENABLE_ROTATION"):
        raw = os.environ.get(name)
        if raw is None:
            continue
        if str(raw).strip().lower() in {"0", "false", "no", "n", "off", "disable", "disabled"}:
            logger.warning("[push_stream] rotation disabled by env %s=%s", name, raw)
            return True
    return False


def _set_rotation_preserve(enable_rotate: Optional[bool]) -> bool:
    if _rotation_disabled_by_env():
        try:
            enable_rotation(False)
        except Exception:
            logger.exception("[push_stream] enable_rotation(False) failed")
        return False

    if enable_rotate is None:
        current = bool(getattr(state, "_rotation_enabled", False))
        logger.info("[push_stream] preserve rotation enabled=%s", current)
        return current

    try:
        enable_rotation(bool(enable_rotate))
    except Exception:
        logger.exception("[push_stream] enable_rotation failed enable_rotate=%s", enable_rotate)

    return bool(getattr(state, "_rotation_enabled", False))


def _start_thread_if_needed(*, attr_name: str, target: Any, name: str) -> threading.Thread:
    th = getattr(state, attr_name, None)
    try:
        if th is not None and th.is_alive():
            logger.info("[push_stream] thread already alive name=%s", name)
            return th
    except Exception:
        pass

    th = threading.Thread(target=target, name=name, daemon=True)
    setattr(state, attr_name, th)
    th.start()
    logger.info("[push_stream] thread started name=%s", name)
    return th


def _sync_runtime_status_after_start() -> None:
    try:
        _safe_set_runtime("push_stream_running", True)
        _safe_set_runtime("push_writer_running", bool(state._flush_thread and state._flush_thread.is_alive()))
        _safe_set_runtime("subscription_refresh_running", _is_refresh_callable_alive())
        _safe_set_runtime("rotation_enabled", bool(getattr(state, "_rotation_enabled", False)))
    except Exception:
        logger.debug("[push_stream] sync runtime status failed", exc_info=True)


def _run_vendor_forever(ws_app: Any) -> None:
    """Official kabu Station sample uses ws.run_forever() without ping args."""
    vendor_mode = _env_bool("PUSH_WS_VENDOR_RUN_FOREVER", True)
    enable_ping = _env_bool("PUSH_WS_ENABLE_PING", False)
    if vendor_mode and not enable_ping:
        logger.warning("[push_stream] run_forever vendor mode: no ping_interval/no reconnect arg")
        ws_app.run_forever()
        return

    ping_interval = max(0.0, _env_float("PUSH_WS_PING_INTERVAL_SEC", 20.0))
    ping_timeout = max(0.0, _env_float("PUSH_WS_PING_TIMEOUT_SEC", 10.0))
    logger.warning("[push_stream] run_forever ping mode interval=%.1f timeout=%.1f", ping_interval, ping_timeout)
    try:
        ws_app.run_forever(ping_interval=ping_interval, ping_timeout=ping_timeout, reconnect=0)
    except TypeError:
        ws_app.run_forever(ping_interval=ping_interval, ping_timeout=ping_timeout)


def _wait_while_rotation_registering() -> None:
    if not _env_bool("PUSH_STREAM_PAUSE_RECONNECT_DURING_REGISTER", True):
        return
    while bool(getattr(state, "_rotation_register_in_progress", False)) and not state._stop_event.is_set():
        logger.info("[push_stream] reconnect paused: rotation register in progress")
        time.sleep(0.1)


# ============================================================
# 再接続バックオフの状態 (R17)
# ============================================================

_RECONNECT_CONSECUTIVE = 0
_RECONNECT_TOTAL = 0
# 2026-10-29: 場外 (寄り前・昼休み・引け後・休日) の切断は別の回数で数える。
#   場中の回数に混ぜると、昼休み・寄り前の monitor の自己切断で回数が積み上がり、
#   場が開いた時点で既に 15 秒待ちになっていた (10/27 場中の受信 0 の秒 56%、10/28 45%)。
_RECONNECT_OFF_CONSECUTIVE = 0
_RECONNECT_LAST_SESSION: Optional[str] = None


def _session_aware_enabled() -> bool:
    """PUSH_STREAM_RECONNECT_SESSION_AWARE=0 で 2026-10-28 以前の待ち方 (1 本の回数・1,2,4,8,15 秒) に戻る。"""
    return _env_bool("PUSH_STREAM_RECONNECT_SESSION_AWARE", True)


def reconnect_stats() -> dict:
    """WS 再接続の状況。storm 判定用。"""
    session = _RECONNECT_LAST_SESSION
    consecutive = _RECONNECT_CONSECUTIVE
    if _session_aware_enabled() and session == "off":
        consecutive = _RECONNECT_OFF_CONSECUTIVE
    return {
        "consecutive_fast_deaths": consecutive,
        "consecutive_intraday": _RECONNECT_CONSECUTIVE,
        "consecutive_off_session": _RECONNECT_OFF_CONSECUTIVE,
        "session": session,
        "total_reconnects": _RECONNECT_TOTAL,
        "base_sec": _env_float("PUSH_STREAM_RECONNECT_BACKOFF_BASE_SEC", RECONNECT_WAIT_SEC),
        "max_sec": _env_float("PUSH_STREAM_RECONNECT_BACKOFF_MAX_SEC", 30.0),
        "intraday_max_sec": _env_float("PUSH_STREAM_RECONNECT_INTRADAY_MAX_SEC", 2.0),
        "min_healthy_sec": _env_float("PUSH_STREAM_WS_MIN_HEALTHY_SEC", 60.0),
    }


def reset_reconnect_stats_for_test() -> None:
    global _RECONNECT_CONSECUTIVE, _RECONNECT_TOTAL, _RECONNECT_OFF_CONSECUTIVE, _RECONNECT_LAST_SESSION
    _RECONNECT_CONSECUTIVE = 0
    _RECONNECT_TOTAL = 0
    _RECONNECT_OFF_CONSECUTIVE = 0
    _RECONNECT_LAST_SESSION = None


def _parse_ladder(raw: Any, fallback: list) -> list:
    out = []
    for part in str(raw).split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(max(0.0, float(part)))
        except Exception:
            continue
    return out or list(fallback)


def _reconnect_backoff_ladder() -> list:
    """即死が続くときの待機秒ラダー (既定 1,2,4,8,15 秒で頭打ち)。

    従来は base * 2**(n-1) を max_wait で頭打ちにしていたが、
    max_wait の既定が 1.0 秒だったため 300 回連続の即死でも
    1 秒間隔で再接続を撃ち続け、kabu Station とローカル資源を
    無駄に叩いていた (2026-08-31 実測 consecutive=316 / next wait=1.0s)。

    2026-10-29: 場外 (と PUSH_STREAM_RECONNECT_SESSION_AWARE=0 のとき) だけこのラダーを使う。
    """
    return _parse_ladder(
        os.environ.get("PUSH_STREAM_RECONNECT_BACKOFF_LADDER_SEC", "1,2,4,8,15"),
        [1.0, 2.0, 4.0, 8.0, 15.0],
    )


def _intraday_backoff_ladder() -> list:
    """場中の待機秒ラダー (既定 0.3,0.5,1,2 秒。上限は PUSH_STREAM_RECONNECT_INTRADAY_MAX_SEC=2)。

    場中は kabu Station 側からの RST (WinError 10054) が数秒〜数十秒ごとに来る (2026-10-22〜、理由は未特定)。
    15 秒待つと RST 1 回ごとに 16〜20 秒の空白になる。上限を 2 秒にして空白を約 1 秒にする。
    8/28 のような「0.5 秒間隔で撃ち続ける」状態にはしないため、上限は 2 秒で止める (0 にはしない)。
    """
    return _parse_ladder(
        os.environ.get("PUSH_STREAM_RECONNECT_INTRADAY_LADDER_SEC", "0.3,0.5,1,2"),
        [0.3, 0.5, 1.0, 2.0],
    )


def _jst_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).replace(tzinfo=None)


_OPEN_TIMES = ((9, 0), (12, 30))


def _market_session(now: Optional[dt.datetime] = None) -> str:
    """'intraday' (前場 9:00〜11:30・後場 12:30〜15:30 の平日) か 'off'。祝日は見ない (受信が無いだけで害は無い)。"""
    now = now or _jst_now()
    if now.weekday() >= 5:
        return "off"
    hm = now.hour * 60 + now.minute
    if 9 * 60 <= hm < 11 * 60 + 30 or 12 * 60 + 30 <= hm < 15 * 60 + 30:
        return "intraday"
    return "off"


def _seconds_until_open(now: Optional[dt.datetime] = None) -> Optional[float]:
    """同じ日のうちに次に場が開く (9:00 / 12:30) までの秒数。無ければ None。"""
    now = now or _jst_now()
    if now.weekday() >= 5:
        return None
    for h, m in _OPEN_TIMES:
        t = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if t > now:
            return (t - now).total_seconds()
    return None


def _ladder_wait(consecutive: int, *, base: float, max_wait: float, ladder: list) -> float:
    if consecutive <= 1:
        # 単発の切断はこれまで通り即復帰する (遅くしない)。
        return float(base)
    idx = min(len(ladder) - 1, consecutive - 2)
    return float(min(max_wait, max(base, ladder[idx])))


def _note_disconnect_and_wait(
    uptime: float,
    *,
    base: float,
    max_wait: float,
    min_healthy: float,
    received: Optional[int] = None,
    self_close_reason: Optional[str] = None,
    now: Optional[dt.datetime] = None,
) -> Tuple[float, str]:
    """切断を記録し、次の再接続までの待機秒数を返す。

    uptime >= min_healthy なら「正常な切断」として連続失敗を 0 に戻すので、
    通常の切断からの復帰は base 秒のまま (= 遅くしない)。
    即死が続くときだけ指数的に待つ。

    2026-10-29 (PUSH_STREAM_RECONNECT_SESSION_AWARE=1、既定):
      - 場中と場外で回数を分ける。場が開いたら場中の回数を 0 に戻す。
      - 場中: その接続で 1 件でも受信していたら回数を 0 に戻す (PUSH_STREAM_RECONNECT_RESET_ON_RECEIVE)。
              monitor の自己切断 (self_close_reason あり) は回数に数えない。
              待ちは 0.3,0.5,1,2 秒・上限 2 秒 (PUSH_STREAM_RECONNECT_INTRADAY_*)。
      - 場外: 従来のラダー (1,2,4,8,15 秒) で数える。ただし次に場が開く時刻をまたいで待たない。
    """
    global _RECONNECT_CONSECUTIVE, _RECONNECT_TOTAL, _RECONNECT_OFF_CONSECUTIVE, _RECONNECT_LAST_SESSION

    _RECONNECT_TOTAL += 1

    if not _session_aware_enabled():
        if uptime >= min_healthy:
            _RECONNECT_CONSECUTIVE = 0
            verdict = "healthy_disconnect"
        else:
            _RECONNECT_CONSECUTIVE += 1
            verdict = "fast_death"
        wait_sec = _ladder_wait(
            _RECONNECT_CONSECUTIVE, base=base, max_wait=max_wait, ladder=_reconnect_backoff_ladder(),
        )
        return float(wait_sec), verdict

    session = _market_session(now)
    if session == "intraday" and _RECONNECT_LAST_SESSION != "intraday":
        if _RECONNECT_CONSECUTIVE:
            logger.warning(
                "[push_stream] market session opened -> reset intraday reconnect count %d -> 0",
                _RECONNECT_CONSECUTIVE,
            )
        _RECONNECT_CONSECUTIVE = 0
    _RECONNECT_LAST_SESSION = session

    if session == "intraday":
        if self_close_reason:
            verdict = "self_close"
        elif uptime >= min_healthy:
            _RECONNECT_CONSECUTIVE = 0
            verdict = "healthy_disconnect"
        elif (received or 0) > 0 and _env_bool("PUSH_STREAM_RECONNECT_RESET_ON_RECEIVE", True):
            _RECONNECT_CONSECUTIVE = 0
            verdict = "received_then_reset"
        else:
            _RECONNECT_CONSECUTIVE += 1
            verdict = "fast_death"
        intraday_max = max(base, _env_float("PUSH_STREAM_RECONNECT_INTRADAY_MAX_SEC", 2.0))
        wait_sec = _ladder_wait(
            _RECONNECT_CONSECUTIVE, base=base, max_wait=intraday_max, ladder=_intraday_backoff_ladder(),
        )
        return float(wait_sec), verdict

    # 場外
    if uptime >= min_healthy:
        _RECONNECT_OFF_CONSECUTIVE = 0
        verdict = "off_session_healthy_disconnect"
    else:
        _RECONNECT_OFF_CONSECUTIVE += 1
        verdict = "off_session_fast_death"
    wait_sec = _ladder_wait(
        _RECONNECT_OFF_CONSECUTIVE, base=base, max_wait=max_wait, ladder=_reconnect_backoff_ladder(),
    )
    until_open = _seconds_until_open(now)
    if until_open is not None and until_open < wait_sec:
        wait_sec = max(base, until_open)
        verdict += "_until_open"
    return float(wait_sec), verdict


def _take_disconnect_info() -> dict:
    """ws_callbacks / monitor が記録した直前の切断の理由を取り出して消す。"""
    try:
        info = dict(getattr(state, "_disconnect_info", None) or {})
    except Exception:
        info = {}
    try:
        state._disconnect_info = {}
    except Exception:
        pass
    return info


def _format_disconnect_cause(info: dict) -> str:
    parts = []
    cause = info.get("cause")
    if cause:
        parts.append(str(cause))
    if "close_code" in info or "close_msg" in info:
        parts.append("close_code=%s close_msg=%s" % (info.get("close_code"), info.get("close_msg")))
    err = info.get("error")
    if err:
        parts.append("error=%s" % str(err).replace("\n", " ")[:160])
    return " ".join(parts) or "unknown(run_forever returned without callback)"


def _run_forever_loop() -> None:
    _safe_set_runtime("push_stream_running", True)
    ws_url = _resolve_ws_url()
    logger.info(
        "[push_stream] run loop start url=%s version=%s vendor_ws=%s pause_during_register=%s",
        ws_url,
        VERSION,
        _env_bool("PUSH_WS_VENDOR_RUN_FOREVER", True),
        _env_bool("PUSH_STREAM_PAUSE_RECONNECT_DURING_REGISTER", True),
    )

    while not state._stop_event.is_set():
        # 反復ごとに必ず初期化する (前回の値が残ると uptime を過大評価する)
        _conn_started = time.monotonic()
        _wait_while_rotation_registering()
        # 2026-10-29: この接続で何件受けたか・なぜ切れたかを接続ごとに測る。
        _recv_at_start = int(getattr(state, "_total_received", 0) or 0)
        _take_disconnect_info()
        try:
            _safe_set_runtime("push_ws_url", ws_url)
            ws_app = websocket.WebSocketApp(
                ws_url,
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
            )

            with state._ws_state_lock:
                state._ws_app = ws_app

            start_trace.trace(6, ws_url=ws_url, ws_app=hex(id(ws_app)))

            logger.info(
                "[push_stream] websocket callbacks wired open=%s message=%s error=%s close=%s",
                callable(on_open),
                callable(on_message),
                callable(on_error),
                callable(on_close),
            )

            _conn_started = time.monotonic()
            instance_id = register_ws_instance()
            try:
                active = active_ws_instance_count()
                if active > 1:
                    logger.error(
                        "[push_stream][MULTI_WS_DETECTED] 同一プロセス内で websocket が %d 本走っている "
                        "pid=%s ws_instance_id=%s receiver_thread_id=%s (kabu Station の PUSH は 1 接続のみ)",
                        active, os.getpid(), instance_id, threading.get_ident(),
                    )
                _safe_set_runtime("push_ws_instance_id", instance_id)
                _safe_set_runtime("active_ws_instance_count", active)
                logger.info(
                    "[push_stream] ws instance start ws_instance_id=%s active_ws_instance_count=%s "
                    "receiver_thread_id=%s pid=%s",
                    instance_id, active, threading.get_ident(), os.getpid(),
                )
                # 接続世代が変わったら 09/12 をもう一度出せるようにする。
                start_trace.reset_once("conn:")
                start_trace.trace(8, ws_instance_id=instance_id, active_ws=active, ws_url=ws_url)
                _run_vendor_forever(ws_app)
            finally:
                unregister_ws_instance()
                _safe_set_runtime("active_ws_instance_count", active_ws_instance_count())

        except Exception:
            logger.exception("[push_stream] run_forever crashed")
        finally:
            try:
                with state._ws_state_lock:
                    state._ws_app = None
            except Exception:
                logger.debug("[push_stream] ws_app clear failed", exc_info=True)

        if state._stop_event.is_set():
            break

        # ------------------------------------------------------------
        # 再接続バックオフ (R17)
        #
        # 旧実装:
        #     base     = max(0.1, env(BACKOFF_BASE_SEC, 0.5))
        #     max_wait = max(base, env(BACKOFF_MAX_SEC, base))
        #     wait_sec = min(max_wait, base)      # <- 常に base
        # max_wait >= base なので min(max_wait, base) は必ず base になり、
        # 名前に反して **バックオフしていなかった**。
        # 失敗回数も数えていないため、接続が即死する状況では
        # 0.5 秒間隔で永久に再接続を撃ち続ける。
        # 2026-08-28 の実測では 84 分で 833 回 (9.9 回/分)、
        # すべて code=None msg=None だった。
        #
        # 新実装:
        #   - 直前の接続が min_healthy 秒もったら「正常な切断」とみなし
        #     連続失敗カウンタを 0 に戻す -> 従来どおり base 秒で再接続。
        #     (通常の切断からの復帰は遅くしない = timeout 延長ではない)
        #   - 即死が続く場合だけ指数バックオフし、NAS/API を叩き続けない。
        #   - 何秒もったか・連続何回目かをログに残し、次回ログで
        #     サーバ側の弾き (別セッションが購読を奪った等) を判定できるようにする。
        # ------------------------------------------------------------
        base = max(0.1, _env_float("PUSH_STREAM_RECONNECT_BACKOFF_BASE_SEC", RECONNECT_WAIT_SEC))
        max_wait = max(base, _env_float("PUSH_STREAM_RECONNECT_BACKOFF_MAX_SEC", 15.0))
        min_healthy = max(0.0, _env_float("PUSH_STREAM_WS_MIN_HEALTHY_SEC", 60.0))

        uptime = -1.0
        try:
            uptime = time.monotonic() - _conn_started
        except Exception:
            uptime = -1.0

        try:
            recv_in_conn = max(0, int(getattr(state, "_total_received", 0) or 0) - _recv_at_start)
        except Exception:
            recv_in_conn = 0
        dinfo = _take_disconnect_info()
        cause_text = _format_disconnect_cause(dinfo)
        self_close_reason = dinfo.get("self_close_reason")

        wait_sec, verdict = _note_disconnect_and_wait(
            uptime, base=base, max_wait=max_wait, min_healthy=min_healthy,
            received=recv_in_conn, self_close_reason=self_close_reason,
        )
        stats = reconnect_stats()

        # 2026-10-29: 切断の理由を 1 行で残す (10054 / close code / 自己切断の理由 / 受信件数 / 次の待ち)。
        logger.warning(
            "[push_stream] reconnect after %.1fs uptime=%.1fs verdict=%s "
            "consecutive=%d total=%d base=%.1fs max=%.1fs min_healthy=%.0fs vendor_ws=%s "
            "cause=%s recv_in_conn=%d session=%s",
            wait_sec, uptime, verdict,
            stats.get("consecutive_fast_deaths", _RECONNECT_CONSECUTIVE), _RECONNECT_TOTAL,
            base, max_wait, min_healthy,
            _env_bool("PUSH_WS_VENDOR_RUN_FOREVER", True),
            cause_text, recv_in_conn, stats.get("session"),
        )
        if stats.get("consecutive_fast_deaths", _RECONNECT_CONSECUTIVE) >= 5 and not self_close_reason:
            logger.error(
                "[push_stream][WS RECONNECT STORM] 接続が %d 回連続で %.0f 秒未満で切れている。"
                "サーバ側で弾かれている可能性が高い (別セッションが PUSH 購読を奪った / "
                "認証失効 / 多重 websocket)。次の待機は %.1fs",
                stats.get("consecutive_fast_deaths", _RECONNECT_CONSECUTIVE), min_healthy, wait_sec,
            )

        if state._stop_event.wait(wait_sec):
            break

    _safe_set_runtime("push_stream_running", False)
    logger.info("[push_stream] run loop stopped")


def start_push_stream(
    ws_url: Optional[str] = None,
    stream_writer: Any = None,
    order_book_writer: Any = None,
    refresh_callable: Optional[Any] = None,
    enable_rotate: Optional[bool] = None,
) -> Dict[str, Any]:
    """PUSH WebSocket をバックグラウンド起動する。

    戻り値 (2026-09-03 追加):
        {"started": bool, "reason": str, ...}

    旧実装は起動できなくても None を返して終わっていたため、
    呼び出し側 (data_collectors.push_runtime) が
    「例外が出なかった = 起動成功」と誤認し、
    WebSocket が 1 本も無いまま heartbeat だけ出し続けていた
    (2026-09-03 実測 pid=41016 / active_ws=0 / uptime 1186s)。
    起動しなかった理由を必ず返す。
    """
    with state._runtime_lock:
        if state._ws_thread is not None and state._ws_thread.is_alive():
            logger.info(
                "[push_stream] already running refresh_callable=%s rotation_enabled=%s status=%s",
                _is_refresh_callable_alive(),
                bool(getattr(state, "_rotation_enabled", False)),
                get_status(),
            )
            return {"started": True, "reason": "already_running"}

        # ------------------------------------------------------------
        # role による所有権の指定 (ws_owner のファイルロックより前に効かせる)
        #
        # ws_owner.acquire_push_ws_owner() は「先に取った者勝ち」なので、
        # main.py が先に起動すると main.py が PUSH を所有してしまい、
        # PUSH DB writer を持つ push_receiver が締め出される。
        # 所有してよい role は push_receiver だけ、と先に決める。
        # silent skip にせず必ず WARNING を出す。
        # ------------------------------------------------------------
        from .ownership import is_push_owner, log_owner_banner, warn_non_owner_call

        log_owner_banner()
        role_owner, _role, _reason = is_push_owner()
        if not role_owner:
            warn_non_owner_call("start_push_stream")
            start_trace.abort(
                2, "role_not_push_receiver:%s" % _reason, resolved_role=_role,
            )
            try:
                enable_rotation(False)
            except Exception:
                logger.debug("[push_stream] disable rotation for non-owner failed", exc_info=True)
            _safe_set_runtime("push_ws_owner", False)
            _safe_set_runtime("push_stream_running", False)
            return {"started": False, "reason": "role_not_push_receiver:%s" % _reason, "role": _role}

        owner_role = _resolve_ws_owner_role()
        owner_ok, holder = acquire_push_ws_owner(owner_role)
        if not owner_ok:
            start_trace.abort(
                2,
                "ws_owner_lock_held_by_other_process",
                holder_pid=holder.get("pid"),
                holder_argv=holder.get("argv"),
                holder_acquired_at=holder.get("acquired_at"),
                holder_pid_alive=holder.get("pid_alive"),
            )
            logger.error(
                "[push_stream][MULTI_WS_DETECTED] PUSH WebSocket の所有権を取得できなかったので "
                "websocket も rotation も起動しない。"
                "process_id=%s parent_pid=%s ws_owner_role=%s holder=%s "
                "(kabu Station の PUSH は 1 セッション 1 接続。2 本張ると互いを切り合う)",
                os.getpid(), _safe_ppid(), owner_role, holder,
            )
            try:
                enable_rotation(False)
            except Exception:
                logger.debug("[push_stream] enable_rotation(False) failed", exc_info=True)
            _safe_set_runtime("push_stream_running", False)
            _safe_set_runtime("push_stream_ws_skipped_multi_owner", True)
            _safe_set_runtime("push_ws_owner", False)
            return {
                "started": False,
                "reason": "ws_owner_lock_held_by_other_process",
                "holder": holder,
            }

        _safe_set_runtime("push_ws_owner", True)
        _safe_set_runtime("push_ws_owner_role", owner_role)
        start_trace.trace(2, owner_role=owner_role, lock_acquired=True)

        if ws_url:
            _safe_set_runtime("push_ws_url", ws_url)

        state._stop_event.clear()
        _ensure_runtime_flags()
        state._ring_buffer = _init_ring_buffer()

        force_memory_only = _should_disable_push_db_write_in_this_process()

        if force_memory_only:
            db_write_enabled = False
            order_book_write_enabled = False
            stream_writer = False
            order_book_writer = False
            _safe_set_runtime("push_stream_memory_only", True)
            _safe_set_runtime("push_stream_db_write_enabled", False)
            _safe_set_runtime("push_stream_order_book_write_enabled", False)
            logger.warning(
                "[push_stream] MAIN MEMORY-ONLY mode: DB writer/order_book writer/flush worker disabled; main_database.py handles PUSH DB storage"
            )
        else:
            db_write_enabled = _env_bool("PUSH_STREAM_DB_WRITE", True) and stream_writer is not False
            order_book_write_enabled = _env_bool("PUSH_STREAM_ORDER_BOOK_WRITE", True) and order_book_writer is not False
            _safe_set_runtime("push_stream_memory_only", False)

        if db_write_enabled:
            state._stream_writer = stream_writer if stream_writer is not None else _init_stream_writer()
            start_trace.trace(
                4,
                writer_ready=state._stream_writer is not None,
                writer_type=type(state._stream_writer).__name__,
                injected=stream_writer is not None,
            )
            if state._stream_writer is None:
                start_trace.abort(4, "stream_writer_init_returned_none")
        else:
            state._stream_writer = None
            _safe_set_runtime("push_stream_db_write_enabled", False)
            start_trace.abort(
                4,
                "db_write_disabled",
                force_memory_only=force_memory_only,
                stream_writer_arg=repr(stream_writer),
            )
            logger.warning("[push_stream] DB write disabled; WebSocket updates memory df/latest cache only")

        if order_book_write_enabled:
            state._order_book_writer = order_book_writer if order_book_writer is not None else _init_order_book_writer()
        else:
            state._order_book_writer = None
            _safe_set_runtime("push_stream_order_book_write_enabled", False)
            logger.warning("[push_stream] order book DB write disabled")

        # 重要:
        # refresh_callable は rotation worker とは別物。
        # enable_rotate=False でも on_open 後の一括登録には refresh_callable が必要。
        refresh_alive = _set_refresh_callable_preserve(refresh_callable)
        rotation_enabled = _set_rotation_preserve(enable_rotate)

        logger.info(
            "[push_stream] start config refresh_callable=%s rotation_enabled=%s enable_rotate_arg=%s db_write=%s order_book_write=%s memory_only=%s version=%s",
            refresh_alive,
            rotation_enabled,
            enable_rotate,
            db_write_enabled,
            order_book_write_enabled,
            force_memory_only,
            VERSION,
        )

        if db_write_enabled:
            _flush_th = _start_thread_if_needed(
                attr_name="_flush_thread", target=_flush_worker, name="push-flush-worker"
            )
            start_trace.trace(
                5,
                flush_thread_alive=bool(_flush_th is not None and _flush_th.is_alive()),
                flush_thread_id=getattr(_flush_th, "ident", None),
            )
        else:
            state._flush_thread = None
            _safe_set_runtime("push_writer_running", False)
            start_trace.abort(5, "db_write_disabled_no_flush_worker")
            logger.warning("[push_stream] flush worker not started because db_write_enabled=False")

        _start_thread_if_needed(attr_name="_monitor_thread", target=_monitor_worker, name="push-monitor-worker")

        # 2026-09-02 P0: global_data.push_df 同期を WS 読み取りスレッドから外す。
        # memory_only (main.py) でも必要なので db_write_enabled とは無関係に起動する。
        try:
            from .runtime import _global_sync_worker

            _start_thread_if_needed(
                attr_name="_gsync_thread", target=_global_sync_worker, name="push-global-sync"
            )
        except Exception:
            logger.exception("[push_stream] global sync worker start failed")

        # PUSH -> WS -> queue -> writer -> DB -> feeder -> Summary を 1 行で出す。
        try:
            from trading.push.push_pipeline_probe import start_pipeline_probe

            start_pipeline_probe()
        except Exception:
            logger.exception("[push_stream] pipeline probe start failed")

        if rotation_enabled and not is_push_ws_owner():
            elsewhere, holder = owned_elsewhere()
            logger.error(
                "[push_stream][MULTI_WS_DETECTED] rotation worker を起動しない: "
                "このプロセスは PUSH WebSocket の所有者ではない pid=%s owned_elsewhere=%s holder=%s "
                "(register/unregister は所有プロセスだけが行う)",
                os.getpid(), elsewhere, holder,
            )
            try:
                enable_rotation(False)
            except Exception:
                logger.debug("[push_stream] enable_rotation(False) failed", exc_info=True)
            rotation_enabled = False

        if rotation_enabled:
            _start_thread_if_needed(attr_name="_rotate_thread", target=_rotation_worker, name="push-rotation-worker")
        else:
            state._rotate_thread = None
            logger.warning("[push_stream] rotation worker not started because rotation_enabled=False")

        ws_th = _start_thread_if_needed(
            attr_name="_ws_thread", target=_run_forever_loop, name="push-ws-thread"
        )
        start_trace.trace(
            7,
            ws_thread_id=getattr(ws_th, "ident", None),
            ws_thread_alive=bool(ws_th is not None and ws_th.is_alive()),
            rotation_enabled=rotation_enabled,
            db_write=db_write_enabled,
        )
        _sync_runtime_status_after_start()

        logger.info(
            "[push_stream] started version=%s refresh_callable=%s rotation_enabled=%s db_write=%s memory_only=%s",
            VERSION,
            _is_refresh_callable_alive(),
            bool(getattr(state, "_rotation_enabled", False)),
            db_write_enabled,
            force_memory_only,
        )

        ws_thread = getattr(state, "_ws_thread", None)
        snapshot = ownership_snapshot(owner_role)
        logger.warning(
            "[push_stream][WS OWNERSHIP] process_id=%s parent_pid=%s ws_owner_role=%s is_ws_owner=%s "
            "connection_generation=%s ws_instance_id=%s receiver_thread_id=%s receiver_thread_alive=%s "
            "active_ws_instance_count=%s lock_path=%s rotation_enabled=%s",
            snapshot.get("process_id"),
            snapshot.get("parent_pid"),
            snapshot.get("ws_owner_role"),
            snapshot.get("is_ws_owner"),
            snapshot.get("connection_generation"),
            _safe_get_runtime("push_ws_instance_id", None),
            getattr(ws_thread, "ident", None),
            bool(ws_thread is not None and ws_thread.is_alive()),
            snapshot.get("active_ws_instance_count"),
            snapshot.get("lock_path"),
            bool(getattr(state, "_rotation_enabled", False)),
        )

        return {
            "started": bool(ws_thread is not None and ws_thread.is_alive()),
            "reason": "ok",
            "ws_thread_alive": bool(ws_thread is not None and ws_thread.is_alive()),
            "flush_thread_alive": bool(state._flush_thread is not None and state._flush_thread.is_alive()),
            "writer_ready": state._stream_writer is not None,
            "rotation_enabled": bool(getattr(state, "_rotation_enabled", False)),
            "db_write": bool(db_write_enabled),
            "memory_only": bool(force_memory_only),
        }


def stop_push_stream(wait: float = 5.0) -> None:
    with state._runtime_lock:
        logger.info("[push_stream] stopping...")
        state._stop_event.set()

        try:
            if state._ws_app is not None:
                state._ws_app.close()
        except Exception:
            logger.exception("[push_stream] ws close failed")

        started = time.time()
        for th in [state._ws_thread, state._flush_thread, state._monitor_thread, state._rotate_thread, getattr(state, '_gsync_thread', None)]:
            try:
                if th is not None and th.is_alive():
                    remain = max(0.1, wait - (time.time() - started))
                    th.join(timeout=remain)
            except Exception:
                logger.exception("[push_stream] thread join failed")

        state._connected_event.clear()
        _clear_sender()
        _safe_set_runtime("ws_connected", False)
        _safe_set_runtime("push_stream_running", False)
        _safe_set_runtime("push_writer_running", False)
        _safe_set_runtime("subscription_refresh_running", False)
        logger.info("[push_stream] stopped")


def get_status() -> Dict[str, Any]:
    return {
        "ws_connected": state._connected_event.is_set(),
        "ws_alive": _is_ws_alive(),
        "push_stream_running": bool(state._ws_thread and state._ws_thread.is_alive()),
        "push_writer_running": bool(state._flush_thread and state._flush_thread.is_alive()),
        "subscription_refresh_running": bool(_safe_get_runtime("subscription_refresh_running", False)),
        "refresh_callable": _is_refresh_callable_alive(),
        "rotation_enabled": bool(getattr(state, "_rotation_enabled", False)),
        "queue_size": state._push_queue.qsize(),
        "df_rows": 0 if state._push_df is None else len(state._push_df),
        "last_push_received_at": _safe_iso(state._last_message_at),
        "last_push_db_flush_at": _safe_iso(state._last_flush_at),
        "last_error_at": _safe_iso(state._last_error_at),
        "last_connect_at": _safe_iso(state._last_connect_at),
        "last_disconnect_at": _safe_iso(state._last_disconnect_at),
        "total_received": state._total_received,
        "total_flushed": state._total_flushed,
        "total_dropped": state._total_dropped,
        "total_errors": state._total_errors,
        "ws_url": _resolve_ws_url(),
        "version": VERSION,
    }


_LAST_RECONNECT_REQUEST_AT = 0.0
_RECONNECT_REQUEST_LOCK = threading.Lock()


def request_reconnect(*, reason: str = "", min_interval_sec: float = 60.0) -> bool:
    """現在の WebSocket を 1 回だけ閉じて、run loop に張り直させる。

    新しい接続をここで作らない。閉じるだけで _run_forever_loop が
    バックオフ付きで再接続する (二重 WS を作らないための唯一の作法)。

    連打防止:
      min_interval_sec 以内の再要求は False を返して何もしない。
      PUSH 停滞のたびに close を撃つと、kabu Station 側から見て
      短時間に何度も切断・再接続する形になり、状況を悪くする。
    """
    global _LAST_RECONNECT_REQUEST_AT

    if state._ws_thread is None or not getattr(state._ws_thread, "is_alive", lambda: False)():
        logger.warning("[push_stream] reconnect 要求を無視: push_stream の run loop が動いていない reason=%s", reason)
        return False

    now = time.monotonic()
    with _RECONNECT_REQUEST_LOCK:
        if (now - _LAST_RECONNECT_REQUEST_AT) < float(min_interval_sec):
            logger.warning(
                "[push_stream] reconnect 要求を無視: %ss 以内に要求済み reason=%s (連打しない)",
                min_interval_sec, reason,
            )
            return False
        _LAST_RECONNECT_REQUEST_AT = now

    with state._ws_state_lock:
        ws_app = state._ws_app

    if ws_app is None:
        logger.warning("[push_stream] reconnect 要求: ws_app が無い (再接続待ち中) reason=%s", reason)
        return False

    logger.error(
        "[push_stream] reconnect 要求を受け付けた reason=%s -> 現接続を閉じて run loop に張り直させる "
        "(新規接続はここで作らない。二重 WS を避けるため)",
        reason,
    )
    try:
        ws_app.close()
        return True
    except Exception:
        logger.exception("[push_stream] reconnect 要求の close に失敗 reason=%s", reason)
        return False


def reset_reconnect_request_for_test() -> None:
    global _LAST_RECONNECT_REQUEST_AT
    _LAST_RECONNECT_REQUEST_AT = 0.0


def start(*args: Any, **kwargs: Any) -> None:
    return start_push_stream(*args, **kwargs)


def run_background(*args: Any, **kwargs: Any) -> None:
    return start_push_stream(*args, **kwargs)


__all__ = ["start_push_stream", "stop_push_stream", "get_status", "start", "run_background",
           "request_reconnect", "reset_reconnect_request_for_test"]
