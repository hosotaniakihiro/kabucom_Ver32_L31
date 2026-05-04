import pandas as pd
from trading.summary.unified_close_builder import build_unified_close_1min
from trading.summary.unified_ma_builder import build_unified_ma_1min

def test_unified_close_priority():

    df_push = pd.DataFrame([
        {"symbol": "7203", "datetime": "2026-01-01 09:01", "close_price": 2000},
    ])

    df_rank = pd.DataFrame([
        {"symbol": "7203", "snapshot_time": "2026-01-01 09:01", "current_price": 1990},
    ])

    df_yahoo = pd.DataFrame([
        {"symbol": "7203", "datetime": "2026-01-01 09:01", "close": 1980},
    ])

    df = build_unified_close_1min(df_push, df_rank, df_yahoo)
    row = df.iloc[0]

    assert row["close"] == 2000
    assert row["source"] == "PUSH"


def test_unified_ma_build():

    df_close = pd.DataFrame([
        {"symbol": "7203", "datetime": f"2026-01-01 09:{i:02d}", "close": 100 + i, "source": "RANKING"}
        for i in range(80)
    ])

    df_ma = build_unified_ma_1min(df_close)

    last = df_ma.iloc[-1]
    assert "ma5" in last
    assert "ma25" in last
    assert "ma75" in last
    assert last["ma75"] is not None
