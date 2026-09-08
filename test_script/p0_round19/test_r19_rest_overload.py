# -*- coding: utf-8 -*-
"""
第19ラウンド: localhost:18080 読み取りAPI同時多発タイムアウト対策 (実注文なし)

2026-09-01 のログ (console_20260901_102907_74088.log ほか) で確認した事実:

  * /orders     pending_monitor が 5 秒 timeout を反復 (本ログ 286 件)
                force_cancel_loop の get_orders も反復 timeout (766 件)
  * /positions  約 3 秒 timeout を反復。broker_ok=False が 741 件すべて。
  * ranking     ALL/TP/TS/TG すべて 5 秒 x 2 回 = 約 10.4s/区分 で失敗し、
                それでも 28 呼び出しを最後まで実行 (1 サイクル 305 秒)。
  * Discord     TLS CERTIFICATE_VERIFY_FAILED が 385 件。
                例外ログに Webhook URL 全体 (ID + トークン) が出ていた。

このテストが守る不変条件:

  1. broker(/positions) が読めないとき entry_allowed=False /
     positions_known=False になる (新規 ENTRY は fail-closed)。
  2. その状態でも EXIT 向けの DB スナップショットは維持される
     (positions を 0 件に潰さない)。
  3. broker 失敗は指数バックオフされ、REST を連打しない。
  4. ranking は連続 timeout 後に残りの呼び出しを打ち切り、
     次サイクルは breaker で即時 skip、期限後 probe で復帰できる。
  5. /orders は TTL + singleflight + 失敗時バックオフで重複読み取りを抑える。
     読めなかったことを「注文 0 件」と混同しない。
  6. Discord の例外ログに Webhook URL / トークンが出ず、
     TLS 失敗時は連打せずバックオフする (verify=False は使わない)。

実注文 / 取消 / 返済は一切発生しない (すべてモック)。
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

PASS = 0
FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  OK   {label} {detail}")
    else:
        FAIL += 1
        print(f"  FAIL {label} {detail}")


def section(title):
    print()
    print("=" * 70)
    print(title)
    print("=" * 70)


# ============================================================
# [1] positions: broker timeout -> ENTRY fail-closed / EXIT 継続
# ============================================================

def test_positions_broker_timeout_fail_closed() -> None:
    section("[1] broker(/positions) timeout 時の fail-closed と EXIT 継続")

    import trading.position.position_state_service as pss

    os.environ["POSITION_STATE_CACHE_TTL_SEC"] = "0.2"
    os.environ["POSITION_STATE_BROKER_BACKOFF_SEC"] = "1,2,5"

    db_rows = {
        "7203": {"symbol": "7203", "qty": 100, "entry_price": 2500.0,
                 "side": "BUY", "_position_source": "DB.positions"},
    }

    def db_with():
        return dict(db_rows)

    def db_empty():
        return {}

    def broker_timeout():
        return {}, False, "timed out"

    def broker_ok():
        return {}, True, ""

    # ---- 1-a: DB に建玉あり / broker timeout ----
    pss._read_db_positions_raising = db_with
    pss._read_broker_positions = broker_timeout
    pss.reset_for_test()

    st = pss.get_positions_state(force=True, caller="r19.broker_timeout.with_db")
    check("db_ok=True", st.db_ok)
    check("broker_ok=False", not st.broker_ok)
    check("entry_allowed=False (新規ENTRYは fail-closed)", not st.entry_allowed,
          f"entry_allowed={st.entry_allowed}")
    check("positions_known=False", not st.positions_known,
          f"positions_known={st.positions_known}")
    check("entry_block_reason=broker_unreadable",
          st.entry_block_reason == "broker_unreadable", st.entry_block_reason)
    check("EXIT 向けに DB の建玉は維持される",
          "7203" in st.positions and st.count == 1,
          f"count={st.count} symbols={st.symbols}")
    check("source_used=db", st.source_used == pss.SOURCE_DB, st.source_used)

    # ---- 1-b: DB 0 件 / broker timeout -> 0 件を「建玉なし」と確定しない ----
    pss._read_db_positions_raising = db_empty
    pss._read_broker_positions = broker_timeout
    pss.reset_for_test()

    st = pss.get_positions_state(force=True, caller="r19.broker_timeout.empty_db")
    check("DB 0 件 + broker timeout でも entry_allowed=False",
          not st.entry_allowed, f"entry_allowed={st.entry_allowed}")
    check("DB 0 件 + broker timeout でも positions_known=False",
          not st.positions_known, f"positions_known={st.positions_known}")
    check("entry_allowed() ヘルパも False",
          not pss.entry_allowed(caller="r19"), "")

    # ---- 1-c: broker 復旧 -> ENTRY 再開 ----
    pss._read_db_positions_raising = db_empty
    pss._read_broker_positions = broker_ok
    pss.reset_for_test()

    st = pss.get_positions_state(force=True, caller="r19.broker_recovered")
    check("broker 復旧後は positions_known=True", st.positions_known)
    check("broker 復旧後は entry_allowed=True", st.entry_allowed)

    # ---- 1-d: DB も broker も NG -> 従来どおり last-good を保持 ----
    def db_io_error():
        raise sqlite3.OperationalError("disk I/O error")

    pss._read_db_positions_raising = db_with
    pss._read_broker_positions = broker_ok
    pss.reset_for_test()
    pss.get_positions_state(force=True, caller="r19.warmup_last_good")

    pss._read_db_positions_raising = db_io_error
    pss._read_broker_positions = broker_timeout
    st = pss.get_positions_state(force=True, caller="r19.both_ng")
    check("DB/broker 両方 NG は positions_known=False", not st.positions_known)
    check("DB/broker 両方 NG でも last-good を 0 件にしない",
          st.count == 1 and "7203" in st.positions,
          f"count={st.count} symbols={st.symbols}")
    check("entry_block_reason=db_and_broker_unreadable",
          st.entry_block_reason == "db_and_broker_unreadable", st.entry_block_reason)

    os.environ.pop("POSITION_STATE_CACHE_TTL_SEC", None)
    os.environ.pop("POSITION_STATE_BROKER_BACKOFF_SEC", None)


def test_positions_broker_backoff() -> None:
    section("[2] broker(/positions) 失敗時のバックオフ (REST を連打しない)")

    import trading.position.position_state_service as pss

    os.environ["POSITION_STATE_CACHE_TTL_SEC"] = "0.01"
    os.environ["POSITION_STATE_BROKER_BACKOFF_SEC"] = "5,10,30"

    calls = {"broker": 0}

    def db_empty():
        return {}

    def broker_timeout():
        calls["broker"] += 1
        return {}, False, "timed out"

    pss._read_db_positions_raising = db_empty
    pss._read_broker_positions = broker_timeout
    pss.reset_for_test()

    for _ in range(30):
        pss.get_positions_state(force=True, caller="r19.backoff")

    check("broker read は 1 回だけ (以降は 5 秒バックオフで skip)",
          calls["broker"] == 1, f"broker_calls={calls['broker']}")

    st = pss.get_positions_state(force=True, caller="r19.backoff.state")
    check("バックオフ中も broker_ok=False のまま (安全側)", not st.broker_ok)
    check("バックオフ中も entry_allowed=False", not st.entry_allowed)
    check("broker_error に backoff が出る", "backoff" in st.broker_error, st.broker_error)

    os.environ.pop("POSITION_STATE_CACHE_TTL_SEC", None)
    os.environ.pop("POSITION_STATE_BROKER_BACKOFF_SEC", None)


# ============================================================
# [3] ENTRY 中枢ゲート (can_entry_symbol)
# ============================================================

def test_entry_gate_blocks_on_unknown_positions() -> None:
    section("[3] can_entry_symbol は建玉状態が確定しないと ENTRY を止める")

    import trading.position.position_state_service as pss
    import trading.summary.position_filter as pf
    from global_state import global_data

    os.environ["POSITION_STATE_CACHE_TTL_SEC"] = "0.2"
    os.environ["POSITION_STATE_BROKER_BACKOFF_SEC"] = "0.5"
    os.environ["ALLOW_ORDERS"] = "1"
    os.environ.pop("ENTRY_POSITION_STATE_GATE_ENABLED", None)

    try:
        global_data.allow_orders = True
    except Exception:
        pass
    try:
        global_data.open_positions = {}
        global_data.entry_inflight = set()
        global_data.entry_cooldown_until = {}
    except Exception:
        pass

    def db_empty():
        return {}

    # broker timeout -> ENTRY 禁止
    pss._read_db_positions_raising = db_empty
    pss._read_broker_positions = lambda: ({}, False, "timed out")
    pss.reset_for_test()
    pss.get_positions_state(force=True, caller="r19.gate.warm")

    check("broker timeout 時は can_entry_symbol=False",
          pf.can_entry_symbol("7203", "BUY", source="SUMMARY") is False)

    # broker 復旧 -> ENTRY 再開
    pss._read_broker_positions = lambda: ({}, True, "")
    pss.reset_for_test()
    pss.get_positions_state(force=True, caller="r19.gate.recovered")

    check("broker 復旧後は can_entry_symbol=True",
          pf.can_entry_symbol("7203", "BUY", source="SUMMARY") is True)

    # ---- 建玉状態サービス自体が使えない場合も fail-closed ----
    # 「状態を取れない」は「建玉が無い」ではない。許可側へ倒してはならない。
    _real_get_state = pss.get_positions_state
    try:
        def _raise(*a, **k):
            raise RuntimeError("position state service unavailable")

        pss.get_positions_state = _raise
        check("get_positions_state が例外なら can_entry_symbol=False",
              pf.can_entry_symbol("7203", "BUY", source="SUMMARY") is False)
        blocked, why = pf._positions_state_gate_blocked()
        check("理由に service_error が入る", blocked and "service_error" in why, why)
        check("理由に例外型が入る", "RuntimeError" in why, why)

        pss.get_positions_state = lambda *a, **k: None
        check("get_positions_state が None なら can_entry_symbol=False",
              pf.can_entry_symbol("7203", "BUY", source="SUMMARY") is False)
        blocked, why = pf._positions_state_gate_blocked()
        check("理由に state_unavailable が入る",
              blocked and "state_unavailable" in why, why)
    finally:
        pss.get_positions_state = _real_get_state

    # 復旧後は通る (上の差し替えを戻せば元に戻ること)
    pss._read_broker_positions = lambda: ({}, True, "")
    pss.reset_for_test()
    pss.get_positions_state(force=True, caller="r19.gate.after_service_error")
    check("サービス復旧後は can_entry_symbol=True",
          pf.can_entry_symbol("7203", "BUY", source="SUMMARY") is True)

    # 明示的な運用解除フラグ
    os.environ["ENTRY_POSITION_STATE_GATE_ENABLED"] = "0"
    pss._read_broker_positions = lambda: ({}, False, "timed out")
    pss.reset_for_test()
    pss.get_positions_state(force=True, caller="r19.gate.override")
    check("ENTRY_POSITION_STATE_GATE_ENABLED=0 で従来動作へ戻せる",
          pf.can_entry_symbol("7203", "BUY", source="SUMMARY") is True)
    os.environ.pop("ENTRY_POSITION_STATE_GATE_ENABLED", None)

    os.environ.pop("POSITION_STATE_CACHE_TTL_SEC", None)
    os.environ.pop("POSITION_STATE_BROKER_BACKOFF_SEC", None)


# ============================================================
# [4] ranking: fail-fast + circuit breaker + probe
# ============================================================

def test_ranking_fail_fast_and_breaker() -> None:
    section("[4] ranking は連続 timeout で残りを打ち切り、breaker で skip / probe で復帰")

    import trading.ranking.api_circuit as circuit
    import trading.ranking.collectors as collectors

    os.environ["RANKING_API_FAIL_FAST_STREAK"] = "3"
    os.environ["RANKING_API_BREAKER_BACKOFF_SEC"] = "1,2,4"
    os.environ["RANKING_API_PROBE_TIMEOUT_SEC"] = "2"
    os.environ["RANKING_API_CALL_SLEEP_SEC"] = "0"

    circuit.reset_for_test()

    calls = {"n": 0, "probe_timeouts": []}

    def api_timeout(params, *, timeout_sec=None, retry_max=None):
        calls["n"] += 1
        if timeout_sec is not None:
            calls["probe_timeouts"].append(timeout_sec)
        return None  # api_client は失敗時 None を返す

    collectors.get_data_from_api = api_timeout

    class _NullSnapshot:
        def add(self, *a, **k):
            return None

    now = dt.datetime(2026, 9, 1, 12, 30, 0)

    # ---- 4-a: 1 サイクル目は fail-fast で打ち切る ----
    raw, snap = collectors.collect_ranking_rows(now, _NullSnapshot())
    check("失敗サイクルは 28 呼び出しを最後まで実行しない",
          calls["n"] == 3, f"api_calls={calls['n']} (期待=3)")
    check("収集結果は空", raw == [] and snap == [])
    st = circuit.stats()
    check("breaker が開いた", st["open"], str(st))

    # ---- 4-b: breaker が開いている間は即時 skip ----
    before = calls["n"]
    t0 = time.perf_counter()
    collectors.collect_ranking_rows(now, _NullSnapshot())
    elapsed = time.perf_counter() - t0
    check("breaker 中は API を 1 回も叩かない", calls["n"] == before,
          f"api_calls={calls['n']}")
    check("breaker 中は即時 skip (<0.5s)", elapsed < 0.5, f"elapsed={elapsed:.3f}s")

    # ---- 4-c: バックオフ期限後は probe が 1 回だけ走る ----
    time.sleep(1.1)
    before = calls["n"]
    collectors.collect_ranking_rows(now, _NullSnapshot())
    check("バックオフ期限後の probe は 1 呼び出しだけ",
          calls["n"] == before + 1, f"delta={calls['n'] - before}")
    check("probe は短い timeout を使う",
          calls["probe_timeouts"] and calls["probe_timeouts"][-1] == 2.0,
          str(calls["probe_timeouts"][-1:]))
    st = circuit.stats()
    check("probe 失敗で breaker は開き直す", st["open"], str(st))

    # ---- 4-d: probe 成功で通常サイクルへ復帰 ----
    ok_payload = {"Ranking": []}

    def api_ok(params, *, timeout_sec=None, retry_max=None):
        calls["n"] += 1
        return ok_payload

    collectors.get_data_from_api = api_ok
    time.sleep(2.1)
    before = calls["n"]
    collectors.collect_ranking_rows(now, _NullSnapshot())
    check("復旧 probe は 1 呼び出しだけ", calls["n"] == before + 1,
          f"delta={calls['n'] - before}")
    st = circuit.stats()
    check("probe 成功で breaker が閉じた", not st["open"], str(st))

    before = calls["n"]
    collectors.collect_ranking_rows(now, _NullSnapshot())
    check("復帰後は通常どおり 28 呼び出し", calls["n"] - before == 28,
          f"delta={calls['n'] - before}")

    for k in ("RANKING_API_FAIL_FAST_STREAK", "RANKING_API_BREAKER_BACKOFF_SEC",
              "RANKING_API_PROBE_TIMEOUT_SEC", "RANKING_API_CALL_SLEEP_SEC"):
        os.environ.pop(k, None)
    circuit.reset_for_test()


# ============================================================
# [5] /orders: TTL + singleflight + backoff
# ============================================================

def test_orders_read_cache() -> None:
    section("[5] /orders は TTL / singleflight / 失敗時バックオフで重複読み取りを抑える")

    import threading

    from kabu_api import orders_read_cache as orc

    os.environ["KABU_ORDERS_CACHE_TTL_SEC"] = "2.0"
    os.environ["KABU_ORDERS_FAIL_BACKOFF_SEC"] = "5,10"
    orc.reset_for_test()

    fetches = {"n": 0}

    def fetch_ok():
        fetches["n"] += 1
        return [{"OrderId": "X1", "State": 3}], True, ""

    # ---- 5-a: TTL 内は 1 回だけ ----
    for _ in range(20):
        res = orc.get_orders_cached(fetch_ok, caller="r19")
        assert res.ok
    check("TTL 内の重複 GET は 1 回にまとまる", fetches["n"] == 1,
          f"fetches={fetches['n']}")

    # ---- 5-b: singleflight ----
    orc.reset_for_test()
    fetches["n"] = 0
    barrier = threading.Event()

    def fetch_slow():
        fetches["n"] += 1
        barrier.wait(1.0)
        return [{"OrderId": "X1", "State": 3}], True, ""

    threads = [threading.Thread(target=lambda: orc.get_orders_cached(fetch_slow, caller="r19"))
               for _ in range(8)]
    for t in threads:
        t.start()
    time.sleep(0.2)
    barrier.set()
    for t in threads:
        t.join(3.0)
    check("同時 8 caller でも実 GET は 1 本 (singleflight)", fetches["n"] == 1,
          f"fetches={fetches['n']}")

    # ---- 5-c: 失敗時バックオフ ----
    orc.reset_for_test()
    fetches["n"] = 0

    def fetch_timeout():
        fetches["n"] += 1
        return [], False, "ReadTimeout"

    results = [orc.get_orders_cached(fetch_timeout, caller="r19") for _ in range(30)]
    check("timeout 後は 5 秒バックオフで GET を打たない", fetches["n"] == 1,
          f"fetches={fetches['n']}")
    check("読めなかった結果は ok=False", all(not r.ok for r in results))
    check("読めなかった結果を 0 件と混同しない (orders は空 + ok=False)",
          all(r.orders == [] for r in results))
    check("バックオフ中は source=backoff_skip",
          results[-1].source == orc.SOURCE_BACKOFF, results[-1].source)

    # ---- 5-d: 失敗時に古いキャッシュを配らない ----
    orc.reset_for_test()
    fetches["n"] = 0
    orc.get_orders_cached(fetch_ok, caller="r19")
    time.sleep(2.1)  # TTL 切れ
    res = orc.get_orders_cached(fetch_timeout, caller="r19")
    check("失敗時に古い注文状態を返さない (誤キャンセル防止)",
          (not res.ok) and res.orders == [], f"ok={res.ok} orders={res.orders}")

    # ---- 5-e: invalidate ----
    orc.reset_for_test()
    fetches["n"] = 0
    orc.get_orders_cached(fetch_ok, caller="r19")
    orc.invalidate(reason="test")
    orc.get_orders_cached(fetch_ok, caller="r19")
    check("invalidate 後は必ず取り直す (取消送信直後)", fetches["n"] == 2,
          f"fetches={fetches['n']}")

    os.environ.pop("KABU_ORDERS_CACHE_TTL_SEC", None)
    os.environ.pop("KABU_ORDERS_FAIL_BACKOFF_SEC", None)
    orc.reset_for_test()


def test_orders_consumers_share_cache() -> None:
    section("[6] pending_monitor と force_cancel_loop が同じ /orders キャッシュを共有する")

    from kabu_api import orders_read_cache as orc

    os.environ["KABU_ORDERS_CACHE_TTL_SEC"] = "2.0"
    orc.reset_for_test()

    import force_cancel_loop as fcl
    import trading.handlers.pending_monitor as pm

    gets = {"n": 0}

    def fake_fetch():
        gets["n"] += 1
        return [{"OrderId": "X1", "State": 3}], True, ""

    pm._fetch_orders_once = fake_fetch
    fcl._fetch_orders_once = fake_fetch

    a = pm.get_orders()
    b = fcl.get_orders()
    c = pm.get_orders()
    check("2 経路 x 3 回の読み取りで実 GET は 1 本",
          gets["n"] == 1, f"gets={gets['n']}")
    check("両経路とも同じ注文一覧を得る",
          a == b == c and len(a) == 1, f"a={a} b={b}")

    # 読めない場合、互換 API は従来どおり [] を返す
    orc.reset_for_test()
    gets["n"] = 0

    def fake_fetch_ng():
        gets["n"] += 1
        return [], False, "ReadTimeout"

    pm._fetch_orders_once = fake_fetch_ng
    fcl._fetch_orders_once = fake_fetch_ng
    check("読めないときの互換 get_orders() は []", pm.get_orders() == [])
    res = fcl.get_orders_state()
    check("get_orders_state() は ok=False で 0 件と区別できる", not res.ok, res.source)

    os.environ.pop("KABU_ORDERS_CACHE_TTL_SEC", None)
    orc.reset_for_test()


# ============================================================
# [7] Discord: 秘密値マスク + TLS ブレーカー
# ============================================================

class _CaptureHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        try:
            self.records.append(self.format(record))
        except Exception:
            self.records.append(str(record.msg))

    def text(self):
        return "\n".join(self.records)


def test_discord_masking_and_breaker() -> None:
    section("[7] Discord: Webhook URL をログに出さない / TLS 失敗で連打しない")

    import requests as _requests

    import utils.alerts_util as au

    secret_id = "1335823633867739168"
    secret_token = "o1KusD-dSp6XVGoennYY6S_G_fyOQvOAH5NvR7_Os1yOU03_pyF-dF1hzSWVEkYgdVox"
    webhook = f"https://discordapp.com/api/webhooks/{secret_id}/{secret_token}"

    # ---- 7-a: マスク関数 ----
    masked = au._mask_webhook_url(webhook)
    check("マスク後に webhook トークンが残らない", secret_token not in masked, masked)
    check("マスク後に webhook ID が残らない", secret_id not in masked, masked)
    check("ホスト名は診断用に残る", "discordapp.com" in masked, masked)

    exc_text = (
        "HTTPSConnectionPool(host='discordapp.com', port=443): Max retries exceeded "
        f"with url: /api/webhooks/{secret_id}/{secret_token} "
        "(Caused by SSLError(SSLCertVerificationError(1, '[SSL: CERTIFICATE_VERIFY_FAILED]')))"
    )
    scrubbed = au._mask_secrets(exc_text)
    check("例外文字列からトークンが落ちる", secret_token not in scrubbed, scrubbed[:120])
    check("例外文字列から ID が落ちる", secret_id not in scrubbed, scrubbed[:120])

    # ---- 7-b: 実際の送信失敗ログに秘密値が出ない ----
    au.reset_discord_transport_breaker()
    os.environ["DISCORD_TRANSPORT_BACKOFF_SCALE"] = "1"

    handler = _CaptureHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    au.logger.addHandler(handler)
    au.logger.setLevel(logging.DEBUG)

    posts = {"n": 0, "verify": []}

    def fake_post(url, **kwargs):
        posts["n"] += 1
        posts["verify"].append(kwargs.get("verify"))
        raise _requests.exceptions.SSLError(exc_text)

    real_post = _requests.post
    _requests.post = fake_post
    try:
        ok1 = au.send_discord_message(content="test", webhook_url=webhook)
        check("TLS 失敗時は False を返す", ok1 is False)

        # ブレーカーが開いている間は送信しない
        for _ in range(20):
            au.send_discord_message(content="test", webhook_url=webhook)
        check("TLS 失敗後は連打しない (POST は 1 回だけ)", posts["n"] == 1,
              f"posts={posts['n']}")

        text = handler.text()
        check("ログに webhook トークンが出ない", secret_token not in text)
        check("ログに webhook ID が出ない", secret_id not in text)
        check("TLS 失敗が診断できるログは出ている",
              "Discord通信失敗" in text or "SSLError" in text)
        check("verify=False を渡していない",
              all(v is not False for v in posts["verify"]), str(posts["verify"]))
        check("CA バンドルを明示している",
              all(v is True or isinstance(v, str) for v in posts["verify"]),
              str(posts["verify"]))

        st = au.discord_transport_status()
        check("ブレーカーが開いている", st["open"], str(st))
        check("ブレーカー状態にも秘密値が出ない",
              secret_token not in str(st) and secret_id not in str(st), str(st))
    finally:
        _requests.post = real_post
        au.logger.removeHandler(handler)
        au.reset_discord_transport_breaker()
        os.environ.pop("DISCORD_TRANSPORT_BACKOFF_SCALE", None)

    # ---- 7-c: CA 解決は verify=False を返さない ----
    verify = au._resolve_ca_bundle()
    check("_resolve_ca_bundle は False を返さない", verify is not False, str(verify))


# ============================================================
if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL)

    test_positions_broker_timeout_fail_closed()
    test_positions_broker_backoff()
    test_entry_gate_blocks_on_unknown_positions()
    test_ranking_fail_fast_and_breaker()
    test_orders_read_cache()
    test_orders_consumers_share_cache()
    test_discord_masking_and_breaker()

    print()
    print("=" * 70)
    print(f"PASS={PASS} FAIL={FAIL}")
    print("=" * 70)
    sys.exit(1 if FAIL else 0)
