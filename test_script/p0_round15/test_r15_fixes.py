"""第15ラウンド dry-run テスト.

実注文なし。NAS 本番 DB は読み取り専用。

対象:
  P0-2   PUSH OWNER HEALTH の項目が実際に解決される (前回は全部 None)
  P0-3   WS 停止 / writer 停止 / owner 死亡 の切り分け
  追加P0-A  Tonosama fresh summary 無し -> fail-close
  追加P0-B  stale ranking bootstrap を realtime global へ出さない
"""
from __future__ import annotations

import io
import logging
import os
import sys
import traceback

ROOT = os.environ.get(
    "KABU_SRC_ROOT",
    r"W:\BUSINESS\事業\04_株式自動売買システム\ソースコード",
)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ["AUTOSTOCK_DRY_RUN"] = "1"
os.environ["ALLOW_ORDERS"] = "0"

RESULTS: list = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def section(t: str) -> None:
    print()
    print("=" * 74)
    print(t)
    print("=" * 74)


# ============================================================
# P0-2 / P0-3
# ============================================================

def test_health_fields_resolve() -> None:
    section("P0-2  PUSH OWNER HEALTH が実際の state 属性を読む")
    import data_collectors.push_runtime as pr
    from trading.push.push_stream import state as st

    # 前回 None だった原因は属性名の推測ミス。実在を確かめる。
    for attr in ("_connected_event", "_ws_thread", "_flush_thread", "_push_queue",
                 "_push_df", "_last_message_at", "_last_flush_at", "_last_connect_at",
                 "_last_disconnect_at", "_total_received", "_total_flushed",
                 "_total_dropped", "_total_errors", "_stream_writer"):
        check(f"state.{attr} が存在する", hasattr(st, attr), "")

    # 値を入れてスナップショットに反映されるか
    import datetime as dt

    st._total_received = 17182
    st._total_flushed = 17004
    st._total_dropped = 0
    st._last_message_at = dt.datetime.now()
    snap = pr._push_health_snapshot()
    check("received_total が読める", snap.get("received_total") == 17182,
          str(snap.get("received_total")))
    check("written_total が読める", snap.get("written_total") == 17004,
          str(snap.get("written_total")))
    check("last_recv_age が数値になる",
          isinstance(snap.get("last_recv_age"), float), str(snap.get("last_recv_age")))
    check("ws_connected が bool になる",
          isinstance(snap.get("ws_connected"), bool), str(snap.get("ws_connected")))
    check("queue が読める", snap.get("queue") is not None, str(snap.get("queue")))
    check("uptime_sec がある", snap.get("uptime_sec") is not None, str(snap.get("uptime_sec")))

    for k in ("flush_thread_alive", "writer_ready", "memory_rows",
              "dropped_total", "error_total", "rotation_enabled"):
        check(f"snapshot に {k} がある", k in snap, "")


def test_fault_classification_matches_13_43() -> None:
    section("P0-3  13:43 停止を case B (writer_problem) と判定できる")
    import data_collectors.push_runtime as pr

    # 2026-08-28 13:45:04 の実測値をそのまま使う:
    #   recv は増え続け (17166 -> 17182)、flushed は 17004 で固着、queue は増加
    observed = {"last_recv_age": 1.0, "last_write_age": 95.0}
    check("recv fresh / write stale -> writer_problem",
          pr._classify_push_fault(observed) == "writer_problem",
          pr._classify_push_fault(observed))

    check("recv stale / write stale -> websocket_or_subscription_problem",
          pr._classify_push_fault({"last_recv_age": 60.0, "last_write_age": 60.0})
          == "websocket_or_subscription_problem")
    check("両方 fresh -> owner_ok",
          pr._classify_push_fault({"last_recv_age": 1.0, "last_write_age": 1.0})
          == "owner_ok")
    check("owner 情報なし -> websocket_or_subscription_problem (D 相当)",
          pr._classify_push_fault({"last_recv_age": None, "last_write_age": None})
          == "websocket_or_subscription_problem")


# ============================================================
# 追加P0-A  Tonosama fail-close
# ============================================================

def test_tonosama_fail_close() -> None:
    section("追加P0-A  fresh summary 無しなら Tonosama body を呼ばない")
    import trading.entry.tonosama.fresh_summary_wait as fw

    src = io.open(os.path.join(ROOT, "trading", "entry", "tonosama",
                               "fresh_summary_wait.py"), encoding="utf-8").read()
    check("fail_open_empty の既定が False になっている",
          'TONOSAMA_WAIT_PUSH_SUMMARY_FAIL_OPEN_IF_EMPTY", False' in src, "")
    check("latest=None で fail-close する分岐がある",
          "fail-close (Tonosama body を呼ばない)" in src, "")

    # 実際に呼んで False になること (fresh summary は無い状態)
    os.environ.pop("TONOSAMA_WAIT_PUSH_SUMMARY_FAIL_OPEN_IF_EMPTY", None)
    os.environ["TONOSAMA_WAIT_PUSH_SUMMARY_WAIT_SEC"] = "0"
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    lg = logging.getLogger("trading.entry.tonosama.fresh_summary_wait")
    lg.addHandler(h)
    old = lg.level
    lg.setLevel(logging.INFO)
    try:
        fn = None
        for cand in ("_patched_wait_fresh_push_summary_before_tonosama",
                     "wait_for_fresh_push_summary", "_wait_for_fresh_push_summary"):
            fn = getattr(fw, cand, None)
            if callable(fn):
                break
        if fn is None:
            check("wait 関数が見つかる", False, "not found")
        else:
            got = fn()
            check("fresh summary 無し -> False (fail-close)", got is False, f"got={got}")
            out = buf.getvalue()
            check("fail-open ログが出ない", "fail-open to Tonosama body" not in out,
                  out[:160])
    finally:
        lg.removeHandler(h)
        lg.setLevel(old)
        os.environ.pop("TONOSAMA_WAIT_PUSH_SUMMARY_WAIT_SEC", None)

    # 明示的に戻せること
    os.environ["TONOSAMA_WAIT_PUSH_SUMMARY_FAIL_OPEN_IF_EMPTY"] = "1"
    try:
        check("env=1 で従来の fail-open へ戻せる",
              fw._env_bool("TONOSAMA_WAIT_PUSH_SUMMARY_FAIL_OPEN_IF_EMPTY", False) is True)
    finally:
        os.environ.pop("TONOSAMA_WAIT_PUSH_SUMMARY_FAIL_OPEN_IF_EMPTY", None)


# ============================================================
# 追加P0-B  stale ranking bootstrap を realtime へ出さない
# ============================================================

def test_ranking_realtime_guard() -> None:
    section("追加P0-B  stale ranking bootstrap を realtime global へ publish しない")
    import numpy as np
    import pandas as pd
    from global_state import global_data, GlobalDataCompat as G
    from core.global_context.context import global_context as GC

    published: list = []
    # GC には set_ranking_merged_summary が無く、
    # global_state 側は set_merged_summary(source="ranking") へ委譲する。
    # 実際に realtime へ出る経路 = GC.set_merged_summary を見張る。
    orig = GC.set_merged_summary

    def spy(interval, df, *a, **k):
        published.append((interval, len(df)))

    GC.set_merged_summary = spy
    try:
        # --- stale bootstrap (2026-08-28 の実測と同じ形) ---
        old_ts = pd.Timestamp.now() - pd.Timedelta(hours=3)
        n_sym = 1000
        stale = pd.DataFrame({
            "symbol": [str(1000 + i % n_sym) for i in range(33650)],
            "datetime": [old_ts] * 33650,
            "close": np.random.rand(33650) * 100,
        })
        published.clear()
        global_data.set_ranking_merged_summary(3, stale)
        check("3 時間古い 33,650 行は realtime へ publish されない",
              len(published) == 0, f"published={published}")
        hist = getattr(global_data, "ranking_history_merged_3", None)
        check("履歴側 (ranking_history_merged_3) には保持される",
              hist is not None and len(hist) == 33650,
              f"rows={None if hist is None else len(hist)}")

        # --- fresh なら通す ---
        fresh_ts = pd.Timestamp.now()
        fresh = pd.DataFrame({
            "symbol": [str(1000 + i % 200) for i in range(800)],
            "datetime": [fresh_ts] * 800,
            "close": np.random.rand(800) * 100,
        })
        published.clear()
        global_data.set_ranking_merged_summary(3, fresh)
        check("fresh なら publish される", len(published) == 1, f"published={published}")
        if published:
            check("1 symbol = 1 row へ畳まれている (800 -> 200)",
                  published[0][1] == 200, f"rows={published[0][1]}")

        # --- latest 不明なら出さない (fail-close) ---
        published.clear()
        nodt = pd.DataFrame({"symbol": ["1", "2"], "close": [1.0, 2.0]})
        global_data.set_ranking_merged_summary(3, nodt)
        check("latest 不明なら publish しない (fail-close)",
              len(published) == 0, f"published={published}")

        # --- env で緩められる (既定は締める) ---
        os.environ["RANKING_REALTIME_MAX_AGE_SEC"] = "999999"
        published.clear()
        try:
            global_data.set_ranking_merged_summary(3, stale)
            check("RANKING_REALTIME_MAX_AGE_SEC を伸ばせば従来動作",
                  len(published) == 1, f"published={published}")
        finally:
            os.environ.pop("RANKING_REALTIME_MAX_AGE_SEC", None)
    finally:
        GC.set_merged_summary = orig


def main() -> int:
    tests = [
        test_health_fields_resolve,
        test_fault_classification_matches_13_43,
        test_tonosama_fail_close,
        test_ranking_realtime_guard,
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
