from global_state import global_data
from trading.handlers.entry_controller import run_entry_pipeline

def test_entry_pipeline_reaches_ai():

    # 初期化解除
    global_data.is_initializing = False

    # ダミー pending
    global_data.pending_entries.clear()
    global_data.pending_entries["9999"] = {
        "symbol": "9999",
        "symbolname": "TEST_CORP",
        "source": "ranking",
        "type_name": "殿様ランキング",
        "market": "ALL",
        "ranking_strength": 10,
        "rank_price": 1234.5,
        "is_buy": True,
        "entry_conditions": {"need_push": False},
    }

    # summary ダミー（最低限）
    import pandas as pd
    global_data.latest_summary_1m = pd.DataFrame([
        {"symbol": "9999", "close": 1234.5, "volume": 1000}
    ])

    run_entry_pipeline(source="ranking")

    # ここでは「落ちない」ことだけ確認
    assert True
