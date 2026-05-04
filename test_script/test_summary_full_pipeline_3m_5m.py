import os
import datetime as dt
import pandas as pd
import pytest

# heavy calculation
from trading.summary.heavy_calculator import recalc_heavy_indicators

# scoring（テスト専用）
from scoring.scoring_light import apply_scoring_light as apply_scoring

# DB saver / loader
from trading.summary.summary_saver import save_summary_to_db
from trading.summary import summary_loader

# test utilities
from test_utils.create_test_db import create_test_summary_db
from test_utils.make_dummy_summary_row import make_dummy_row


# ======================================================================
# 3min / 5min テスト共通
# ======================================================================
def _run_interval_test(tmp_path, monkeypatch, interval):

    assert interval in (3, 5)
    today = dt.date.today()
    db_today = tmp_path / f"summary{today:%Y%m%d}.db"

    # DB override
    monkeypatch.setattr(
        summary_loader,
        "_summary_db_path",
        lambda d: os.path.join(tmp_path, f"summary{today:%Y%m%d}.db")
    )

    monkeypatch.setattr(
        summary_saver,
        "_SUMMARY_DB_OVERRIDE",
        lambda d: os.path.join(tmp_path, f"summary{today:%Y%m%d}.db")
    )

    # DB 作成
    create_test_summary_db(db_today)

    # heavy → scoring_light
    df = make_dummy_row(interval, today)
    df_h = recalc_heavy_indicators(df, interval)
    df_s = apply_scoring(df_h, interval)

    # test 用：time_range / start / end / time を付与
    start = dt.datetime.combine(today, dt.time(9, 0))
    end = start + dt.timedelta(minutes=interval)

    df_s["time_range"] = f"{start:%H:%M} - {end:%H:%M}"
    df_s["start_time"] = start
    df_s["end_time"] = end
    df_s["time"] = start

    # save
    save_summary_to_db(df_s, interval)

    # load
    summary_dict = summary_loader.load_summary_from_db()
    df_loaded = summary_dict[interval]

    # check
    assert not df_loaded.empty, f"❌ loader が {interval}min を読み込めていない"

    row = df_loaded.iloc[0]
    assert row["symbol"] == df_s.iloc[0]["symbol"]
    assert row["time_range"] == df_s.iloc[0]["time_range"]

    print(f"✅ {interval}min loader/saver pipeline OK")


# 3min
def test_full_pipeline_3min(tmp_path, monkeypatch):
    _run_interval_test(tmp_path, monkeypatch, 3)


# 5min
def test_full_pipeline_5min(tmp_path, monkeypatch):
    _run_interval_test(tmp_path, monkeypatch, 5)
