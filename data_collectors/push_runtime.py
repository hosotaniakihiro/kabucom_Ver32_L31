# ============================================================
# File   : data_collectors/push_runtime.py
# Version: DATA-COLLECTORS-PUSH-RUNTIME-V3-SEED-ACTIVE-SYMBOLS
# ------------------------------------------------------------
# Purpose:
#   - PUSH受信本体を main.py から独立して起動する
#   - PUSH A/B 50銘柄ローテーションを明示的に有効化する
#   - rotation worker から subscription_manager.refresh_subscriptions を呼び、
#     株ステーションへの登録を実行する
#   - main.py から切り離したため、main_database.py 側でPUSH登録対象を生成・注入する
# ============================================================

from __future__ import annotations

import logging
import os
import schedule
import time
from typing import Any, Iterable, List

from data_collectors.config import (
    PUSH_BATCH_SIZE,
    PUSH_REGISTER_SEC,
    PUSH_SWITCH_GAP_SEC,
    PUSH_TARGET_TOTAL,
)
from data_collectors.heartbeat import write_heartbeat
from data_collectors.import_resolver import resolve_callable

logger = logging.getLogger(__name__)


SUBSCRIPTION_REFRESH_CANDIDATES = [
    ("trading.push.subscription_manager", "refresh_subscriptions"),
    ("trading.push.subscription_manager.core", "refresh_subscriptions"),
    ("trading.push.subscription_manager", "force_refresh_subscriptions"),
    ("trading.push.subscription_manager.core", "force_refresh_subscriptions"),
]

SUBSCRIPTION_MANAGER_START_CANDIDATES = [
    ("trading.push.subscription_manager", "start_symbol_subscription_manager"),
    ("trading.push.subscription_manager.core", "start_symbol_subscription_manager"),
    ("trading.push.symbol_subscription_manager", "start_symbol_subscription_manager"),
]

PUSH_START_CANDIDATES = [
    ("trading.push.push_stream.runtime", "start_push_stream"),
    ("trading.push.push_stream.runtime", "start"),
    ("trading.push.push_stream", "start_push_stream"),
    ("trading.push.push_stream", "start"),
    ("trading.push.push_stream.core", "start_push_stream"),
]

# ローテーション時間の外部設定用フック。
# 現行の trading/push/push_stream/rotation.py は rotation_core / rotation_settings への
# 互換ラッパーであり、これらの関数は存在しない (=常に未解決になる)。
# ローテーション値は rotation_settings.py 側が環境変数既定値として保持しており
#   PUSH_ROTATION_HOLD_SEC=4.8 / PUSH_ROTATION_UNREGISTER_WAIT_SEC=0.2
# は data_collectors/config.py の PUSH_REGISTER_SEC / PUSH_SWITCH_GAP_SEC と一致する。
# したがって未解決でも挙動は仕様どおり。将来 rotation 側に設定関数が追加された
# 場合に自動的に使われるよう、候補リスト自体は残す。
ROTATION_CONFIG_CANDIDATES = [
    ("trading.push.push_stream.rotation", "set_rotation_timing"),
    ("trading.push.push_stream.rotation", "configure_rotation"),
]


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return bool(default)
    s = str(v).strip().lower()
    if s in {"1", "true", "yes", "y", "on"}:
        return True
    if s in {"0", "false", "no", "n", "off"}:
        return False
    return bool(default)


def _call_with_fallback(fn, *args, **kwargs) -> Any:
    """既存関数の引数差分を吸収する。"""
    try:
        return fn(*args, **kwargs)
    except TypeError:
        pass

    try:
        return fn()
    except TypeError:
        pass

    try:
        return fn(schedule)
    except TypeError:
        pass

    return fn(*args, **kwargs)


def _normalize_symbol(x: Any) -> str | None:
    if x is None:
        return None
    s = str(x).strip().upper()
    if not s or s in {"NONE", "NULL", "NAN", "NA", "-", "0"}:
        return None
    if s.startswith("FILLER"):
        return None
    if s.endswith(".T"):
        s = s[:-2]
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]
    if not s.isalnum():
        return None
    if not (3 <= len(s) <= 5):
        return None
    return s


def _dedupe_symbols(items: Iterable[Any]) -> List[str]:
    out: List[str] = []
    seen: set[str] = set()
    for x in items or []:
        s = _normalize_symbol(x)
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _extract_symbols(src: Any) -> List[str]:
    if src is None:
        return []

    try:
        if hasattr(src, "columns"):
            cols = list(getattr(src, "columns", []))
            for c in ("symbol", "Symbol", "code", "Code", "stock_code", "銘柄コード"):
                if c in cols:
                    return _dedupe_symbols(src[c].tolist())
    except Exception:
        pass

    if isinstance(src, dict):
        for key in (
            "symbols",
            "codes",
            "items",
            "data",
            "active_symbols",
            "monitor_symbols",
            "push_symbols",
            "register_symbols",
            "subscription_symbols",
            "daily_watchlist_symbols",
        ):
            if key in src and src[key]:
                return _extract_symbols(src[key])
        return _dedupe_symbols(src.keys())

    if isinstance(src, str):
        return _dedupe_symbols([src])

    try:
        return _dedupe_symbols(list(src))
    except Exception:
        return []


def _set_global_symbols(symbols: List[str], *, source: str) -> None:
    """push_stream.rotation_symbols が読む global_data/runtime attr へ同じ100銘柄を注入する。"""
    if not symbols:
        return

    try:
        from global_state import global_data
    except Exception:
        global_data = None

    attrs = (
        "monitor_symbols",
        "candidate_push_symbols",
        "push_candidate_symbols",
        "push_symbols_100",
        "active_symbols",
        "ats_register_targets",
        "ats_targets",
        "should_register_symbols",
        "push_symbols",
        "register_symbols",
        "subscription_symbols",
        "daily_watchlist_symbols",
    )

    if global_data is not None:
        for attr in attrs:
            try:
                setattr(global_data, attr, list(symbols))
            except Exception:
                logger.debug("[PUSH RUNTIME] failed to set global_data.%s", attr, exc_info=True)
        try:
            global_data.symbols_active = set(symbols)
            global_data.active_symbol_source = source
            global_data.push_symbol_seed_source = source
            global_data.push_symbol_seed_count = len(symbols)
        except Exception:
            pass

    try:
        from trading.push.push_stream.runtime import _safe_set_runtime
        for attr in attrs:
            _safe_set_runtime(attr, list(symbols))
        _safe_set_runtime("symbols_active", list(symbols))
        _safe_set_runtime("push_symbol_seed_source", source)
        _safe_set_runtime("push_symbol_seed_count", len(symbols))
    except Exception:
        logger.debug("[PUSH RUNTIME] failed to set push_stream runtime symbols", exc_info=True)

    logger.warning(
        "[PUSH RUNTIME] seeded push/register symbols source=%s count=%d head=%s",
        source,
        len(symbols),
        symbols[:20],
    )


def _provider_call(fn) -> Any:
    patterns = (
        lambda: fn(force=True),
        lambda: fn(limit=PUSH_TARGET_TOTAL),
        lambda: fn(max_symbols=PUSH_TARGET_TOTAL),
        lambda: fn(PUSH_TARGET_TOTAL),
        lambda: fn(),
    )
    last_err: BaseException | None = None
    for caller in patterns:
        try:
            return caller()
        except TypeError as e:
            last_err = e
            continue
        except Exception:
            logger.debug("[PUSH RUNTIME] provider call failed fn=%s", fn, exc_info=True)
            return None
    if last_err is not None:
        logger.debug("[PUSH RUNTIME] provider signature mismatch fn=%s err=%s", fn, last_err)
    return None


def seed_push_symbols_once() -> List[str]:
    """
    main.py からPUSH stackを切り離したため、main_database.py側で
    active_symbol_manager.update_active_symbols() を明示実行して登録対象を作る。
    """
    providers = [
        ("trading.ranking.active_symbol_manager", "update_active_symbols"),
        ("trading.ranking.active_symbol_manager", "get_register_symbols"),
        ("trading.ranking.active_symbol_manager", "get_push_symbols"),
        ("trading.ranking.active_symbol_manager", "get_active_symbols"),
        ("optional.batch.daily_watchlist", "load_daily_watchlist_symbols"),
        ("optional.batch.daily_watchlist", "get_daily_watchlist_symbols"),
    ]

    for module_name, func_name in providers:
        try:
            import importlib
            mod = importlib.import_module(module_name)
            fn = getattr(mod, func_name, None)
        except Exception:
            logger.debug("[PUSH RUNTIME] symbol seed import failed %s.%s", module_name, func_name, exc_info=True)
            continue

        if not callable(fn):
            continue

        src = _provider_call(fn)
        symbols = _extract_symbols(src)[:PUSH_TARGET_TOTAL]
        logger.info(
            "[PUSH RUNTIME] symbol seed provider=%s.%s count=%d head=%s",
            module_name,
            func_name,
            len(symbols),
            symbols[:10],
        )
        if symbols:
            _set_global_symbols(symbols, source=f"{module_name}.{func_name}")
            return symbols

    # 全プロバイダーが空 = Ranking 系が落ちている。
    # ここで [] を返すと登録候補ゼロになり、PUSH が止まって
    # Summary も Ranking も作れなくなる循環に入る (2026-09-01 実測)。
    # Ranking に依存しない下敷き (last_good / symbol_flags / watchlist) を使う。
    try:
        from trading.push.fallback_universe import resolve_fallback_symbols

        fallback, fb_source = resolve_fallback_symbols(PUSH_TARGET_TOTAL)
    except Exception:
        logger.exception("[PUSH RUNTIME] fallback universe resolve failed")
        fallback, fb_source = [], ""

    if fallback:
        logger.warning(
            "[PUSH RUNTIME] all providers returned empty -> fallback を使用 source=%s count=%d head=%s "
            "(現在の登録は壊さない)",
            fb_source, len(fallback), fallback[:10],
        )
        _set_global_symbols(fallback, source=f"fallback:{fb_source}")
        return fallback

    logger.error(
        "[PUSH RUNTIME] failed to seed push/register symbols: all providers returned empty "
        "and no fallback available (現在の登録は維持する)"
    )
    return []


def schedule_symbol_seed_refresh() -> None:
    """
    ranking DB writer が後からランキングを取り込む場合に備えて、
    PUSH登録対象を定期更新する。
    """
    try:
        schedule.every(30).seconds.do(seed_push_symbols_once).tag("data_collectors_push_symbol_seed")
        logger.info("[PUSH RUNTIME] scheduled symbol seed refresh every 30 seconds")
    except Exception:
        logger.exception("[PUSH RUNTIME] failed to schedule symbol seed refresh")


def configure_push_rotation_if_supported() -> None:
    fn = resolve_callable(ROTATION_CONFIG_CANDIDATES, required=False, warn_if_missing=False)
    if fn is None:
        logger.info(
            "[PUSH RUNTIME] rotation config function not found (想定内); "
            "rotation_settings.py の既定値/環境変数をそのまま使用する "
            "(register=%.2fs switch_gap=%.2fs batch=%s total=%s)",
            PUSH_REGISTER_SEC,
            PUSH_SWITCH_GAP_SEC,
            PUSH_BATCH_SIZE,
            PUSH_TARGET_TOTAL,
        )
        return

    kwargs = {
        "register_seconds": PUSH_REGISTER_SEC,
        "switch_gap_seconds": PUSH_SWITCH_GAP_SEC,
        "batch_size": PUSH_BATCH_SIZE,
        "target_total": PUSH_TARGET_TOTAL,
    }

    try:
        logger.info("[PUSH RUNTIME] configure rotation kwargs=%s", kwargs)
        _call_with_fallback(fn, **kwargs)
    except Exception:
        logger.exception("[PUSH RUNTIME] configure rotation failed")


def resolve_subscription_refresh_callable():
    return resolve_callable(SUBSCRIPTION_REFRESH_CANDIDATES, required=False)


def start_subscription_manager_if_requested() -> bool:
    if not _env_bool("DATA_COLLECTORS_START_SUB_MANAGER_LOOP", False):
        logger.info("[PUSH RUNTIME] subscription manager background loop skipped; rotation worker handles registration")
        return True

    fn = resolve_callable(SUBSCRIPTION_MANAGER_START_CANDIDATES, required=False)
    if fn is None:
        logger.error("[PUSH RUNTIME] no subscription manager start function resolved")
        return False

    logger.info("[PUSH RUNTIME] call subscription manager loop function: %s", fn)

    try:
        result = _call_with_fallback(fn)
        logger.info("[PUSH RUNTIME] subscription manager loop start returned: %r", result)
        return True
    except Exception:
        logger.exception("[PUSH RUNTIME] subscription manager loop start failed")
        return False


def _ws_thread_alive() -> bool:
    """PUSH WebSocket スレッドが実際に走っているか (起動成功の唯一の判定)。"""
    try:
        from trading.push.push_stream import state as _st

        th = getattr(_st, "_ws_thread", None)
        return bool(th is not None and th.is_alive())
    except Exception:
        logger.debug("[PUSH RUNTIME] ws thread probe failed", exc_info=True)
        return False


def _push_start_trace():
    try:
        from trading.push.push_stream import start_trace

        return start_trace
    except Exception:
        return None


def start_push_stream() -> bool:
    """PUSH WebSocket を起動する。

    2026-09-03 修正:
      旧実装は「例外が出なかった = True」だった。
      runner 側は ws_owner ロックを取れないと戻り値 None で return するため、
      WebSocket が 1 本も無いのに push_ok=True になり、
      run_forever() が heartbeat だけを出し続けていた
      (pid=41016 / active_ws=0 / ws_thread_alive=None / uptime 1186s)。
      起動できたかは state._ws_thread が生きているかだけで判定する。
    """
    trace = _push_start_trace()

    fn = resolve_callable(PUSH_START_CANDIDATES, required=False)
    if fn is None:
        if trace:
            trace.abort(6, "no_push_start_function_resolved", candidates=PUSH_START_CANDIDATES)
        logger.error("[PUSH RUNTIME] no push start function resolved")
        return False

    refresh_callable = resolve_subscription_refresh_callable()

    logger.info(
        "[PUSH RUNTIME] call push stream function=%s enable_rotate=True refresh_callable=%s",
        fn,
        bool(callable(refresh_callable)),
    )

    try:
        result = _call_with_fallback(
            fn,
            refresh_callable=refresh_callable,
            enable_rotate=True,
        )
        logger.info("[PUSH RUNTIME] push stream start returned: %r", result)
    except Exception:
        if trace:
            trace.abort(6, "push_stream_start_exception")
        logger.exception("[PUSH RUNTIME] push stream start failed")
        return False

    started = _ws_thread_alive()
    reason = ""
    if isinstance(result, dict):
        reason = str(result.get("reason") or "")
        if not result.get("started") and started:
            # runner が False を返したのに thread が生きている = 別経路が起動済み。
            logger.warning(
                "[PUSH RUNTIME] runner reported not-started but ws thread is alive reason=%s", reason
            )

    if not started:
        if trace:
            trace.abort(
                7,
                reason or "ws_thread_not_alive_after_start",
                runner_result=repr(result),
            )
        logger.error(
            "[PUSH RUNTIME] push stream did not actually start: ws thread is not alive "
            "runner_result=%r (silent success を禁止するため False を返す)",
            result,
        )
        return False

    logger.warning("[PUSH RUNTIME] push stream start confirmed: ws thread alive runner_result=%r", result)
    return True


# ============================================================
# P0-2 / P0-4: PUSH OWNER HEALTH
# ------------------------------------------------------------
# main.py は WS owner ではない。PUSH 接続の owner はこのプロセスだけ。
# 2026-08-28 11:13 に PUSH DB の書き込みが止まったとき、
# main 側からは「DB が古い」ことしか分からず、
# WS が死んだのか writer が死んだのか区別できなかった。
#
# ここで owner 自身の健康状態を必ず出す。
# 多重 WS は絶対に張らない (このプロセス内の再接続のみ)。
# ============================================================

_PUSH_HEALTH_INTERVAL_SEC = 10.0
_PROCESS_START_TS = time.time()

# DB 進捗プローブの前回値 (rowid が増えているかを見る)
_db_probe_last_rowid = None
_db_probe_last_at = 0.0
_db_probe_last_grew_at = 0.0


def _probe_push_db_progress() -> dict:
    """push DB の MAX(rowid) / MAX(datetime) を直接見る。

    writer 側の内部カウンタが取れなくても、
    「DB が実際に伸びているか」はこれで断定できる。
    WS 停止と writer 停止の切り分けに使う。
    """
    global _db_probe_last_rowid, _db_probe_last_at, _db_probe_last_grew_at

    out: dict = {}
    try:
        from scheduler_jobs.summary.memory_latest_1m import _push_db_path

        path = _push_db_path()
    except Exception:
        return out

    import sqlite3

    conn = None
    try:
        conn = sqlite3.connect(str(path), timeout=2.0)
        conn.execute("PRAGMA query_only=ON")
        # MAX(datetime) は全表スキャンになる (実測 543MB で 12.4 秒)。
        # tick は追記専用なので rowid 最大行の datetime が最新。
        row = conn.execute(
            "SELECT rowid, datetime FROM stream_data_raw ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
    except Exception:
        return out
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    if not row:
        return out

    max_rowid, max_dt = row[0], row[1]
    now_m = time.monotonic()
    out["db_path"] = str(path)
    out["db_max_rowid"] = max_rowid
    out["db_max_datetime"] = str(max_dt) if max_dt else None

    if _db_probe_last_rowid is not None and max_rowid is not None:
        delta = int(max_rowid) - int(_db_probe_last_rowid)
        out["db_rowid_delta"] = delta
        if delta > 0:
            _db_probe_last_grew_at = now_m
    if _db_probe_last_grew_at > 0.0:
        out["db_last_grew_age"] = round(now_m - _db_probe_last_grew_at, 1)
        # writer が生きているかは「DB が伸びた時刻」で測る
        out["last_write_age"] = out["db_last_grew_age"]

    _db_probe_last_rowid = max_rowid
    _db_probe_last_at = now_m
    return out


def _push_health_snapshot() -> dict:
    snap: dict = {
        "pid": os.getpid(),
        "uptime_sec": round(time.time() - _PROCESS_START_TS, 1),
        "ws_connected": None,
        "ws_thread_alive": None,
        "last_recv_at": None,
        "last_recv_age": None,
        "last_write_at": None,
        "last_write_age": None,
        "received_total": None,
        "written_total": None,
        "queue": None,
        "registered_count": None,
        "rotation": None,
        "token_tail": None,
        "db_path": None,
    }
    now = time.time()

    def _age_of(v) -> Any:
        """datetime / epoch のどちらでも秒齢にする。"""
        if v is None:
            return None
        try:
            if hasattr(v, "timestamp"):
                return round(now - v.timestamp(), 1)
            return round(now - float(v), 1)
        except Exception:
            return None

    # trading/push/push_stream/state.py の実際の名前に合わせる。
    # (前回は connected / total_received 等と推測して書いたため
    #  全項目が None になっていた。2026-08-28 13:58 のログで判明。)
    try:
        from trading.push.push_stream import state as _st

        ev = getattr(_st, "_connected_event", None)
        snap["ws_connected"] = bool(ev.is_set()) if ev is not None else None
        th = getattr(_st, "_ws_thread", None)
        snap["ws_thread_alive"] = bool(th.is_alive()) if th is not None else None

        snap["received_total"] = getattr(_st, "_total_received", None)
        snap["written_total"] = getattr(_st, "_total_flushed", None)
        snap["dropped_total"] = getattr(_st, "_total_dropped", None)
        snap["error_total"] = getattr(_st, "_total_errors", None)

        q = getattr(_st, "_push_queue", None)
        try:
            snap["queue"] = q.qsize() if q is not None else None
        except Exception:
            snap["queue"] = None

        lr = getattr(_st, "_last_message_at", None)
        snap["last_recv_at"] = str(lr) if lr is not None else None
        snap["last_recv_age"] = _age_of(lr)

        lf = getattr(_st, "_last_flush_at", None)
        snap["last_flush_at"] = str(lf) if lf is not None else None
        snap["last_flush_age"] = _age_of(lf)

        lc = getattr(_st, "_last_connect_at", None)
        snap["last_connect_at"] = str(lc) if lc is not None else None
        ld = getattr(_st, "_last_disconnect_at", None)
        snap["last_disconnect_at"] = str(ld) if ld is not None else None
        le = getattr(_st, "_last_error_at", None)
        snap["last_error_at"] = str(le) if le is not None else None

        snap["rotation_enabled"] = getattr(_st, "_rotation_enabled", None)

        flush_th = getattr(_st, "_flush_thread", None)
        snap["flush_thread_alive"] = (
            bool(flush_th.is_alive()) if flush_th is not None else None
        )
        snap["writer_ready"] = getattr(_st, "_stream_writer", None) is not None

        # Ver2.0: on_message は deque へ積むだけになったので、
        # DataFrame ではなく tick リングの長さを見る。
        try:
            rows = getattr(_st, "_push_rows", None)
            if rows is not None:
                snap["memory_rows"] = len(rows)
            else:
                df = getattr(_st, "_push_df", None)
                snap["memory_rows"] = len(df) if df is not None else None
        except Exception:
            snap["memory_rows"] = None
    except Exception:
        logger.debug("[PUSH OWNER HEALTH] push_stream state unavailable", exc_info=True)

    # 登録銘柄数は subscription 側から取る。
    try:
        from trading.push.push_stream import rotation_core as _rc

        regs = getattr(_rc, "_LAST_REGISTERED", None) or getattr(_rc, "_last_registered", None)
        if regs is not None:
            snap["registered_count"] = len(regs)
            snap["registered_head"] = list(regs)[:5]
    except Exception:
        logger.debug("[PUSH OWNER HEALTH] rotation state unavailable", exc_info=True)

    # DB writer 側 (実際に push DB へ書けているか)。
    # last_write_age は「WS は受けているが writer が死んだ」を切り分ける鍵。
    try:
        from trading.push import push_db_writer as _w

        for cand in ("LAST_WRITE_AT", "_LAST_WRITE_AT", "_last_write_at"):
            lw = getattr(_w, cand, None)
            if lw is not None:
                snap["last_write_at"] = str(lw)
                snap["last_write_age"] = _age_of(lw)
                break
        for cand in ("TOTAL_WRITTEN", "_TOTAL_WRITTEN", "_total_written"):
            tw = getattr(_w, cand, None)
            if tw is not None:
                snap["written_total"] = tw
                break
        for cand in ("WRITE_FAIL_TOTAL", "_WRITE_FAIL_TOTAL", "_write_fail_total"):
            wf = getattr(_w, cand, None)
            if wf is not None:
                snap["write_fail_total"] = wf
                break
        for cand in ("LAST_DB_PATH", "_LAST_DB_PATH", "_last_db_path"):
            dp = getattr(_w, cand, None)
            if dp:
                snap["db_path"] = str(dp)
                break
    except Exception:
        logger.debug("[PUSH OWNER HEALTH] push_db_writer state unavailable", exc_info=True)

    # writer 側の名前が取れない場合の最後の砦: DB の MAX(rowid) を直接見る。
    # これが増えていなければ「書けていない」と断定できる。
    if snap.get("last_write_age") is None:
        try:
            snap.update(_probe_push_db_progress())
        except Exception:
            logger.debug("[PUSH OWNER HEALTH] db progress probe failed", exc_info=True)

    try:
        tok = str(os.getenv("API_TOKEN") or "")
        snap["token_tail"] = tok[-4:] if tok else None
    except Exception:
        snap["token_tail"] = None

    snap["rotation"] = "A/B batch=%s target=%s" % (PUSH_BATCH_SIZE, PUSH_TARGET_TOTAL)
    return snap


def _classify_push_fault(snap: dict) -> str:
    """P0-4: WS 停止 / writer 停止 / reader 停止 を区別する。

    A: WS fresh  / write stale -> writer_problem
    B: WS stale  / write stale -> websocket_or_subscription_problem
    C: WS fresh  / write fresh -> owner_ok (main が古いなら reader_problem)
    """
    thr = 10.0
    recv_age = snap.get("last_recv_age")
    write_age = snap.get("last_write_age")
    recv_stale = recv_age is None or recv_age > thr
    write_stale = write_age is None or write_age > thr

    if not recv_stale and write_stale:
        return "writer_problem"
    if recv_stale and write_stale:
        return "websocket_or_subscription_problem"
    if not recv_stale and not write_stale:
        return "owner_ok"
    return "unknown"


# ============================================================
# PUSH HEALTH: 再起動後の合格条件をそのまま観測できる 1 行
# ------------------------------------------------------------
# 2026-08-31 の多重 WebSocket (main.py と push_receiver が同時接続) は
# 個別の値を突き合わせないと分からなかった。合否をここで 1 語にする。
#
#   HEALTHY        所有者が 1 プロセス / WS 1 本 / 受信も書き込みも増えている
#   MULTI_WS       同一プロセス内に WebSocket が 2 本以上
#   OWNER_MISMATCH 所有者でないのに WS を持っている
#   RECONNECT_STORM 連続即死が閾値以上
#   NO_DATA        接続しているのに受信が増えない
#   DISCONNECTED   未接続
#   STARTING       起動直後で判定材料が足りない
# ============================================================

_HEALTH_PREV: dict = {}
_HEALTH_STORM_MIN = 3
_HEALTH_NO_DATA_SEC = 30.0
_HEALTH_MIN_UPTIME_SEC = 60.0


def _health_delta(key: str, value) -> Any:
    """前回ログからの増分。初回は None。"""
    try:
        if value is None:
            return None
        prev = _HEALTH_PREV.get(key)
        _HEALTH_PREV[key] = value
        if prev is None:
            return None
        return value - prev
    except Exception:
        return None


def _push_health_status(snap: dict) -> tuple[str, dict]:
    """再起動後に確認したい合格条件をまとめて判定する。"""
    extra: dict = {}
    try:
        from trading.push.push_stream import ownership as _own

        owner, role, reason = _own.is_push_owner()
        extra["role"] = role
        extra["owner"] = owner
        extra["owner_reason"] = reason
        extra["ws_instance_id"] = _own.ws_instance_id()
        extra["generation"] = _own.current_connection_generation()
        extra["active_ws"] = _own.active_ws_instances()
    except Exception:
        logger.debug("[PUSH HEALTH] ownership probe failed", exc_info=True)
        extra.setdefault("active_ws", -1)
        extra.setdefault("owner", None)

    try:
        from trading.push.push_stream.runner import reconnect_stats

        st = reconnect_stats() or {}
        extra["fast_death_consecutive"] = st.get("consecutive_fast_deaths")
        extra["total_reconnects"] = st.get("total_reconnects")
    except Exception:
        extra["fast_death_consecutive"] = None
        extra["total_reconnects"] = None

    # プロセス内で実際に走っている WebSocket 本数 (runner 側の登録簿)。
    try:
        from trading.push.push_stream.runner import active_ws_instance_count

        extra["active_ws"] = int(active_ws_instance_count())
    except Exception:
        pass

    extra["received_delta"] = _health_delta("received_total", snap.get("received_total"))
    extra["flushed_delta"] = _health_delta("written_total", snap.get("written_total"))

    connected = bool(snap.get("ws_connected"))
    last_recv_age = snap.get("last_recv_age")
    uptime = snap.get("uptime_sec") or 0.0
    active_ws = extra.get("active_ws")
    fast = extra.get("fast_death_consecutive")

    if isinstance(active_ws, int) and active_ws > 1:
        return "MULTI_WS", extra
    if extra.get("owner") is False and (connected or (isinstance(active_ws, int) and active_ws > 0)):
        return "OWNER_MISMATCH", extra
    if isinstance(fast, int) and fast >= _HEALTH_STORM_MIN:
        return "RECONNECT_STORM", extra
    if not connected:
        return "DISCONNECTED", extra
    if last_recv_age is None or (isinstance(last_recv_age, (int, float)) and last_recv_age >= _HEALTH_NO_DATA_SEC):
        return "NO_DATA", extra
    if float(uptime or 0.0) < _HEALTH_MIN_UPTIME_SEC:
        return "STARTING", extra
    return "HEALTHY", extra


def _log_push_health_line(snap: dict) -> dict:
    status, extra = _push_health_status(snap)
    log = logger.info if status == "HEALTHY" else logger.warning
    log(
        "[PUSH HEALTH] status=%s owner_pid=%s role=%s owner=%s active_ws=%s connected=%s "
        "uptime=%s generation=%s ws_instance_id=%s fast_death_consecutive=%s total_reconnects=%s "
        "received_total=%s received_delta=%s flushed_total=%s flushed_delta=%s "
        "last_recv_age=%s dropped=%s reason=%s",
        status, snap.get("pid"), extra.get("role"), extra.get("owner"), extra.get("active_ws"),
        snap.get("ws_connected"), snap.get("uptime_sec"), extra.get("generation"),
        extra.get("ws_instance_id"), extra.get("fast_death_consecutive"), extra.get("total_reconnects"),
        snap.get("received_total"), extra.get("received_delta"),
        snap.get("written_total"), extra.get("flushed_delta"),
        snap.get("last_recv_age"), snap.get("dropped_total"), extra.get("owner_reason"),
    )
    snap["health_status"] = status
    return snap


def _push_ws_ownership() -> tuple:
    """(このプロセスが WS 所有者か, 所有者 pid) を返す。

    2026-09-03: 同じ日に 2 スタックが二重起動され、
    WS を張れなかった待機側 (pid 57420) が所有者 (pid 46696) と
    **同じ** push_owner_health.heartbeat.json を上書きしていた。
    その結果 heartbeat が
        ws_connected=false / recv_total=0 / writer_alive=false /
        health_status=DISCONNECTED
    になり、実際には所有者が 98 分無切断で受信し続けていたのに
    「PUSH が死んでいる」ように見えていた。
    待機側は所有者のファイルに触らせない。
    """
    try:
        from trading.push.push_stream.ws_owner import is_push_ws_owner, owned_elsewhere

        mine = bool(is_push_ws_owner())
        holder_pid = None
        if not mine:
            try:
                elsewhere, holder = owned_elsewhere()
                if elsewhere and isinstance(holder, dict):
                    holder_pid = holder.get("pid")
            except Exception:
                holder_pid = None
            if holder_pid is None:
                try:
                    from trading.push.push_stream.ws_owner import read_owner_info

                    info = read_owner_info()
                    if isinstance(info, dict):
                        holder_pid = info.get("pid")
                except Exception:
                    holder_pid = None
        return mine, holder_pid
    except Exception:
        # 判定できないときは「所有者ではない」に倒す。
        # 所有者のファイルを壊さない方が安全。
        return False, None


def _log_push_owner_health() -> dict:
    snap = _push_health_snapshot()
    fault = _classify_push_fault(snap)
    snap["fault"] = fault
    try:
        _log_push_health_line(snap)
    except Exception:
        logger.debug("[PUSH HEALTH] emit failed", exc_info=True)
    log = logger.warning if fault not in ("owner_ok",) else logger.info
    log(
        "[PUSH OWNER HEALTH] pid=%s uptime=%ss ws_connected=%s ws_thread_alive=%s "
        "flush_thread_alive=%s writer_ready=%s "
        "last_recv_at=%s last_recv_age=%s last_write_age=%s last_flush_age=%s "
        "received_total=%s written_total=%s dropped_total=%s error_total=%s "
        "write_fail_total=%s queue=%s memory_rows=%s "
        "registered_count=%s registered_head=%s rotation=%s rotation_enabled=%s "
        "last_connect_at=%s last_disconnect_at=%s last_error_at=%s "
        "db_max_rowid=%s db_rowid_delta=%s db_max_datetime=%s "
        "token_tail=%s db_path=%s fault=%s",
        snap.get("pid"), snap.get("uptime_sec"),
        snap.get("ws_connected"), snap.get("ws_thread_alive"),
        snap.get("flush_thread_alive"), snap.get("writer_ready"),
        snap.get("last_recv_at"), snap.get("last_recv_age"),
        snap.get("last_write_age"), snap.get("last_flush_age"),
        snap.get("received_total"), snap.get("written_total"),
        snap.get("dropped_total"), snap.get("error_total"),
        snap.get("write_fail_total"), snap.get("queue"), snap.get("memory_rows"),
        snap.get("registered_count"), snap.get("registered_head"),
        snap.get("rotation"), snap.get("rotation_enabled"),
        snap.get("last_connect_at"), snap.get("last_disconnect_at"),
        snap.get("last_error_at"),
        snap.get("db_max_rowid"), snap.get("db_rowid_delta"), snap.get("db_max_datetime"),
        snap.get("token_tail"), snap.get("db_path"), fault,
    )
    try:
        owner, holder_pid = _push_ws_ownership()
        snap["ws_owner"] = owner
        snap["ws_owner_holder_pid"] = holder_pid
        if owner:
            write_heartbeat("push_owner_health", status="alive", **snap)
        else:
            # 所有者のファイルは絶対に上書きしない。待機側は別名で出す。
            write_heartbeat(
                "push_owner_standby_%s" % os.getpid(),
                status="standby",
                note="WS 所有者は別プロセス。ここは待機中なので "
                     "ws_connected/recv_total が 0 なのは正常。",
                **snap
            )
    except Exception:
        logger.debug("[PUSH OWNER HEALTH] heartbeat write failed", exc_info=True)
    return snap


def _ws_owner_holder_text() -> str:
    """ロックを持っている別プロセスの情報 (取れなければ空文字)。"""
    try:
        from trading.push.push_stream.ws_owner import owned_elsewhere

        elsewhere, holder = owned_elsewhere()
        if not elsewhere:
            return ""
        return "holder_pid=%s holder_argv=%s acquired_at=%s pid_alive=%s" % (
            holder.get("pid"),
            holder.get("argv"),
            holder.get("acquired_at"),
            holder.get("pid_alive"),
        )
    except Exception:
        return ""


# PUSH WebSocket が起動できなかったときの再試行間隔 (秒)。
# 旧実装はここで return 1 して終わっていたため、
# 先行プロセスが後から死んでも PUSH は二度と復旧しなかった。
_PUSH_START_RETRY_SEC = float(os.environ.get("PUSH_START_RETRY_SEC", "20"))


def run_forever() -> int:
    logger.info("[PUSH RUNTIME] START")
    # 2026-10-03: 2 つ目の口座 (別 PC) の PUSH 中継の受信口 (settings.ini [push_relay])
    try:
        from trading.push.push_relay_server import start_relay_server_safe
        start_relay_server_safe()
    except Exception:
        logger.exception("[PUSH RUNTIME] push relay server start failed")
    _trace = _push_start_trace()
    if _trace:
        _trace.trace(1, entrypoint="data_collectors.push_runtime.run_forever")
    logger.info(
        "[PUSH RUNTIME] rotation target_total=%s batch_size=%s register_sec=%s gap_sec=%s",
        PUSH_TARGET_TOTAL,
        PUSH_BATCH_SIZE,
        PUSH_REGISTER_SEC,
        PUSH_SWITCH_GAP_SEC,
    )

    os.environ.setdefault("PUSH_REGISTER_SEC", str(PUSH_REGISTER_SEC))
    os.environ.setdefault("PUSH_SWITCH_GAP_SEC", str(PUSH_SWITCH_GAP_SEC))
    os.environ.setdefault("PUSH_ROTATION_HOLD_SEC", str(PUSH_REGISTER_SEC))
    os.environ.setdefault("PUSH_ROTATION_UNREGISTER_WAIT_SEC", str(PUSH_SWITCH_GAP_SEC))
    os.environ.setdefault("PUSH_BATCH_SIZE", str(PUSH_BATCH_SIZE))
    os.environ.setdefault("PUSH_TARGET_TOTAL", str(PUSH_TARGET_TOTAL))

    # 重要:
    #   main.pyから切り離したため、PUSH登録対象100銘柄をこのプロセス内で作る。
    _seeded = seed_push_symbols_once()
    if _trace:
        if _seeded:
            _trace.trace(3, seeded=len(_seeded), head=_seeded[:5])
        else:
            _trace.abort(3, "symbol_seed_empty")
    schedule_symbol_seed_refresh()

    configure_push_rotation_if_supported()

    sub_loop_ok = start_subscription_manager_if_requested()
    push_ok = start_push_stream()

    if not sub_loop_ok:
        logger.error("[PUSH RUNTIME] subscription manager background loop could not start")

    # ------------------------------------------------------------
    # 2026-09-03 P0: 起動できなくてもプロセスを落とさず再試行する。
    #
    # 旧実装は push_ok=False で return 1 していたが、
    # 実際に起きたのは「push_ok が常に True になり、
    # WebSocket が無いまま heartbeat だけ出し続ける」状態だった。
    # start_push_stream() を実測ベース (ws_thread の生死) に直した結果、
    # ここで return 1 すると今度は PUSH owner プロセスが即死する。
    #
    # 起動できない主因は「前回起動の push_receiver が生き残っていて
    # ws_owner ロックを握っている」ケースなので、
    # 先行プロセスが終了した時点で自力で復帰できるよう再試行する。
    # 多重 WebSocket は runner 側の ws_owner ロックが引き続き防ぐ。
    # ------------------------------------------------------------
    if not push_ok:
        logger.error(
            "[PUSH RUNTIME] push stream could not start -> %.0f 秒間隔で再試行する "
            "(プロセスは落とさない) %s",
            _PUSH_START_RETRY_SEC,
            _ws_owner_holder_text(),
        )

    last_hb = 0.0
    last_health = 0.0
    last_start_retry = time.time()

    # P0-1: この while ループを絶対に抜けさせない。
    #
    # 旧実装は write_heartbeat() / logger.info() が try の外にあった。
    # 当時 heartbeat の書き先は NAS (X:\logs\console\*.heartbeat.json) で、
    # SMB 障害時にここで例外が出るとループを抜けてプロセスが終了し、
    # PUSH owner が居なくなっていた。
    # 2026-09-05 に書き先を LOG_DIR (既定でローカル) へ移したので
    # この経路で NAS を掴むことは無くなったが、握り潰しは残しておく
    # (KABU_CONSOLE_LOG_DIR で NAS へ戻せるため)。
    # 2026-08-28 11:11:14 を最後に heartbeat ログが止まり、
    # 11:12:23 から interpreter shutdown が始まっていた
    # (heartbeat 間隔も 30s -> 114s/62s/53s と伸びていた)。
    # ループ本体は BaseException 以外すべて飲み込む。
    while True:
        try:
            try:
                schedule.run_pending()
            except Exception:
                logger.exception("[PUSH RUNTIME] schedule.run_pending failed")

            now = time.time()

            # PUSH WebSocket が張れていない間は諦めずに張り直す。
            # ws_thread が生きている限り runner 側が何もしないので副作用は無い。
            if not _ws_thread_alive() and (now - last_start_retry) >= _PUSH_START_RETRY_SEC:
                last_start_retry = now
                holder = _ws_owner_holder_text()
                logger.error(
                    "[PUSH START TRACE] ABORT step=07 ws_thread_start reason=ws_thread_not_alive "
                    "pid=%s retry_sec=%.0f %s",
                    os.getpid(), _PUSH_START_RETRY_SEC, holder,
                )
                push_ok = start_push_stream()
                if push_ok:
                    logger.warning(
                        "[PUSH RUNTIME] push stream recovered by retry (ws thread alive again)"
                    )

            if now - last_health >= _PUSH_HEALTH_INTERVAL_SEC:
                last_health = now
                try:
                    _log_push_owner_health()
                except Exception:
                    logger.exception("[PUSH OWNER HEALTH] snapshot failed")

            if now - last_hb >= 30:
                last_hb = now
                try:
                    write_heartbeat(
                        "push_receiver",
                        status="alive",
                        subscription_loop_started=sub_loop_ok,
                        # push_started は「起動要求を出した」ではなく
                        # 「WebSocket スレッドが今生きている」を報告する。
                        push_started=_ws_thread_alive(),
                        push_start_requested=push_ok,
                        rotation_enabled=True,
                        register_seconds=PUSH_REGISTER_SEC,
                        switch_gap_seconds=PUSH_SWITCH_GAP_SEC,
                        batch_size=PUSH_BATCH_SIZE,
                        target_total=PUSH_TARGET_TOTAL,
                    )
                except Exception:
                    # NAS が書けなくても PUSH 受信は続ける。
                    logger.warning(
                        "[PUSH RUNTIME] heartbeat write failed (NAS?) -> 受信は継続する",
                        exc_info=True,
                    )
                try:
                    logger.info("[PUSH RUNTIME] heartbeat alive")
                except Exception:
                    pass
        except Exception:
            # ここに来ても絶対に return しない。owner が消える方が危険。
            try:
                logger.exception("[PUSH RUNTIME] main loop iteration failed; continue")
            except Exception:
                pass

        time.sleep(1.0)
