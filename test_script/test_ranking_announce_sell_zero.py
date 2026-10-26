# 2026-10-23: score_sell が全部 0 のとき、SELL TOP を BUY TOP と同じ並びにしない
import pandas as pd

from trading.ranking.summary import announce as A


def _df(sell):
    return pd.DataFrame({"symbol": ["1", "2", "3"], "score_buy": [50.0, 49.0, 48.0], "score_sell": sell,
                         "best_rank_position": [1, 2, 3]})


def test_all_zero_sell_is_not_real():
    x, col, real = A._resolve_score_for_sell(_df([0.0, 0.0, 0.0]))
    assert real is False and col == "_tmp_sell_score"


def test_real_sell_kept():
    x, col, real = A._resolve_score_for_sell(_df([-50.0, -49.0, -48.0]))
    assert real is True and col == "score_sell"
