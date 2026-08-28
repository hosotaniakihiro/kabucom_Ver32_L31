# ============================================================
# File   : trading/ranking/summary/utils.py
# Ver    : PRODUCTION-STABLE-REV1.0-RANKING-SUMMARY-UTILS
# ------------------------------------------------------------
# 【概要】
#   ランキング由来サマリー用の共通 utility
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
from pathlib import Path
from typing import Any, Iterable, Optional

import pandas as pd
from nas_root import nas_path, nas_root

logger = logging.getLogger(__name__)


def safe_str(v: Any) -> str:
    if v is None:
        return ""

    try:
        if pd.isna(v):
            return ""
    except Exception:
        pass

    s = str(v).strip()
    if s.lower() in {"nan", "none", "nat"}:
        return ""
    return s


def fmt_price(v: Any) -> str:
    try:
        if pd.isna(v):
            return "-"
        return f"{float(v):.1f}"
    except Exception:
        return "-"


def fmt2(v: Any) -> str:
    try:
        if pd.isna(v):
            return "-"
        return f"{float(v):.2f}"
    except Exception:
        return "-"


def fmt_int(v: Any) -> str:
    try:
        if pd.isna(v):
            return "-"
        return f"{int(float(v)):,}"
    except Exception:
        return "-"


def normalize_trade_date(
    trade_date: Optional[str | int | dt.date | dt.datetime] = None,
) -> dt.date:
    if trade_date is None:
        return dt.date.today()

    if isinstance(trade_date, dt.datetime):
        return trade_date.date()

    if isinstance(trade_date, dt.date):
        return trade_date

    s = str(trade_date).strip()

    for fmt in ("%Y%m%d", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except Exception:
            pass

    try:
        return pd.to_datetime(s, errors="raise").date()
    except Exception:
        logger.warning(
            "[RANKING SUMMARY UTILS] invalid trade_date=%r -> today",
            trade_date,
        )
        return dt.date.today()


# ============================================================
# NAS root の表記ゆれ対策 (R17)
# ------------------------------------------------------------
# 2026-08-28 17:1x の実測:
#   os.path.exists(UNC + r"\raw_data\kabu_station\ranking\ranking20260828.db")
#       -> False  (3 回連続。SMB セッションが落ちている)
#   os.path.exists(r"X:\raw_data\kabu_station\ranking\ranking20260828.db")
#       -> True   (607.2MB)
#
# 同じ共有に UNC とドライブレターの 2 表記で触ると Windows は
# 別セッションとして扱う (第14ラウンドで子プロセス起動でも同じ問題を確認済み)。
# UNC 側のセッションが切れると path_exists() が False になり、
# read_sqlite_table() が「db not found」で **空 DataFrame を返す**。
# 「無かった」と「読めなかった」が同じ空フレームになるのは危険なので、
# 到達可能な表記へ寄せる。
#
# ENV:
#   AUTOSTOCK_NAS_ROOT           明示指定 (最優先)
#   AUTOSTOCK_NAS_ROOT_FALLBACK  既定 "X:" (UNC が到達不能なときに使う)
# ============================================================

_NAS_ROOT_UNC = nas_root()
_NAS_ROOT_CACHE: Optional[str] = None


def _nas_root() -> str:
    """到達可能な NAS root 表記を返す (プロセス内で 1 回だけ判定)。"""
    global _NAS_ROOT_CACHE
    if _NAS_ROOT_CACHE is not None:
        return _NAS_ROOT_CACHE

    explicit = str(os.getenv("AUTOSTOCK_NAS_ROOT", "")).strip()
    if explicit:
        _NAS_ROOT_CACHE = explicit.rstrip("\\/")
        logger.warning(
            "[RANKING SUMMARY UTILS] NAS root は env 指定を使う root=%s",
            _NAS_ROOT_CACHE,
        )
        return _NAS_ROOT_CACHE

    fallback = str(os.getenv("AUTOSTOCK_NAS_ROOT_FALLBACK", "X:")).strip().rstrip("\\/")

    try:
        if os.path.exists(_NAS_ROOT_UNC):
            _NAS_ROOT_CACHE = _NAS_ROOT_UNC
            return _NAS_ROOT_CACHE
    except Exception:
        logger.debug("[RANKING SUMMARY UTILS] UNC root probe failed", exc_info=True)

    try:
        if fallback and os.path.exists(fallback + "\\"):
            _NAS_ROOT_CACHE = fallback
            logger.warning(
                "[RANKING SUMMARY UTILS] UNC root %s へ到達できないため %s を使う "
                "(同一共有への二重 SMB セッションを避ける。"
                "AUTOSTOCK_NAS_ROOT で明示指定できる)",
                _NAS_ROOT_UNC, fallback,
            )
            return _NAS_ROOT_CACHE
    except Exception:
        logger.debug("[RANKING SUMMARY UTILS] fallback root probe failed", exc_info=True)

    # どちらも駄目なら従来どおり UNC を返す (呼び出し側で not found として扱われる)
    _NAS_ROOT_CACHE = _NAS_ROOT_UNC
    logger.error(
        "[RANKING SUMMARY UTILS] NAS root へ到達できない unc=%s fallback=%s "
        "(ranking/yahoo DB は読めない見込み)",
        _NAS_ROOT_UNC, fallback,
    )
    return _NAS_ROOT_CACHE


def reset_nas_root_cache_for_test() -> None:
    global _NAS_ROOT_CACHE
    _NAS_ROOT_CACHE = None


def default_ranking_db_path(trade_date: dt.date) -> str:
    ymd = trade_date.strftime("%Y%m%d")
    return _nas_root() + rf"\raw_data\kabu_station\ranking\ranking{ymd}.db"


def default_yahoo_db_path(trade_date: dt.date) -> str:
    ymd = trade_date.strftime("%Y%m%d")
    return _nas_root() + rf"\raw_data\yahoo\intraday\yahoo_1min_{ymd}.db"


def path_exists(path: Optional[str]) -> bool:
    if not path:
        return False

    try:
        return Path(path).exists()
    except Exception:
        try:
            return os.path.exists(str(path))
        except Exception:
            return False


def first_existing_col(df: pd.DataFrame, candidates: Iterable[str]) -> Optional[str]:
    if df is None or df.empty:
        return None

    cols = set(map(str, df.columns))

    for c in candidates:
        if c in cols:
            return c

    return None


def normalize_symbols(symbols: Optional[Iterable[str]]) -> Optional[list[str]]:
    if symbols is None:
        return None

    out: list[str] = []

    for s in symbols:
        if s is None:
            continue

        ss = str(s).strip()

        if not ss:
            continue

        if ss.upper().startswith("FILLER_"):
            continue

        out.append(ss)

    if not out:
        return None

    return sorted(set(out))


def log_df_profile(label: str, df: pd.DataFrame) -> None:
    try:
        if df is None or df.empty:
            logger.info("[RANKING SUMMARY RUNNER][PROFILE] %s rows=0", label)
            return

        msg: dict[str, Any] = {
            "rows": len(df),
            "cols": list(df.columns),
        }

        if "symbol" in df.columns:
            msg["symbols"] = df["symbol"].astype(str).nunique()

        if "datetime" in df.columns:
            s = pd.to_datetime(df["datetime"], errors="coerce")
            msg["dt_min"] = str(s.min())
            msg["dt_max"] = str(s.max())

        if "close" in df.columns:
            c = pd.to_numeric(df["close"], errors="coerce")
            msg["close_nonnull"] = int(c.notna().sum())
            msg["close_gt0"] = int((c > 0).sum())

        logger.info("[RANKING SUMMARY RUNNER][PROFILE] %s %s", label, msg)

    except Exception:
        logger.exception("[RANKING SUMMARY RUNNER][PROFILE] failed label=%s", label)


def combine_numeric_columns(
    df: pd.DataFrame,
    candidates: list[str] | tuple[str, ...],
    *,
    default: float | None = None,
) -> pd.Series:
    """
    複数の候補列から、最初に有効な数値を採用して 1 本の float Series を作る。

    例:
        close = combine_numeric_columns(df, ["close", "current_price", "price"])

    修正点:
      - pd.NA + dtype="float64" は pandas 環境によって TypeError になる
      - float64 の欠損初期値は np.nan を使う
      - 候補列が存在しない場合も落とさず NaN / default を返す
      - 文字列数値、カンマ付き数値、空文字を安全に処理する
    """
    import numpy as np
    import pandas as pd

    if df is None:
        return pd.Series(dtype="float64")

    # ★重要修正：pd.NA ではなく np.nan を使う
    out = pd.Series(np.nan, index=df.index, dtype="float64")

    if not candidates:
        if default is not None:
            out = out.fillna(float(default))
        return out

    for col in candidates:
        if col not in df.columns:
            continue

        try:
            s = df[col]

            # DataFrame になっている場合、重複列の可能性があるので先頭を使う
            if isinstance(s, pd.DataFrame):
                if s.shape[1] == 0:
                    continue
                s = s.iloc[:, 0]

            # 文字列数値対策
            if s.dtype == object:
                s = (
                    s.astype(str)
                    .str.replace(",", "", regex=False)
                    .str.replace("円", "", regex=False)
                    .str.strip()
                    # 文字列 Series へ np.nan を混ぜると object->float の暗黙ダウンキャスト。
                    # 直後の pd.to_numeric(errors="coerce") で同じ NaN になる。
                    .replace({"None": "", "nan": "", "NaN": "", "<NA>": ""})
                )

            num = pd.to_numeric(s, errors="coerce")

            # まだ out が NaN の場所だけ埋める
            out = out.where(out.notna(), num)

        except Exception:
            # 候補列 1 本の異常で全体を止めない
            continue

    if default is not None:
        out = out.fillna(float(default))

    return out.astype("float64")


def combine_text_columns(
    df: pd.DataFrame,
    candidates: Iterable[str],
    *,
    default: str = "",
) -> pd.Series:
    if df is None or df.empty:
        return pd.Series(dtype="object")

    out = pd.Series(pd.NA, index=df.index, dtype="object")

    for c in candidates:
        if c not in df.columns:
            continue

        try:
            s = df[c].astype(object)
            s = s.where(~pd.isna(s), pd.NA)
            s = s.astype(str).str.strip()
            s = s.where(~s.str.lower().isin(["", "nan", "none", "nat"]), pd.NA)
            out = out.combine_first(s)
        except Exception:
            continue

    return out.fillna(default).astype(str)

def safe_df(
    data: Any = None,
    *,
    columns: list[str] | tuple[str, ...] | None = None,
    copy: bool = True,
) -> pd.DataFrame:
    """
    任意の入力を安全に DataFrame 化する互換関数。

    対応:
      - None
      - pd.DataFrame
      - pd.Series
      - dict
      - list[dict]
      - list / tuple / generator

    bootstrap_loader.py 側で DataFrame 前提の処理をしても
    起動停止しないようにする。
    """
    try:
        if data is None:
            return pd.DataFrame(columns=list(columns) if columns else None)

        if isinstance(data, pd.DataFrame):
            df = data.copy() if copy else data
            if columns:
                for c in columns:
                    if c not in df.columns:
                        df[c] = pd.NA
            return df

        if isinstance(data, pd.Series):
            df = data.to_frame().T
            if columns:
                for c in columns:
                    if c not in df.columns:
                        df[c] = pd.NA
            return df

        if isinstance(data, dict):
            # {"rows": [...]} / {"data": [...]} 形式にも対応
            for key in ("rows", "data", "records", "results", "items"):
                val = data.get(key)
                if isinstance(val, pd.DataFrame):
                    return safe_df(val, columns=columns, copy=copy)
                if isinstance(val, list):
                    return safe_df(val, columns=columns, copy=copy)

            df = pd.DataFrame([data])
            if columns:
                for c in columns:
                    if c not in df.columns:
                        df[c] = pd.NA
            return df

        if isinstance(data, (list, tuple)):
            df = pd.DataFrame(data)
            if columns:
                for c in columns:
                    if c not in df.columns:
                        df[c] = pd.NA
            return df

        # generator / iterable
        if isinstance(data, Iterable) and not isinstance(data, (str, bytes)):
            df = pd.DataFrame(list(data))
            if columns:
                for c in columns:
                    if c not in df.columns:
                        df[c] = pd.NA
            return df

        # scalar
        df = pd.DataFrame([{"value": data}])
        if columns:
            for c in columns:
                if c not in df.columns:
                    df[c] = pd.NA
        return df

    except Exception:
        return pd.DataFrame(columns=list(columns) if columns else None)


def to_records(
    data: Any,
    *,
    drop_none: bool = False,
    drop_nan: bool = False,
) -> list[dict[str, Any]]:
    """
    DataFrame / Series / dict / list を list[dict] に変換する互換関数。

    bootstrap_loader.py でログ出力・AI結果処理・executor渡しに使える形式へ寄せる。
    """
    df = safe_df(data)

    if df.empty:
        return []

    records = df.to_dict(orient="records")

    if not drop_none and not drop_nan:
        return records

    cleaned: list[dict[str, Any]] = []

    for rec in records:
        out: dict[str, Any] = {}

        for k, v in rec.items():
            if drop_none and v is None:
                continue

            if drop_nan:
                try:
                    if pd.isna(v):
                        continue
                except Exception:
                    pass

            out[str(k)] = v

        cleaned.append(out)

    return cleaned


def is_empty_df(data: Any) -> bool:
    """
    DataFrame相当が空かどうかを安全に判定する補助関数。
    """
    try:
        return safe_df(data).empty
    except Exception:
        return True


def ensure_columns(
    df: pd.DataFrame,
    columns: list[str] | tuple[str, ...],
    *,
    default: Any = pd.NA,
) -> pd.DataFrame:
    """
    指定列が無ければ追加する。
    """
    work = safe_df(df)

    for c in columns:
        if c not in work.columns:
            work[c] = default

    return work


def first_existing_col(
    df: pd.DataFrame,
    candidates: list[str] | tuple[str, ...],
) -> str | None:
    """
    candidates のうち最初に存在する列名を返す。
    大文字小文字違いにも軽く対応。
    """
    if df is None or df.empty:
        return None

    for c in candidates:
        if c in df.columns:
            return c

    lower_map = {str(c).lower(): c for c in df.columns}

    for c in candidates:
        hit = lower_map.get(str(c).lower())
        if hit is not None:
            return hit

    return None


def safe_numeric_series(
    s: Any,
    *,
    default: float = 0.0,
) -> pd.Series:
    """
    Series相当を安全に数値化する。
    """
    try:
        return pd.to_numeric(s, errors="coerce").fillna(default)
    except Exception:
        try:
            return pd.Series(s).pipe(pd.to_numeric, errors="coerce").fillna(default)
        except Exception:
            return pd.Series(dtype="float64")


def safe_datetime_series(s: Any) -> pd.Series:
    """
    Series相当を安全に datetime 化する。
    """
    try:
        return pd.to_datetime(s, errors="coerce")
    except Exception:
        try:
            return pd.to_datetime(pd.Series(s), errors="coerce")
        except Exception:
            return pd.Series(dtype="datetime64[ns]")