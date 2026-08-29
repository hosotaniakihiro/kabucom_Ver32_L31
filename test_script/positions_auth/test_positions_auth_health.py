# kabu_api/positions.py の 401 経路を検証する。
# 本番 API には接続しない。requests.get をモックに差し替えるだけ。
import sys
import types

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
S = r"W:/BUSINESS/事業/04_株式自動売買システム/ソースコード"
sys.path.insert(0, S)


class _Resp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else []

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            err = requests.exceptions.HTTPError("%d Client Error" % self.status_code)
            err.response = self
            raise err


def run():
    import kabu_api.positions as P
    import requests

    # token を通す
    from global_state import global_data
    global_data.token_value = "dummy-token-32chars-000000000000"

    results = []

    def scenario(name, responses, expect_healthy, expect_failure):
        P._POS_CACHE = None
        P._POS_CACHE_TIME = 0.0
        P._POS_FETCH_HEALTHY = True
        P._POS_LAST_FAILURE = ""
        seq = list(responses)
        requests.get = lambda *a, **k: seq.pop(0) if seq else _Resp(500)
        ret = P._get_positions_impl()
        h = P.positions_fetch_healthy()
        f = P.positions_health()["last_failure"]
        ok = (h == expect_healthy) and (f == expect_failure)
        results.append(ok)
        print("%-26s ret=%-6s healthy=%-6s last_failure=%-12s %s"
              % (name, ret, h, f or "(なし)", "OK" if ok else "NG"))

    scenario("正常 (200・建玉ゼロ)", [_Resp(200, [])], True, "")
    scenario("正常 (200・建玉あり)", [_Resp(200, [{"Symbol": "7203"}])], True, "")
    scenario("401 認証失敗", [_Resp(401)], False, "auth")
    scenario("403 認証失敗", [_Resp(403)], False, "auth")
    scenario("500 サーバエラー", [_Resp(500)], False, "http")
    # 429 はキャッシュがあれば健全（古いが本物のデータを返す）、
    # 無ければ [] が「建玉ゼロ」に見えてしまうので不健全にする。
    scenario("429 (キャッシュ無し)", [_Resp(429)], False, "rate_limited_no_cache")

    def scenario_with_cache(name, responses, expect_healthy, expect_failure):
        P._POS_CACHE = [{"Symbol": "9984"}]
        P._POS_CACHE_TIME = 0.0
        P._POS_FETCH_HEALTHY = True
        P._POS_LAST_FAILURE = ""
        seq = list(responses)
        requests.get = lambda *a, **k: seq.pop(0) if seq else _Resp(500)
        ret = P._get_positions_impl()
        h = P.positions_fetch_healthy()
        f = P.positions_health()["last_failure"]
        ok = (h == expect_healthy) and (f == expect_failure)
        results.append(ok)
        print("%-26s ret=%-6s healthy=%-6s last_failure=%-12s %s"
              % (name, len(ret), h, f or "(なし)", "OK" if ok else "NG"))

    scenario_with_cache("429 (キャッシュ有り)", [_Resp(429)], True, "")
    scenario_with_cache("401 (キャッシュ有り)", [_Resp(401)], False, "auth")

    print()
    print("★ 重要: 401 と「建玉ゼロ」の区別")
    P._POS_CACHE = None
    P._POS_FETCH_HEALTHY = True
    P._POS_LAST_FAILURE = ""
    requests.get = lambda *a, **k: _Resp(200, [])
    a = P._get_positions_impl()
    print("  建玉ゼロ : ret=%s  healthy=%s" % (a, P.positions_fetch_healthy()))

    P._POS_CACHE = None
    P._POS_CACHE_TIME = 0.0
    P._POS_FETCH_HEALTHY = True
    P._POS_LAST_FAILURE = ""
    requests.get = lambda *a, **k: _Resp(401)
    b = P._get_positions_impl()
    print("  401      : ret=%s  healthy=%s" % (b, P.positions_fetch_healthy()))
    distinguishable = (a == b) and (P.positions_fetch_healthy() is False)
    results.append(distinguishable)
    print("  戻り値は同じだが healthy で区別できる: %s" % ("OK" if distinguishable else "NG"))

    print()
    print("=== 例外を投げないという既存契約 ===")
    P._POS_CACHE = None
    requests.get = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        r = P._get_positions_impl()
        print("  想定外例外でも投げない: OK (ret=%s)" % r)
        results.append(True)
    except Exception as e:
        print("  NG: 例外が漏れた %r" % e)
        results.append(False)

    print()
    print("結果: %d/%d 合格" % (sum(1 for r in results if r), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(run())
