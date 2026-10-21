# ============================================================
# File   : news_shadow.py
# ------------------------------------------------------------
# 検証先行(シャドー)モード。
#   発注判断には一切介入しない。やることは2つだけ:
#     1) JPX 開示を収集して news_disclosure に溜める
#     2) 現在 OPEN のポジションについて、建て時刻 as-of の news_score を
#        news_shadow_log に1本だけ記録する
#
#   後で news_shadow_eval.py が、この記録と約定結果(PnL)を突き合わせ、
#   「news_score を反映していたら勝てたか」を数値で評価する。
#
# ⚠ entry_controller / main / 発注系には import も呼び出しもしない。
#   読むのは positions テーブルと news_disclosure テーブルだけ。
# ============================================================

from __future__ import annotations

import time as _time
import logging
import datetime as dt
from typing import Optional

import news_pipeline as npl

logger = logging.getLogger(__name__)

DEFAULT_LOOKBACK_HOURS = 48.0


def ensure_tables() -> None:
    """専用ローカル news DB に news_disclosure / news_shadow_log を作成。"""
    npl.ensure_news_table()  # Base_news.metadata.create_all で両テーブル作成
    logger.info("[SHADOW] tables ready")


def _db_has_positions(path) -> bool:
    """path の sqlite に positions テーブルが実在するか（読み取り専用で確認）。"""
    import os
    import sqlite3

    try:
        if not path or not os.path.exists(path) or os.path.getsize(path) == 0:
            return False
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            cur = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='positions'"
            )
            return cur.fetchone() is not None
        finally:
            con.close()
    except Exception:
        return False


def _resolve_positions_path():
    """
    positions テーブルを持つ positions.db を選ぶ。
    ローカルstaging優先、空/未同期ならNAS正本にフォールバック。
    """
    candidates = []
    try:
        from trading.position import positions_db_location as _pdl
        try:
            candidates.append(_pdl.positions_db_path())      # ローカルstaging（場中は最新）
        except Exception:
            pass
        try:
            candidates.append(_pdl.nas_positions_db_path())  # NAS正本
        except Exception:
            pass
    except Exception:
        logger.exception("[SHADOW] positions_db_location を import できない")
        return None

    for p in candidates:
        if _db_has_positions(p):
            return p
    logger.warning("[SHADOW] positions テーブルを持つ positions.db が見つからない 候補=%s", candidates)
    return None


def _positions_engine():
    """
    稼働中の positions.db を読む専用エンジン（init_engines は踏まない・読み取り用）。
    """
    from sqlalchemy import create_engine

    path = _resolve_positions_path()
    if path is None:
        return None
    return create_engine(
        f"sqlite:///{path}",
        connect_args={"timeout": 30, "check_same_thread": False},
    )


def _hold_key(symbol: str, entry_time, hold_id: Optional[str]) -> str:
    if hold_id:
        return str(hold_id)
    ts = entry_time.strftime("%Y%m%d%H%M%S") if isinstance(entry_time, dt.datetime) else str(entry_time)
    return f"{symbol}|{ts}"


def _fetch_open_positions():
    """positions.db から OPEN ポジションを生SQLで読む（読み取りのみ）。"""
    from sqlalchemy import text

    eng = _positions_engine()
    if eng is None:
        return []
    try:
        with eng.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT symbol, side, entry_time, hold_id "
                    "FROM positions WHERE status = 'OPEN'"
                )
            ).all()
        # (symbol, side, entry_time, hold_id) のタプル列。entry_time は文字列の場合あり。
        return [(r[0], r[1], _as_dt(r[2]), r[3]) for r in rows]
    except Exception:
        logger.exception("[SHADOW] OPEN ポジション取得失敗")
        return []
    finally:
        eng.dispose()


def _as_dt(v):
    if v is None or isinstance(v, dt.datetime):
        return v
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M"):
        try:
            return dt.datetime.strptime(str(v), fmt)
        except Exception:
            continue
    return None


def snapshot_open_positions(
    now: Optional[dt.datetime] = None,
    lookback_hours: float = DEFAULT_LOOKBACK_HOURS,
) -> int:
    """
    現在 OPEN の各ポジションについて、建て時刻 as-of の news_score を
    news_shadow_log へ記録（hold_id 単位で初回のみ）。戻り値: 新規記録数。
    """
    from database.models import NewsShadowLog

    now = now or dt.datetime.now()
    positions = _fetch_open_positions()
    if not positions:
        return 0

    recorded = 0
    session = npl._news_session()
    try:
        for symbol, side, entry_time, hold_id in positions:
            key = _hold_key(symbol, entry_time, hold_id)

            if session.query(NewsShadowLog).filter(NewsShadowLog.hold_id == key).first():
                continue  # 既に entry スナップショット済み

            # as-of は「建て時刻」。materialが建て時までに出ていた分だけで採点。
            as_of = entry_time if isinstance(entry_time, dt.datetime) else now
            score = npl.compute_news_score(symbol, now=as_of, lookback_hours=lookback_hours)

            session.add(
                NewsShadowLog(
                    hold_id=key,
                    symbol=str(symbol),
                    side=str(side) if side is not None else None,
                    entry_time=entry_time if isinstance(entry_time, dt.datetime) else None,
                    news_score=float(score),
                    lookback_hours=float(lookback_hours),
                    note="entry_snapshot",
                )
            )
            session.commit()
            recorded += 1
    except Exception:
        session.rollback()
        logger.exception("[SHADOW] スナップショット失敗")
    finally:
        session.close()

    if recorded:
        logger.info("[SHADOW] recorded entry snapshots=%s", recorded)
    return recorded


def run_loop(interval_sec: int = 120, lookback_hours: float = DEFAULT_LOOKBACK_HOURS) -> None:
    """常駐: JPX 収集 → OPEN ポジションの news_score スナップショット。"""
    ensure_tables()
    logger.info("[SHADOW] loop start interval=%ss lookback=%sh", interval_sec, lookback_hours)
    while True:
        try:
            npl.collect_jpx_once()
            snapshot_open_positions(lookback_hours=lookback_hours)
        except Exception:
            logger.exception("[SHADOW] loop iteration error")
        _time.sleep(interval_sec)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "once":
        ensure_tables()
        n1 = npl.collect_jpx_once()
        n2 = snapshot_open_positions()
        print(f"collected={n1} snapshots={n2}")
    else:
        run_loop()
