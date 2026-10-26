# 2026-10-21: PUSH 50 銘柄ずつのローテーション(A=1〜50 / B=51〜100)の組み分けと入れ替え準備
from trading.push.push_stream import rotation_core as R
from trading.push.push_stream import rotation_stability as ST


def _ab50_on(monkeypatch):
    # 2026-10-23 に既定を 0 (無効) に戻したので、AB50 の挙動を見るテストでは有効にする
    monkeypatch.setattr(R, "AB50_ENABLED", True)
    monkeypatch.setenv("PUSH_ROTATION_TARGET_MAX_KEEP", "100")


def _targets(n):
    return ["F1", "F2"] + [f"S{i:03d}" for i in range(n)]


def test_ab50_splits_100_into_two_50s(monkeypatch):
    _ab50_on(monkeypatch)
    monkeypatch.setattr(R, "_resolve_protected_safe", lambda: ["F1", "F2"])
    a, b, fixed = ST.build_protected_rotation_batches(_targets(100))
    assert len(a) == 50 and len(b) == 50 and fixed == ["F1", "F2"]
    assert set(a) & set(b) == {"F1", "F2"}          # 重なるのは固定(保有・発注中)だけ
    assert a[2] == "S000" and b[2] == "S048"


def test_no_b_turn_when_nothing_to_swap(monkeypatch):
    monkeypatch.setattr(R, "_resolve_protected_safe", lambda: ["F1", "F2"])
    a, b, _ = ST.build_protected_rotation_batches(_targets(40))
    assert len(a) == 42 and b == []                  # 固定だけの B を回して可変銘柄を外さない


def test_swap_prep_removes_before_adding():
    assert R.plan_swap_prep(["F1", "F2", "a1", "a2"], ["F1", "F2", "b1", "b2"]) == ["F1", "F2"]
    assert R.plan_swap_prep([], ["F1", "b1"]) == []                    # 初回は準備不要
    assert R.plan_swap_prep(["F1", "a1"], ["F1", "a1", "a2"]) == []    # 追加だけ
    assert R.plan_swap_prep(["F1", "a1", "a2"], ["F1", "a1"]) == []    # 解除だけ
    assert R.plan_swap_prep(["a1"], ["b1"]) == []                      # 共通なし(差分登録が解除→登録)


def test_split_keeps_100_without_relay(monkeypatch):
    _ab50_on(monkeypatch)
    monkeypatch.setattr(R, "_relay_connected", lambda: False)
    assert len(R._split_targets(_targets(100))) == 100
