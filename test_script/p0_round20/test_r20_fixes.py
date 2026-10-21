# -*- coding: utf-8 -*-
"""
第20ラウンド: 2026-09-01 12:53:05-12:55:37 の実運用ログで出た不具合の回帰テスト

対象 (ログ実測):
  * /orders Read timeout 14 / kabu_position_reader timeout 55 / get_orders timeout 31
  * /positions と /orders で 429 "API実行回数エラー"
  * EXIT の 5 秒ジョブが 8.9 秒かかり "previous still running" が多発
    -> [EXIT LOOP] / [POSITIONS_STATE] が毎回 2 行ずつ出ていた (二重駆動)
  * ranking_snapshot_1min が空 / Ranking 代替 40 銘柄が全件除外
  * Tonosama が MIN_LATEST_VOLUME=50000 で 39 行すべて除外
  * Summary-AI の BUY 候補が AI_OK 後に 3m/5m 欠落で全件 NG
  * slope 列はあるのに _slope が無く傾きフィルターが skip
  * macd=0 / signal=0 が並ぶ
  * PUSH DB feeder が約 101 秒 新規行 0 件

実注文 / 取消 / 返済 / Discord 送信 / kabu Station への本番リクエストは
一切発生しない (すべてモック)。
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

os.environ.setdefault("AUTOSTOCK_TEST", "1")

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
            self.records.append(record.getMessage())
        except Exception:
            self.records.append(str(record.msg))

    def text(self) -> str:
        return "\n".join(self.records)


# ============================================================
# [1] /positions への同時要求が実 GET 1 回に集約される
# ============================================================

def test_positions_singleflight() -> None:
    section("[1] /positions への複数スレッド同時要求が実 GET 1 回に集約される")

    from kabu_api import positions_read_cache as prc

    prc.reset_for_test()

    calls = {"n": 0}
    started = threading.Event()

    def slow_fetch():
        calls["n"] += 1
        started.set()
        # 実 REST の応答待ちを模す。この間に他スレッドが来る。
        time.sleep(0.35)
        return [{"Symbol": "7203", "MarginTradeType": 2, "LeavesQty": 100, "Price": 2000.0}], True, ""

    results = {}

    def worker(i):
        results[i] = prc.get_positions_cached(slow_fetch, key="product2", caller=f"t{i}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    threads[0].start()
    started.wait(2.0)
    for t in threads[1:]:
        t.start()
    for t in threads:
        t.join(5.0)

    check("同時 8 スレッドでも実 GET は 1 本", calls["n"] == 1, f"fetches={calls['n']}")
    ok_count = sum(1 for r in results.values() if r.ok)
    check("全スレッドが同じ結果を得る", ok_count == 8, f"ok={ok_count}/8")
    payloads = {str(r.payload) for r in results.values() if r.ok}
    check("payload が全スレッドで一致", len(payloads) == 1, f"variants={len(payloads)}")

    st = prc.stats(key="product2")["product2"]
    check("cache_hits が計上される", st["cache_hits"] >= 1, str(st["cache_hits"]))
    check("呼び出し元が診断ログに残る", len(st["callers"]) == 8, str(sorted(st["callers"])))


# ============================================================
# [2] /orders への同時要求が実 GET 1 回に集約される
# ============================================================

def test_orders_singleflight() -> None:
    section("[2] /orders への複数スレッド同時要求が実 GET 1 回に集約される")

    from kabu_api import orders_read_cache as orc

    orc.reset_for_test()

    calls = {"n": 0}
    started = threading.Event()

    def slow_fetch():
        calls["n"] += 1
        started.set()
        time.sleep(0.35)
        return [{"OrderId": "X1", "State": 3}], True, ""

    results = {}

    def worker(i):
        results[i] = orc.get_orders_cached(slow_fetch, caller=f"t{i}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    threads[0].start()
    started.wait(2.0)
    for t in threads[1:]:
        t.start()
    for t in threads:
        t.join(5.0)

    check("同時 8 スレッドでも実 GET は 1 本", calls["n"] == 1, f"fetches={calls['n']}")
    check("全スレッドが同じ注文一覧を得る",
          all(r.ok and r.orders == [{"OrderId": "X1", "State": 3}] for r in results.values()),
          f"ok={sum(1 for r in results.values() if r.ok)}/8")

    # pending_monitor と force_cancel_loop が同じキャッシュを共有する
    import force_cancel_loop
    import trading.handlers.pending_monitor as pending_monitor

    orc.reset_for_test()
    gets = {"n": 0}

    def fetch_once():
        gets["n"] += 1
        return [{"OrderId": "Y1", "State": 3}], True, ""

    orig_a = force_cancel_loop._fetch_orders_once
    orig_b = pending_monitor._fetch_orders_once
    try:
        force_cancel_loop._fetch_orders_once = fetch_once
        pending_monitor._fetch_orders_once = fetch_once
        for _ in range(3):
            force_cancel_loop.get_orders()
            pending_monitor.get_orders()
        check("2 経路 x 3 回でも実 GET は 1 本 (共有キャッシュ)", gets["n"] == 1, f"gets={gets['n']}")
    finally:
        force_cancel_loop._fetch_orders_once = orig_a
        pending_monitor._fetch_orders_once = orig_b


# ============================================================
# [3] API 別の最短間隔が 1 箇所で管理されている
# ============================================================

def test_rest_limits_single_owner() -> None:
    section("[3] API 別の間隔 / TTL / timeout / backoff が 1 箇所で決まる")

    from kabu_api import rest_limits

    rest_limits.reset_for_test()
    p_orders = rest_limits.policy("orders")
    p_pos = rest_limits.policy("positions")

    check("orders ポリシーがある", p_orders.timeout_sec > 0, str(p_orders.as_log_dict()))
    check("positions ポリシーがある", p_pos.timeout_sec > 0, str(p_pos.as_log_dict()))

    # ENV で上書きできる = 各モジュールに数値が散っていない
    os.environ["KABU_READ_POSITIONS_TIMEOUT_SEC"] = "7.5"
    rest_limits.reset_for_test()
    check("ENV で timeout を一元的に変えられる",
          abs(rest_limits.policy("positions").timeout_sec - 7.5) < 1e-9,
          str(rest_limits.policy("positions").timeout_sec))

    from trading.position import kabu_position_reader as kpr
    check("kabu_position_reader が同じ値を見る",
          abs(kpr._positions_timeout_sec() - 7.5) < 1e-9,
          str(kpr._positions_timeout_sec()))

    del os.environ["KABU_READ_POSITIONS_TIMEOUT_SEC"]
    rest_limits.reset_for_test()

    from kabu_api import orders_read_cache as orc
    from trading.position import position_state_service as pss
    check("orders_read_cache の backoff が rest_limits 由来",
          orc._backoff_schedule() == rest_limits.policy("orders").backoff_sec,
          str(orc._backoff_schedule()))
    check("position_state_service の backoff が rest_limits 由来",
          pss._broker_backoff_schedule() == rest_limits.policy("positions").backoff_sec,
          str(pss._broker_backoff_schedule()))


# ============================================================
# [4] EXIT ループが二重起動しない
# ============================================================

def test_exit_loop_not_duplicated() -> None:
    section("[4] EXIT ループの駆動元が 1 つに限定され、二重起動しない")

    from core.startup.bootstrap import schedule_loop

    schedule_loop.reset_run_pending_owner_for_test()
    os.environ.pop("SCHEDULE_RUN_PENDING_OWNER", None)

    # dispatcher thread が動いていない -> main loop が回してよい
    check("dispatcher が居なければ main loop が駆動できる",
          schedule_loop.may_run_pending_here("main_loop") is True)

    # dispatcher thread が動いている場合は main loop に渡さない
    orig = schedule_loop.is_schedule_run_pending_loop_running
    try:
        schedule_loop.is_schedule_run_pending_loop_running = lambda: True
        schedule_loop.reset_run_pending_owner_for_test()
        check("dispatcher 稼働中は main loop が run_pending を呼ばない",
              schedule_loop.may_run_pending_here("main_loop") is False)
        check("dispatcher 自身は駆動できる",
              schedule_loop.may_run_pending_here("schedule_loop") is True)
    finally:
        schedule_loop.is_schedule_run_pending_loop_running = orig
        schedule_loop.reset_run_pending_owner_for_test()

    # main.py の判定関数も同じ結論になる
    import main as main_mod
    orig2 = schedule_loop.is_schedule_run_pending_loop_running
    try:
        schedule_loop.is_schedule_run_pending_loop_running = lambda: True
        schedule_loop.reset_run_pending_owner_for_test()
        main_mod._SCHEDULE_DRIVE_STATE["allowed"] = None
        check("main._may_drive_schedule() が False になる",
              main_mod._may_drive_schedule() is False)
    finally:
        schedule_loop.is_schedule_run_pending_loop_running = orig2
        schedule_loop.reset_run_pending_owner_for_test()
        main_mod._SCHEDULE_DRIVE_STATE["allowed"] = None

    # 後段の保険: 同時に 2 本呼ばれても本体は 1 回しか走らない
    from core.startup.scheduler import exit_bootstrap

    exit_bootstrap.reset_exit_loop_guard_for_test()
    handler = CapturingHandler()
    logging.getLogger("core.startup.scheduler.exit_bootstrap").addHandler(handler)

    entered = {"n": 0}
    inside = threading.Event()
    release = threading.Event()

    def fake_core():
        entered["n"] += 1
        inside.set()
        release.wait(2.0)

    orig_core = exit_bootstrap._run_exit_loop_market_guarded_core
    try:
        exit_bootstrap._run_exit_loop_market_guarded_core = fake_core
        t1 = threading.Thread(target=exit_bootstrap.run_exit_loop_market_guarded)
        t1.start()
        inside.wait(2.0)
        # 2 本目 (別経路からの直接呼び出しを模す)
        exit_bootstrap.run_exit_loop_market_guarded()
        release.set()
        t1.join(3.0)
    finally:
        exit_bootstrap._run_exit_loop_market_guarded_core = orig_core
        logging.getLogger("core.startup.scheduler.exit_bootstrap").removeHandler(handler)
        exit_bootstrap.reset_exit_loop_guard_for_test()

    check("EXIT loop 本体は 1 回しか実行されない", entered["n"] == 1, f"entered={entered['n']}")
    check("二重起動を握り潰さずログに残す",
          "二重起動しない" in handler.text(), handler.text()[-160:])


# ============================================================
# [5] Ranking: API 応答 -> raw / snapshot 保存 -> Summary 読取
# ============================================================

def _ranking_payload(n: int = 5) -> dict:
    rows = []
    for i in range(n):
        rows.append({
            "No": i + 1,
            "Rank": i + 1,
            "Symbol": str(9800 + i),
            "IssueName": "テスト銘柄%d" % i,
            "CurrentPrice": 1000.0 + i * 10,
            "ChangePercentage": 3.5 + i * 0.1,
            "TradingVolume": 250000 + i * 1000,
            "TurnOver": 300000000 + i * 100000,
            "ExchangeName": "東証プ",
        })
    return {"Type": "1", "ExchangeDivision": "ALL", "Ranking": rows}


def test_ranking_collect_and_persist() -> None:
    section("[5] Ranking API 応答が raw / snapshot へ保存され、Summary が読める")

    import trading.ranking.collectors as collectors
    from trading.ranking import api_circuit
    from trading.ranking.ranking_snapshot_manager import RankingSnapshotManager

    api_circuit.reset_for_test()
    collectors._CYCLE_LOCK = threading.Lock()

    calls = {"n": 0}

    def fake_api(params, **kwargs):
        calls["n"] += 1
        return _ranking_payload()

    orig = collectors.get_data_from_api
    orig_spacing = os.environ.get("RANKING_API_SPREAD_OVER_MINUTE")
    try:
        collectors.get_data_from_api = fake_api
        os.environ["RANKING_API_SPREAD_OVER_MINUTE"] = "0"
        now = dt.datetime(2026, 9, 1, 12, 53, 0)
        raw_rows, snapshot_rows = collectors.collect_ranking_rows(now, RankingSnapshotManager(maxlen=5))
    finally:
        collectors.get_data_from_api = orig
        if orig_spacing is None:
            os.environ.pop("RANKING_API_SPREAD_OVER_MINUTE", None)
        else:
            os.environ["RANKING_API_SPREAD_OVER_MINUTE"] = orig_spacing

    check("API 応答から raw 行ができる", len(raw_rows) > 0, f"raw={len(raw_rows)}")
    check("API 応答から snapshot 行ができる", len(snapshot_rows) > 0, f"snapshot={len(snapshot_rows)}")

    if snapshot_rows:
        s0 = snapshot_rows[0]
        check("snapshot に price が入る", float(s0.get("price") or 0) > 0, str(s0.get("price")))
        check("snapshot に volume が入る", float(s0.get("volume") or 0) > 0, str(s0.get("volume")))
        check("snapshot に turnover が入る", float(s0.get("turnover") or 0) > 0, str(s0.get("turnover")))
        check("snapshot の change_rate が 0 でない (全件除外の原因)",
              abs(float(s0.get("change_rate") or 0)) > 0, str(s0.get("change_rate")))

    # ---- 一時 DB へ保存して読み戻す (本番 DB は触らない) ----
    tmpdir = tempfile.mkdtemp(prefix="r20_ranking_")
    db_path = os.path.join(tmpdir, "ranking20260901.db")

    import database.crud.crud_ranking_snapshot as crud_snap
    from sqlalchemy import create_engine

    engine = create_engine("sqlite:///" + db_path.replace("\\", "/"), future=True)
    orig_engine_fn = crud_snap.get_ranking_engine
    try:
        crud_snap.get_ranking_engine = lambda: engine
        saved = crud_snap.insert_ranking_snapshot_1min(snapshot_rows)
    finally:
        crud_snap.get_ranking_engine = orig_engine_fn

    check("ranking_snapshot_1min へ INSERT される (table 作成だけで終わらない)",
          saved > 0, f"saved={saved}")

    with sqlite3.connect(db_path) as conn:
        cnt = conn.execute("SELECT COUNT(*) FROM ranking_snapshot_1min").fetchone()[0]
    check("DB に実際に行が残っている (commit 漏れなし)", cnt > 0, f"rows={cnt}")

    # ---- Summary loader が読める ----
    from trading.ranking.summary import loader as ranking_loader

    ranking_loader.reset_ranking_snapshot_cache()
    df = ranking_loader.load_ranking_snapshot_1min(
        trade_date="20260901", lookback_minutes=1440, ranking_db_path=db_path
    )
    check("Ranking Summary が保存済みデータを読める", not df.empty, f"rows={len(df)}")
    if not df.empty:
        check("読み出した行に symbol がある", "symbol" in df.columns, str(list(df.columns)[:12]))
        nonzero_change = 0
        for col in ("change_rate", "day_change_pct", "change_percentage"):
            if col in df.columns:
                import pandas as pd
                nonzero_change = int((pd.to_numeric(df[col], errors="coerce").fillna(0) != 0).sum())
                break
        check("day_change_pct 相当が 0 埋めになっていない", nonzero_change > 0, f"nonzero={nonzero_change}")


# ============================================================
# [6] Ranking: 代替データを「通常ランキング」と区別する
# ============================================================

def test_ranking_fallback_is_labeled() -> None:
    section("[6] Ranking が無いとき PUSH を通常ランキングとして偽装しない")

    import inspect
    import trading.ranking.entry_from_ranking as efr

    src = inspect.getsource(efr)
    check("代替経路には専用の識別ログがある",
          "FALLBACK" in src.upper(),
          "RANKING ENTRY SOURCE DB FALLBACK 等")

    from trading.ranking import api_circuit
    api_circuit.reset_for_test()
    # ブレーカーが開いている間は空を返し、部分データを snapshot として保存しない
    for _ in range(5):
        api_circuit.note_failure("timed out")
    st = api_circuit.stats()
    check("連続失敗でブレーカーが開く", bool(st.get("open")), str(st))


# ============================================================
# [7] Summary-AI 候補へ 3 分足 / 5 分足が付く
# ============================================================

def _mtf_frame(interval: int, symbols, base: dt.datetime, score: float):
    import pandas as pd

    rows = []
    for sym in symbols:
        for k in range(3):
            ts = base - dt.timedelta(minutes=interval * (k + 1))
            rows.append({
                "symbol": sym,
                "datetime": ts,
                "close": 1000.0,
                "score": score + k * 0.1,
                "buy_score": score + k * 0.1,
                "sell_score": 0.0,
                "final_score": score + k * 0.1,
            })
    return pd.DataFrame(rows)


def test_summary_ai_mtf_attach() -> None:
    section("[7] Summary-AI の 1 分足候補へ 3 分足 / 5 分足が付与される")

    import pandas as pd
    from trading.entry.summary_ai import mtf_attach

    now = dt.datetime(2026, 9, 1, 12, 53, 5)
    symbols = ["9856", "9788", "9742"]

    candidates = pd.DataFrame([
        # 型不一致 (int / '9788.0' / str) が混ざっていても突き合わせできること
        {"symbol": 9856, "ai_side": "BUY", "close": 1278.0, "datetime": now - dt.timedelta(minutes=1)},
        {"symbol": "9788.0", "ai_side": "BUY", "close": 525.0, "datetime": now - dt.timedelta(minutes=1)},
        {"symbol": "9742", "ai_side": "BUY", "close": 2357.0, "datetime": now - dt.timedelta(minutes=1)},
    ])

    frames = {
        3: _mtf_frame(3, symbols, now, 4.2),
        5: _mtf_frame(5, symbols, now, 3.8),
    }

    orig_loader = mtf_attach._load_tf_frame
    try:
        mtf_attach._load_tf_frame = lambda tf: (frames.get(tf), "test_merged")
        out = mtf_attach.attach_mtf_scores(candidates, interval=1, source="SUMMARY", now=now)
    finally:
        mtf_attach._load_tf_frame = orig_loader

    check("score_3m 列ができる", "score_3m" in out.columns, str([c for c in out.columns if "3m" in c]))
    check("score_5m 列ができる", "score_5m" in out.columns, str([c for c in out.columns if "5m" in c]))
    check("全候補に 3 分足が付く", bool(out["score_3m_present"].all()),
          str(list(out["score_3m_present"])))
    check("全候補に 5 分足が付く", bool(out["score_5m_present"].all()),
          str(list(out["score_5m_present"])))
    check("3 分足のスコアが 0 でない",
          bool((pd.to_numeric(out["score_3m"], errors="coerce").fillna(0) != 0).all()),
          str(list(out["score_3m"])))
    check("採用した足の時刻を診断に残す", "mtf_3m_datetime" in out.columns and out["mtf_3m_datetime"].notna().all(),
          str(list(out.get("mtf_3m_datetime", []))))
    check("データソースを診断に残す", set(out["mtf_3m_source"]) == {"test_merged"},
          str(set(out["mtf_3m_source"])))

    # 銘柄コードの型が違っても取り違えない
    check("int/str/'.0' 表記が同じ銘柄として突き合わされる",
          list(out["score_3m_present"]) == [True, True, True],
          str(list(out["symbol"])))

    # 未確定足は採用しない (fail-closed)
    forming = pd.DataFrame([{
        "symbol": "9856", "datetime": now - dt.timedelta(seconds=30),
        "close": 1000.0, "buy_score": 5.0,
    }])
    try:
        mtf_attach._load_tf_frame = lambda tf: (forming, "test_forming")
        out2 = mtf_attach.attach_mtf_scores(
            candidates.head(1), interval=1, source="SUMMARY", now=now
        )
    finally:
        mtf_attach._load_tf_frame = orig_loader
    check("未確定足 (30 秒前開始) は採用しない",
          not bool(out2["score_3m_present"].any()),
          str(list(out2["score_3m_present"])))

    # データが無ければ列を作らない = 従来どおり fail-closed
    try:
        mtf_attach._load_tf_frame = lambda tf: (None, "empty")
        out3 = mtf_attach.attach_mtf_scores(candidates, interval=1, source="SUMMARY", now=now)
    finally:
        mtf_attach._load_tf_frame = orig_loader
    check("3m/5m が本当に無いときは present=False (0 で通さない)",
          (not out3["score_3m_present"].any()) and ("score_3m" not in out3.columns),
          str([c for c in out3.columns if "3m" in c]))

    # entry_gate が NA として扱えること
    from AI.entry_gate import _ai_final_entry_check_core
    from trading.entry.summary_ai.row_adapter import convert_summary_row_to_ai_gate_row

    row_ok = convert_summary_row_to_ai_gate_row(out.iloc[0], side="BUY")
    check("row_adapter が score_3m を拾える",
          bool(row_ok.get("score_3m_present")) and float(row_ok.get("score_3m") or 0) > 0,
          f"score_3m={row_ok.get('score_3m')}")

    res_ok = _ai_final_entry_check_core(dict(row_ok))
    check("entry_gate の reason が 3m=NA にならない",
          "3m=NA" not in str(res_ok.get("reason")),
          str(res_ok.get("reason"))[:140])

    row_ng = convert_summary_row_to_ai_gate_row(out3.iloc[0], side="BUY")
    check("3m/5m が無い行は present=False のまま row へ渡る",
          (not row_ng.get("score_3m_present")) and (not row_ng.get("score_5m_present")),
          f"3m={row_ng.get('score_3m_present')} 5m={row_ng.get('score_5m_present')}")
    # score 自体は十分にしてスコアゲートを通し、3m/5m 欠落の表示だけを見る
    row_ng2 = dict(row_ng)
    row_ng2.update({
        "buy_score": 5.0, "sell_score": 0.0, "score_total": 5.0, "score": 5.0,
        "turnover": 500_000_000.0, "trading_value": 500_000_000.0,
        "volume": 500_000.0, "close": 1278.0, "close_price": 1278.0,
    })
    res_ng = _ai_final_entry_check_core(row_ng2)
    check("データが無いときは 3m=NA / DATA_NOT_READY と出る (0 と区別する)",
          ("3m=NA" in str(res_ng.get("reason"))) and ("DATA_NOT_READY" in str(res_ng.get("reason"))),
          str(res_ng.get("reason"))[:160])


# ============================================================
# [8] Tonosama の実効出来高 / 売買代金
# ============================================================

def test_tonosama_effective_thresholds() -> None:
    section("[8] Tonosama の実効出来高が 10,000 / 売買代金が 3,000,000 になる")

    import importlib
    import trading.entry.tonosama.config as tcfg

    for name in ("TONOSAMA_MIN_LATEST_VOLUME", "TONOSAMA_MIN_LATEST_TURNOVER"):
        os.environ.pop(name, None)
    tcfg = importlib.reload(tcfg)

    check("MIN_LATEST_VOLUME = 10000", tcfg.MIN_LATEST_VOLUME == 10000.0, str(tcfg.MIN_LATEST_VOLUME))
    check("MIN_LATEST_TURNOVER = 3,000,000", tcfg.MIN_LATEST_TURNOVER == 3_000_000.0,
          str(tcfg.MIN_LATEST_TURNOVER))

    cfg = tcfg.effective_liquidity_config()
    check("_latest_volume の意味が最新1分足出来高だと明記される",
          cfg["_latest_volume_meaning"] == "latest_completed_1min_bar_volume",
          cfg["_latest_volume_meaning"])
    check("_latest_turnover の意味が明記される",
          "turnover" in cfg["_latest_turnover_meaning"],
          cfg["_latest_turnover_meaning"])

    tcfg.reset_effective_log_for_test()
    handler = CapturingHandler()
    logging.getLogger("trading.entry.tonosama.config").addHandler(handler)
    try:
        tcfg.log_effective_config_once()
        tcfg.log_effective_config_once()
        tcfg.log_effective_config_once()
    finally:
        logging.getLogger("trading.entry.tonosama.config").removeHandler(handler)
    check("起動時ログは 1 回だけ",
          sum(1 for m in handler.records if "[TONOSAMA CONFIG]" in m) == 1,
          str(len(handler.records)))

    # 設定値が floor に潰されないこと (旧 50000 floor の再発防止)
    os.environ["TONOSAMA_MIN_LATEST_VOLUME"] = "25000"
    tcfg2 = importlib.reload(tcfg)
    check("ENV でより厳しくできる", tcfg2.MIN_LATEST_VOLUME == 25000.0, str(tcfg2.MIN_LATEST_VOLUME))
    os.environ.pop("TONOSAMA_MIN_LATEST_VOLUME", None)
    importlib.reload(tcfg)

    # 最終ガード / AI fallback が 50000 を直書きしていない
    import trading.entry.tonosama.pending_writer as pw
    pw = importlib.reload(pw)
    check("pending_writer の最終ガードが config を見る",
          pw.MIN_FINAL_LATEST_VOLUME == 10000.0, str(pw.MIN_FINAL_LATEST_VOLUME))


# ============================================================
# [9] slope 列が傾きフィルターへ渡る
# ============================================================

def test_slope_column_reaches_filter() -> None:
    section("[9] slope 列が _slope としてフィルターへ渡る")

    import pandas as pd
    import trading.entry.tonosama.runner as trunner

    df = pd.DataFrame([
        {"symbol": "9856", "symbolname": "A", "open": 1000.0, "high": 1020.0, "low": 995.0,
         "close": 1015.0, "volume": 60000.0, "slope": 0.004,
         "_max_volume_surge_ratio": 4.0, "_max_price_change_pct": 0.9},
        {"symbol": "9788", "symbolname": "B", "open": 500.0, "high": 505.0, "low": 499.0,
         "close": 504.0, "volume": 80000.0, "slope": 0.0000001,
         "_max_volume_surge_ratio": 4.0, "_max_price_change_pct": 0.9},
    ])

    out = trunner._ensure_actual_movement_cols(df)
    check("_slope が作られる", "_slope" in out.columns, str([c for c in out.columns if "slope" in c]))
    check("_slope の値が slope 由来", abs(float(out["_slope"].iloc[0]) - 0.004) < 1e-12,
          str(out["_slope"].iloc[0]))
    check("_slope_source が記録される", out.get("_slope_source", pd.Series([""])).iloc[0] == "slope",
          str(out.get("_slope_source", pd.Series([""])).iloc[0]))

    handler = CapturingHandler()
    logging.getLogger("trading.entry.tonosama.runner").addHandler(handler)
    try:
        filtered = trunner._apply_primary_filters(df)
    finally:
        logging.getLogger("trading.entry.tonosama.runner").removeHandler(handler)

    check("傾きフィルターが skip されない",
          "missing _slope" not in handler.text(),
          handler.text()[-160:] if "missing _slope" in handler.text() else "")
    check("傾きが小さい行は落ちる", "9788" not in set(filtered.get("symbol", [])),
          str(sorted(set(filtered.get("symbol", [])))))
    check("傾きが十分な行は残る", "9856" in set(filtered.get("symbol", [])),
          str(sorted(set(filtered.get("symbol", [])))))


# ============================================================
# [10] MACD / signal
# ============================================================

def test_macd_nonzero_and_not_clobbered() -> None:
    section("[10] 正常な価格系列で MACD / signal が計算され、短履歴で消されない")

    import numpy as np
    import pandas as pd
    from trading.summary.indicators.indicator_calculator import add_all_indicators
    from trading.summary.pipeline.indicator_pipeline import _preserve_prior_indicator_values

    base = dt.datetime(2026, 9, 1, 9, 0)
    rows = []
    price = 1000.0
    for i in range(80):
        price += (1.5 if i % 3 else -0.7)
        rows.append({
            "symbol": "9856",
            "datetime": base + dt.timedelta(minutes=i),
            "open": price - 0.5, "high": price + 1.2, "low": price - 1.4,
            "close": price, "volume": 20000 + i * 10,
        })
    df = pd.DataFrame(rows)

    out = add_all_indicators(df, interval="1min")
    macd = pd.to_numeric(out.get("macd"), errors="coerce")
    sig = pd.to_numeric(out.get("signal"), errors="coerce")
    check("十分な本数があれば macd が計算される",
          int(macd.notna().sum()) > 0 and int((macd.fillna(0) != 0).sum()) > 0,
          f"nonnull={int(macd.notna().sum())} nonzero={int((macd.fillna(0) != 0).sum())}")
    check("十分な本数があれば signal が計算される",
          int(sig.notna().sum()) > 0 and int((sig.fillna(0) != 0).sum()) > 0,
          f"nonnull={int(sig.notna().sum())} nonzero={int((sig.fillna(0) != 0).sum())}")

    # 履歴不足の再計算が、計算済みの値を NaN で塗り潰さないこと
    before = pd.DataFrame({
        "symbol": ["9856", "9788"],
        "macd": [1.25, -0.75],
        "signal": [0.9, -0.4],
        "rsi": [66.6, 30.1],
        "slope": [0.004, -0.002],
    })
    after = before.copy()
    after[["macd", "signal", "rsi", "slope"]] = np.nan

    merged = _preserve_prior_indicator_values(before, after.copy(), 1)
    check("履歴不足で NaN になった macd が直前値で残る",
          float(merged["macd"].iloc[0]) == 1.25, str(list(merged["macd"])))
    check("履歴不足で NaN になった signal が直前値で残る",
          float(merged["signal"].iloc[0]) == 0.9, str(list(merged["signal"])))
    check("復元した行は carried_over で区別できる",
          bool(merged.get("macd_carried_over", pd.Series([False])).iloc[0]),
          str(merged.get("macd_carried_over", pd.Series([False])).iloc[0]))

    # 再計算できた値は上書きしない
    after2 = before.copy()
    after2["macd"] = [2.5, np.nan]
    merged2 = _preserve_prior_indicator_values(before, after2.copy(), 1)
    check("再計算できた値は前回値で上書きしない",
          float(merged2["macd"].iloc[0]) == 2.5, str(list(merged2["macd"])))
    check("NaN の行だけ前回値で埋める",
          float(merged2["macd"].iloc[1]) == -0.75, str(list(merged2["macd"])))


# ============================================================
# [11] range_pct の単位
# ============================================================

def test_range_pct_unit() -> None:
    section("[11] range_pct の単位が閾値 2.5 (=2.5%) と一致する")

    from trading.entry.summary_ai import executor as ex
    from trading.entry.summary_ai import range_defs as rd

    r = rd.compute_intrabar_range_pct({"high": 1018.8, "low": 1000.0, "close": 1000.0})
    check("range_defs は percent を返す (1.88% -> 1.88)",
          abs(float(r["value"]) - 1.88) < 1e-6, str(r["value"]))
    check("range_defs が単位を明示する", r.get("unit") == "percent", str(r.get("unit")))

    v = ex._pick_range_pct({"high": 1018.8, "low": 1000.0, "close": 1000.0})
    check("executor も percent を返す", abs(v - 1.88) < 1e-6, str(v))

    check("比率で書かれた閾値は境界で 1 度だけ percent へ換算",
          abs(ex._as_percent_threshold(0.005, name="t") - 0.5) < 1e-9,
          str(ex._as_percent_threshold(0.005, name="t")))
    check("percent の閾値はそのまま",
          abs(ex._as_percent_threshold(2.5, name="t") - 2.5) < 1e-9,
          str(ex._as_percent_threshold(2.5, name="t")))

    # blowoff 危険判定が閾値 2.5% で実際に発火する (旧実装は 0.0188>=2.5 で常に False)
    row = {
        "close": 1030.0, "high": 1035.0, "low": 1000.0,
        "rsi": 85.0, "slope": 0.01,
    }
    danger, detail = ex._is_dangerous_blowoff_row(row, side="BUY")
    check("3.4% の値幅で blowoff 危険判定が働く", danger is True, str(detail))
    check("range_pct が percent でログに出る",
          abs(float(detail["range_pct"]) - 3.398) < 0.01, str(detail["range_pct"]))

    safe_row = dict(row, high=1031.0, low=1029.0)
    danger2, detail2 = ex._is_dangerous_blowoff_row(safe_row, side="BUY")
    check("0.2% の値幅では発火しない", danger2 is False, str(detail2["range_pct"]))

    # 値幅が小さい行は strict prefilter で落ちる (percent 閾値 0.5%)
    ok, det = ex._passes_strict_candidate_quality_core({
        "symbol": "9856", "side": "BUY", "close": 1000.0, "high": 1001.0, "low": 1000.0,
    })
    check("0.1% の値幅は strict prefilter で落ちる", ok is False, str(det.get("reason")))
    ok2, _ = ex._passes_strict_candidate_quality_core({
        "symbol": "9856", "side": "BUY", "close": 1000.0, "high": 1010.0, "low": 1000.0,
    })
    check("1.0% の値幅は通る", ok2 is True)

    # entry_order_builder は内部が比率、列は percent
    from trading.handlers import entry_order_builder as eob
    check("percent -> ratio 変換が 1 箇所にある",
          abs(eob._rc_percent_to_ratio(1.88) - 0.0188) < 1e-9,
          str(eob._rc_percent_to_ratio(1.88)))
    check("ratio -> percent 変換が 1 箇所にある",
          abs(eob._rc_ratio_to_percent(0.0188) - 1.88) < 1e-9,
          str(eob._rc_ratio_to_percent(0.0188)))


# ============================================================
# [12] PUSH 停滞と復旧
# ============================================================

def test_push_stall_and_recovery() -> None:
    section("[12] PUSH 停滞を検知し、再接続を 1 回だけ要求して連打しない")

    from trading.push import push_stall_guard as psg

    fake_now = {"t": 1000.0}
    psg.reset_for_test(clock=lambda: fake_now["t"])
    psg.mark_started()

    os.environ["PUSH_STALL_THRESHOLD_SEC"] = "60"
    os.environ["PUSH_STALL_RECONNECT_COOLDOWN_SEC"] = "180"

    orig_market = psg.is_market_time_now
    orig_subs = psg.PushStallGuard._subscribed_symbol_count
    orig_ws = psg.PushStallGuard._ws_connected
    try:
        psg.is_market_time_now = lambda now=None: True
        psg.PushStallGuard._subscribed_symbol_count = lambda self: 100
        psg.PushStallGuard._ws_connected = lambda self: True

        # 停滞していない
        fake_now["t"] = 1010.0
        st = psg.check()
        check("30 秒未満は停滞ではない", st.state == psg.STATE_OK, st.state)

        reconnects = {"n": 0}

        def request():
            reconnects["n"] += 1
            return True

        # 101 秒停滞 (実測と同じ長さ)
        fake_now["t"] = 1101.0
        st = psg.check(request_reconnect=request)
        check("101 秒の停滞を検知する", st.state == psg.STATE_STALL,
              f"{st.state} idle={st.since_last_tick_sec:.0f}s")
        check("再接続を 1 回だけ要求する", reconnects["n"] == 1 and st.reconnect_requested,
              f"requests={reconnects['n']}")
        check("購読銘柄数を確認している", st.subscribed_symbols == 100, str(st.subscribed_symbols))
        check("PUSH 接続状態を確認している", st.ws_connected is True, str(st.ws_connected))

        # cool-down 中は連打しない
        fake_now["t"] = 1131.0
        st = psg.check(request_reconnect=request)
        check("cool-down 中は再接続を連打しない", reconnects["n"] == 1, f"requests={reconnects['n']}")
        check("抑止理由をログ用に残す", "cooldown" in st.reconnect_suppressed_reason,
              st.reconnect_suppressed_reason)

        # 復旧
        fake_now["t"] = 1140.0
        psg.note_tick(5)
        st = psg.check(request_reconnect=request)
        check("新規行が来れば停滞解除", st.state == psg.STATE_OK, st.state)

        # 購読 0 件のときは再接続ではなく購読の問題として報告する
        psg.PushStallGuard._subscribed_symbol_count = lambda self: 0
        fake_now["t"] = 1400.0
        st = psg.check(request_reconnect=request)
        check("購読 0 件は no_subscription として報告",
              st.state == psg.STATE_NO_SUBSCRIPTION, st.state)
        check("購読 0 件では再接続しない", reconnects["n"] == 1, f"requests={reconnects['n']}")

        # 市場時間外は停滞と呼ばない
        psg.is_market_time_now = lambda now=None: False
        fake_now["t"] = 1800.0
        st = psg.check(request_reconnect=request)
        check("市場時間外は停滞扱いしない", st.state == psg.STATE_MARKET_CLOSED, st.state)
    finally:
        psg.is_market_time_now = orig_market
        psg.PushStallGuard._subscribed_symbol_count = orig_subs
        psg.PushStallGuard._ws_connected = orig_ws
        os.environ.pop("PUSH_STALL_THRESHOLD_SEC", None)
        os.environ.pop("PUSH_STALL_RECONNECT_COOLDOWN_SEC", None)
        psg.reset_for_test()

    # stale データを下流へ渡さない
    os.environ["PUSH_STALE_DATA_MAX_AGE_SEC"] = "90"
    now = dt.datetime(2026, 9, 1, 12, 55, 0)
    check("101 秒前の足は stale",
          psg.data_is_stale(now - dt.timedelta(seconds=101), now=now) is True)
    check("30 秒前の足は stale ではない",
          psg.data_is_stale(now - dt.timedelta(seconds=30), now=now) is False)
    check("時刻が読めない足は stale 扱い (安全側)",
          psg.data_is_stale("not-a-time", now=now) is True)
    os.environ.pop("PUSH_STALE_DATA_MAX_AGE_SEC", None)


# ============================================================
# [13] positions.db の重複読み取りが減る
# ============================================================

def test_positions_db_read_dedupe() -> None:
    section("[13] positions.db の短時間の重複読み取りがまとまる")

    from trading.position import open_position_sync as ops

    ops.reset_db_read_cache_for_test()

    calls = {"n": 0}
    sample = {"7203": {"symbol": "7203", "qty": 100, "entry_price": 2000.0,
                       "side": "BUY", "status": "OPEN"}}

    real = ops.load_open_positions_from_db_strict

    def fake_query():
        calls["n"] += 1
        ops._db_read_cache_put(sample)
        return sample

    # キャッシュ層の挙動だけを見る (DB へは触らない)
    for _ in range(5):
        cached = ops._db_read_cache_get()
        if cached is None:
            fake_query()

    check("TTL 内の 5 回読みが実クエリ 1 回にまとまる", calls["n"] == 1, f"queries={calls['n']}")
    st = ops.db_read_cache_stats()
    check("cache_hits が計上される", st["cache_hits"] >= 4, str(st))
    check("TTL が rest_limits 由来", st["ttl_sec"] > 0, str(st["ttl_sec"]))

    ops.invalidate_db_read_cache(reason="test")
    check("invalidate 後は取り直す", ops._db_read_cache_get() is None)
    ops.reset_db_read_cache_for_test()
    check("strict loader は差し替えていない", ops.load_open_positions_from_db_strict is real)


# ============================================================
# [14] EXIT が /positions を 1 回転で二重に読まない
# ============================================================

def test_exit_loop_single_broker_read() -> None:
    section("[14] EXIT の 1 回転で broker /positions を二重に読まない")

    import inspect
    from trading.exit import exit_utils

    src = inspect.getsource(exit_utils.get_open_positions_safe)
    check("get_open_positions_safe が read_kabu_open_positions を直接呼ばない",
          "read_kabu_open_positions" not in src,
          "PositionStateService 経由に統一")
    check("state を 1 回だけ取得している",
          src.count("get_positions_state(") == 1,
          f"count={src.count('get_positions_state(')}")

    from trading.position import position_state_service as pss
    from kabu_api import positions_read_cache as prc

    pss.reset_for_test()
    prc.reset_for_test()

    reads = {"n": 0}

    def fake_broker():
        reads["n"] += 1
        return {}, True, ""

    orig = pss._read_broker_positions
    orig_db = pss._read_db_positions_raising
    try:
        pss._read_broker_positions = fake_broker
        pss._read_db_positions_raising = lambda: {}
        exit_utils.get_open_positions_safe()
        check("EXIT 1 回転で broker 読み取りは 1 回", reads["n"] == 1, f"reads={reads['n']}")
    finally:
        pss._read_broker_positions = orig
        pss._read_db_positions_raising = orig_db
        pss.reset_for_test()


# ============================================================
# [15] numba は任意依存 (import できなくても main.py を止めない)
# ============================================================

def test_numba_optional_dependency() -> None:
    section("[15] numba が import できなくても EXIT import が通る")

    import numpy as np
    from core.compat import numba_compat as nc

    st = nc.status()
    check("numba_compat が状態を公開する",
          set(("numba_available", "import_error", "disabled_by_env")) <= set(st),
          str(st))

    # --- identity decorator は 2 通りの書き方どちらでも動く ---
    @nc.njit
    def bare(p):
        return p[0], p.max(), p.min(), p[-1]

    @nc.njit(cache=True, fastmath=True)
    def parened(p):
        return p[0], p.max(), p.min(), p[-1]

    x = np.array([101.0, 105.5, 99.25, 103.0, 100.75])
    want = (x[0], x.max(), x.min(), x[-1])
    check("@njit で結果が変わらない", bare(x) == want, str(bare(x)))
    check("@njit(...) で結果が変わらない", parened(x) == want, str(parened(x)))

    # --- exit_handler は numba の有無に関わらず import できる ---
    import trading.handlers.exit_handler as eh
    check("exit_handler が import できる", eh is not None)
    check("build_5s_bar_fast がある", callable(getattr(eh, "build_5s_bar_fast", None)))
    check("run_exit_pipeline がある", callable(getattr(eh, "run_exit_pipeline", None)))
    check("exit_handler が compat 層と同じ判定を使う",
          eh.NUMBA_AVAILABLE == nc.NUMBA_AVAILABLE,
          "eh=%s compat=%s" % (eh.NUMBA_AVAILABLE, nc.NUMBA_AVAILABLE))

    # --- calc_ohlc は JIT 有無で同じ値を返す ---
    got = eh.calc_ohlc(x)
    check("calc_ohlc が参照値と一致 (JIT 有無で不変)", tuple(got) == want, str(got))

    # --- numba を強制的に「使えない」状態にしても import が通る ---
    import importlib
    import sys

    saved_env = os.environ.get("AUTOSTOCK_DISABLE_NUMBA")
    saved_mod = sys.modules.pop("core.compat.numba_compat", None)
    try:
        os.environ["AUTOSTOCK_DISABLE_NUMBA"] = "1"
        forced = importlib.import_module("core.compat.numba_compat")
        check("AUTOSTOCK_DISABLE_NUMBA=1 で JIT を明示的に切れる",
              forced.NUMBA_AVAILABLE is False, str(forced.status()))

        @forced.njit
        def f2(p):
            return p[0], p.max(), p.min(), p[-1]

        check("強制無効でも計算結果は同じ", f2(x) == want, str(f2(x)))
    finally:
        sys.modules.pop("core.compat.numba_compat", None)
        if saved_env is None:
            os.environ.pop("AUTOSTOCK_DISABLE_NUMBA", None)
        else:
            os.environ["AUTOSTOCK_DISABLE_NUMBA"] = saved_env
        if saved_mod is not None:
            sys.modules["core.compat.numba_compat"] = saved_mod

    # --- installer の normal exit 定数が適用される ---
    from core.startup.runtime_env import installer

    ok = installer._install_normal_exit_scalp_align("main")
    check("_install_normal_exit_scalp_align('main') が成功する", ok is True, str(ok))
    check("NORMAL_* 定数が入る",
          eh.NORMAL_MAX_HOLD_SEC > 0 and eh.NORMAL_TAKE_PROFIT > 0,
          "take=%s max_hold=%s" % (eh.NORMAL_TAKE_PROFIT, eh.NORMAL_MAX_HOLD_SEC))

    # --- プロジェクト内に素の numba import が残っていないこと ---
    import re

    pat = re.compile(r"^\s*(?:from\s+numba(?:\.[\w.]+)?\s+import|import\s+numba)", re.M)
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    skip = {"__pycache__", ".venv", ".git", ".idea", ".pytest_cache"}
    offenders = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip]
        for fn in filenames:
            if not fn.endswith(".py") or fn.startswith("._"):
                continue
            fp = os.path.join(dirpath, fn)
            rel = os.path.relpath(fp, root).replace("\\", "/")
            if rel.startswith("core/compat/") or rel.startswith("test_script/"):
                continue  # compat 層とテスト自身は例外
            try:
                src = open(fp, encoding="utf-8").read()
            except Exception:
                continue
            if pat.search(src):
                offenders.append(rel)
    check("numba の直接 import が compat 層以外に無い",
          not offenders, str(offenders[:5]))


# ============================================================
# main
# ============================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s:%(name)s:%(message)s")

    tests = [
        test_positions_singleflight,
        test_orders_singleflight,
        test_rest_limits_single_owner,
        test_exit_loop_not_duplicated,
        test_ranking_collect_and_persist,
        test_ranking_fallback_is_labeled,
        test_summary_ai_mtf_attach,
        test_tonosama_effective_thresholds,
        test_slope_column_reaches_filter,
        test_macd_nonzero_and_not_clobbered,
        test_range_pct_unit,
        test_push_stall_and_recovery,
        test_positions_db_read_dedupe,
        test_exit_loop_single_broker_read,
        test_numba_optional_dependency,
    ]

    for t in tests:
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
    if FAILURES:
        print("FAILURES:")
        for f in FAILURES:
            print("  -", f)
    print("=" * 70)
    sys.exit(1 if FAIL else 0)
