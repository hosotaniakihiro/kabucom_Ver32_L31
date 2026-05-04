# ============================================================
# pytest 基本テスト → summary_loader の動作確認
# ============================================================

import pandas as pd
import datetime as dt

from trading.summary.summary_loader import load_summary_from_db


def test_summary_loader_basic():
    """summary_loader が3日分をロードして DataFrame を返すことを確認"""

    result = load_summary_from_db()

    # interval keys が揃っているか？
    assert set(result.keys()) == {1, 3, 5}

    # DataFrame が返っているか？
    assert isinstance(result[1], pd.DataFrame)
    assert isinstance(result[3], pd.DataFrame)
    assert isinstance(result[5], pd.DataFrame)


def test_summary_loader_has_today_data():
    """当日の summaryDB があれば、今日の日付データが含まれるか？"""

    today = dt.date.today()

    result = load_summary_from_db()
    df1 = result[1]

    if df1.empty:
        # 当日DBが無い日は skip
        return

    # date がすべて dt.date 型であること
    assert df1["date"].apply(lambda x: isinstance(x, dt.date)).all()

    # 今日の日付が1行以上あること（当日DBが存在する場合）
    assert (df1["date"] == today).any()
