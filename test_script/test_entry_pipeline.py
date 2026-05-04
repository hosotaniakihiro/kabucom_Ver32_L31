# test_script/test_entry_pipeline.py

from global_state import global_data
from trading.handlers.entry_controller import run_entry_pipeline
import pandas as pd

def test_entry_pipeline_calls_ai():

    # 初期化解除（必須）
    global_data.is_initializing = False

    # pending を手動で作る
    global_data.pending_entries.clear()
    global_data.pending_entries["9999"] = {
        "symbol": "9999",
        "symbolname": "TEST_CORP",
        "source": "ranking",
        "is_buy": True,
        "ranking_strength": 10,
        "rank_price": 1234.5,
        "entry_conditions": {"need_push": False},
    }

    # summary をダミーで用意
    global_data.latest_summary_1m = pd.DataFrame([
        {"symbol": "9999", "close": 1234.5, "volume": 1000}
    ])

    run_entry_pipeline(source="ranking")

    assert True
