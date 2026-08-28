"""第13ラウンド修正の dry-run テスト.

実注文・実返済注文は一切行わない。NAS の本番 DB は読み取り専用。

対象:
  P0-1  FAST_PATH_DECISION ログ / hit 率
  P0-2  main tick が heavy fallback を同期で回さない
  P0-3  cancel checkpoint (normalize_summary_df)
  P0-4/5 realtime projection の共通化と常時適用
  P0-6  setter enrich の予算打ち切り
  P0-7/8 pending terminal state (pending_moved_without_order 誤検出ゼロ)
  性能  sanitize_df の多重コピー除去
"""
from __future__ import annotations

import io
import logging
import os
import statistics
import sys
import threading
import time
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


def _frame(rows: int, cols: int = 250, symbols: int = 214):
    import numpy as np
    import pandas as pd

    d = {
        "symbol": [str(1000 + i % symbols) for i in range(rows)],
        "symbolname": ["名"] * rows,
        "datetime": pd.date_range("2026-08-28 09:00", periods=rows, freq="s"),
        "close": np.random.rand(rows) * 1000,
        "price": np.random.rand(rows) * 1000,
        "volume": np.random.rand(rows) * 100,
    }
    for c in range(max(0, cols - 6)):
        d[f"c{c}"] = np.random.rand(rows)
    return pd.DataFrame(d)


# ============================================================
# sanitize_df 多重コピー除去
# ============================================================

def test_sanitize_single_copy() -> None:
    section("性能  normalize_summary_df の sanitize が 1 回になった")
    import trading.summary.controller_utils as cu

    calls = {"real": 0}
    orig = cu.sanitize_df

    def counting(df, *a, **k):
        if not k.get("_pre_sanitized"):
            calls["real"] += 1
        return orig(df, *a, **k)

    cu.__dict__["sanitize_df"] = counting
    try:
        df = _frame(881)
        calls["real"] = 0
        out = cu.normalize_summary_df(df)
        n = calls["real"]
    finally:
        cu.__dict__["sanitize_df"] = orig

    check("実コピーは 1 回 (旧実装は 9 回)", n == 1, f"copies={n}")
    check("出力は従来どおり symbol/datetime を持つ",
          "symbol" in out.columns and "datetime" in out.columns)
    check("OHLC が揃う",
          all(c in out.columns for c in ("open", "high", "low", "close", "volume")))
    check("入力フレームは書き換えられない", "open" not in df.columns)

    for rows in (881, 7953):
        df = _frame(rows)
        t = []
        for _ in range(3):
            t0 = time.perf_counter()
            cu.normalize_summary_df(df)
            t.append(time.perf_counter() - t0)
        print(f"    rows={rows:6d} normalize_summary_df median={statistics.median(t) * 1000:8.1f}ms")


# ============================================================
# P0-4 / P0-5  realtime projection
# ============================================================

def test_realtime_projection_always() -> None:
    section("P0-4/P0-5  realtime merged は 1 symbol = 1 row")
    from global_state import GlobalDataCompat as G

    # 881 rows / 214 symbols (本番実測と同じ形)
    df = _frame(881, cols=20, symbols=214)
    out = G._realtime_projection(df, 1, "unittest-set_merged_summary")
    check("881 行 / 214 銘柄 -> 214 行へ畳まれる",
          len(out) == 214, f"rows={len(out)} symbols={out['symbol'].nunique()}")

    # 936 rows / 230 symbols
    df2 = _frame(936, cols=20, symbols=230)
    out2 = G._realtime_projection(df2, 1, "unittest-936")
    check("936 行 / 230 銘柄 -> 230 行", len(out2) == 230, f"rows={len(out2)}")

    # 既に 1 symbol = 1 row なら触らない
    df3 = _frame(214, cols=20, symbols=214)
    out3 = G._realtime_projection(df3, 1, "unittest-already")
    check("既に projection 済みなら素通し", len(out3) == 214, f"rows={len(out3)}")

    # 最新行が選ばれているか
    import pandas as pd

    latest_in = pd.to_datetime(df["datetime"]).max()
    latest_out = pd.to_datetime(out["datetime"]).max()
    check("最新 datetime が保存される", latest_in == latest_out,
          f"{latest_in} == {latest_out}")

    # 旧挙動へ戻せる
    os.environ["SUMMARY_MERGED_PROJECTION_ALWAYS"] = "0"
    try:
        out4 = G._realtime_projection(_frame(881, cols=20, symbols=214), 1, "unittest-off")
        check("SUMMARY_MERGED_PROJECTION_ALWAYS=0 で旧挙動 (閾値未満は畳まない)",
              len(out4) == 881, f"rows={len(out4)}")
    finally:
        os.environ.pop("SUMMARY_MERGED_PROJECTION_ALWAYS", None)


# ============================================================
# P0-6  setter enrich budget
# ============================================================

def test_setter_enrich_budget() -> None:
    section("P0-6  setter enrich が予算で打ち切られる")
    import trading.summary.summary_controller as sc
    from global_state import GlobalDataCompat as G

    df = _frame(214, cols=20, symbols=214)
    orig = sc._enrich
    os.environ["SUMMARY_SETTER_ENRICH_BUDGET_SEC"] = "0.3"
    os.environ["SUMMARY_SETTER_ENRICH_DEDUPE"] = "0"

    started = threading.Event()

    def slow_enrich(d, **k):
        started.set()
        time.sleep(10)          # cold NAS を模す
        return d

    sc._enrich = slow_enrich
    try:
        t0 = time.perf_counter()
        out = G._setter_enrich(df, 1, "unittest-slow")
        el = time.perf_counter() - t0
    finally:
        sc._enrich = orig

    check(f"10 秒かかる enrich が {el:.2f}s で打ち切られる", el < 2.0, f"{el:.2f}s")
    check("打ち切り時は入力をそのまま publish (値を捨てない)",
          len(out) == len(df), f"rows={len(out)}")
    check("enrich は実際に起動していた", started.is_set())

    # 速い enrich は通る
    def fast_enrich(d, **k):
        d = d.copy()
        d["ranking_score"] = 1.0
        return d

    sc._enrich = fast_enrich
    try:
        out2 = G._setter_enrich(df, 1, "unittest-fast")
    finally:
        sc._enrich = orig
        os.environ.pop("SUMMARY_SETTER_ENRICH_BUDGET_SEC", None)
        os.environ.pop("SUMMARY_SETTER_ENRICH_DEDUPE", None)
    check("予算内なら enrich 結果が反映される", "ranking_score" in out2.columns)


# ============================================================
# P0-3  cancel checkpoint
# ============================================================

def test_cancel_checkpoint_in_normalize() -> None:
    section("P0-3  normalize_summary_df に cancel の降り場所がある")
    from scheduler_jobs.summary import stage_tracker as st
    import trading.summary.controller_utils as cu

    os.environ["SUMMARY_WORKER_HARD_CANCEL"] = "1"
    os.environ["SUMMARY_WORKER_HEARTBEAT_SEC"] = "0"

    ctx_box = {}
    started = threading.Event()
    stopped = {"t": None}
    df = _frame(3000, cols=60, symbols=200)

    def body():
        try:
            with st.worker("test:normalize", generation=1, owner="t", budget=60.0) as ctx:
                ctx_box["ctx"] = ctx
                started.set()
                for _ in range(500):
                    cu.normalize_summary_df(df)
        finally:
            stopped["t"] = time.perf_counter()

    th = threading.Thread(target=body, daemon=True, name="t-normalize")
    th.start()
    started.wait(5)
    time.sleep(0.5)
    t_cancel = time.perf_counter()
    st.cancel(ctx_box["ctx"], reason="superseded")
    th.join(30)
    latency = (stopped["t"] or time.perf_counter()) - t_cancel

    check(f"cancel から {latency:.2f}s で降りる (<5s)", latency < 5.0, f"{latency:.3f}s")
    check("スレッドは終了している", not th.is_alive())

    for k in ("SUMMARY_WORKER_HARD_CANCEL", "SUMMARY_WORKER_HEARTBEAT_SEC"):
        os.environ.pop(k, None)


# ============================================================
# P0-2  main tick が heavy fallback を同期で回さない
# ============================================================

def test_no_sync_heavy_fallback() -> None:
    section("P0-2  tier1 miss で main tick が heavy を待たない")
    import pandas as pd
    import scheduler_jobs.summary.runner_core as rc

    rc.reset_fast_path_stats()
    orig_t1 = rc._job_summary_tier1_direct_memory
    orig_t2 = rc._job_summary_tier2_light_runner
    heavy_started = threading.Event()

    def empty_t1(now, run_entry=True):
        return pd.DataFrame()

    def slow_t2(interval, now, **kw):
        heavy_started.set()
        time.sleep(20)
        return pd.DataFrame()

    rc._job_summary_tier1_direct_memory = empty_t1
    rc._job_summary_tier2_light_runner = slow_t2
    rc._HEAVY_BG_LAST_AT = 0.0
    try:
        import datetime as dt

        t0 = time.perf_counter()
        out = rc.job_summary(1, display=False, now=dt.datetime.now(), run_entry=False)
        el = time.perf_counter() - t0
    finally:
        rc._job_summary_tier1_direct_memory = orig_t1
        rc._job_summary_tier2_light_runner = orig_t2

    check(f"tier1 miss でも main tick は {el:.3f}s で戻る (旧実装は 123s 待った)",
          el < 2.0, f"{el:.3f}s")
    check("空を返す (既存 merged を潰さない / Entry は fresh 無しで fail-closed)",
          isinstance(out, pd.DataFrame) and out.empty, f"rows={len(out)}")
    heavy_started.wait(3)
    check("heavy rebuild は background で走っている", heavy_started.is_set())

    st = rc.fast_path_stats()
    check("FAST_PATH の miss が計上される", st["misses"] >= 1, str(st))

    # 同期モードへ戻せる
    os.environ["SUMMARY_MAIN_SYNC_HEAVY_FALLBACK"] = "1"
    try:
        check("SUMMARY_MAIN_SYNC_HEAVY_FALLBACK=1 で従来動作へ戻せる",
              rc._sync_heavy_fallback_allowed() is True)
    finally:
        os.environ.pop("SUMMARY_MAIN_SYNC_HEAVY_FALLBACK", None)


def test_fast_path_decision_log() -> None:
    section("P0-1  FAST_PATH_DECISION が hit/miss を 1 行で出す")
    import pandas as pd
    import scheduler_jobs.summary.runner_core as rc

    rc.reset_fast_path_stats()
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.setLevel(logging.INFO)
    lg = logging.getLogger("scheduler_jobs.summary.runner_core")
    lg.addHandler(h)
    old = lg.level
    lg.setLevel(logging.INFO)
    try:
        import datetime as dt

        now = dt.datetime(2026, 8, 28, 9, 46, 0)
        df = pd.DataFrame({
            "symbol": [str(1000 + i) for i in range(200)],
            "datetime": [pd.Timestamp("2026-08-28 09:45:00")] * 200,
            "close": [100.0] * 200,
            "technical_ready": [True] * 200,
        })
        rc._log_fast_path_decision(now, hit=True, rows=200, df=df, elapsed=2.247)
        rc._log_fast_path_decision(now, hit=False, rows=0, df=None, elapsed=9.430,
                                   reject_reason="tier1_empty_or_timeout",
                                   fallback_reason="heavy_rebuild_bg_submitted")
    finally:
        lg.removeHandler(h)
        lg.setLevel(old)

    out = buf.getvalue()
    check("[FAST_PATH_DECISION] が出る", "[FAST_PATH_DECISION]" in out, out[:100])
    for k in ("result=HIT", "result=MISS", "rows=", "symbols=", "freshest_latest=",
              "age=", "technical_ready=", "required_cols_missing=", "reject_reason=",
              "fallback_reason=", "hit_rate="):
        check(f"項目 {k} がある", k in out, "")
    st = rc.fast_path_stats()
    check("hit 率が集計される", st["hits"] == 1 and st["misses"] == 1,
          str(st))


# ============================================================
# P0-7 / P0-8  pending terminal state
# ============================================================

def test_pending_terminal_state() -> None:
    section("P0-7/P0-8  prune された候補は vanished ではない")
    import trading.entry.candidate_id as ci

    # 2026-08-28 の実データと同じ形
    cid = ci.new_id("6030", "RANKING")
    ci.note(cid, "pending_created", symbol="6030")
    check("terminal 前は vanished として報告される",
          ci.log_vanished([cid], reason="pending_moved_without_order") == [cid])

    ci.mark_terminal(cid, ci.STATE_REJECTED, reason="RANKING_FINAL_NG:ATR_1M_FILTER_NG")
    check("terminal 後は vanished 対象外",
          ci.log_vanished([cid], reason="pending_moved_without_order") == [])
    check("is_terminal=True", ci.is_terminal(cid))
    st = ci.terminal_state(cid)
    check("terminal reason が残る", st is not None and "ATR_1M_FILTER_NG" in st[1], str(st))

    # symbol 逆引き
    cid2 = ci.new_id("3994", "RANKING")
    ci.note(cid2, "pending_created", symbol="3994")
    n = ci.mark_terminal_symbol("3994", ci.STATE_REJECTED, reason="RANKING_FINAL_NG:ATR_1M_FILTER_NG")
    check("symbol 逆引きで terminal にできる", n == 1 and ci.is_terminal(cid2), f"n={n}")
    check("symbol 逆引き後も vanished 対象外",
          ci.log_vanished([cid2], reason="pending_moved_without_order") == [])

    # 理由による分類
    from trading.entry.pending_manager import _terminal_state_for_reason

    cases = [
        ("RANKING_FINAL_NG:ATR_1M_FILTER_NG", ci.STATE_REJECTED),
        ("SCORE_NG", ci.STATE_REJECTED),
        ("stale_timeout", ci.STATE_EXPIRED),
        ("expired_max_age", ci.STATE_EXPIRED),
        ("cancelled_by_user", ci.STATE_CANCELLED),
    ]
    for reason, expected in cases:
        got = _terminal_state_for_reason(reason)
        check(f"reason={reason!r} -> {expected}", got == expected, f"got={got}")

    # terminal でない state を渡したら REJECTED に倒す
    cid3 = ci.new_id("9999", "RANKING")
    ci.note(cid3, "pending_created")
    ci.mark_terminal(cid3, "EVALUATING", reason="bogus")
    st3 = ci.terminal_state(cid3)
    check("terminal でない state は REJECTED へ倒す",
          st3 is not None and st3[0] == ci.STATE_REJECTED, str(st3))


def test_prune_marks_terminal() -> None:
    section("P0-7  pending prune が terminal を記録する")
    import trading.entry.candidate_id as ci
    from global_state import global_data
    import trading.entry.pending_manager as pm

    global_data.pending_entries = {}
    cid = ci.new_id("4443", "RANKING")
    ci.note(cid, "pending_created", symbol="4443")
    entry = {
        "symbol": "4443", "source": "RANKING", "side": "BUY",
        "interval": 1, "candidate_id": cid,
        "created_at": __import__("datetime").datetime.now(),
    }
    global_data.pending_entries["4443"] = [entry]

    removed = pm.prune_entries(
        lambda sym, e: sym == "4443",
        reason="RANKING_FINAL_NG:ATR_1M_FILTER_NG",
    )
    check("prune された", removed == 1, f"removed={removed}")
    check("terminal が記録された", ci.is_terminal(cid))
    st = ci.terminal_state(cid)
    check("REJECTED + 理由付き",
          st is not None and st[0] == ci.STATE_REJECTED and "ATR_1M_FILTER_NG" in st[1],
          str(st))
    check("vanished として報告されない",
          ci.log_vanished([cid], reason="pending_moved_without_order") == [])


def test_ranking_snapshot_cache() -> None:
    section("P0-10/P0-11  ranking snapshot を 1 tick で読み直さない")
    import pandas as pd
    import trading.ranking.summary.loader as ld

    os.environ["RANKING_SNAPSHOT_CACHE_TTL_SEC"] = "45"
    ld.reset_ranking_snapshot_cache()

    made = {"n": 0}
    fake = pd.DataFrame({
        "symbol": [str(1000 + i) for i in range(500)],
        "datetime": [pd.Timestamp("2026-08-28 09:45:00")] * 500,
        "close": [100.0] * 500,
        "ranking_type": ["値上がり率"] * 500,
    })

    orig_prep = ld.normalize_ranking_snapshot_df

    def counting_prep(df, *a, **k):
        made["n"] += 1
        return fake.copy()

    ld.__dict__["normalize_ranking_snapshot_df"] = counting_prep
    orig_exists = ld.path_exists
    orig_tbl = ld.table_exists_sqlite
    orig_read = ld.read_sqlite_table
    ld.__dict__["path_exists"] = lambda p: True
    ld.__dict__["table_exists_sqlite"] = lambda db_path, table_name: True
    ld.__dict__["read_sqlite_table"] = lambda **k: fake.copy()
    try:
        outs = [
            ld.load_ranking_snapshot_1min(
                trade_date="20260828", lookback_minutes=240, ranking_db_path="x.db")
            for _ in range(3)
        ]
    finally:
        ld.__dict__["normalize_ranking_snapshot_df"] = orig_prep
        ld.__dict__["path_exists"] = orig_exists
        ld.__dict__["table_exists_sqlite"] = orig_tbl
        ld.__dict__["read_sqlite_table"] = orig_read

    st = ld.ranking_snapshot_cache_stats()
    check(f"1m/3m/5m の 3 回で実読み込みは {made['n']} 回",
          made["n"] == 1, f"prep_calls={made['n']} stats={st}")
    check("2 回目以降は cache hit", st["hits"] == 2, str(st))
    check("同じ内容が返る", all(o.equals(outs[0]) for o in outs))

    # TTL=0 で従来動作
    os.environ["RANKING_SNAPSHOT_CACHE_TTL_SEC"] = "0"
    ld.reset_ranking_snapshot_cache()
    made["n"] = 0
    ld.__dict__["normalize_ranking_snapshot_df"] = counting_prep
    ld.__dict__["path_exists"] = lambda p: True
    ld.__dict__["table_exists_sqlite"] = lambda db_path, table_name: True
    ld.__dict__["read_sqlite_table"] = lambda **k: fake.copy()
    try:
        for _ in range(3):
            ld.load_ranking_snapshot_1min(
                trade_date="20260828", lookback_minutes=240, ranking_db_path="x.db")
    finally:
        ld.__dict__["normalize_ranking_snapshot_df"] = orig_prep
        ld.__dict__["path_exists"] = orig_exists
        ld.__dict__["table_exists_sqlite"] = orig_tbl
        ld.__dict__["read_sqlite_table"] = orig_read
        os.environ.pop("RANKING_SNAPSHOT_CACHE_TTL_SEC", None)
        ld.reset_ranking_snapshot_cache()
    check("TTL=0 で従来動作 (毎回読む)", made["n"] == 3, f"prep_calls={made['n']}")


def main() -> int:
    tests = [
        test_sanitize_single_copy,
        test_realtime_projection_always,
        test_setter_enrich_budget,
        test_cancel_checkpoint_in_normalize,
        test_no_sync_heavy_fallback,
        test_fast_path_decision_log,
        test_pending_terminal_state,
        test_prune_marks_terminal,
        test_ranking_snapshot_cache,
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
