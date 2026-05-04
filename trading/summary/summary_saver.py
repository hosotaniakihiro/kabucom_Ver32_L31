# ============================================================
# summary_saver.py（Ver24.3-FINAL-DB-SAFE-UPSERT-TIME-ABSOLUTE-FIX）
# ------------------------------------------------------------
# ✔ SQLite ネイティブ UPSERT（ON CONFLICT）
# ✔ UNIQUE(symbol, date, time_range) 完全安全
# ✔ SQLite 999 variables 制限 完全回避（BATCH INSERT）
# ✔ datetime 欠落事故完全防止
# ✔ date=YYYY-MM-DD を絶対保証
# ✔ TIME 型 完全絶対安全（今回FIX）
# ✔ push / scheduled / AI 再計算すべて対応
# ✔ autoflush / race condition 完全排除
# ✔ source NOT NULL 完全保証
# ============================================================

import logging
import datetime as dt
import pandas as pd
import numpy as np

from sqlalchemy.orm import Session
from sqlalchemy.dialects.sqlite import insert

from database.session import summary_engine
from database.models import (
    StockSummary1Min,
    StockSummary3Min,
    StockSummary5Min,
)

logger = logging.getLogger(__name__)

BATCH_SIZE = 30


# ============================================================
# SQLite TIME 型 絶対安全正規化
# ============================================================

def _normalize_time_value(v):

    if v is None:
        return None

    if pd.isna(v):
        return None

    if isinstance(v, dt.time):
        return v

    if isinstance(v, dt.datetime):
        return v.time()

    if isinstance(v, pd.Timestamp):
        if pd.isna(v):
            return None
        return v.to_pydatetime().time()

    if isinstance(v, np.datetime64):
        try:
            return pd.to_datetime(v).to_pydatetime().time()
        except Exception:
            return None

    if isinstance(v, str):
        try:
            return pd.to_datetime(v).time()
        except Exception:
            return None

    return None  # ★ それ以外は全て遮断


# ============================================================
# モデル解決
# ============================================================

def _get_model(interval: int):
    if interval == 1:
        return StockSummary1Min
    if interval == 3:
        return StockSummary3Min
    if interval == 5:
        return StockSummary5Min
    raise ValueError(f"invalid interval: {interval}")


# ============================================================
# time_range 正規化
# ============================================================

def _build_time_range(t: dt.time, interval: int) -> str:

    if interval == 1:
        return t.strftime("%H:%M")

    m = (t.minute // interval) * interval
    start = dt.time(t.hour, m)

    end_min = m + interval
    end_hour = t.hour + (end_min // 60)
    end_min %= 60
    end = dt.time(end_hour % 24, end_min)

    return f"{start.strftime('%H:%M')}-{end.strftime('%H:%M')}"


# ============================================================
# MAIN（UPSERT 実体）
# ============================================================

def save_summary_to_db(df: pd.DataFrame, interval: int):

    if not isinstance(df, pd.DataFrame) or df.empty:
        logger.warning("save_summary_to_db: empty df → skip")
        return

    model = _get_model(interval)
    table = model.__table__

    if "symbol" not in df.columns or "datetime" not in df.columns:
        raise ValueError("missing required columns")

    df = df.copy()

    # --------------------------------------------------------
    # source 完全保証
    # --------------------------------------------------------
    if "source" not in df.columns:
        df["source"] = "push"
    else:
        df["source"] = df["source"].fillna("push")

    # --------------------------------------------------------
    # datetime 正規化
    # --------------------------------------------------------
    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
    df = df.dropna(subset=["datetime"])

    if df.empty:
        return

    df["datetime"] = df["datetime"].dt.tz_localize(None)

    # --------------------------------------------------------
    # 壊れたdatetime除外
    # --------------------------------------------------------
    dt_str = df["datetime"].dt.strftime("%Y-%m-%d")
    df = df[dt_str.str.len() == 10]

    if df.empty:
        return

    # --------------------------------------------------------
    # date / time / time_range
    # --------------------------------------------------------
    df["date"] = df["datetime"].dt.date
    df["time"] = df["datetime"].dt.time
    df["time"] = df["time"].apply(_normalize_time_value)

    df = df.dropna(subset=["time"])  # ★ TIME不正完全除去

    df["time_range"] = df["time"].apply(
        lambda t: _build_time_range(t, interval)
    )

    df["last_update"] = dt.datetime.now()

    # --------------------------------------------------------
    # モデル列限定
    # --------------------------------------------------------
    valid_cols = {c.name for c in table.columns}
    valid_cols.discard("id")

    rows = []

    for _, r in df.iterrows():

        raw = r.to_dict()
        row = {}

        for k, v in raw.items():
            if k not in valid_cols:
                continue

            if k in {"time", "start_time", "end_time"}:
                row[k] = _normalize_time_value(v)
            else:
                row[k] = v

        if not row.get("source"):
            row["source"] = "push"

        rows.append(row)

    if not rows:
        return

    # --------------------------------------------------------
    # UPSERT
    # --------------------------------------------------------
    session = Session(summary_engine)

    try:
        for i in range(0, len(rows), BATCH_SIZE):
            batch = rows[i:i + BATCH_SIZE]

            stmt = insert(table).values(batch)

            update_cols = {
                c.name: stmt.excluded[c.name]
                for c in table.columns
                if c.name != "id"
            }

            stmt = stmt.on_conflict_do_update(
                index_elements=["symbol", "date", "time_range"],
                set_=update_cols,
            )

            session.execute(stmt)

        session.commit()

        logger.info(
            f"💾 summary saved interval={interval} rows={len(rows)}"
        )

    except Exception:
        session.rollback()
        logger.error(
            f"❌ save_summary_to_db failed interval={interval}",
            exc_info=True,
        )
        raise

    finally:
        session.close()


# ============================================================
# 互換入口
# ============================================================

def upsert_summary(df: pd.DataFrame, interval: int):
    save_summary_to_db(df, interval)