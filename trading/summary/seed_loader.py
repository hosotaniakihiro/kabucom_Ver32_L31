# ============================================================
# seed_loader.py（Ver25.0-FINAL-SEED-STABLE-MA75-GUARDED）
# ------------------------------------------------------------
# ✔ 指標破綻しない最小 seed 抽出
# ✔ 1min / 3min / 5min 共通
# ✔ ORM 取得のみ（判断ロジックなし）
# ✔ symbol / datetime / time 欠落 完全防止
# ✔ tz-aware / tz-naive 完全統一（★最重要）
# ✔ bars 不足・逆順・日跨ぎ 全耐性
# ✔ 空DFでも schema 完全保証
# ✔ summary_loader / ranking / AI 全対応
# ✔ Ver19〜Ver25 完全互換
# ============================================================

from __future__ import annotations

import pandas as pd
import logging
from sqlalchemy.orm import Session


from database.models import (
    StockSummary1Min,
    StockSummary3Min,
    StockSummary5Min,
)

logger = logging.getLogger(__name__)

# ============================================================
# 共通 schema（summary_loader と完全一致）
# ============================================================

SUMMARY_COLUMNS = [
    "symbol",
    "datetime",
    "date",
    "time",
    "start_time",
    "end_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "turnover",
    "vwap",
    "ma5",
    "ma25",
    "ma75",
    "rsi",
    "macd",
    "macd_signal",
    "macd_hist",
    "bb_upper",
    "bb_middle",
    "bb_lower",
    "atr",
]

# ============================================================
# 空 seed（schema 保証）
# ============================================================

def _empty_seed_df() -> pd.DataFrame:
    return pd.DataFrame(columns=SUMMARY_COLUMNS)

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
# rows → DataFrame（完全安全変換）
# ============================================================

def _rows_to_df(rows) -> pd.DataFrame:
    if not rows:
        return _empty_seed_df()

    df = pd.DataFrame([
        {
            k: v
            for k, v in r.__dict__.items()
            if not k.startswith("_")
        }
        for r in rows
    ])

    # --- 必須カラム検証 ---
    required = {"symbol", "datetime"}
    if not required.issubset(df.columns):
        logger.error(
            "[SEED] missing required columns "
            f"required={required} actual={df.columns.tolist()}"
        )
        return _empty_seed_df()

    # --- 正規化（★超重要） ---
    df["symbol"] = df["symbol"].astype(str)

    df["datetime"] = (
        pd.to_datetime(df["datetime"], errors="coerce")
          .dt.tz_localize(None)
    )

    df = df.dropna(subset=["datetime"])
    if df.empty:
        return _empty_seed_df()

    # --- time / date 補完（summary 側互換） ---
    if "time" not in df.columns or df["time"].isna().all():
        df["time"] = df["datetime"]

    if "date" not in df.columns or df["date"].isna().all():
        df["date"] = df["datetime"].dt.normalize()

    # --- 時系列安定化 ---
    df = (
        df.sort_values(["symbol", "datetime"])
          .drop_duplicates(subset=["symbol", "datetime"], keep="last")
          .reset_index(drop=True)
    )

    return df

# ============================================================
# seed 取得（汎用・完全防御）
# ============================================================

def load_seed_summary(
    interval: int,
    bars: int = 120,   # ← 80では足りない
    *,
    symbol: str | None = None,
) -> pd.DataFrame:
    from database.session import summary_engine
    model = _get_model(interval)

    try:
        with Session(summary_engine) as session:

            # 単一銘柄
            if symbol is not None:
                rows = (
                    session.query(model)
                    .filter(model.symbol == str(symbol))
                    .order_by(model.datetime.desc())
                    .limit(bars)
                    .all()
                )
                return _rows_to_df(rows)

            # 🔥 全銘柄
            symbols = session.query(model.symbol).distinct().all()

            all_rows = []

            for (sym,) in symbols:
                rows = (
                    session.query(model)
                    .filter(model.symbol == sym)
                    .order_by(model.datetime.desc())
                    .limit(bars)
                    .all()
                )
                all_rows.extend(rows)

        df = _rows_to_df(all_rows)

        logger.info(
            f"[SEED] loaded interval={interval} "
            f"symbol=ALL rows={len(df)}"
        )

        return df

    except Exception:
        logger.exception(
            f"[SEED] load failed interval={interval}"
        )
        return _empty_seed_df()
# ============================================================
# 日跨ぎ seed（意味付けラッパー）
# ============================================================

def load_crossday_seed(
    interval: int,
    bars: int = 80,
    *,
    symbol: str | None = None,
) -> pd.DataFrame:
    """
    前日まで含めた seed を取得するための意味付けラッパー
    """
    return load_seed_summary(
        interval,
        bars=bars,
        symbol=symbol,
    )

# ============================================================
# 互換API（旧コード用）
# ============================================================

def load_seed_1min(bars: int = 80) -> pd.DataFrame:
    """
    旧コード互換用（1min・全銘柄）
    """
    return load_seed_summary(1, bars=bars)

# ============================================================
# exports
# ============================================================

__all__ = [
    "load_seed_summary",
    "load_crossday_seed",
    "load_seed_1min",
]