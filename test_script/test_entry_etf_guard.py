"""trading/entry/etf_guard と pending_manager.add_pending の ETF 除外 (2026-10-26)。"""
from trading.entry import etf_guard


def test_is_etf_name():
    assert etf_guard.is_etf_name("ｉＦｒｅｅＥＴＦ　日経ダブルインバース・インデックス")
    assert etf_guard.is_etf_name("ＮＥＸＴ　ＦＵＮＤＳ　日経平均インバース・インデックス連動型上場投信")
    assert etf_guard.is_etf_name("日本ビルファンド投資法人")
    assert not etf_guard.is_etf_name("ニッスイ")
    assert not etf_guard.is_etf_name("アドバンテスト")
    assert not etf_guard.is_etf_name("")


def test_reject_reason_uses_entry_name_and_env(monkeypatch):
    e = {"symbol": "1366", "symbolname": "ｉＦｒｅｅＥＴＦ　日経ダブルインバース・インデックス"}
    assert etf_guard.reject_reason(e).startswith("ETF_FUND:")
    monkeypatch.setenv("ENTRY_ETF_GUARD_ENABLED", "0")
    assert etf_guard.reject_reason(e) == ""


def test_add_pending_rejects_etf(monkeypatch):
    from trading.entry import pending_manager as pm

    called = []
    monkeypatch.setattr(pm, "_add_pending_core", lambda entry: called.append(entry) or True)
    monkeypatch.setattr(pm, "_ppp_protect_symbol", lambda *a, **k: called.append("protect"))
    monkeypatch.setattr(pm, "_cdp_source_enabled", lambda entry: False)
    monkeypatch.setattr(etf_guard, "resolve_name", lambda s, e=None: {"1366": "ｉＦｒｅｅＥＴＦ　日経ダブルインバース"}.get(s, "普通株"))
    assert pm.add_pending({"symbol": "1366", "source": "SUMMARY_AI"}) is False
    assert called == []  # 候補にも PUSH 保護にも入らない
    assert pm.add_pending({"symbol": "6525", "source": "SUMMARY_AI"}) is True
    assert called and called[-1] == "protect"


def test_names_seen_in_db_2026_10():
    etf = ["東証リート", "ｉＦリート２", "上場インデックスファンドアジアリート", "ハンセンブル", "コスピベア",
           "金２倍ブル", "Ｊ４百ベア２", "ｉＦ米＄ブル", "インドベア", "金先物ベア"]
    stock = ["アクリート", "日本コンクリート工業", "ビーエイブル", "ブルドック", "ミネベアミツミ",
             "ブルーゾーン", "ダブル・スコープ", "ダブルスタンダード", "ＦＵＮＤＩＮＮＯ", "ブルーイノベーション"]
    assert [n for n in etf if not etf_guard.is_etf_name(n)] == []
    assert [n for n in stock if etf_guard.is_etf_name(n)] == []
