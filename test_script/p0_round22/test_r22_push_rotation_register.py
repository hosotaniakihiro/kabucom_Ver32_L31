# -*- coding: utf-8 -*-
"""
第22ラウンド: PUSH WebSocket reconnect storm の回帰テスト

2026-09-02 09:14-10:34 の実測 (push_receiver_runner PID 25404):

    09:14:30 --- CONNECTED --- connection_generation=1
    09:15:10 [push_stream] refresh skipped reason=rotation_A ws_not_ready
    09:15:10 [push_stream] rotation A register returned ok=False
    09:15:10 [push_stream] rotation A register failed -> backoff 180.0s failure_count=1
    ...
    10:09:10 [push_stream] reconnect ... uptime=4.3s verdict=fast_death consecutive=170
    10:09:10 [push_stream][WS RECONNECT STORM] 接続が 170 回連続で 60 秒未満で切れている

    WS uptime: 23 サイクルで min=1.0s max=6.0s avg=4.1s
    そのうち重い処理 (summary rebuild / DB upsert / ranking 集約) を
    含むサイクルは 7/23 だけ。負荷とは無関係。

原因:
    PUSH_ROTATION_REGISTER_WITH_WS_CLOSED=0 (defaults.py) により
    transport._allow_refresh_with_ws_closed("rotation_A") が False になり、
    rotation の **REST** 登録 (PUT /kabusapi/register) が
    "ws_not_ready" で skip されていた。
    PUSH_STREAM_SKIP_AFTER_OPEN_REFRESH=1 のため on_open 側でも登録しないので、
    **銘柄登録を行う経路が 1 つも残らない**。
        登録が無い -> kabu Station が PUSH を切る
        -> 切れているから登録できない  (相互ロック)
    失敗ごとに 180s -> 600s のバックオフが付くため自力復旧もしない。

このテストが守る不変条件:
    1. 出荷時の既定値で rotation の REST 登録が WS 閉でも実行できる
    2. on_open など WS 送信に依存する refresh は従来どおり WS 必須のまま
    3. rotation 登録が skip されたときは ERROR で理由が残る
       (握り潰して 600s 沈黙しない)

kabu Station へは接続しない (transport の判定関数だけを見る)。
実注文 / 取消 / 返済は一切発生しない。
"""
from __future__ import annotations

import logging
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

PASS = 0
FAIL = 0
FAILURES: list = []


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  OK   {label} {detail}")
    else:
        FAIL += 1
        FAILURES.append(f"{label} {detail}")
        print(f"  FAIL {label} {detail}")


def section(title):
    print()
    print("=" * 70)
    print(title)
    print("=" * 70)


class CapturingHandler(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        try:
            self.records.append("%s|%s" % (record.levelname, record.getMessage()))
        except Exception:
            self.records.append("%s|%s" % (record.levelname, record.msg))

    def text(self):
        return "\n".join(self.records)


def _clear_env():
    os.environ.pop("PUSH_ROTATION_REGISTER_WITH_WS_CLOSED", None)


# ============================================================
# [1] 出荷時の既定値
# ============================================================

def test_shipped_default() -> None:
    section("[1] 出荷時の既定値で rotation の REST 登録が WS 閉でも許可される")

    from core.startup.runtime_env import defaults as D

    val = D.PUSH_DEFAULTS.get("PUSH_ROTATION_REGISTER_WITH_WS_CLOSED")
    check("defaults.py の既定値が '1'", val == "1", "value=%r" % val)
    check("SKIP_AFTER_OPEN_REFRESH は 1 のまま (設計変更しない)",
          D.PUSH_DEFAULTS.get("PUSH_STREAM_SKIP_AFTER_OPEN_REFRESH") == "1",
          str(D.PUSH_DEFAULTS.get("PUSH_STREAM_SKIP_AFTER_OPEN_REFRESH")))

    _clear_env()
    D._set_defaults(D.PUSH_DEFAULTS)
    check("_set_defaults が env へ '1' を入れる",
          os.environ.get("PUSH_ROTATION_REGISTER_WITH_WS_CLOSED") == "1",
          str(os.environ.get("PUSH_ROTATION_REGISTER_WITH_WS_CLOSED")))


# ============================================================
# [2] 判定関数
# ============================================================

def test_allow_flag() -> None:
    section("[2] rotation は WS 閉でも許可 / on_open は従来どおり WS 必須")

    from core.startup.runtime_env import defaults as D
    from trading.push.push_stream import transport as T

    _clear_env()
    D._set_defaults(D.PUSH_DEFAULTS)

    check("rotation_A は許可", T._allow_refresh_with_ws_closed("rotation_A") is True)
    check("rotation_B は許可", T._allow_refresh_with_ws_closed("rotation_B") is True)
    check("on_open は許可しない", T._allow_refresh_with_ws_closed("on_open") is False)
    check("manual は許可しない", T._allow_refresh_with_ws_closed("manual") is False)

    # 明示的な opt-out は残す (障害切り分け用)
    os.environ["PUSH_ROTATION_REGISTER_WITH_WS_CLOSED"] = "0"
    check("env=0 で明示的に従来動作へ戻せる",
          T._allow_refresh_with_ws_closed("rotation_A") is False)
    _clear_env()
    D._set_defaults(D.PUSH_DEFAULTS)


# ============================================================
# [3] WS が閉じていても rotation の refresh が実行される
# ============================================================

def test_refresh_runs_with_ws_closed() -> None:
    section("[3] WS 閉のまま rotation refresh が実際に呼ばれる (storm の直接原因)")

    from core.startup.runtime_env import defaults as D
    from trading.push.push_stream import state
    from trading.push.push_stream import transport as T

    _clear_env()
    D._set_defaults(D.PUSH_DEFAULTS)

    calls = {"n": 0, "reason": None, "symbols": None}

    def fake_refresh(*args, **kwargs):
        calls["n"] += 1
        calls["reason"] = kwargs.get("reason")
        calls["symbols"] = kwargs.get("symbols")
        return True

    saved_fn = state._refresh_callable
    saved_ws = state._ws_app
    was_set = state._connected_event.is_set()
    saved_last_started = getattr(T, "_last_refresh_started_ts", 0.0)
    saved_last_done = getattr(T, "_last_refresh_done_ts", 0.0)
    try:
        # WS は完全に閉じている状態を作る (storm 中と同じ)
        state._refresh_callable = fake_refresh
        state._ws_app = None
        state._connected_event.clear()
        T._last_refresh_started_ts = 0.0
        T._last_refresh_done_ts = 0.0

        check("前提: WS は閉じている", T._is_ws_alive() is False)

        result = T._call_refresh(force=True, reason="rotation_A", symbols=["7203", "9984"])
        check("rotation_A の refresh が実際に呼ばれた", calls["n"] == 1, "calls=%d" % calls["n"])
        check("reason が伝わる", calls["reason"] == "rotation_A", str(calls["reason"]))
        check("戻り値が None にならない (ok=False 化しない)", result is True, repr(result))

        # on_open は従来どおり skip (WS 送信に依存するため)
        calls["n"] = 0
        T._last_refresh_started_ts = 0.0
        T._last_refresh_done_ts = 0.0
        result2 = T._call_refresh(force=True, reason="on_open", symbols=["7203"])
        check("on_open は WS 閉なら従来どおり skip", calls["n"] == 0 and result2 is None,
              "calls=%d result=%r" % (calls["n"], result2))
    finally:
        state._refresh_callable = saved_fn
        state._ws_app = saved_ws
        if was_set:
            state._connected_event.set()
        else:
            state._connected_event.clear()
        T._last_refresh_started_ts = saved_last_started
        T._last_refresh_done_ts = saved_last_done


# ============================================================
# [4] skip されたときは握り潰さず ERROR で残す
# ============================================================

def test_skip_is_loud() -> None:
    section("[4] rotation 登録が skip されたら ERROR で理由が残る")

    from trading.push.push_stream import state
    from trading.push.push_stream import transport as T

    handler = CapturingHandler()
    log = logging.getLogger("trading.push.push_stream.transport")
    log.addHandler(handler)
    prev_level = log.level
    log.setLevel(logging.DEBUG)

    saved_fn = state._refresh_callable
    saved_ws = state._ws_app
    was_set = state._connected_event.is_set()
    try:
        os.environ["PUSH_ROTATION_REGISTER_WITH_WS_CLOSED"] = "0"  # 旧設定を再現
        state._refresh_callable = lambda *a, **k: True
        state._ws_app = None
        state._connected_event.clear()

        result = T._call_refresh(force=True, reason="rotation_A", symbols=["7203"])
        check("旧設定では従来どおり skip される", result is None, repr(result))
        text = handler.text()
        check("ERROR レベルで記録される", "ERROR|" in text,
              [r[:40] for r in handler.records][:3])
        check("reconnect storm になると明記される", "reconnect storm" in text)
        check("直し方 (env=1) が書いてある", "PUSH_ROTATION_REGISTER_WITH_WS_CLOSED=1" in text)
    finally:
        log.removeHandler(handler)
        log.setLevel(prev_level)
        state._refresh_callable = saved_fn
        state._ws_app = saved_ws
        if was_set:
            state._connected_event.set()
        else:
            state._connected_event.clear()
        _clear_env()
        from core.startup.runtime_env import defaults as D
        D._set_defaults(D.PUSH_DEFAULTS)


# ============================================================
# [5] 登録が 0 件のときは backoff を短くする
# ============================================================

def test_backoff_shortened_when_no_registration() -> None:
    section("[5] 確定登録 0 件 (PUSH 全断) のときは 10 分待たない")

    os.environ["PUSH_ROTATION_FAILURE_BACKOFF_SEC"] = "180"
    os.environ["PUSH_ROTATION_FAILURE_BACKOFF_MAX_SEC"] = "600"
    os.environ.pop("PUSH_ROTATION_FAILURE_BACKOFF_EMPTY_MAX_SEC", None)

    from trading.push import registration_state as RS
    from trading.push.push_stream import rotation_core as RC

    RS.reset_for_test()
    try:
        check("前提: 確定登録は 0 件", RS.registered_count() == 0, str(RS.registered_count()))
        b1 = RC._rotation_failure_backoff_seconds(1)
        b5 = RC._rotation_failure_backoff_seconds(5)
        check("0 件なら初回 backoff は 30s 以下", b1 <= 30.0, "%.1fs" % b1)
        check("0 件なら失敗が続いても 30s 以下", b5 <= 30.0, "%.1fs" % b5)

        # 1 件でも登録できていれば従来どおり長い backoff (データは流れているため)
        RS.mark_registered_confirmed(["7203", "9984"], label="A", source="test")
        check("登録あり: registered_count=2", RS.registered_count() == 2, str(RS.registered_count()))
        c1 = RC._rotation_failure_backoff_seconds(1)
        c5 = RC._rotation_failure_backoff_seconds(5)
        check("登録ありなら従来どおり 180s", abs(c1 - 180.0) < 0.01, "%.1fs" % c1)
        check("登録ありなら上限 600s のまま", abs(c5 - 600.0) < 0.01, "%.1fs" % c5)
    finally:
        RS.reset_for_test()
        for k in ("PUSH_ROTATION_FAILURE_BACKOFF_SEC", "PUSH_ROTATION_FAILURE_BACKOFF_MAX_SEC"):
            os.environ.pop(k, None)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s:%(name)s:%(message)s")
    for t in (test_shipped_default, test_allow_flag,
              test_refresh_runs_with_ws_closed, test_skip_is_loud,
              test_backoff_shortened_when_no_registration):
        try:
            t()
        except Exception:
            FAIL += 1
            FAILURES.append("%s raised" % t.__name__)
            print(f"  FAIL {t.__name__} raised")
            import traceback
            traceback.print_exc()

    print()
    print("=" * 70)
    print(f"PASS={PASS} FAIL={FAIL}")
    for f in FAILURES:
        print("  -", f)
    print("=" * 70)
    sys.exit(1 if FAIL else 0)
