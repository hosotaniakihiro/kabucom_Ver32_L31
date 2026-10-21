"""ランキングAPIの売買代金・売買高が正準単位 (円 / 株) で取り込まれること。

背景 (2026-08-17):
  kabu Station の /ranking は TradingVolume を「千株」、TradingValue を
  「百万円」で返す。PUSH (websocket) の同名フィールドは「株」「円」の実数で、
  システム内の閾値もすべて円建て
  (PUSH_REGISTER_MIN_DAY_TURNOVER=10,000,000 /
   WATCHLIST_RECENT_LIQ_MIN_TURNOVER_YEN=1,000,000)。

  変換せず保存していたためランキング由来の売買代金だけが 1/1,000,000 の桁で
  流動性判定に入り、ほぼ全銘柄が TURNOVER_LOW となって
  WATCHLIST RECENT LIQ 100->4 / PUSH LIQUIDITY GUARD kept_before=0
  という「実質全除外 + rescue で穴埋め」状態になっていた。

  実データ (同一銘柄・同一時刻) での裏取り:
    6701 PUSH volume=5,421,300 株      ranking trading_volume=5,415.7
    6701 PUSH value=26,114,286,600 円  ranking trading_value=26,087.6
"""
from __future__ import annotations

import datetime as dt

import pytest

from trading.ranking import collectors


def test_unit_constants_are_kabu_ranking_api_scale():
    assert collectors.RANKING_API_VOLUME_TO_SHARES == 1_000.0
    assert collectors.RANKING_API_VALUE_TO_YEN == 1_000_000.0


def _payload(symbol: str, price: float, volume_k: float, value_m: float) -> dict:
    return {
        "Ranking": [
            {
                "No": 1,
                "Symbol": symbol,
                "IssueName": "テスト銘柄",
                "CurrentPrice": price,
                "ChangeRatio": 0.0,
                "ChangePercentage": 1.0,
                "TradingVolume": volume_k,   # 千株
                "TradingValue": value_m,     # 百万円
                "TickCount": 10,
            }
        ]
    }


def test_ranking_rows_are_converted_to_yen_and_shares(monkeypatch):
    """6701 の実測値をそのまま流し、PUSH 側の桁と一致することを確認する。"""
    price, volume_k, value_m = 4766.0, 5415.7, 26087.6006

    monkeypatch.setattr(
        collectors, "get_data_from_api",
        lambda params, **kw: _payload("6701", price, volume_k, value_m),
    )
    # API を1回だけ叩く計画にし、呼び出し間 sleep を消す
    monkeypatch.setattr(collectors, "_ranking_call_plan", lambda: [(1, "値上がり率", "ALL")])
    monkeypatch.setattr(collectors, "_sleep_between_ranking_calls", lambda *a, **k: None)

    class _Mgr:
        def __getattr__(self, name):
            return lambda *a, **k: None

    raw_rows, _snapshot_rows = collectors.collect_ranking_rows(
        dt.datetime(2026, 8, 17, 10, 53), _Mgr()
    )

    assert raw_rows, "ランキング行が生成されなかった"
    row = raw_rows[0]

    # 株 / 円 になっていること
    assert row["trading_volume"] == pytest.approx(5_415_700.0)
    if "volume" in row:  # normalize 後は落ちている場合がある
        assert row["volume"] == pytest.approx(5_415_700.0)
    assert row["trading_value"] == pytest.approx(26_087_600_600.0, rel=1e-9)
    assert row["turnover"] == pytest.approx(26_087_600_600.0, rel=1e-9)

    # 売買代金 ≒ 株価 x 株数 (VWAP と終値の差の範囲で一致すること)
    implied = price * row["trading_volume"]
    assert 0.8 < row["turnover"] / implied < 1.25, (
        "turnover と price*volume の桁が合わない: turnover=%s implied=%s"
        % (row["turnover"], implied)
    )


def test_turnover_clears_liquidity_thresholds_for_a_liquid_symbol(monkeypatch):
    """変換後は主力銘柄が流動性閾値を通ること (変換前は必ず落ちていた)。"""
    price, volume_k, value_m = 3674.0, 13240.0, 48872.5546  # 8306

    monkeypatch.setattr(
        collectors, "get_data_from_api",
        lambda params, **kw: _payload("8306", price, volume_k, value_m),
    )
    monkeypatch.setattr(collectors, "_ranking_call_plan", lambda: [(4, "売買代金", "ALL")])
    monkeypatch.setattr(collectors, "_sleep_between_ranking_calls", lambda *a, **k: None)

    class _Mgr:
        def __getattr__(self, name):
            return lambda *a, **k: None

    raw_rows, _ = collectors.collect_ranking_rows(dt.datetime(2026, 8, 17, 10, 53), _Mgr())
    turnover = raw_rows[0]["turnover"]

    # PUSH_REGISTER_MIN_DAY_TURNOVER と WATCHLIST_RECENT_LIQ_MIN_TURNOVER_YEN
    assert turnover >= 10_000_000.0
    assert turnover >= 1_000_000.0
    # 変換前の生値ではどちらの閾値にも届かなかったことを明示しておく
    assert value_m < 1_000_000.0


# ============================================================
# recent_liquidity: 当日累積の turnover 列を bars 本合計しないこと
# ============================================================

def _make_summary_db(tmp_path, rows):
    import sqlite3

    db = tmp_path / "summary_test.db"
    con = sqlite3.connect(str(db))
    try:
        con.execute(
            "CREATE TABLE stock_summary_1min ("
            "symbol TEXT, datetime TEXT, close REAL, volume REAL, turnover REAL)"
        )
        con.executemany(
            "INSERT INTO stock_summary_1min (symbol, datetime, close, volume, turnover) "
            "VALUES (?,?,?,?,?)", rows,
        )
        con.commit()
    finally:
        con.close()
    return str(db)


def test_recent_liquidity_uses_per_bar_close_times_volume(tmp_path, monkeypatch):
    """turnover 列が当日累積でも、直近窓の売買代金が水増しされないこと。

    5本すべてに同じ累積値 25,766,540,700 円 が入っている状況を作る。
    旧実装はこれを合計して約 1288 億円と誤認していた。
    正しい直近5本の売買代金は close x volume の合計。
    """
    from trading.ranking.active_symbols import recent_liquidity as rl

    cum = 25_766_540_700.0
    rows = [
        ("6701", "2026-08-17 10:5%d:00" % i, 4760.0 + i, 10_000.0, cum)
        for i in range(5)
    ]
    db = _make_summary_db(tmp_path, rows)
    monkeypatch.setattr(rl, "_wrl_summary_db_path", lambda: db)
    monkeypatch.setenv("WATCHLIST_RECENT_LIQ_BARS", "5")
    rl._wrl_cache.clear()

    stats = rl._wrl_recent_stats("6701")
    assert stats, "統計が取れなかった"

    expected = sum((4760.0 + i) * 10_000.0 for i in range(5))
    assert stats["total_turnover"] == pytest.approx(expected)
    # 累積列をそのまま合計した値 (旧実装) にはならないこと
    assert stats["total_turnover"] < cum
    # 当日累積は別キーで保持し、合計ではなく max であること
    assert stats["day_turnover"] == pytest.approx(cum)


def test_recent_liquidity_ignores_null_turnover_column(tmp_path, monkeypatch):
    """turnover 列が NULL でも close x volume で判定できること。

    本番では enrich が届いた分しか turnover が入らず大半が NULL だった。
    旧実装は NULL 銘柄だけ fallback で円建てになり、データがある銘柄が
    落ちるという逆転を起こしていた。
    """
    from trading.ranking.active_symbols import recent_liquidity as rl

    rows = [("9999", "2026-08-17 10:5%d:00" % i, 1000.0, 5_000.0, None) for i in range(5)]
    db = _make_summary_db(tmp_path, rows)
    monkeypatch.setattr(rl, "_wrl_summary_db_path", lambda: db)
    monkeypatch.setenv("WATCHLIST_RECENT_LIQ_BARS", "5")
    rl._wrl_cache.clear()

    stats = rl._wrl_recent_stats("9999")
    assert stats["total_turnover"] == pytest.approx(1000.0 * 5_000.0 * 5)
    assert stats["day_turnover"] == 0.0
