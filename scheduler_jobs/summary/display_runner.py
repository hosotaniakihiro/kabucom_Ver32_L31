# ============================================================
# File   : scheduler_jobs/summary/display_runner.py
# Version: V4.5-TOP10-SCORE-ONLY-DISPLAY-GUARD
# ------------------------------------------------------------
# ✔ 1分足もDiscord通知できるように変更
# ✔ SUMMARY_NOTIFY_1MIN_DISCORD=1 を既定ON
# ✔ PUSH / RANKING 両方に適用
# ✔ close <= 200 除外
# ✔ 表示TOP10では slope 条件を既定OFF
# ✔ エントリー判定は変更しない。表示専用の緩和。
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Any, Optional

import numpy as np
import pandas as pd

from .dependencies import resolve_display_functions
from .display_prepare import prepare_display_df

logger = logging.getLogger(__name__)


DEFAULT_DISPLAY_MIN_PRICE = 200.0
DEFAULT_DISPLAY_MIN_BUY_SLOPE = 0.03
DEFAULT_DISPLAY_MAX_SELL_SLOPE = -0.03


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _env_bool(name: str, default: bool) -> bool:
    try:
        raw = str(os.getenv(name, "")).strip().lower()
        if raw in {"1", "true", "yes", "on", "enable", "enabled"}:
            return True
        if raw in {"0", "false", "no", "off", "disable", "disabled"}:
            return False
    except Exception:
        pass
    return bool(default)


def _resolve_display_min_price() -> float:
    v1 = os.getenv("SUMMARY_DISPLAY_MIN_PRICE")
    if v1 is not None and str(v1).strip() != "":
        return _env_float("SUMMARY_DISPLAY_MIN_PRICE", DEFAULT_DISPLAY_MIN_PRICE)

    v2 = os.getenv("TRADE_UNIVERSE_MIN_PRICE")
    if v2 is not None and str(v2).strip() != "":
        return _env_float("TRADE_UNIVERSE_MIN_PRICE", DEFAULT_DISPLAY_MIN_PRICE)

    return float(DEFAULT_DISPLAY_MIN_PRICE)


def _resolve_display_min_buy_slope() -> float:
    v1 = os.getenv("SUMMARY_DISPLAY_MIN_BUY_SLOPE")
    if v1 is not None and str(v1).strip() != "":
        return _env_float("SUMMARY_DISPLAY_MIN_BUY_SLOPE", DEFAULT_DISPLAY_MIN_BUY_SLOPE)

    v2 = os.getenv("ENTRY_MIN_BUY_SLOPE")
    if v2 is not None and str(v2).strip() != "":
        return _env_float("ENTRY_MIN_BUY_SLOPE", DEFAULT_DISPLAY_MIN_BUY_SLOPE)

    return float(DEFAULT_DISPLAY_MIN_BUY_SLOPE)


def _resolve_display_max_sell_slope() -> float:
    v1 = os.getenv("SUMMARY_DISPLAY_MAX_SELL_SLOPE")
    if v1 is not None and str(v1).strip() != "":
        return _env_float("SUMMARY_DISPLAY_MAX_SELL_SLOPE", DEFAULT_DISPLAY_MAX_SELL_SLOPE)

    v2 = os.getenv("ENTRY_MAX_SELL_SLOPE")
    if v2 is not None and str(v2).strip() != "":
        return _env_float("ENTRY_MAX_SELL_SLOPE", DEFAULT_DISPLAY_MAX_SELL_SLOPE)

    return float(DEFAULT_DISPLAY_MAX_SELL_SLOPE)


def _top10_slope_filter_enabled() -> bool:
    # 表示TOP10では既定OFF。旧挙動へ戻したい場合だけ 1 にする。
    return _env_bool("SUMMARY_DISPLAY_TOP10_REQUIRE_SLOPE", False)


def _select_first_col(df: pd.DataFrame, candidates: tuple[str, ...]) -> Optional[str]:
    if df is None or df.empty:
        return None
    for c in candidates:
        if c in df.columns:
            return c
    return None


def _select_price_col(df: pd.DataFrame) -> Optional[str]:
    return _select_first_col(
        df,
        (
            "disp_close",
            "close",
            "close_price",
            "current_price",
            "price",
            "last_price",
        ),
    )


def _select_slope_col(df: pd.DataFrame) -> Optional[str]:
    return _select_first_col(
        df,
        (
            "disp_slope",
            "slope",
            "score_slope",
            "slope_atr_scaled",
            "ma75_slope",
        ),
    )


def _select_buy_score_col(df: pd.DataFrame) -> Optional[str]:
    return _select_first_col(df, ("disp_buy_score", "score_buy", "buy_score", "buy", "score"))


def _select_sell_score_col(df: pd.DataFrame) -> Optional[str]:
    return _select_first_col(df, ("disp_sell_score", "score_sell", "sell_score", "sell"))


def _to_num_series(df: pd.DataFrame, col: Optional[str], default: float = 0.0) -> pd.Series:
    if col is None or col not in df.columns:
        return pd.Series(default, index=df.index, dtype="float64")
    return pd.to_numeric(df[col], errors="coerce").fillna(default)


def _apply_display_universe_guard(
    df: pd.DataFrame,
    *,
    interval: int,
    source: str,
) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    out = df.copy()

    price_col = _select_price_col(out)
    slope_col = _select_slope_col(out)
    buy_col = _select_buy_score_col(out)
    sell_col = _select_sell_score_col(out)

    min_price = _resolve_display_min_price()
    min_buy_slope = _resolve_display_min_buy_slope()
    max_sell_slope = _resolve_display_max_sell_slope()
    require_slope = _top10_slope_filter_enabled()

    before = len(out)

    if price_col is None:
        logger.warning(
            "[DISPLAY UNIVERSE GUARD] price column missing source=%s interval=%s rows=%s cols=%s",
            source,
            interval,
            before,
            list(out.columns),
        )

    if slope_col is None:
        logger.warning(
            "[DISPLAY UNIVERSE GUARD] slope column missing source=%s interval=%s rows=%s cols=%s",
            source,
            interval,
            before,
            list(out.columns),
        )

    price_s = _to_num_series(out, price_col, 0.0)
    slope_s = _to_num_series(out, slope_col, 0.0)
    buy_s = _to_num_series(out, buy_col, 0.0)
    sell_s = _to_num_series(out, sell_col, 0.0).abs()

    price_ok = price_s > float(min_price)
    buy_ok = price_ok & (buy_s > 0.0)
    sell_ok = price_ok & (sell_s > 0.0)
    if require_slope:
        buy_ok &= slope_s > float(min_buy_slope)
        sell_ok &= slope_s < float(max_sell_slope)
    keep_mask = buy_ok | sell_ok

    filtered = out.loc[keep_mask].copy()
    after = len(filtered)

    try:
        skipped_head = []
        if "symbol" in out.columns:
            skipped = out.loc[~keep_mask].copy()
            cols = ["symbol"]
            for c in (price_col, slope_col, buy_col, sell_col):
                if c and c not in cols:
                    cols.append(c)
            skipped_head = skipped[cols].head(20).to_dict(orient="records")
    except Exception:
        skipped_head = []

    logger.info(
        "[DISPLAY UNIVERSE GUARD] source=%s interval=%s price_col=%s slope_col=%s buy_col=%s sell_col=%s "
        "condition='price > %.1f and ((buy > 0%s) or (sell > 0%s))' "
        "before=%s after=%s skipped=%s slope_required=%s skipped_head=%s",
        source,
        interval,
        price_col,
        slope_col,
        buy_col,
        sell_col,
        float(min_price),
        f" and slope > {float(min_buy_slope):.4f}" if require_slope else "",
        f" and slope < {float(max_sell_slope):.4f}" if require_slope else "",
        before,
        after,
        before - after,
        require_slope,
        skipped_head,
    )

    return filtered.reset_index(drop=True)


def _fallback_when_guard_empty(df: pd.DataFrame, *, interval: int, source: str) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    if not _env_bool("SUMMARY_DISPLAY_FALLBACK_WHEN_GUARD_EMPTY", True):
        return pd.DataFrame()

    out = df.copy()
    price_col = _select_price_col(out)
    score_col = _select_first_col(
        out,
        (
            "disp_score",
            "display_score",
            "final_score",
            "score",
            "score_total",
            "score_buy",
            "score_sell",
        ),
    )
    buy_col = _select_buy_score_col(out)
    sell_col = _select_sell_score_col(out)
    slope_col = _select_slope_col(out)

    if price_col is not None:
        price_s = _to_num_series(out, price_col, 0.0)
        out = out.loc[price_s > _resolve_display_min_price()].copy()

    if out.empty:
        logger.warning(
            "[DISPLAY FALLBACK] skipped source=%s interval=%s reason=no_rows_after_price_filter",
            source,
            interval,
        )
        return pd.DataFrame()

    if score_col is not None:
        score_s = _to_num_series(out, score_col, 0.0)
    else:
        score_s = pd.Series(0.0, index=out.index)

    if buy_col is None or buy_col not in out.columns:
        out["score_buy"] = np.maximum(score_s, 0.0)
    if sell_col is None or sell_col not in out.columns:
        out["score_sell"] = np.maximum(-score_s, 0.0)

    sort_col = score_col if score_col is not None and score_col in out.columns else None
    if sort_col:
        out["_display_abs_score"] = _to_num_series(out, sort_col, 0.0).abs()
        if slope_col and slope_col in out.columns:
            out["_display_abs_slope"] = _to_num_series(out, slope_col, 0.0).abs()
        else:
            out["_display_abs_slope"] = 0.0
        out = out.sort_values(["_display_abs_score", "_display_abs_slope"], ascending=[False, False]).drop(
            columns=["_display_abs_score", "_display_abs_slope"],
            errors="ignore",
        )

    limit = int(_env_float("SUMMARY_DISPLAY_FALLBACK_ROWS", 20.0))
    out = out.head(max(limit, 1)).copy()
    out["display_fallback_reason"] = "guard_empty_fallback"

    logger.warning(
        "[DISPLAY FALLBACK] source=%s interval=%s guard_empty -> fallback rows=%s price_col=%s score_col=%s slope_col=%s",
        source,
        interval,
        len(out),
        price_col,
        score_col,
        slope_col,
    )
    return out.reset_index(drop=True)


def _should_notify_discord(interval: int) -> bool:
    try:
        iv = int(interval)
    except Exception:
        return True
    if iv == 1:
        return _env_bool("SUMMARY_NOTIFY_1MIN_DISCORD", True)
    return True


def display_push_summary(
    df: pd.DataFrame,
    interval: int,
    *,
    now: Optional[dt.datetime] = None,
) -> None:
    try:
        display_push, _ = resolve_display_functions()

        if not callable(display_push):
            logger.warning("[summary.display_runner] display_push callable missing interval=%s", interval)
            return

        if not isinstance(df, pd.DataFrame) or df.empty:
            logger.info("[summary.display_runner] PUSH display skipped interval=%s reason=empty_input", interval)
            return

        df_prepared = prepare_display_df(df, interval=interval, now=now)
        df_disp = df_prepared if isinstance(df_prepared, pd.DataFrame) and not df_prepared.empty else df

        if df_disp.empty:
            logger.info("[summary.display_runner] PUSH display skipped interval=%s reason=empty_prepared", interval)
            return

        guarded = _apply_display_universe_guard(df_disp, interval=interval, source="PUSH")
        if guarded.empty:
            fallback = _fallback_when_guard_empty(df_disp, interval=interval, source="PUSH")
            if fallback.empty:
                logger.info(
                    "[summary.display_runner] PUSH display skipped after universe guard interval=%s fallback_empty=True",
                    interval,
                )
                return
            df_disp = fallback
        else:
            df_disp = guarded

        notify_discord = _should_notify_discord(interval)
        if not notify_discord:
            logger.info("[DISCORD] skip %smin PUSH summary by SUMMARY_NOTIFY_1MIN_DISCORD", interval)

        try:
            display_push(
                summary_df=df_disp,
                interval=interval,
                interval_label=f"{interval}min",
                now=now,
                notify_discord=notify_discord,
            )
        except TypeError:
            display_push(
                summary_df=df_disp,
                interval=interval,
                interval_label=f"{interval}min",
                now=now,
            )

        logger.info(
            "[summary.display_runner] PUSH display called interval=%s rows=%s notify_discord=%s",
            interval,
            len(df_disp),
            notify_discord,
        )

    except Exception:
        logger.exception("[display_runner] push display failed")


def display_ranking_summary(
    df: pd.DataFrame,
    interval: int,
    *,
    now: Optional[dt.datetime] = None,
) -> None:
    try:
        _, display_ranking = resolve_display_functions()

        if not callable(display_ranking):
            logger.warning("[summary.display_runner] display_ranking callable missing interval=%s", interval)
            return

        if not isinstance(df, pd.DataFrame) or df.empty:
            logger.info("[summary.display_runner] RANKING display skipped interval=%s reason=empty_input", interval)
            return

        df_prepared = prepare_display_df(df, interval=interval, now=now)
        df_disp = df_prepared if isinstance(df_prepared, pd.DataFrame) and not df_prepared.empty else df

        if df_disp.empty:
            logger.info("[summary.display_runner] RANKING display skipped interval=%s reason=empty_prepared", interval)
            return

        guarded = _apply_display_universe_guard(df_disp, interval=interval, source="RANKING")
        if guarded.empty:
            fallback = _fallback_when_guard_empty(df_disp, interval=interval, source="RANKING")
            if fallback.empty:
                logger.info(
                    "[summary.display_runner] RANKING display skipped after universe guard interval=%s fallback_empty=True",
                    interval,
                )
                return
            df_disp = fallback
        else:
            df_disp = guarded

        notify_discord = _should_notify_discord(interval)
        if not notify_discord:
            logger.info("[DISCORD] skip %smin RANKING summary by SUMMARY_NOTIFY_1MIN_DISCORD", interval)

        try:
            display_ranking(
                summary_df=df_disp,
                interval=interval,
                interval_label=f"{interval}min",
                now=now,
                notify_discord=notify_discord,
            )
        except TypeError:
            display_ranking(
                summary_df=df_disp,
                interval=interval,
                interval_label=f"{interval}min",
                now=now,
            )

        logger.info(
            "[summary.display_runner] RANKING display called interval=%s rows=%s notify_discord=%s",
            interval,
            len(df_disp),
            notify_discord,
        )

    except Exception:
        logger.exception("[display_runner] ranking display failed")


def display_closed_day_summary(
    df: pd.DataFrame,
    interval: int,
    *,
    now: Optional[dt.datetime] = None,
) -> None:
    display_push_summary(df=df, interval=interval, now=now)


def run_display_push_summary(
    df: pd.DataFrame,
    interval: int,
    *,
    now: Optional[dt.datetime] = None,
) -> None:
    display_push_summary(df=df, interval=interval, now=now)


def run_display_ranking_summary(
    df: pd.DataFrame,
    interval: int,
    *,
    now: Optional[dt.datetime] = None,
) -> None:
    display_ranking_summary(df=df, interval=interval, now=now)


__all__ = [
    "display_push_summary",
    "display_ranking_summary",
    "display_closed_day_summary",
    "run_display_push_summary",
    "run_display_ranking_summary",
]
