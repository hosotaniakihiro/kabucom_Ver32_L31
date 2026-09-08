# -*- coding: utf-8 -*-
"""
第18ラウンド: token preflight の unreachable / no_response 分離 (実注文なし)

2026-08-28 の夜間検証で、失効トークンに対して kabu Station が
401 ではなく「12 秒ハング後に RST」を返すことを実測した。
旧実装はこれを「kabu Station に繋がらない」と扱って abort し、
トークン再取得を試みないため自力復旧できなかった。

  - TCP すら繋がらない (接続拒否)        -> unreachable -> 従来どおり abort
  - 繋がったが応答が返らない (RST/timeout) -> no_response -> 1 回だけ再取得
"""
from __future__ import annotations

import os
import socket
import sys
import urllib.error

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import kabu_api.token_bootstrap as tb

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


# ============================================================
print("[1] classify_transport_error")
# ============================================================
CASES = [
    ("接続拒否", ConnectionRefusedError(10061, "対象のコンピューターに拒否されたため"),
     tb.TRANSPORT_UNREACHABLE),
    ("名前解決失敗", socket.gaierror(11001, "getaddrinfo failed"),
     tb.TRANSPORT_UNREACHABLE),
    ("接続リセット (今回の実機)", ConnectionResetError(10054, "既存の接続はリモート ホストに強制的に切断されました"),
     tb.TRANSPORT_NO_RESPONSE),
    ("タイムアウト", TimeoutError("timed out"), tb.TRANSPORT_NO_RESPONSE),
    ("socket.timeout", socket.timeout("timed out"), tb.TRANSPORT_NO_RESPONSE),
    ("URLError(拒否) でラップ",
     urllib.error.URLError(ConnectionRefusedError(10061, "refused")),
     tb.TRANSPORT_UNREACHABLE),
    ("URLError(リセット) でラップ",
     urllib.error.URLError(ConnectionResetError(10054, "reset")),
     tb.TRANSPORT_NO_RESPONSE),
    ("文言のみ timed out", RuntimeError("timed out"), tb.TRANSPORT_NO_RESPONSE),
    ("文言のみ refused", RuntimeError("connection refused"), tb.TRANSPORT_UNREACHABLE),
    ("不明な例外は unreachable 側 (安全側)", RuntimeError("???"), tb.TRANSPORT_UNREACHABLE),
]
for label, exc, want in CASES:
    got = tb.classify_transport_error(exc)
    check(f"分類 {label}", got == want, f"{got} (期待 {want})")

# ============================================================
print("[2] preflight_token が kind を分ける")
# ============================================================


def fn_factory(content):
    return lambda url, tok, to: (False, None, content)


pf = tb.preflight_token("tok", request_fn=fn_factory("no_response: timed out"), label="t")
check("RST/timeout -> PF_NO_RESPONSE", pf.kind == tb.PF_NO_RESPONSE, pf.kind)
check("is_no_response が True", pf.is_no_response is True)
check("is_transport_error も True (互換)", pf.is_transport_error is True)

pf = tb.preflight_token("tok", request_fn=fn_factory("unreachable: refused"), label="t")
check("接続拒否 -> PF_TRANSPORT_ERROR", pf.kind == tb.PF_TRANSPORT_ERROR, pf.kind)
check("is_no_response は False", pf.is_no_response is False)

# 接頭辞の無い content (独自 request_fn) は従来どおり
pf = tb.preflight_token("tok", request_fn=fn_factory("something went wrong"), label="t")
check("接頭辞なしは従来どおり transport_error", pf.kind == tb.PF_TRANSPORT_ERROR, pf.kind)

pf = tb.preflight_token("tok", request_fn=lambda u, t, to: (False, 401, {}), label="t")
check("401 は auth_rejected のまま", pf.kind == tb.PF_AUTH_REJECTED, pf.kind)
pf = tb.preflight_token("tok", request_fn=lambda u, t, to: (True, 200, {}), label="t")
check("200 は ok のまま", pf.ok and pf.kind == tb.PF_OK, pf.kind)
pf = tb.preflight_token("tok", request_fn=lambda u, t, to: (False, 500, "err"), label="t")
check("500 は accepted_non_auth のまま", pf.ok and pf.kind == tb.PF_ACCEPTED_NON_AUTH, pf.kind)

# ============================================================
print("[3] bootstrap: no_response ならトークンを取り直す")
# ============================================================


def run_bootstrap(pf_kind, *, cached="oldtoken", fresh_ok=True, allow_recovery=True):
    """bootstrap を 1 回走らせ、(結果, /token 呼び出し回数) を返す。"""
    tb.reset_startup_state()   # 真偽値と試行回数の両方を戻す
    calls = {"token": 0}

    def read_cached():
        return cached

    def request_fresh(apipassword=None, reason=""):
        calls["token"] += 1
        if not fresh_ok:
            raise RuntimeError("token api down")
        return "freshtoken"

    def preflight(token, lbl):
        if token == "freshtoken":
            return tb.PreflightResult(ok=True, kind=tb.PF_OK, endpoint="/x", status=200)
        return tb.PreflightResult(ok=False, kind=pf_kind, endpoint="/x", status=None,
                                  content="%s: simulated" % pf_kind)

    res = tb.bootstrap_startup_token(
        apipassword="pw",
        read_cached_token=read_cached,
        request_fresh_token=request_fresh,
        preflight=preflight,
        allow_startup_recovery=allow_recovery,
        label="test",
    )
    return res, calls["token"]


res, n = run_bootstrap(tb.PF_NO_RESPONSE)
check("no_response なら /token を 1 回呼ぶ", n == 1, f"calls={n}")
check("no_response から復旧して ok になる", res.ok is True, f"ok={res.ok} kind={res.failure_kind}")
check("復旧後のトークンは fresh", res.token == "freshtoken", res.token)

res, n = run_bootstrap(tb.PF_TRANSPORT_ERROR)
check("unreachable では /token を呼ばない", n == 0, f"calls={n}")
check("unreachable は abort のまま", res.ok is False, f"ok={res.ok}")
check("failure_kind=preflight_transport_error",
      res.failure_kind == tb.FAIL_PREFLIGHT_TRANSPORT, res.failure_kind)

res, n = run_bootstrap(tb.PF_AUTH_REJECTED)
check("401 は従来どおり /token を 1 回呼ぶ", n == 1, f"calls={n}")
check("401 から復旧して ok になる", res.ok is True, f"ok={res.ok}")

res, n = run_bootstrap(tb.PF_NO_RESPONSE, allow_recovery=False)
check("recovery 禁止なら no_response でも呼ばない", n == 0, f"calls={n}")
check("failure_kind=preflight_no_response",
      res.failure_kind == tb.FAIL_PREFLIGHT_NO_RESPONSE, res.failure_kind)

res, n = run_bootstrap(tb.PF_NO_RESPONSE, fresh_ok=False)
check("再取得も失敗したら ok=False", res.ok is False, f"ok={res.ok}")
check("再取得は 1 回だけ (撃ち続けない)", n == 1, f"calls={n}")

# ============================================================
print("[4] one-shot ガード: 失敗した /token で食い潰さない")
# ============================================================
# 2026-08-29 10:11 の実機で main_database が abort した原因。
# sitecustomize 段階の startup_policy が先に bootstrap を回して
# one-shot を消費し、その試行が失敗していても消費されたままだった。


def run_with_shared_state(seq):
    """同一プロセスで bootstrap を複数回走らせる (state はリセットしない)。

    seq: [(preflight_kind, fresh_ok), ...]
    戻り値: [(BootstrapResult, その回の /token 呼び出し回数), ...]
    """
    tb.reset_startup_state()
    out = []
    for pf_kind, fresh_ok in seq:
        calls = {"n": 0}

        def request_fresh(apipassword=None, reason="", _ok=fresh_ok, _c=calls):
            _c["n"] += 1
            if not _ok:
                raise RuntimeError("token api down")
            return "freshtoken"

        def preflight(token, lbl, _k=pf_kind):
            if token == "freshtoken":
                return tb.PreflightResult(ok=True, kind=tb.PF_OK, endpoint="/x", status=200)
            return tb.PreflightResult(ok=False, kind=_k, endpoint="/x", status=None,
                                      content="%s: simulated" % _k)

        res = tb.bootstrap_startup_token(
            apipassword="pw",
            read_cached_token=lambda: "oldtoken",
            request_fresh_token=request_fresh,
            preflight=preflight,
            label="phase",
        )
        out.append((res, calls["n"]))
    return out


# 1 回目 (sitecustomize 相当) が失敗 -> 2 回目 (main_database 相当) は呼べる
(r1, n1), (r2, n2) = run_with_shared_state([
    (tb.PF_AUTH_REJECTED, False),   # startup_policy: /token 失敗
    (tb.PF_AUTH_REJECTED, True),    # main_database : /token 成功
])
check("1回目 (失敗) は /token を呼ぶ", n1 == 1, f"calls={n1}")
check("1回目は ok=False", r1.ok is False, f"ok={r1.ok}")
check("**2回目も /token を呼べる (今回の修正点)**", n2 == 1, f"calls={n2}")
check("2回目で復旧して ok になる", r2.ok is True, f"ok={r2.ok} kind={r2.failure_kind}")

# 1 回目が成功したら 2 回目は呼ばない
(r1, n1), (r2, n2) = run_with_shared_state([
    (tb.PF_AUTH_REJECTED, True),
    (tb.PF_AUTH_REJECTED, True),
])
check("1回目が成功したら 2回目は /token を呼ばない", n2 == 0, f"calls={n2}")
check("2回目は recovery_already_used",
      r2.failure_kind == tb.FAIL_RECOVERY_ALREADY_USED, r2.failure_kind)

# 上限 (既定 2) を超えたら打ち切る = ループしない
res = run_with_shared_state([
    (tb.PF_AUTH_REJECTED, False),
    (tb.PF_AUTH_REJECTED, False),
    (tb.PF_AUTH_REJECTED, False),
])
total_calls = sum(n for _, n in res)
check("失敗続きでも上限 2 回で打ち切る (ループしない)", total_calls == 2, f"calls={total_calls}")
check("3回目は recovery_already_used",
      res[2][0].failure_kind == tb.FAIL_RECOVERY_ALREADY_USED, res[2][0].failure_kind)

# env で上限を変えられる
os.environ["KABU_TOKEN_STARTUP_MAX_CALLS"] = "3"
check("KABU_TOKEN_STARTUP_MAX_CALLS で上限を変えられる",
      tb.startup_token_max_calls() == 3, str(tb.startup_token_max_calls()))
os.environ["KABU_TOKEN_STARTUP_MAX_CALLS"] = "0"
check("上限は最低 1 にクランプ", tb.startup_token_max_calls() == 1,
      str(tb.startup_token_max_calls()))
os.environ.pop("KABU_TOKEN_STARTUP_MAX_CALLS", None)

tb.reset_startup_state()
check("reset_startup_state で試行回数も戻る", tb.startup_token_calls() == 0)

print(f"\n結果: {PASS} / {PASS + FAIL}")
sys.exit(1 if FAIL else 0)
