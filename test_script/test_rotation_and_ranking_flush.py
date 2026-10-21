"""2026-08-17 の停止 (PUSH ローテーション / ランキング保存) の再発防止。

本番で出ていたのは以下の2つで、どちらも「呼ぶまで分からない」NameError だった。

    NameError: name 'resolve_monitor_symbols_patched' is not defined
        trading/push/push_stream/rotation_stability.resolve_register_targets()
    NameError: name 'target' is not defined
        trading/ranking/ranking_db_writer._rdl_flush()

過去の検証プローブは壊れていた関数自体をスタブしていたので素通りしていた。
ここではスタブせず本物を呼ぶ。副作用を本番へ出さないため、
ランキング側はテンポラリDBへ書く。
"""
from __future__ import annotations

import datetime as dt
import sqlite3
import types

import pytest


# ============================================================
# PUSH ローテーション
# ============================================================

def test_resolve_register_targets_does_not_raise_name_error():
    from trading.push.push_stream import rotation_stability as st

    try:
        out = st.resolve_register_targets()
    except NameError as exc:  # 本番で落ちていた形
        pytest.fail("resolve_register_targets が NameError: %s" % exc)
    except Exception:
        # データ源が無い環境での失敗は対象外 (NameError でないことが要件)
        return
    assert isinstance(out, list)


def test_resolve_monitor_symbols_does_not_raise_name_error():
    from trading.push.push_stream import rotation_stability as st

    try:
        out = st.resolve_monitor_symbols()
    except NameError as exc:
        pytest.fail("resolve_monitor_symbols が NameError: %s" % exc)
    except Exception:
        return
    assert isinstance(out, list)


def test_rotation_selects_targets_from_active_symbols(tmp_path):
    """active symbols があれば登録対象が 0 件にならない (要件C)。"""
    from global_state import global_data
    from trading.push.push_stream import rotation_stability as st

    symbols = ["%04d" % (1301 + i) for i in range(120)]
    prev = getattr(global_data, "active_symbols", None)
    global_data.active_symbols = list(symbols)
    try:
        monitor = st.resolve_monitor_symbols()
        targets = st.resolve_register_targets()
    finally:
        global_data.active_symbols = prev

    assert monitor, "active symbols があるのに monitor が空"
    assert targets, "active symbols があるのに register targets が空"


# ============================================================
# ランキング保存
# ============================================================

def test_rdl_type_counts_returns_counts():
    """旧 patch の target 引数が消えた後も種別カウントが機能すること。"""
    import trading.ranking.ranking_db_writer as writer

    counts = writer._rdl_type_counts(
        [{"ranking_type": "値上がり率"}, {"ranking_type": "値上がり率"}, {"ranking_type": "売買代金上位"}]
    )
    assert counts, "種別カウントが空"
    assert sum(counts.values()) == 3


def test_ranking_flush_writes_rows(tmp_path):
    """flush が NameError にならず、実際に行が入ること (要件B)。"""
    import trading.ranking.ranking_db_writer as writer
    from database.schema.ranking_raw_schema import ensure_ranking_raw_table
    from database.schema.ranking_snapshot_schema import ensure_ranking_snapshot_table

    db_path = str(tmp_path / "ranking_test.db")
    con = sqlite3.connect(db_path)
    try:
        ensure_ranking_raw_table(con)
        ensure_ranking_snapshot_table(con)
        con.commit()
    finally:
        con.close()

    ts = dt.datetime.now().replace(second=0, microsecond=0).strftime("%Y-%m-%d %H:%M:%S")
    rows = [
        {
            "symbol": "%04d" % (1301 + i), "ranking_type": "値上がり率", "market": "ALL",
            "rank": i + 1, "current_price": 1000.0 + i, "change_percentage": 10.0 - i * 0.1,
            "trading_volume": 1_000_000 - i * 1000, "trading_value": 1e9 - i * 1e6,
            "datetime": ts, "snapshot_time": ts,
        }
        for i in range(50)
    ]

    class _Lock:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    stub = types.SimpleNamespace(
        lock=_Lock(), raw_buffer=list(rows), snapshot_buffer=list(rows), legacy_buffer=[],
        db_path=db_path, total_flushed_raw=0, total_flushed_snapshot=0, total_flushed_legacy=0,
        total_flush_errors=0, last_flush_at=None, last_flush_elapsed_sec=0.0,
    )

    result = writer.RankingDBWriter.flush(stub)

    assert result is True, "flush が失敗した"
    assert stub.total_flush_errors == 0

    con = sqlite3.connect(db_path)
    try:
        assert con.execute("SELECT COUNT(*) FROM ranking_snapshot_1min").fetchone()[0] == 50
        assert con.execute("SELECT COUNT(*) FROM ranking_raw_1min").fetchone()[0] == 50
    finally:
        con.close()

    assert not stub.raw_buffer and not stub.snapshot_buffer, "flush 後もバッファが残っている"
