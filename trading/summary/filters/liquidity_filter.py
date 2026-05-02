# ============================================================
# File   : trading/summary/filters/liquidity_filter.py
# Version: PRODUCTION-STABLE-LIQUIDITY-FILTER-V1.0
# ------------------------------------------------------------
# 【概要】
#   SUMMARY / RANKING SUMMARY / AI ENTRY 共通の流動性フィルタ。
#
# 【目的】
#   - 出来高が少ない銘柄を TOP10 表示・AI候補・発注候補から除外する
#   - volume / trading_volume / 出来高 の表記揺れを吸収する
#   - close / close_price / current_price / price の表記揺れを吸収する
#   - 出来高だけでなく売買代金も見て、薄商い銘柄を除外する
#
# 【使い方】
#   from trading.summary.filters.liquidity_filter import (
#       filter_liquid_summary_candidates,
#       log_liquidity_profile,
#   )
#
#   df = filter_liquid_summary_candidates(
#       df,
#       interval=interval,
#       source="PUSH",
#   )
#
# 【重要】
#   - このフィルタは「表示・候補抽出前」に使う。
#   - DB保存前には原則使わない。
#     DBには低出来高銘柄も保存し、表示・エントリー側で除外する方が解析しやすい。
# ============================================================

from __future__ import annotations

import logging
from typing import Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# デフォルト閾値
# ------------------------------------------------------------
# volume は「その足の出来高」として扱う想定。
# 1min / 3min / 5min で最低出来高と最低売買代金を変える。
# ============================================================

DEFAULT_MIN_VOLUME_BY_INTERVAL = {
    1: 3000,
    3: 5000,
    5: 8000,
}

DEFAULT_MIN_TURNOVER_BY_INTERVAL = {
    1: 3_000_000,
    3: 5_000_000,
    5: 8_000_000,
}


# ============================================================
# 基本ユーティリティ
# ============================================================

def to_int_interval(interval: int | str | None) -> int:
    """
    interval の表記揺れを int に寄せる。

    Examples
    --------
    1       -> 1
    "1"     -> 1
    "1min"  -> 1
    "3min"  -> 3
    None    -> 1
    """
    try:
        s = str(interval).lower().replace("min", "").replace("m", "").strip()
        return int(s)
    except Exception:
        return 1


def resolve_close_column(df: pd.DataFrame) -> Optional[str]:
    """
    close 系カラムの表記揺れを吸収する。

    優先順位:
      1. close
      2. close_price
      3. current_price
      4. price
      5. 現在値
    """
    if df is None or not isinstance(df, pd.DataFrame):
        return None

    for col in ("close", "close_price", "current_price", "price", "現在値"):
        if col in df.columns:
            return col

    return None


def resolve_volume_column(df: pd.DataFrame) -> Optional[str]:
    """
    volume 系カラムの表記揺れを吸収する。

    優先順位:
      1. volume
      2. trading_volume
      3. volume_1m
      4.出来高
      5. 売買高
    """
    if df is None or not isinstance(df, pd.DataFrame):
        return None

    for col in ("volume", "trading_volume", "volume_1m", "出来高", "売買高"):
        if col in df.columns:
            return col

    return None


def get_liquidity_thresholds(interval: int | str | None) -> Tuple[float, float]:
    """
    interval 別の最低出来高・最低売買代金を返す。

    Returns
    -------
    tuple[float, float]
        (min_volume, min_turnover)
    """
    iv = to_int_interval(interval)

    min_volume = DEFAULT_MIN_VOLUME_BY_INTERVAL.get(
        iv,
        DEFAULT_MIN_VOLUME_BY_INTERVAL[1],
    )
    min_turnover = DEFAULT_MIN_TURNOVER_BY_INTERVAL.get(
        iv,
        DEFAULT_MIN_TURNOVER_BY_INTERVAL[1],
    )

    return float(min_volume), float(min_turnover)


def _safe_numeric_series(
    df: pd.DataFrame,
    col: str,
    *,
    default: float = 0.0,
) -> pd.Series:
    """
    指定カラムを安全に数値 Series に変換する。
    """
    if df is None or not isinstance(df, pd.DataFrame) or col not in df.columns:
        return pd.Series(dtype="float64")

    return pd.to_numeric(df[col], errors="coerce").fillna(default)


def attach_liquidity_columns(
    df: pd.DataFrame,
    *,
    volume_col: Optional[str] = None,
    close_col: Optional[str] = None,
    volume_output_col: str = "liquidity_volume",
    turnover_output_col: str = "liquidity_turnover",
) -> pd.DataFrame:
    """
    流動性判定用カラムを付与する。

    付与するカラム:
      - liquidity_volume
      - liquidity_turnover

    Notes
    -----
    - 元の df は破壊しない。
    - close がない場合、turnover は 0 になる。
    """
    if df is None or not isinstance(df, pd.DataFrame):
        return pd.DataFrame()

    out = df.copy()

    if out.empty:
        out[volume_output_col] = pd.Series(dtype="float64")
        out[turnover_output_col] = pd.Series(dtype="float64")
        return out

    if volume_col is None:
        volume_col = resolve_volume_column(out)

    if close_col is None:
        close_col = resolve_close_column(out)

    if volume_col is not None and volume_col in out.columns:
        volume = pd.to_numeric(out[volume_col], errors="coerce").fillna(0)
    else:
        volume = pd.Series([0.0] * len(out), index=out.index, dtype="float64")

    if close_col is not None and close_col in out.columns:
        close = pd.to_numeric(out[close_col], errors="coerce").fillna(0)
    else:
        close = pd.Series([0.0] * len(out), index=out.index, dtype="float64")

    out[volume_output_col] = volume
    out[turnover_output_col] = close * volume

    return out


# ============================================================
# ログ
# ============================================================

def log_liquidity_profile(
    df: pd.DataFrame,
    *,
    interval: int | str | None,
    source: str = "UNKNOWN",
    label: str = "liquidity_profile",
) -> None:
    """
    volume / turnover の状態をログ出力する。

    低出来高銘柄が混ざる原因調査用。
    """
    try:
        if df is None or not isinstance(df, pd.DataFrame):
            logger.warning(
                "[SUMMARY LIQUIDITY PROFILE] source=%s label=%s interval=%s reason=not_dataframe",
                source,
                label,
                interval,
            )
            return

        if df.empty:
            logger.info(
                "[SUMMARY LIQUIDITY PROFILE] source=%s label=%s interval=%s rows=0",
                source,
                label,
                interval,
            )
            return

        volume_col = resolve_volume_column(df)
        close_col = resolve_close_column(df)

        if volume_col is None:
            logger.warning(
                "[SUMMARY LIQUIDITY PROFILE] source=%s label=%s interval=%s rows=%d "
                "volume_col=None close_col=%s cols=%s",
                source,
                label,
                interval,
                len(df),
                close_col,
                list(df.columns),
            )
            return

        tmp = attach_liquidity_columns(
            df,
            volume_col=volume_col,
            close_col=close_col,
        )

        volume = pd.to_numeric(tmp["liquidity_volume"], errors="coerce").fillna(0)
        turnover = pd.to_numeric(tmp["liquidity_turnover"], errors="coerce").fillna(0)

        logger.info(
            "[SUMMARY LIQUIDITY PROFILE] source=%s label=%s interval=%s rows=%d "
            "volume_col=%s close_col=%s "
            "volume_min=%.2f volume_median=%.2f volume_max=%.2f "
            "volume_zero=%d volume_nonzero=%d "
            "turnover_min=%.2f turnover_median=%.2f turnover_max=%.2f",
            source,
            label,
            interval,
            len(df),
            volume_col,
            close_col,
            float(volume.min()) if len(volume) else 0.0,
            float(volume.median()) if len(volume) else 0.0,
            float(volume.max()) if len(volume) else 0.0,
            int((volume <= 0).sum()),
            int((volume > 0).sum()),
            float(turnover.min()) if len(turnover) else 0.0,
            float(turnover.median()) if len(turnover) else 0.0,
            float(turnover.max()) if len(turnover) else 0.0,
        )

    except Exception:
        logger.exception(
            "[SUMMARY LIQUIDITY PROFILE] failed source=%s label=%s interval=%s",
            source,
            label,
            interval,
        )


# ============================================================
# メインフィルタ
# ============================================================

def filter_liquid_summary_candidates(
    df: pd.DataFrame,
    *,
    interval: int | str | None,
    source: str = "UNKNOWN",
    min_volume: float | None = None,
    min_turnover: float | None = None,
    require_turnover: bool = True,
    missing_volume_policy: str = "empty",
    log_profile: bool = True,
) -> pd.DataFrame:
    """
    SUMMARY候補から低出来高・低売買代金銘柄を除外する。

    Parameters
    ----------
    df:
        対象DataFrame。

    interval:
        1 / 3 / 5 / "1min" / "3min" / "5min" など。

    source:
        ログ識別用。例: "PUSH", "RANKING", "SUMMARY_AI"。

    min_volume:
        最低出来高。None の場合は interval 別デフォルト。

    min_turnover:
        最低売買代金。None の場合は interval 別デフォルト。

    require_turnover:
        True:
            close 系カラムがある場合、売買代金条件も必須。
        False:
            出来高条件のみで判定。

    missing_volume_policy:
        volume 系カラムがない場合の挙動。

        "empty":
            空DFを返す。低出来高混入防止を優先。

        "pass":
            フィルタせずそのまま返す。互換性優先。

    log_profile:
        True の場合、フィルタ前後の流動性プロファイルをログ出力する。

    Returns
    -------
    pd.DataFrame
        フィルタ後DataFrame。

    Notes
    -----
    - 元の df は破壊しない。
    - 内部判定用カラムは返却前に削除する。
    - フィルタ失敗時は安全側に倒して空DFを返す。
    """
    try:
        if df is None or not isinstance(df, pd.DataFrame):
            logger.warning(
                "[SUMMARY LIQUIDITY FILTER] source=%s interval=%s reason=not_dataframe",
                source,
                interval,
            )
            return pd.DataFrame()

        if df.empty:
            logger.info(
                "[SUMMARY LIQUIDITY FILTER] source=%s interval=%s before=0 after=0 reason=empty_df",
                source,
                interval,
            )
            return df.copy()

        if log_profile:
            log_liquidity_profile(
                df,
                interval=interval,
                source=source,
                label="before_filter",
            )

        default_min_volume, default_min_turnover = get_liquidity_thresholds(interval)

        if min_volume is None:
            min_volume = default_min_volume

        if min_turnover is None:
            min_turnover = default_min_turnover

        volume_col = resolve_volume_column(df)
        close_col = resolve_close_column(df)

        before = len(df)

        if volume_col is None:
            if missing_volume_policy == "pass":
                logger.warning(
                    "[SUMMARY LIQUIDITY FILTER] source=%s interval=%s before=%d after=%d "
                    "reason=missing_volume_col policy=pass cols=%s",
                    source,
                    interval,
                    before,
                    before,
                    list(df.columns),
                )
                return df.copy()

            logger.warning(
                "[SUMMARY LIQUIDITY FILTER] source=%s interval=%s before=%d after=0 "
                "reason=missing_volume_col policy=empty cols=%s",
                source,
                interval,
                before,
                list(df.columns),
            )
            return df.iloc[0:0].copy()

        out = attach_liquidity_columns(
            df,
            volume_col=volume_col,
            close_col=close_col,
            volume_output_col="_liquidity_volume",
            turnover_output_col="_liquidity_turnover",
        )

        volume = pd.to_numeric(out["_liquidity_volume"], errors="coerce").fillna(0)
        turnover = pd.to_numeric(out["_liquidity_turnover"], errors="coerce").fillna(0)

        volume_ok = volume >= float(min_volume)

        if require_turnover and close_col is not None:
            turnover_ok = turnover >= float(min_turnover)
            mask = volume_ok & turnover_ok
        else:
            turnover_ok = pd.Series([True] * len(out), index=out.index)
            mask = volume_ok

        filtered = out.loc[mask].copy()

        drop_cols = [
            "_liquidity_volume",
            "_liquidity_turnover",
            "liquidity_volume",
            "liquidity_turnover",
        ]
        filtered.drop(
            columns=[c for c in drop_cols if c in filtered.columns],
            inplace=True,
            errors="ignore",
        )

        logger.info(
            "[SUMMARY LIQUIDITY FILTER] source=%s interval=%s before=%d after=%d removed=%d "
            "volume_col=%s close_col=%s min_volume=%.2f min_turnover=%.2f "
            "require_turnover=%s volume_ng=%d turnover_ng=%d",
            source,
            interval,
            before,
            len(filtered),
            before - len(filtered),
            volume_col,
            close_col,
            float(min_volume),
            float(min_turnover),
            require_turnover,
            int((~volume_ok).sum()),
            int((~turnover_ok).sum()) if require_turnover and close_col is not None else 0,
        )

        if log_profile:
            log_liquidity_profile(
                filtered,
                interval=interval,
                source=source,
                label="after_filter",
            )

        return filtered

    except Exception:
        logger.exception(
            "[SUMMARY LIQUIDITY FILTER] failed source=%s interval=%s rows=%s",
            source,
            interval,
            len(df) if isinstance(df, pd.DataFrame) else None,
        )

        # 安全側: フィルタ失敗時に薄商い銘柄を通さない
        if isinstance(df, pd.DataFrame):
            return df.iloc[0:0].copy()

        return pd.DataFrame()


# ============================================================
# TOP10向けエイリアス
# ============================================================

def filter_liquid_summary_for_display(
    df: pd.DataFrame,
    *,
    interval: int | str | None,
    source: str = "DISPLAY",
    min_volume: float | None = None,
    min_turnover: float | None = None,
) -> pd.DataFrame:
    """
    TOP10表示前専用の薄商い除外フィルタ。

    safe_io.py / announce.py から呼ぶ想定。
    """
    return filter_liquid_summary_candidates(
        df,
        interval=interval,
        source=source,
        min_volume=min_volume,
        min_turnover=min_turnover,
        require_turnover=True,
        missing_volume_policy="empty",
        log_profile=True,
    )


def filter_liquid_summary_for_entry(
    df: pd.DataFrame,
    *,
    interval: int | str | None,
    source: str = "ENTRY",
    min_volume: float | None = None,
    min_turnover: float | None = None,
) -> pd.DataFrame:
    """
    AIエントリー候補前専用の薄商い除外フィルタ。

    trading/entry/summary_ai/runner.py から呼ぶ想定。
    """
    return filter_liquid_summary_candidates(
        df,
        interval=interval,
        source=source,
        min_volume=min_volume,
        min_turnover=min_turnover,
        require_turnover=True,
        missing_volume_policy="empty",
        log_profile=True,
    )


__all__ = [
    "DEFAULT_MIN_VOLUME_BY_INTERVAL",
    "DEFAULT_MIN_TURNOVER_BY_INTERVAL",
    "to_int_interval",
    "resolve_close_column",
    "resolve_volume_column",
    "get_liquidity_thresholds",
    "attach_liquidity_columns",
    "log_liquidity_profile",
    "filter_liquid_summary_candidates",
    "filter_liquid_summary_for_display",
    "filter_liquid_summary_for_entry",
]