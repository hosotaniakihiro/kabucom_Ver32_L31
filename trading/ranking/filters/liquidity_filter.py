# ============================================================
# File   : trading/ranking/filters/liquidity_filter.py
# Version: Ver3.1-RECALCULATE-BROKEN-TURNOVER
# ------------------------------------------------------------
# ✔ numeric sanitize 強化
# ✔ close / close_price / current_price / price を価格列として認識
# ✔ volume / trading_volume を出来高列として認識
# ✔ turnover / trading_value を売買代金列として認識
# ✔ turnover / trading_value が0・NaN・不自然に小さい場合は close * volume で補正
# ✔ 200円未満の銘柄を除外
# ✔ サマリー表示・エントリー前・保存前の互換aliasを提供
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any, Iterable, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# default thresholds
# ============================================================

MIN_VOLUME = int(float(os.environ.get("LIQUIDITY_MIN_VOLUME", "10000")))
MIN_TURNOVER = int(float(os.environ.get("LIQUIDITY_MIN_TURNOVER", "1000000")))

# 100円ではなく200円。低位株をここで落とす。
MIN_PRICE = float(os.environ.get("LIQUIDITY_MIN_PRICE", "200"))
MAX_PRICE = float(os.environ.get("LIQUIDITY_MAX_PRICE", "10000"))

ENABLE_LIQUIDITY_FILTER = str(
    os.environ.get("ENABLE_LIQUIDITY_FILTER", "1")
).strip().lower() not in {"0", "false", "no", "off", "ng"}

REQUIRE_PRICE_COLUMN = str(
    os.environ.get("LIQUIDITY_REQUIRE_PRICE_COLUMN", "1")
).strip().lower() not in {"0", "false", "no", "off", "ng"}

PRICE_COLUMNS = (
    "close",
    "close_price",
    "current_price",
    "price",
    "last_price",
)

# ------------------------------------------------------------
# 流動性スクリーニングに使う出来高列 (2026-09-01 修正)
# ------------------------------------------------------------
# LIQUIDITY_MIN_VOLUME=10000 / MIN_TURNOVER=1,000,000 は
# 「その銘柄が日中どれだけ商いがあるか」を見る *日次流動性* の閾値。
# ところが従来は列の優先順位が "volume" 先頭だったため、
# ranking_pipeline が渡す *1本の足の出来高* (3分足 max 5500) と
# 日次流動性の閾値を突き合わせ、50 銘柄中 48 銘柄を落として
# "empty after volume filter" になっていた (実測 13:46/13:48/13:50)。
#
# kabu Station PUSH の TradingVolume は当日累計出来高で、
# 本システムでは trading_volume 列がそれにあたる
# (memory_latest_1m は集計時に "last" を取っている = 累計の最新値)。
# 流動性判定では累計側を優先する。閾値は変更しない。
CUMULATIVE_VOLUME_COLUMNS = (
    "trading_volume",
    "day_volume",
    "cum_volume",
    "volume_cum",
)

PER_BAR_VOLUME_COLUMNS = (
    "volume",
)

VOLUME_COLUMNS = CUMULATIVE_VOLUME_COLUMNS + PER_BAR_VOLUME_COLUMNS

TURNOVER_COLUMNS = (
    "turnover",
    "trading_value",
)


# ============================================================
# helpers
# ============================================================

def _first_existing_col(df: pd.DataFrame, candidates: Iterable[str]) -> Optional[str]:
    for c in candidates:
        if c in df.columns:
            return c
    return None


def _to_numeric_col(df: pd.DataFrame, col: str, default: float = 0.0) -> pd.Series:
    if col not in df.columns:
        return pd.Series([default] * len(df), index=df.index, dtype="float64")
    return pd.to_numeric(df[col], errors="coerce").replace([np.inf, -np.inf], np.nan).fillna(default)


def _symbol_head(df: pd.DataFrame, limit: int = 20) -> list[str]:
    if df is None or df.empty or "symbol" not in df.columns:
        return []
    try:
        return (
            df["symbol"]
            .dropna()
            .astype(str)
            .drop_duplicates()
            .head(limit)
            .tolist()
        )
    except Exception:
        return []


def _sanitize_numeric(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    try:
        for col in list(df.columns):
            try:
                if pd.api.types.is_string_dtype(df[col]) and col not in {"symbol", "symbolname", "name", "source", "ranking_type", "rank_types", "type", "market", "date", "time", "datetime", "start_time", "end_time", "time_range"}:
                    try:
                        df[col] = pd.to_numeric(df[col], errors="raise")
                    except (ValueError, TypeError):
                        pass
            except Exception:
                pass

        for c in (*PRICE_COLUMNS, *VOLUME_COLUMNS, *TURNOVER_COLUMNS, "vwap"):
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce").replace([np.inf, -np.inf], np.nan).fillna(0).clip(lower=0)

        num_cols = df.select_dtypes(include=np.number).columns
        if len(num_cols) > 0:
            df[num_cols] = df[num_cols].replace([np.inf, -np.inf], np.nan).fillna(0).clip(-1e12, 1e12)
    except Exception:
        logger.exception("[liquidity_filter] numeric sanitize failed")
    return df


def _ensure_standard_columns(df: pd.DataFrame) -> pd.DataFrame:
    """close / volume / turnover / trading_value の標準列を可能な限り作る。"""
    if df is None or df.empty:
        return df

    out = df
    price_col = _first_existing_col(out, PRICE_COLUMNS)
    volume_col = _first_existing_col(out, VOLUME_COLUMNS)
    turnover_col = _first_existing_col(out, TURNOVER_COLUMNS)

    # 1列ずつ代入すると 160 列超のフレームに最大 8 回 frame.insert が走り、
    # pandas が PerformanceWarning (DataFrame is highly fragmented) を出す。
    # 値は変えずに、まとめて 1 回で足す。
    pending: dict[str, Any] = {}

    if price_col:
        price = _to_numeric_col(out, price_col, default=0.0)
        if "close" not in out.columns:
            pending["close"] = price
        for alias in ("close_price", "current_price", "price"):
            if alias not in out.columns:
                pending[alias] = price

    if volume_col:
        volume = _to_numeric_col(out, volume_col, default=0.0)
        if "volume" not in out.columns:
            pending["volume"] = volume
        if "trading_volume" not in out.columns:
            pending["trading_volume"] = volume

    if turnover_col:
        turnover = _to_numeric_col(out, turnover_col, default=0.0)
        if "turnover" not in out.columns:
            pending["turnover"] = turnover
        if "trading_value" not in out.columns:
            pending["trading_value"] = turnover

    if pending:
        # assign でも内部的には1列ずつ挿入されるため、上流で既に断片化している
        # 160列超のフレームでは PerformanceWarning が残る。
        # pandas が案内するとおり concat で一度に連結する (値は変わらない)。
        out = pd.concat([out, pd.DataFrame(pending, index=out.index)], axis=1)

    return out


def _ensure_turnover(df: pd.DataFrame) -> pd.DataFrame:
    """turnover / trading_value を信用しすぎず、壊れている場合は close * volume で再計算する。"""
    if df is None or df.empty:
        return df

    try:
        price_col = _first_existing_col(df, PRICE_COLUMNS)
        volume_col = _first_existing_col(df, VOLUME_COLUMNS)

        price = _to_numeric_col(df, price_col, default=0.0) if price_col else pd.Series(0.0, index=df.index)
        volume = _to_numeric_col(df, volume_col, default=0.0) if volume_col else pd.Series(0.0, index=df.index)
        computed = (price * volume).replace([np.inf, -np.inf], np.nan).fillna(0.0).clip(lower=0)

        existing_col = _first_existing_col(df, TURNOVER_COLUMNS)
        existing = _to_numeric_col(df, existing_col, default=0.0) if existing_col else pd.Series(0.0, index=df.index)

        # 既存turnoverが0/NaN相当、または close*volume より極端に小さい行だけ補正する。
        # 例: PUSH summary で volume/close はあるが turnover=0 のため候補が全滅する問題を防ぐ。
        repair_mask = (computed > 0) & ((existing <= 0) | (existing < computed * 0.05))
        repaired = existing.copy()
        repaired.loc[repair_mask] = computed.loc[repair_mask]

        # 2列を別々に代入すると 160 列超のフレームで frame.insert が2回走り
        # PerformanceWarning (highly fragmented) になる。concat で一度に入れる。
        new_cols = pd.DataFrame({"turnover": repaired, "trading_value": repaired}, index=df.index)
        df = pd.concat([df.drop(columns=[c for c in ("turnover", "trading_value") if c in df.columns]), new_cols], axis=1)

        repaired_count = int(repair_mask.sum()) if len(df) else 0
        if repaired_count > 0:
            logger.info(
                "[liquidity_filter] turnover repaired rows=%s existing_col=%s price_col=%s volume_col=%s computed_min=%.1f computed_max=%.1f",
                repaired_count,
                existing_col,
                price_col,
                volume_col,
                float(computed[repair_mask].min()) if repaired_count else 0.0,
                float(computed[repair_mask].max()) if repaired_count else 0.0,
            )
    except Exception:
        logger.exception("[liquidity_filter] turnover creation/repair failed")
        if "turnover" not in df.columns:
            df["turnover"] = 0.0
        if "trading_value" not in df.columns:
            df["trading_value"] = df["turnover"]

    return df


# ============================================================
# filters
# ============================================================

def _volume_filter(df: pd.DataFrame, min_volume: int) -> pd.DataFrame:
    volume_col = _first_existing_col(df, VOLUME_COLUMNS)
    if not volume_col:
        return df

    before = len(df)
    volume = _to_numeric_col(df, volume_col, default=0.0)

    is_cumulative = volume_col in CUMULATIVE_VOLUME_COLUMNS
    if not is_cumulative:
        # 累計列が無く 1 本足の出来高しか無い。
        # 日次流動性の閾値をそのまま当てると全滅するので、
        # 「何と何を比べているか」を必ず残す。閾値は動かさない。
        logger.warning(
            "[liquidity_filter] 累計出来高列が無いため per-bar 列で判定する col=%s "
            "min_volume=%s vol_min=%.0f vol_max=%.0f rows=%d "
            "(閾値は日次流動性向け。全滅する場合は上流が 1 本足を渡している)",
            volume_col, min_volume,
            float(volume.min()) if before else 0.0,
            float(volume.max()) if before else 0.0,
            before,
        )

    out = df[volume >= float(min_volume)].copy()
    removed = before - len(out)

    if before and len(out) == 0:
        logger.error(
            "[liquidity_filter] volume filter が全銘柄を除外した col=%s cumulative=%s "
            "min_volume=%s vol_min=%.0f vol_max=%.0f rows=%d "
            "(閾値と列の意味が一致しているか確認する)",
            volume_col, is_cumulative, min_volume,
            float(volume.min()), float(volume.max()), before,
        )

    if removed > 0:
        logger.info(
            "[liquidity_filter] volume filter removed=%s min_volume=%s col=%s removed_head=%s",
            removed,
            min_volume,
            volume_col,
            _symbol_head(df.loc[~df.index.isin(out.index)]),
        )
    return out


def _turnover_filter(df: pd.DataFrame, min_turnover: int) -> pd.DataFrame:
    turnover_col = _first_existing_col(df, TURNOVER_COLUMNS)
    if not turnover_col:
        return df

    before = len(df)
    turnover = _to_numeric_col(df, turnover_col, default=0.0)
    out = df[turnover >= float(min_turnover)].copy()
    removed = before - len(out)

    if removed > 0:
        logger.info(
            "[liquidity_filter] turnover filter removed=%s min_turnover=%s col=%s turnover_min=%.1f turnover_max=%.1f removed_head=%s",
            removed,
            min_turnover,
            turnover_col,
            float(turnover.min()) if len(turnover) else 0.0,
            float(turnover.max()) if len(turnover) else 0.0,
            _symbol_head(df.loc[~df.index.isin(out.index)]),
        )
    return out


def _price_filter(
    df: pd.DataFrame,
    min_price: float,
    max_price: float,
    *,
    require_price: bool = REQUIRE_PRICE_COLUMN,
) -> pd.DataFrame:
    price_col = _first_existing_col(df, PRICE_COLUMNS)

    if not price_col:
        if require_price:
            logger.warning(
                "[liquidity_filter] price column missing -> drop all rows rows=%d cols=%s",
                len(df),
                list(df.columns),
            )
            return df.iloc[0:0].copy()
        logger.warning(
            "[liquidity_filter] price column missing but require_price=False -> skip price filter rows=%d",
            len(df),
        )
        return df

    before = len(df)
    price = _to_numeric_col(df, price_col, default=0.0)
    out = df[(price >= float(min_price)) & (price <= float(max_price))].copy()
    removed = before - len(out)

    if removed > 0:
        logger.info(
            "[liquidity_filter] price filter removed=%s min_price=%.1f max_price=%.1f col=%s removed_head=%s",
            removed,
            min_price,
            max_price,
            price_col,
            _symbol_head(df.loc[~df.index.isin(out.index)]),
        )
    return out


def _vwap_guard(df: pd.DataFrame) -> pd.DataFrame:
    # vwap が存在しない、または全体が0の場合はPUSH由来summaryとして通す。
    # ここで全落ちすると表示/候補が空になりやすい。
    if "vwap" not in df.columns:
        return df

    before = len(df)
    vwap = pd.to_numeric(df["vwap"], errors="coerce").replace([np.inf, -np.inf], np.nan).fillna(0)
    if before > 0 and int((vwap > 0).sum()) == 0:
        logger.info("[liquidity_filter] vwap guard skipped because all vwap are zero rows=%s", before)
        return df

    mask = (vwap > 0) & np.isfinite(vwap)
    out = df[mask].copy()
    removed = before - len(out)
    if removed > 0:
        logger.info("[liquidity_filter] vwap guard removed=%s removed_head=%s", removed, _symbol_head(df.loc[~df.index.isin(out.index)]))
    return out


# ============================================================
# main filter
# ============================================================

def apply_liquidity_filter_core(
    df: pd.DataFrame,
    *,
    min_volume: int = MIN_VOLUME,
    min_turnover: int = MIN_TURNOVER,
    min_price: float = MIN_PRICE,
    max_price: float = MAX_PRICE,
    require_price: bool = REQUIRE_PRICE_COLUMN,
    context: str = "",
) -> pd.DataFrame:
    if df is None or not isinstance(df, pd.DataFrame):
        return pd.DataFrame()
    if df.empty:
        return df
    if not ENABLE_LIQUIDITY_FILTER:
        return df

    try:
        before_rows = len(df)
        before_symbols = df["symbol"].nunique() if "symbol" in df.columns else 0

        out = df.copy()
        out = _sanitize_numeric(out)
        out = _ensure_standard_columns(out)
        out = _ensure_turnover(out)
        out = _sanitize_numeric(out)

        out = _price_filter(out, min_price=float(min_price), max_price=float(max_price), require_price=require_price)
        if out.empty:
            logger.info("[liquidity_filter] empty after price filter context=%s before_rows=%d min_price=%.1f", context, before_rows, min_price)
            return out

        if min_volume > 0:
            out = _volume_filter(out, int(min_volume))
            if out.empty:
                logger.info("[liquidity_filter] empty after volume filter context=%s before_rows=%d", context, before_rows)
                return out

        # volume filter 後の行にもturnover補正を再適用する。
        out = _ensure_turnover(out)
        out = _sanitize_numeric(out)

        if min_turnover > 0:
            out = _turnover_filter(out, int(min_turnover))
            if out.empty:
                logger.info("[liquidity_filter] empty after turnover filter context=%s before_rows=%d", context, before_rows)
                return out

        out = _vwap_guard(out)
        if out.empty:
            logger.info("[liquidity_filter] empty after vwap guard context=%s before_rows=%d", context, before_rows)
            return out

        after_rows = len(out)
        after_symbols = out["symbol"].nunique() if "symbol" in out.columns else 0
        logger.info(
            "[liquidity_filter] done context=%s before_rows=%d after_rows=%d removed_rows=%d before_symbols=%d after_symbols=%d min_price=%.1f min_volume=%s min_turnover=%s",
            context,
            before_rows,
            after_rows,
            before_rows - after_rows,
            before_symbols,
            after_symbols,
            min_price,
            min_volume,
            min_turnover,
        )
        return out
    except Exception:
        logger.exception("[liquidity_filter] failed context=%s", context)
        return pd.DataFrame()


# ============================================================
# 流動性フィルタが全件 0 件になったときの段階緩和
#   旧 core/startup/liquidity_empty_fallback_patch.py (V1)
# ------------------------------------------------------------
# [ranking_pipeline] empty after apply_liquidity_filter で候補が全部消えるのを防ぐ。
# 代金 (turnover) のみを段階的に緩め、price は落とさない。
#
# 旧パッチは ENABLE_LIQ_EMPTY_FALLBACK_PATCH が真のときだけ install される
# opt-in (既定 OFF) だった。そのスコープを _liq_empty_fallback_enabled() で再現する。
# ============================================================
def _liq_empty_fallback_enabled() -> bool:
    """旧 sitecustomize._install_liq_empty_fallback_only_if_enabled() の判定を再現する。"""
    try:
        if str(os.environ.get("DISABLE_LIQ_EMPTY_FALLBACK_PATCH", "")).strip() == "1":
            return False
        v = os.environ.get("ENABLE_LIQ_EMPTY_FALLBACK_PATCH")
        if v is None or str(v).strip() == "":
            return False
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return False


def apply_liquidity_filter(
    df: pd.DataFrame,
    *,
    min_volume: int = None,
    min_turnover: int = None,
    min_price: float = None,
    max_price: float = None,
    require_price: bool = None,
    context: str = "",
) -> pd.DataFrame:
    # 既定値はモジュール定数を「呼び出し時に」読む。
    # (旧パッチが getattr(lf, "MIN_VOLUME", ...) で解決していた挙動に合わせる)
    if min_volume is None:
        min_volume = int(MIN_VOLUME)
    if min_turnover is None:
        min_turnover = int(MIN_TURNOVER)
    if min_price is None:
        min_price = float(MIN_PRICE)
    if max_price is None:
        max_price = float(MAX_PRICE)
    if require_price is None:
        require_price = bool(REQUIRE_PRICE_COLUMN)

    before = 0 if df is None else len(df)
    out = apply_liquidity_filter_core(
        df,
        min_volume=min_volume,
        min_turnover=min_turnover,
        min_price=min_price,
        max_price=max_price,
        require_price=require_price,
        context=context,
    )
    if not _liq_empty_fallback_enabled():
        return out
    if out is not None and not out.empty:
        return out
    if df is None or getattr(df, "empty", True):
        return pd.DataFrame()

    for label, turnover, volume in (
        ("turnover_1m", 1_000_000, min_volume),
        ("price_volume_only", 0, min_volume),
        ("price_only", 0, 0),
    ):
        try:
            cand = apply_liquidity_filter_core(
                df,
                min_volume=volume,
                min_turnover=turnover,
                min_price=min_price,
                max_price=max_price,
                require_price=require_price,
                context=f"{context}|fallback:{label}" if context else f"fallback:{label}",
            )
        except Exception:
            logger.debug("[LIQ EMPTY FALLBACK] ranking relax failed step=%s", label, exc_info=True)
            continue
        if cand is not None and not cand.empty:
            logger.warning(
                "[LIQ EMPTY FALLBACK] ranking liquidity relaxed step=%s context=%s before=%d after=%d original_min_turnover=%s min_price=%.1f min_volume=%s",
                label, context, before, len(cand), min_turnover, float(min_price), volume,
            )
            return cand

    logger.warning("[LIQ EMPTY FALLBACK] ranking liquidity still empty context=%s before=%d", context, before)
    return out


# ============================================================
# summary aliases / compatibility
# ============================================================

def apply_summary_liquidity_filter(
    df: pd.DataFrame,
    *,
    context: str = "",
    min_price: Optional[float] = None,
    min_volume: Optional[float] = None,
    min_trading_value: Optional[float] = None,
    require_price: bool = True,
) -> pd.DataFrame:
    return apply_liquidity_filter(
        df,
        min_price=MIN_PRICE if min_price is None else float(min_price),
        min_volume=MIN_VOLUME if min_volume is None else int(min_volume),
        min_turnover=MIN_TURNOVER if min_trading_value is None else int(min_trading_value),
        require_price=require_price,
        context=context,
    )


def filter_summary_before_display(df: pd.DataFrame, *, context: str = "summary_display") -> pd.DataFrame:
    return apply_summary_liquidity_filter(df, context=context, min_price=MIN_PRICE, require_price=True)


def filter_summary_before_entry(df: pd.DataFrame, *, context: str = "summary_entry") -> pd.DataFrame:
    return apply_summary_liquidity_filter(df, context=context, min_price=MIN_PRICE, require_price=True)


def filter_summary_before_persist(df: pd.DataFrame, *, context: str = "summary_persist") -> pd.DataFrame:
    return apply_summary_liquidity_filter(df, context=context, min_price=MIN_PRICE, require_price=True)


def filter_low_price_symbols(df: pd.DataFrame, *, context: str = "") -> pd.DataFrame:
    return apply_summary_liquidity_filter(df, context=context or "low_price", min_price=MIN_PRICE, require_price=True)


__all__ = [
    "MIN_VOLUME",
    "MIN_TURNOVER",
    "MIN_PRICE",
    "MAX_PRICE",
    "ENABLE_LIQUIDITY_FILTER",
    "apply_liquidity_filter",
    "apply_summary_liquidity_filter",
    "filter_summary_before_display",
    "filter_summary_before_entry",
    "filter_summary_before_persist",
    "filter_low_price_symbols",
]
