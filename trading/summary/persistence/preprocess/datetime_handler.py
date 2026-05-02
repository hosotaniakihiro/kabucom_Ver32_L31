# ============================================================
# File   : trading/summary/persistence/preprocess/datetime_handler.py
# Version: Ver1.1-PRODUCTION-DATETIME-HANDLER-HARDENED-FIXED
# ------------------------------------------------------------
# ✔ summary_saver_bulk から完全分離
# ✔ Ver21.1 ロジック互換を維持
# ✔ datetime生成・補正・正規化
# ✔ MultiIndex対応
# ✔ duplicate column除去
# ✔ NaT排除
# ✔ floor(min)統一
# ✔ end_time最終補正を安全化
# ✔ timezone混入文字列に耐性
# ✔ 日跨ぎ安全化
# ✔ production hardened
# ============================================================

from __future__ import annotations

import logging
import pandas as pd

logger = logging.getLogger(__name__)


def _flatten_multiindex_columns(df: pd.DataFrame) -> pd.DataFrame:
    if isinstance(df.columns, pd.MultiIndex):
        df = df.copy()
        df.columns = [
            "_".join([str(c) for c in col if c != ""])
            for col in df.columns
        ]
    return df


def _drop_duplicate_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df.columns.duplicated().any():
        dup = df.columns[df.columns.duplicated()].tolist()
        logger.warning("[DATETIME] duplicate columns removed → %s", dup)
        df = df.loc[:, ~df.columns.duplicated()]
    return df


def _sanitize_time_string_series(s: pd.Series) -> pd.Series:
    x = s.astype(str).str.strip()

    # null系文字列を正規化
    x = x.replace(
        {
            "None": "",
            "NaT": "",
            "nan": "",
            "NaN": "",
            "": "",
        }
    )

    # 末尾 timezone / Z を除去
    x = x.str.replace(r"Z$", "", regex=True)
    x = x.str.replace(r"([+-]\d{2}:\d{2})$", "", regex=True)
    x = x.str.replace(r"([+-]\d{4})$", "", regex=True)
    x = x.str.replace(r"([+-]\d{6})$", "", regex=True)

    # 文字列中に日付まで入っていたら時刻部分だけ抜く
    extracted = x.str.extract(r"(\d{2}:\d{2}:\d{2}(?:\.\d+)?)", expand=False)
    hhmm = x.str.extract(r"(\d{2}:\d{2})", expand=False)

    x = extracted.fillna(hhmm).fillna("")

    return x


def ensure_datetime(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df

    df = df.copy()
    df = _flatten_multiindex_columns(df)
    df = _drop_duplicate_columns(df)

    # --------------------------------------------------------
    # datetime生成（互換維持）
    # --------------------------------------------------------
    if "datetime" not in df.columns:
        if isinstance(df.index, pd.DatetimeIndex):
            df["datetime"] = df.index
        elif "time" in df.columns:
            df["datetime"] = df["time"]
        elif "timestamp" in df.columns:
            df["datetime"] = df["timestamp"]
        else:
            raise RuntimeError("datetime column missing")

    # --------------------------------------------------------
    # 型変換（安全化）
    # --------------------------------------------------------
    df["datetime"] = pd.to_datetime(
        df["datetime"],
        errors="coerce"
    )

    # --------------------------------------------------------
    # NaT除去
    # --------------------------------------------------------
    before = len(df)
    df = df.dropna(subset=["datetime"])
    removed = before - len(df)

    if removed:
        logger.warning(
            "[DATETIME] dropped %d invalid datetime rows",
            removed
        )

    # --------------------------------------------------------
    # 分単位統一
    # --------------------------------------------------------
    df["datetime"] = df["datetime"].dt.floor("min")

    return df


def ensure_required_columns(
    df: pd.DataFrame,
    interval: int
) -> pd.DataFrame:
    if df is None or df.empty:
        return df

    df = ensure_datetime(df)
    if df is None or df.empty:
        return df

    dt = pd.to_datetime(df["datetime"], errors="coerce")

    # datetime が壊れている行は落とす
    valid_mask = dt.notna()
    if not valid_mask.all():
        dropped = int((~valid_mask).sum())
        logger.warning(
            "[DATETIME] dropped %d rows with invalid normalized datetime",
            dropped
        )
        df = df.loc[valid_mask].copy()
        dt = dt.loc[valid_mask]

    # --------------------------------------------------------
    # date / time
    # --------------------------------------------------------
    if "date" not in df.columns:
        df["date"] = dt.dt.date

    if "time" not in df.columns:
        df["time"] = dt.dt.time

    # --------------------------------------------------------
    # start / end
    # --------------------------------------------------------
    if "start_time" not in df.columns:
        df["start_time"] = dt.dt.time

    # end_time は datetime + interval から安全に生成
    computed_end_dt = dt + pd.Timedelta(minutes=interval)

    if "end_time" not in df.columns:
        df["end_time"] = computed_end_dt.dt.time

    # --------------------------------------------------------
    # time_range（3min / 5min対応）
    # --------------------------------------------------------
    if "time_range" not in df.columns:
        start = dt.dt.strftime("%H:%M")
        end = computed_end_dt.dt.strftime("%H:%M")
        df["time_range"] = start + " - " + end

    # --------------------------------------------------------
    # source
    # --------------------------------------------------------
    if "source" not in df.columns:
        df["source"] = "SUMMARY"

    # ========================================================
    # datetime最終補正（安全版）
    # 既存datetimeは維持し、欠損時のみ補完する
    # ========================================================
    try:
        current_dt = pd.to_datetime(df["datetime"], errors="coerce")

        # 原則は既存 datetime を尊重
        missing_mask = current_dt.isna()

        # 万一 datetime 欠損がある場合のみ end_time/date から補完
        if missing_mask.any() and "end_time" in df.columns and "date" in df.columns:
            safe_time = _sanitize_time_string_series(df["end_time"])
            base_str = df["date"].astype(str).str.strip() + " " + safe_time

            reconstructed = pd.to_datetime(
                base_str,
                errors="coerce"
            )

            # 日跨ぎ安全化:
            # end_time が start(datetime) より小さい場合は翌日扱い
            start_dt = pd.to_datetime(df["datetime"], errors="coerce")
            rollover_mask = (
                reconstructed.notna()
                & start_dt.notna()
                & (reconstructed < start_dt)
            )
            if rollover_mask.any():
                reconstructed.loc[rollover_mask] = (
                    reconstructed.loc[rollover_mask] + pd.Timedelta(days=1)
                )

            apply_mask = missing_mask & reconstructed.notna()

            replaced = int(apply_mask.sum())
            if replaced:
                df.loc[apply_mask, "datetime"] = reconstructed.loc[apply_mask]
                logger.info(
                    "[DATETIME] final fix applied safely → %d rows",
                    replaced
                )

        # 最終正規化
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
        before = len(df)
        df = df.dropna(subset=["datetime"]).copy()
        removed = before - len(df)
        if removed:
            logger.warning(
                "[DATETIME] dropped %d rows after final normalization",
                removed
            )

        df["datetime"] = df["datetime"].dt.floor("min")

    except Exception:
        logger.exception("[DATETIME] final fix failed")

    return df