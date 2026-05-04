def test_no_trade_when_indicator_false():
    from AI.master_ai import MasterAI
    class Dummy:
        def predict_proba(self, X): return [[0.1, 0.8, 0.1]]
    ai = MasterAI(Dummy())
    assert ai.decide([[0]], False) == "NO_TRADE"
