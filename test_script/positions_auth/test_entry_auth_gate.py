# trading/summary/position_filter.py の認証ゲートを検証する。
#
# 本番 API には接続しない。kabu_api.positions の健全性フラグを
# 直接書き換えて、ENTRY が止まるかどうかだけを見る。
#
# 確認したいこと:
#   1. 平常時は今日までと同じように通る（誤って全発注を止めない）
#   2. 401/403 を観測したときだけ止まる
#   3. 認証以外の失敗（500・通信断・429）では止めない
#   4. 認証が回復したら自動で通るようになる
#   5. allow_orders=True でも認証異常なら止まる（ここが今回の要点）
#   6. ENTRY_AUTH_GATE_ENABLED=0 で解除できる
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
S = r"W:/BUSINESS/事業/04_株式自動売買システム/ソースコード"
sys.path.insert(0, S)

import kabu_api.positions as P                      # noqa: E402
import trading.summary.position_filter as PF        # noqa: E402
from global_state import global_data                # noqa: E402

results = []


def set_health(healthy, failure):
    P._POS_FETCH_HEALTHY = healthy
    P._POS_LAST_FAILURE = failure


def case(name, healthy, failure, allow_orders, env, expect_pass):
    set_health(healthy, failure)
    global_data.allow_orders = allow_orders
    for k in ("ENTRY_AUTH_GATE_ENABLED",):
        os.environ.pop(k, None)
    if env:
        os.environ.update(env)
    got = PF._ensure_allow_orders_if_possible("7203", "BUY", "SUMMARY")
    ok = (got == expect_pass)
    results.append(ok)
    print("%-34s healthy=%-6s failure=%-10s allow=%-6s -> %-5s %s"
          % (name, healthy, failure or "-", allow_orders,
             "通る" if got else "止める", "OK" if ok else "NG"))
    for k in (env or {}):
        os.environ.pop(k, None)


print("=== 平常時：今日までと同じ挙動であること ===")
case("正常・allow_orders=True", True, "", True, None, True)
case("正常・allow_orders=False", True, "", False, None, True)   # env既定で復旧する

print()
print("=== 認証異常：止まること ===")
case("401観測・allow_orders=True", False, "auth", True, None, False)
case("401観測・allow_orders=False", False, "auth", False, None, False)

print()
print("=== 認証以外の失敗：止めないこと（誤停止の防止） ===")
case("500 サーバエラー", False, "http", True, None, True)
case("通信断", False, "network", True, None, True)
case("429 キャッシュ無し", False, "rate_limited_no_cache", True, None, True)
case("想定外例外", False, "unexpected", True, None, True)

print()
print("=== 回復 ===")
case("401のあと取得成功", True, "", True, None, True)

print()
print("=== 解除スイッチ ===")
case("401だがゲート無効", False, "auth", True, {"ENTRY_AUTH_GATE_ENABLED": "0"}, True)

print()
print("=== 健全性が取れない場合は止めない ===")
_orig = P.positions_health
try:
    P.positions_health = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    global_data.allow_orders = True
    got = PF._ensure_allow_orders_if_possible("7203", "BUY", "SUMMARY")
    results.append(got is True)
    print("  positions_health が例外 -> %s %s"
          % ("通る" if got else "止める", "OK" if got else "NG"))
finally:
    P.positions_health = _orig

print()
print("結果: %d/%d 合格" % (sum(1 for r in results if r), len(results)))
raise SystemExit(0 if all(results) else 1)
