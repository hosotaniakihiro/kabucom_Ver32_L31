# 2026-10-22: 枠 50 のうち 2 つ空けて登録する / 全解除後も 4002006 なら件数を減らしてすぐ入れ直す
import pytest

from trading.push.subscription_manager import register_ops as RO


class HiddenSlotKabu:
    """RegistList に出ない形で枠を hidden 個使っている kabu(10/22 後場の実測の形)。"""

    def __init__(self, registered, hidden):
        self.reg = list(registered)
        self.hidden = hidden
        self.calls = []

    def __call__(self, *, url, method, payload, api_key, timeout=10.0):
        syms = [x["Symbol"] for x in (payload or {}).get("Symbols", [])]
        if url.endswith("/unregister/all"):
            self.calls.append(("unregister_all", 0))
            self.reg = []
        elif url.endswith("/unregister"):
            self.calls.append(("unregister", len(syms)))
            self.reg = [s for s in self.reg if s not in set(syms)]
        else:
            self.calls.append(("register", len(syms)))
            new = [s for s in syms if s not in self.reg]
            if len(self.reg) + len(new) + self.hidden > 50:
                return False, {"Code": 4002006, "Message": "レジスト数エラー"}
            self.reg += new
        return True, {"RegistList": [{"Symbol": s, "Exchange": 1} for s in self.reg]}


@pytest.fixture
def kabu(monkeypatch):
    def make(registered, hidden):
        k = HiddenSlotKabu(registered, hidden)
        monkeypatch.setattr(RO, "_http_json_request", k)
        monkeypatch.setattr(RO, "_resolve_api_key", lambda: "k")
        monkeypatch.setattr(RO, "_VENDOR_REGISTERED", None)
        monkeypatch.setattr(RO, "_reg_state", lambda: None)
        monkeypatch.setattr(RO.time, "sleep", lambda s: None)
        return k
    return make


def syms(n, base):
    return [str(base + i) for i in range(n)]


def test_default_limit_leaves_two_slots():
    assert RO.REGISTER_HEADROOM == 2 and RO.REGISTER_LIMIT == 48
    assert len(RO._truncate_to_register_limit(syms(50, 1000))) == 48


def test_diff_swap_succeeds_with_hidden_slots(kabu):
    cur = syms(48, 1000)
    k = kabu(cur, hidden=2)
    RO._note_vendor_list({"RegistList": [{"Symbol": s} for s in cur]})
    tgt = syms(45, 1000) + syms(5, 2000)                 # 50 渡されても 48 までしか登録しない
    assert RO._try_differential_register(cur, tgt, exchange=1) is True
    assert "unregister_all" not in [c[0] for c in k.calls]
    assert len(k.reg) == 48 and k.reg[-1] == "2002"


def test_full_register_shrinks_instead_of_giving_up(kabu):
    k = kabu(syms(48, 1000), hidden=4)                   # 48 でも入らない
    ok = RO._run_refresh_sequence_core(syms(48, 1000), syms(50, 3000), clear_first=True)
    assert ok is True
    assert k.calls[-1] == ("register", 43)               # 48 → 43 に減らして入った
    assert k.reg == syms(43, 3000)                       # 落ちたのは末尾(順位の低い銘柄)


def test_shrink_disabled(kabu, monkeypatch):
    monkeypatch.setattr(RO, "REGISTER_COUNT_ERROR_SHRINK", 0)
    k = kabu(syms(48, 1000), hidden=4)
    assert RO._run_refresh_sequence_core(syms(48, 1000), syms(50, 3000), clear_first=True) is False
