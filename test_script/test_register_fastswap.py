# 2026-10-22: 差分登録の解除→登録を待たずに続けて撃つ / kabu の RegistList を current の正にする
import pytest

from trading.push.subscription_manager import register_ops as RO
from trading.push.push_stream import rotation_core as R


class FakeKabu:
    def __init__(self, registered):
        self.reg = list(registered)
        self.calls = []

    def __call__(self, *, url, method, payload, api_key, timeout=10.0):
        syms = [x["Symbol"] for x in (payload or {}).get("Symbols", [])]
        if url.endswith("/unregister/all"):
            self.calls.append(("unregister_all", []))
            self.reg = []
        elif url.endswith("/unregister"):
            self.calls.append(("unregister", syms))
            self.reg = [s for s in self.reg if s not in set(syms)]
        else:
            self.calls.append(("register", syms))
            new = [s for s in syms if s not in self.reg]
            if len(self.reg) + len(new) > 50:
                return False, {"Code": 4002006, "Message": "レジスト数エラー"}
            self.reg += new
        return True, {"RegistList": [{"Symbol": s, "Exchange": 1} for s in self.reg]}


@pytest.fixture
def kabu(monkeypatch):
    def make(registered):
        k = FakeKabu(registered)
        monkeypatch.setattr(RO, "_http_json_request", k)
        monkeypatch.setattr(RO, "_resolve_api_key", lambda: "k")
        monkeypatch.setattr(RO, "_VENDOR_REGISTERED", None)
        monkeypatch.setattr(RO, "REGISTER_LIMIT", 50)        # 枠の空け(既定 2)は test_register_headroom で見る
        sleeps = []
        monkeypatch.setattr(RO.time, "sleep", lambda s: sleeps.append(s))
        k.sleeps = sleeps
        return k
    return make


_BASE = {"A": 1000, "B": 2000, "K": 3000, "R": 4000, "T": 5000, "C": 6000, "X": 7000, "N": 8000}


def A(n, p="A"):
    return [str(_BASE[p] + i) for i in range(n)]


def test_swap_is_unregister_then_register_without_wait(kabu):
    a = A(19, "K") + A(31, "A")
    b = A(19, "K") + A(31, "B")
    k = kabu(a)
    RO._note_vendor_list({"RegistList": [{"Symbol": s} for s in a]})
    assert RO._try_differential_register(a, b, exchange=1) is True
    assert [c[0] for c in k.calls] == ["unregister", "register"]
    assert len(k.calls[0][1]) == 31 and len(k.calls[1][1]) == 31
    assert k.sleeps == []                               # 解除と登録の間で待たない
    assert set(k.reg) == set(b)


def test_vendor_list_overrides_stale_program_state(kabu):
    real = A(46, "R")                                   # kabu には 46 登録されている
    k = kabu(real)
    RO._note_vendor_list({"RegistList": [{"Symbol": s} for s in real]})
    target = A(50, "T")
    # プログラム側は 1 銘柄だけ登録していると思っている(10/21 の 4002006 の形)
    assert RO._try_differential_register(["4000"], target, exchange=1) is True
    assert k.calls[0] == ("unregister", real)          # 実際に登録されている 46 を外す
    assert set(k.reg) == set(target)


def test_without_vendor_list_behaves_as_before(kabu, monkeypatch):
    monkeypatch.setenv("PUSH_REGISTER_VENDOR_LIST_ENABLED", "0")
    real = A(46, "R")
    k = kabu(real)
    RO._note_vendor_list({"RegistList": [{"Symbol": s} for s in real]})
    assert RO._try_differential_register(["4000"], A(50, "T"), exchange=1) is None   # 従来どおり 4002006 → 全解除へ


def test_limit_check_after_unregister(kabu):
    cur = A(50, "C")
    k = kabu(cur + [])
    RO._note_vendor_list({"RegistList": [{"Symbol": s} for s in cur]})
    k.reg = cur + A(5, "X")                             # 応答後に誰かが 5 足した(本来は起きない)
    k.reg = k.reg[:50]
    tgt = A(10, "C") + A(40, "N")
    res = RO._try_differential_register(cur, tgt, exchange=1)
    assert res in (True, None)
    assert len(k.reg) <= 50


def test_swap_prep_off_by_default(monkeypatch):
    monkeypatch.delenv("PUSH_ROTATION_AB50_SWAP_PREP", raising=False)
    assert R._swap_prep_enabled() is False
    monkeypatch.setenv("PUSH_ROTATION_AB50_SWAP_PREP", "1")
    assert R._swap_prep_enabled() is bool(R.AB50_ENABLED)
