"""P0 修正の dry-run / mock テスト.

実注文・実返済注文は一切行わない。
すべて mock / 一時 DB / メモリ上のフレームだけで完結する。

対象:
  P0-2  DB read failure != positions empty
  P0-4  positions DB retry storm (bounded exponential backoff)
  P0-7  cancel から実停止まで <= 5s
  P0-9  realtime での 22851 rows 処理 = 0 / REALTIME_HISTORY_LEAK
  P0-11 stale フレームで fresh を上書きしない
  P0-12 EXIT TEMP STOP 誤判定 = 0
  P0-14 positions fail-safe A/B/C/D
  P0-1  runtime_state_store の接続リーク
"""
from __future__ import annotations

import ctypes
import datetime as dt
import gc
import os
import sqlite3
import sys
import tempfile
import time
import traceback
import types

ROOT = os.environ.get(
    "KABU_SRC_ROOT",
    r"W:\BUSINESS\事業\04_株式自動売買システム\ソースコード",
)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# 実発注を絶対にさせない。import 時点で握っておく。
os.environ["AUTOSTOCK_DRY_RUN"] = "1"
os.environ["ALLOW_ORDERS"] = "0"

RESULTS: list = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}" + (f"  -- {detail}" if detail else ""))


def section(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


# ============================================================
# P0-12  EXIT TEMP STOP
# ============================================================

def test_exit_temp_stop() -> None:
    section("P0-12  EXIT TEMP STOP 誤判定")
    from trading.handlers import exit_handler as eh

    cases = [
        # (AUTOSTOCK_TEMP_DISABLE_EXIT, EXIT_TEMP_STOP, expected_disabled)
        (None, None, False),          # 本番で実際に起きていた状態
        ("", "", False),
        ("0", "0", False),
        ("false", "false", False),
        ("off", "off", False),
        ("no", "no", False),
        ("none", "none", False),
        ("1", None, True),
        ("true", None, True),
        ("on", None, True),
        ("yes", None, True),
        (None, "1", True),
        (None, "true", True),
        ("0", "1", True),
        ("garbage", None, False),     # 未知の値は EXIT を止めない側へ
    ]
    for a, b, expected in cases:
        for key, val in (("AUTOSTOCK_TEMP_DISABLE_EXIT", a), ("EXIT_TEMP_STOP", b)):
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
        got = eh._bfe_exit_temp_disabled()
        check(
            f"exit_temp_disabled(AUTOSTOCK={a!r}, EXIT_TEMP_STOP={b!r}) == {expected}",
            got == expected,
            f"got={got}",
        )
    os.environ.pop("AUTOSTOCK_TEMP_DISABLE_EXIT", None)
    os.environ.pop("EXIT_TEMP_STOP", None)


# ============================================================
# P0-9  REALTIME_HISTORY_LEAK
# ============================================================

def test_realtime_history_guard() -> None:
    section("P0-9  realtime 22851 rows guard")
    import pandas as pd
    from trading.summary import realtime_history_guard as g

    g.reset_stats()

    now = pd.Timestamp.now().normalize() + pd.Timedelta(hours=14)
    symbols = [f"{1000 + i}" for i in range(349)]
    rows = []
    for s in symbols:
        for k in range(65):
            rows.append({
                "symbol": s,
                "datetime": now - pd.Timedelta(minutes=64 - k),
                "close": 100.0 + k,
            })
    hist = pd.DataFrame(rows)
    check("履歴フレームを作れた rows=22685", len(hist) == 349 * 65, f"rows={len(hist)}")

    out = g.to_latest_projection(hist, caller="unittest.realtime", interval=1)
    check("22,685 -> 349 行へ畳まれた", len(out) == 349, f"rows_after={len(out)}")
    check("銘柄数が保存されている", out["symbol"].nunique() == 349,
          f"symbols={out['symbol'].nunique()}")
    check("latest が最新足になっている",
          pd.to_datetime(out["datetime"]).max() == pd.to_datetime(hist["datetime"]).max(),
          f"latest={pd.to_datetime(out['datetime']).max()}")

    stats = g.leak_stats()
    check("REALTIME_HISTORY_LEAK が記録された", stats["total"] == 1, f"stats={stats['total']}")
    check("caller が記録された", "unittest.realtime" in stats["by_caller"],
          str(stats["by_caller"]))
    last = stats["last"]
    check("rows_before/rows_after/latest_before/latest_after を記録",
          all(k in last for k in ("rows_before", "rows_after", "latest_before", "latest_after")),
          str(sorted(last.keys())))

    # 既に projection 済みのフレームは触らない
    g.reset_stats()
    latest_only = out.copy()
    out2 = g.to_latest_projection(latest_only, caller="unittest.already_projected")
    check("349 行 (閾値以下) は素通し", len(out2) == len(latest_only) and g.leak_stats()["total"] == 0,
          f"rows={len(out2)} leaks={g.leak_stats()['total']}")

    # 過去日混在
    g.reset_stats()
    yday = hist.copy()
    yday["datetime"] = pd.to_datetime(yday["datetime"]) - pd.Timedelta(days=1)
    mixed = pd.concat([yday, hist], ignore_index=True)
    out3 = g.to_latest_projection(mixed, caller="unittest.mixed_days")
    check("過去日を含む 45,370 行 -> 当日 349 行", len(out3) == 349, f"rows_after={len(out3)}")

    check("is_history_frame(22685 rows) == True", g.is_history_frame(hist))
    check("is_history_frame(349 rows) == False", not g.is_history_frame(out))


# ============================================================
# P0-11  stale が fresh を上書きしない
# ============================================================

def test_push_df_monotonic_guard() -> None:
    section("P0-11  push_df 単調フレッシュネス")
    import pandas as pd
    from global_state import global_data

    def frame(latest: str, rows: int):
        base = pd.Timestamp(latest)
        return pd.DataFrame({
            "symbol": [str(7000 + i) for i in range(rows)],
            "datetime": [base - pd.Timedelta(seconds=i) for i in range(rows)],
            "price": [100.0 + i for i in range(rows)],
        })

    old = frame("2026-08-27 14:08:10", 644)
    new = frame("2026-08-27 15:24:04", 5000)

    global_data.push_df = old
    check("初回は素直に採用される",
          len(global_data.push_df) == 644,
          f"rows={len(global_data.push_df)}")

    global_data.push_df = new
    check("新しいフレームは採用される",
          len(global_data.push_df) == 5000,
          f"rows={len(global_data.push_df)}")

    # 本番で起きていた逆流
    global_data.push_df = old
    got = global_data.push_df
    check("古い 14:08:10 フレームでの上書きは拒否される",
          len(got) == 5000
          and pd.to_datetime(got["datetime"]).max() == pd.Timestamp("2026-08-27 15:24:04"),
          f"rows={len(got)} latest={pd.to_datetime(got['datetime']).max()}")

    # 同じ latest でより少ない行数
    same_small = frame("2026-08-27 15:24:04", 100)
    global_data.push_df = same_small
    check("同じ latest でより少ない行数の上書きも拒否",
          len(global_data.push_df) == 5000, f"rows={len(global_data.push_df)}")

    # latest 不明フレーム
    global_data.push_df = pd.DataFrame({"a": [1, 2, 3]})
    check("latest 不明フレームで潰されない",
          len(global_data.push_df) == 5000, f"rows={len(global_data.push_df)}")

    # より新しいものは通る
    newer = frame("2026-08-27 15:30:52", 200)
    global_data.push_df = newer
    check("さらに新しいフレームは採用される",
          pd.to_datetime(global_data.push_df["datetime"]).max()
          == pd.Timestamp("2026-08-27 15:30:52"),
          f"latest={pd.to_datetime(global_data.push_df['datetime']).max()}")


# ============================================================
# P0-14 / P0-2  positions fail-safe A/B/C/D
# ============================================================

def _install_position_mocks(*, db_mode: str, broker_mode: str, broker_symbols=None):
    """db_mode: ok_empty / ok_with / io_error
       broker_mode: ok_empty / ok_with / ng"""
    import trading.position.position_state_service as pss

    broker_symbols = broker_symbols or []

    def fake_db():
        if db_mode == "io_error":
            raise sqlite3.OperationalError("disk I/O error")
        if db_mode == "ok_with":
            return {"9999": {"symbol": "9999", "qty": 100, "entry_price": 500.0,
                             "side": "BUY", "_position_source": "DB.positions"}}
        return {}

    def fake_broker():
        if broker_mode == "ng":
            return {}, False, "HTTP 500 / connection refused"
        out = {}
        for s in broker_symbols:
            out[s] = {"symbol": s, "qty": 100, "entry_price": 500.0, "side": "BUY",
                      "_position_source": "BROKER.kabu.credit"}
        return out, True, ""

    pss._read_db_positions_raising = fake_db
    pss._read_broker_positions = fake_broker
    pss.reset_for_test()
    return pss


def test_positions_failsafe() -> None:
    section("P0-14  positions fail-safe A/B/C/D (実注文なし)")
    os.environ["POSITION_STATE_CACHE_TTL_SEC"] = "0.2"

    # ---- A: DB OK / broker empty ----
    pss = _install_position_mocks(db_mode="ok_empty", broker_mode="ok_empty")
    st = pss.get_positions_state(force=True, caller="test.A")
    check("A: db_ok=True", st.db_ok)
    check("A: broker_ok=True", st.broker_ok)
    check("A: positions_known=True", st.positions_known)
    check("A: entry_allowed=True", st.entry_allowed)
    check("A: count=0", st.count == 0, f"count={st.count}")
    check("A: source_used=db+broker", st.source_used == pss.SOURCE_DB_BROKER, st.source_used)

    # ---- B: DB disk I/O / broker empty OK ----
    pss = _install_position_mocks(db_mode="io_error", broker_mode="ok_empty")
    st = pss.get_positions_state(force=True, caller="test.B")
    check("B: db_ok=False", not st.db_ok)
    check("B: broker_ok=True", st.broker_ok)
    check("B: broker authoritative empty を採用", st.source_used == pss.SOURCE_BROKER,
          st.source_used)
    check("B: positions_known=True (broker で確定)", st.positions_known)
    check("B: entry_allowed=True", st.entry_allowed)
    check("B: count=0", st.count == 0, f"count={st.count}")
    check("B: disk I/O error を握り潰していない (db_error が残る)",
          "disk I/O error" in st.db_error, st.db_error)

    # ---- C: DB disk I/O / broker has position ----
    pss = _install_position_mocks(db_mode="io_error", broker_mode="ok_with",
                                  broker_symbols=["7203"])
    st = pss.get_positions_state(force=True, caller="test.C")
    check("C: db_ok=False", not st.db_ok)
    check("C: broker の建玉を保持している", "7203" in st.positions, str(st.symbols))
    check("C: count=1 (EXIT 対象になる)", st.count == 1, f"count={st.count}")
    check("C: source_used=broker_authoritative", st.source_used == pss.SOURCE_BROKER,
          st.source_used)
    check("C: entry_allowed=True (状態は確定している)", st.entry_allowed)

    # ---- D: DB disk I/O / broker NG ----
    # 先に C で last-good (7203) を作ってから D にする。
    import trading.position.position_state_service as _pss_mod

    def fake_db_err():
        raise sqlite3.OperationalError("disk I/O error")

    def fake_broker_ng():
        return {}, False, "connection refused"

    _pss_mod._read_db_positions_raising = fake_db_err
    _pss_mod._read_broker_positions = fake_broker_ng
    st = _pss_mod.get_positions_state(force=True, caller="test.D")
    check("D: db_ok=False", not st.db_ok)
    check("D: broker_ok=False", not st.broker_ok)
    check("D: positions_known=False (unknown)", not st.positions_known)
    check("D: entry_allowed=False (ENTRY 禁止 / fail-closed)", not st.entry_allowed)
    check("D: last-good を保持している (0 件にしない)",
          "7203" in st.positions and st.count == 1,
          f"count={st.count} symbols={st.symbols}")
    check("D: source_used=last_good", st.source_used == pss.SOURCE_LAST_GOOD, st.source_used)

    # POSITIONS_STATE のログ項目が全部あるか
    d = st.to_log_dict()
    need = {"db_ok", "broker_ok", "source_used", "last_good_age", "entry_allowed"}
    check("POSITIONS_STATE ログ項目が揃っている", need.issubset(d.keys()),
          str(sorted(d.keys())))

    os.environ.pop("POSITION_STATE_CACHE_TTL_SEC", None)


# ============================================================
# P0-4  retry storm 防止
# ============================================================

def test_backoff_no_retry_storm() -> None:
    section("P0-4  positions DB retry storm 防止 (singleflight + backoff)")
    os.environ["POSITION_STATE_CACHE_TTL_SEC"] = "0.05"
    os.environ["POSITION_STATE_DB_BACKOFF_SEC"] = "1,2,5,10,30"

    import trading.position.position_state_service as pss

    db_calls = {"n": 0}
    broker_calls = {"n": 0}

    def fake_db():
        db_calls["n"] += 1
        raise sqlite3.OperationalError("disk I/O error")

    def fake_broker():
        broker_calls["n"] += 1
        return {}, True, ""

    pss._read_db_positions_raising = fake_db
    pss._read_broker_positions = fake_broker
    pss.reset_for_test()

    t0 = time.perf_counter()
    n = 0
    while time.perf_counter() - t0 < 3.0:
        pss.get_positions_state(caller="test.storm")
        n += 1
        time.sleep(0.01)
    el = time.perf_counter() - t0

    # 旧実装なら毎回 DB を叩いて数百回。backoff 後は 1,2 秒間隔なので数回。
    check(f"3 秒で consumer 呼び出し {n} 回に対し DB read は {db_calls['n']} 回",
          db_calls["n"] <= 4, f"db_calls={db_calls['n']} consumer_calls={n}")
    check("DB 障害中でも broker の安全確認は続いている",
          broker_calls["n"] >= 3, f"broker_calls={broker_calls['n']}")
    check("consumer 呼び出し数 >> DB read 数 (singleflight+cache が効いている)",
          n > db_calls["n"] * 10, f"{n} vs {db_calls['n']}")

    s = pss.stats()
    check("db_errors が記録されている", s["db_errors"] >= 1, str(s["db_errors"]))
    check("first/last db error 時刻が記録されている",
          s["first_db_error_at"] is not None and s["last_db_error_at"] is not None,
          f"{s['first_db_error_at']} .. {s['last_db_error_at']}")
    check("独立障害エピソード数が記録されている",
          s["db_error_episodes"] == 1, str(s["db_error_episodes"]))

    os.environ.pop("POSITION_STATE_CACHE_TTL_SEC", None)
    os.environ.pop("POSITION_STATE_DB_BACKOFF_SEC", None)
    print(f"    (計測: {el:.2f}s, consumer={n}, db_read={db_calls['n']}, broker={broker_calls['n']})")


# ============================================================
# P0-7  cancel latency
# ============================================================

def test_cancel_latency() -> None:
    section("P0-7  cancel から実停止まで <= 5s")
    os.environ["SUMMARY_WORKER_CANCEL_SLA_SEC"] = "5"
    os.environ["SUMMARY_WORKER_HARD_CANCEL"] = "1"
    os.environ["SUMMARY_WORKER_HEARTBEAT_SEC"] = "0"

    import threading

    from scheduler_jobs.summary import stage_tracker as st

    stopped_at = {"t": None}
    started = threading.Event()
    ctx_box = {}

    def worker_body():
        try:
            with st.worker("test:cancel", generation=1, owner="test", budget=60.0) as ctx:
                ctx_box["ctx"] = ctx
                started.set()
                # 本番の light_push_runner 相当: 複数 stage を順に回す
                for i in range(200):
                    with st.stage(f"heavy_stage_{i}"):
                        time.sleep(0.2)
        finally:
            stopped_at["t"] = time.perf_counter()

    t = threading.Thread(target=worker_body, name="test-cancel-worker", daemon=True)
    t.start()
    started.wait(5)
    time.sleep(0.5)

    cancel_at = time.perf_counter()
    st.cancel(ctx_box["ctx"], reason="superseded_by_tick:test")
    t.join(20)

    latency = (stopped_at["t"] or time.perf_counter()) - cancel_at
    check(f"cancel から実停止まで {latency:.2f}s <= 5s", latency <= 5.0, f"{latency:.3f}s")
    check("スレッドが終了している", not t.is_alive())
    check("SLA 違反フラグが立っていない", not ctx_box["ctx"].cancel_sla_breached)

    # checkpoint() でも降りられること
    ctx_box2 = {}
    started2 = threading.Event()
    stopped2 = {"t": None}

    def worker_body2():
        try:
            with st.worker("test:checkpoint", generation=2, owner="test", budget=60.0) as ctx:
                ctx_box2["ctx"] = ctx
                started2.set()
                with st.stage("one_long_stage"):
                    for i in range(2000):
                        st.checkpoint(f"loop_{i}")
                        time.sleep(0.01)
        finally:
            stopped2["t"] = time.perf_counter()

    t2 = threading.Thread(target=worker_body2, name="test-checkpoint-worker", daemon=True)
    t2.start()
    started2.wait(5)
    time.sleep(0.3)
    cancel_at2 = time.perf_counter()
    st.cancel(ctx_box2["ctx"], reason="superseded")
    t2.join(20)
    latency2 = (stopped2["t"] or time.perf_counter()) - cancel_at2
    check(f"stage 内 checkpoint でも {latency2:.3f}s で降りる", latency2 <= 5.0, f"{latency2:.3f}s")

    for k in ("SUMMARY_WORKER_CANCEL_SLA_SEC", "SUMMARY_WORKER_HARD_CANCEL",
              "SUMMARY_WORKER_HEARTBEAT_SEC"):
        os.environ.pop(k, None)


# ============================================================
# P0-1  runtime_state_store 接続リーク
# ============================================================

def _handles() -> int:
    try:
        from ctypes import wintypes

        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k.GetProcessHandleCount.restype = wintypes.BOOL
        k.GetCurrentProcess.restype = wintypes.HANDLE
        n = wintypes.DWORD(0)
        if k.GetProcessHandleCount(k.GetCurrentProcess(), ctypes.byref(n)):
            return int(n.value)
    except Exception:
        pass
    return -1


def test_runtime_state_no_conn_leak() -> None:
    section("P0-1  runtime_state_store の接続リーク (NAS は触らない)")
    from trading.runtime_persistence import runtime_state_store as rss

    tmp = tempfile.mkdtemp(prefix="rss_test_")
    rss.BASE_DIR = tmp
    rss._SCHEMA_DONE.clear()

    path = rss.ensure_runtime_state_db("20260827")
    check("schema を作れた", os.path.exists(path), path)

    gc.collect()
    base_handles = _handles()
    base = rss.connection_stats()

    N = 300
    for i in range(N):
        rss.save_position_state(
            symbol=f"{7000 + (i % 5)}",
            side="BUY",
            entry_time=dt.datetime(2026, 8, 27, 13, 0, 0),
            entry_price=100.0,
            current_price=101.0,
            highest_since_entry=102.0,
            lowest_since_entry=99.0,
            qty=100,
            status="OPEN",
            trade_date="20260827",
        )
    gc.collect()
    after = rss.connection_stats()
    after_handles = _handles()

    opened = after["opened"] - base["opened"]
    check(f"{N} 回保存で接続 {opened} 本 (schema 再作成なし: 1 回 1 本)",
          opened == N, f"opened={opened}")
    check("outstanding == 0 (全部 close されている)",
          after["outstanding"] == 0, f"outstanding={after['outstanding']}")
    if base_handles > 0 and after_handles > 0:
        delta = after_handles - base_handles
        check(f"Win32 ハンドル増加 {delta} <= 20", delta <= 20,
              f"{base_handles} -> {after_handles}")
    else:
        print("    (handle count 取得不可 -- skip)")

    rows = rss.load_open_positions("20260827")
    check("保存した建玉を読み戻せる", len(rows) == 5, f"rows={len(rows)}")

    after2 = rss.connection_stats()
    check("読み出しでも outstanding == 0", after2["outstanding"] == 0,
          f"outstanding={after2['outstanding']}")


# ============================================================
# P0-2  is_symbol_in_open_position の fail-closed
# ============================================================

def test_entry_fail_closed() -> None:
    section("P0-2 / 完成条件3  unknown position で ENTRY 禁止")
    import trading.position.open_position_sync as ops
    import trading.position.position_state_service as pss

    def fake_db_err():
        raise sqlite3.OperationalError("disk I/O error")

    def fake_broker_ng():
        return {}, False, "connection refused"

    pss._read_db_positions_raising = fake_db_err
    pss._read_broker_positions = fake_broker_ng
    pss.reset_for_test()
    os.environ["POSITION_STATE_CACHE_TTL_SEC"] = "0.05"

    st = pss.get_positions_state(force=True, caller="test.failclosed")
    check("建玉状態 unknown", not st.positions_known)
    check("entry_allowed=False", not st.entry_allowed)
    check("entry_allowed() ヘルパも False", not pss.entry_allowed(caller="test"))

    got = ops.is_symbol_in_open_position("7203")
    check("unknown 時は is_symbol_in_open_position -> True (二重建玉を防ぐ)", got is True,
          f"got={got}")

    # 復旧したら通常判定へ戻る
    def fake_db_ok():
        return {}

    def fake_broker_ok():
        return {}, True, ""

    pss._read_db_positions_raising = fake_db_ok
    pss._read_broker_positions = fake_broker_ok
    pss.reset_for_test()
    st2 = pss.get_positions_state(force=True, caller="test.recovered")
    check("復旧後は entry_allowed=True", st2.entry_allowed)

    os.environ.pop("POSITION_STATE_CACHE_TTL_SEC", None)


# ============================================================
# P0-2  load_open_positions_from_db_strict は例外を投げる
# ============================================================

def test_strict_loader_raises() -> None:
    section("P0-2  DB read failure を {} で返さない")
    import trading.position.open_position_sync as ops

    orig = None
    try:
        import database

        orig = getattr(database, "Session_position", None)
    except Exception:
        pass

    class BoomSession:
        def __call__(self, *a, **k):
            return self

        def query(self, *a, **k):
            raise sqlite3.OperationalError("disk I/O error")

        def close(self):
            return None

    import database

    database.Session_position = BoomSession()
    try:
        raised = False
        try:
            ops.load_open_positions_from_db_strict()
        except ops.PositionsDbUnavailable as e:
            raised = True
            check("strict は PositionsDbUnavailable を投げる", True, str(e)[:80])
        except Exception as e:  # noqa: BLE001
            check("strict は PositionsDbUnavailable を投げる", False,
                  f"別の例外: {type(e).__name__}")
        if not raised:
            check("strict は PositionsDbUnavailable を投げる", False, "例外が出なかった")

        # 互換版は {} を返すが、db_read_ok() が False になる
        out = ops.load_open_positions_from_db()
        check("互換版は {} を返す", out == {}, str(out))
        check("db_read_ok() == False で empty と failure を区別できる",
              ops.db_read_ok() is False, str(ops.db_read_status()))
    finally:
        if orig is not None:
            database.Session_position = orig


# ============================================================
# MAIN
# ============================================================

def main() -> int:
    tests = [
        test_exit_temp_stop,
        test_realtime_history_guard,
        test_push_df_monotonic_guard,
        test_positions_failsafe,
        test_backoff_no_retry_storm,
        test_cancel_latency,
        test_runtime_state_no_conn_leak,
        test_entry_fail_closed,
        test_strict_loader_raises,
    ]
    for fn in tests:
        try:
            fn()
        except Exception:
            print(f"[FAIL] {fn.__name__} が例外で落ちた")
            traceback.print_exc()
            RESULTS.append((fn.__name__, False, "exception"))

    section("SUMMARY")
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"total={len(RESULTS)} passed={passed} failed={len(failed)}")
    for n in failed:
        print("  FAILED:", n)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
