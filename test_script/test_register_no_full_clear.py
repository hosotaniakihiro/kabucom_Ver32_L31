# 2026-10-29: 差分登録で枠が足りないとき /unregister/all へ戻らず、はみ出しを銘柄ごとに解除する。
#   登録が 0 件になる瞬間を作らない・上限 (48) を一瞬でも超えない、を偽の kabu で確かめる。
#   10/29 前場の「解除後も枠が足りない -> 全解除」171 回は、解除の応答に「直前の一覧に無かった銘柄」が
#   増えていた形 (kabu の /board・/symbol の自動登録と見られる)。FakeKabu.board_auto で再現する。
import pytest

from trading.push.subscription_manager import register_ops as RO


class FakeKabu:
    """kabu Station の RegistList を持つ偽物。

    - /register は追加、/unregister は指定解除、/unregister/all は全解除。
    - 枠は 50 (hidden は一覧に出ない形で使われている枠)。超えると 4002006。
    - board_auto: 次の REST 応答の直前に、kabu 側で勝手に増える銘柄 (/board の自動登録の形)。
    - 呼び出しのたびに登録件数の最小・最大を記録する。
    """

    def __init__(self, registered, hidden=0):
        self.reg = list(registered)
        self.hidden = hidden
        self.calls = []
        self.board_auto = []
        self.counts = [len(self.reg)]
        self.after_ours = []          # こちらの register が通った直後の件数

    def _apply_board_auto(self):
        while self.board_auto:
            s = self.board_auto.pop(0)
            if s not in self.reg and len(self.reg) + self.hidden < 50:
                self.reg.append(s)
                self.counts.append(len(self.reg))

    def __call__(self, *, url, method, payload, api_key, timeout=10.0):
        syms = [x["Symbol"] for x in (payload or {}).get("Symbols", [])]
        self._apply_board_auto()
        if url.endswith("/unregister/all"):
            self.calls.append(("unregister_all", []))
            self.reg = []
        elif url.endswith("/unregister"):
            self.calls.append(("unregister", syms))
            self.reg = [s for s in self.reg if s not in set(syms)]
        else:
            self.calls.append(("register", syms))
            new = [s for s in syms if s not in self.reg]
            if len(self.reg) + len(new) + self.hidden > 50:
                self.counts.append(len(self.reg))
                return False, {"Code": 4002006, "Message": "レジスト数エラー"}
            self.reg += new
            self.after_ours.append(len(self.reg))
        self.counts.append(len(self.reg))
        return True, {"RegistList": [{"Symbol": s, "Exchange": 1} for s in self.reg]}

    @property
    def kinds(self):
        return [c[0] for c in self.calls]


@pytest.fixture
def kabu(monkeypatch):
    monkeypatch.delenv("PUSH_REGISTER_DIFF_TRIM_EXTRA", raising=False)
    monkeypatch.delenv("PUSH_REGISTER_VENDOR_LIST_ENABLED", raising=False)

    def make(registered, hidden=0):
        k = FakeKabu(registered, hidden)
        monkeypatch.setattr(RO, "_http_json_request", k)
        monkeypatch.setattr(RO, "_resolve_api_key", lambda: "k")
        monkeypatch.setattr(RO, "_VENDOR_REGISTERED", None)
        monkeypatch.setattr(RO, "_reg_state", lambda: None)
        monkeypatch.setattr(RO.time, "sleep", lambda s: None)
        RO._note_vendor_list({"RegistList": [{"Symbol": s} for s in registered]})
        return k
    return make


def syms(n, base):
    return [str(base + i) for i in range(n)]


def test_limit_is_still_48():
    # 48 の理由 (10/22): 一覧に出ない枠が 1〜2 個使われる瞬間があり 50 で 4002006 になった。
    #   10/29 の調べで、その枠は /board・/symbol の自動登録と見られる。2 つ空けたまま、はみ出しの解除で収める。
    assert RO.REGISTER_HEADROOM == 2 and RO.REGISTER_LIMIT == 48


def test_board_auto_registration_is_trimmed_not_cleared(kabu):
    """10/29 09:15:19 の形: 48 登録・1 入れ替え。解除の応答に /board の 1 銘柄 (4288) が増えている。"""
    cur = syms(48, 1000)
    k = kabu(cur, hidden=0)
    k.board_auto = ["4288"]
    tgt = syms(47, 1000) + ["2001"]                      # 1047 を外して 2001 を入れる
    assert RO._try_differential_register(cur, tgt, exchange=1) is True
    assert "unregister_all" not in k.kinds
    assert k.kinds == ["unregister", "unregister", "register"]
    assert k.calls[1] == ("unregister", ["4288"])        # はみ出しだけ銘柄ごとに解除
    assert set(k.reg) == set(tgt)
    assert min(k.counts) >= 46 and max(k.counts) <= 50     # 49 は /board の自動登録の分 (kabu の本当の上限は 50)
    assert max(k.after_ours) <= RO.REGISTER_LIMIT


def test_two_board_symbols_with_four_swaps(kabu):
    """10/29 09:30:41 の形: 48 登録・4 入れ替え・はみ出し 2。"""
    cur = syms(48, 1000)
    k = kabu(cur)
    k.board_auto = ["1366", "3909"]
    tgt = syms(44, 1000) + syms(4, 2000)
    assert RO._try_differential_register(cur, tgt, exchange=1) is True
    assert "unregister_all" not in k.kinds
    assert set(k.reg) == set(tgt)
    assert min(k.counts) > 0 and max(k.counts) <= 50
    assert max(k.after_ours) <= RO.REGISTER_LIMIT


def test_mismatch_after_register_is_trimmed_not_cleared(kabu):
    """登録の応答に目標外が増えていた (登録の直前に /board が 1 つ足した) -> 銘柄ごとに外す。"""
    cur = syms(40, 1000)
    k = kabu(cur)
    tgt = syms(38, 1000) + syms(3, 2000)
    orig = k.__call__

    def hook(**kw):
        if kw["url"].endswith("/register"):
            k.board_auto = ["9984"]
        return orig(**kw)

    RO._http_json_request = hook                          # monkeypatch 済みの属性を上書き (fixture が元に戻す)
    assert RO._try_differential_register(cur, tgt, exchange=1) is True
    assert "unregister_all" not in k.kinds
    assert k.calls[-1] == ("unregister", ["9984"])
    assert set(k.reg) == set(tgt)


def test_full_replacement_never_hits_zero(kabu):
    """残す銘柄が 0 件の入れ替え (48 -> 別の 48)。1 銘柄を残して入れ替え、登録 0 件の瞬間を作らない。"""
    cur = syms(48, 1000)
    k = kabu(cur)
    tgt = syms(48, 3000)
    assert RO._try_differential_register(cur, tgt, exchange=1) is True
    assert "unregister_all" not in k.kinds
    assert min(k.counts) >= 1
    assert max(k.counts) <= RO.REGISTER_LIMIT
    assert set(k.reg) == set(tgt)
    assert k.kinds == ["unregister", "register", "unregister", "register"]


def test_full_replacement_single_symbol(kabu):
    cur = ["1000"]
    k = kabu(cur)
    tgt = syms(48, 3000)
    assert RO._try_differential_register(cur, tgt, exchange=1) is True
    assert min(k.counts) >= 1 and max(k.counts) <= RO.REGISTER_LIMIT
    assert set(k.reg) == set(tgt)


def test_never_exceeds_limit_or_zero_over_many_rotations(kabu):
    """ローテーションを 200 回 (毎回 /board が 0〜3 銘柄足す) 回しても、全解除なし・0 件なし・48 超えなし。"""
    import random

    rnd = random.Random(20261029)
    fixed = syms(19, 5000)                                # 保有・発注中の固定枠
    pool = syms(200, 6000)
    cur = fixed + pool[:29]
    k = kabu(cur)
    for i in range(200):
        k.board_auto = [str(9000 + rnd.randrange(300)) for _ in range(rnd.randrange(4))]
        n_swap = rnd.randrange(0, 8)
        keep = [s for s in cur if s not in fixed]
        rnd.shuffle(keep)
        new = [s for s in pool if s not in cur]
        rnd.shuffle(new)
        tgt = fixed + keep[: 29 - n_swap] + new[:n_swap]
        assert RO._try_differential_register(cur, tgt, exchange=1) is True, i
        cur = list(RO.vendor_registered_symbols())
        assert set(cur) == set(tgt), i
    assert "unregister_all" not in k.kinds
    assert min(k.counts) > 0
    assert max(k.counts) <= 50                            # /board の自動登録を含めても kabu の上限 50 以内
    assert k.after_ours and max(k.after_ours) <= RO.REGISTER_LIMIT   # こちらの登録の直後は必ず 48 以下


def test_refresh_sequence_with_clear_first_uses_trim(kabu):
    """rotation / stale recover の経路 (clear_first=True) でも全解除しない。"""
    cur = syms(48, 1000)
    k = kabu(cur)
    k.board_auto = ["4288"]
    tgt = syms(46, 1000) + syms(2, 2000)
    assert RO._run_refresh_sequence_core(cur, tgt, clear_first=True, unregister_first=True) is True
    assert "unregister_all" not in k.kinds
    assert set(k.reg) == set(tgt)


def test_trim_disabled_falls_back_to_full_clear_as_before(kabu, monkeypatch):
    monkeypatch.setenv("PUSH_REGISTER_DIFF_TRIM_EXTRA", "0")
    cur = syms(48, 1000)
    k = kabu(cur)
    k.board_auto = ["4288"]
    tgt = syms(47, 1000) + ["2001"]
    assert RO._try_differential_register(cur, tgt, exchange=1) is None     # 従来: 全解除→全登録へ


def test_trim_unregister_failure_falls_back(kabu, monkeypatch):
    """はみ出しの解除そのものが失敗したら、最後の手段として従来の全解除へ落とす。"""
    cur = syms(48, 1000)
    k = kabu(cur)
    k.board_auto = ["4288"]
    orig = k.__call__
    n = {"unreg": 0}

    def hook(**kw):
        if kw["url"].endswith("/unregister"):
            n["unreg"] += 1
            if n["unreg"] == 2:
                return False, "timed out"
        return orig(**kw)

    RO._http_json_request = hook
    tgt = syms(47, 1000) + ["2001"]
    assert RO._try_differential_register(cur, tgt, exchange=1) is None
