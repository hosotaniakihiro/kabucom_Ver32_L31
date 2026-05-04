# test_script/test_ai_entry_gate.py

from AI.entry_gate import ai_final_entry_check

def test_ai_entry_gate_basic_allow():

    row = {
        "symbol": "9999",
        "source": "RANKING",
        "interval": 1,
        "entry_decision": "BUY",
        "score_total": 10,
        "buy_score": 8,
        "sell_score": 2,
        "dominant_side": "BUY",
        "dominant_ratio": 0.9,
        "close_price": 1000,
        "datetime": None,
    }

    result = ai_final_entry_check(row)

    assert "allow" in result
    assert "reason" in result

if __name__ == "__main__":
    test_row = {
        "symbol": "9999",
        "source": "RANKING",
        "interval": 1,
        "entry_decision": "BUY",
        "score_total": 10,
        "buy_score": 8,
        "sell_score": 2,
        "dominant_side": "BUY",
        "dominant_ratio": 0.9,
        "close_price": 1000,
        "datetime": None,
    }

    result = ai_final_entry_check(test_row)
    print("AI RESULT =", result)
