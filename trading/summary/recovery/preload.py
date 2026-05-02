# ============================================================
# File   : trading/summary/recovery/preload.py
# Ver    : PRODUCTION-STABLE-REV1.4-PRELOAD-RAW-UPSERT-SEPARATION
#          -RESTORE-TARGET-RANKING-SYMBOLS
# ------------------------------------------------------------
# ✔ recent history preload
# ✔ persisted summary cache restore
# ✔ 1m -> 3m/5m resample helper
# ✔ session clamp helper
# ✔ restore対象は 当日global ranking + 前日ranking DB
# ✔ raw / upsert 用 DF を分離
# ✔ preload系でも finalize_for_upsert の責務を末端へ限定
# ============================================================

from __future__ import annotations

import logging
import pandas as pd

from trading.summary.recovery.helpers import (
    normalize_datetime_columns,
)
from trading.summary.recovery.loaders import (
    load_latest_summary_snapshot,
    load_summary_df_between,
    load_recent_summary_tail_per_symbol,
    load_restore_target_symbols,
)
from trading.summary.recovery.persistence import (
    finalize_for_upsert,
    update_global_cache,
)
from trading.summary.recovery.rebuilders import (
    rebuild_higher_tf_from_1m,
)

from .checkpoints import normalize_dt_like

logger = logging.getLogger(__name__)

DELTA_SOURCE_SESSION_FLOOR_HOUR = 8


def history_window_by_interval(interval: int, bars: int) -> pd.Timedelta:
    interval = int(interval)
    bars = max(int(bars), 1)
    total_min = interval * bars
    return pd.Timedelta(minutes=total_min)


def load_recent_history_for_cache(
    interval: int,
    last_dt,
    *,
    min_bars: int,
    target_dates_ctx=None,
    anchor_day=None,
    max_allowed_dt=None,
    fallback_snapshot: bool = True,
) -> pd.DataFrame:
    """
    delta_push empty 時でも indicator 計算に必要な直近履歴を preload する。
    rebuild 用 source history 読込で使う。

    重要:
      - ここでは raw history を返す
      - finalize_for_upsert は呼ばない
    """
    try:
        base_dt = normalize_dt_like(last_dt)
        if base_dt is None:
            base_dt = normalize_dt_like(pd.Timestamp.now())

        if max_allowed_dt is not None:
            max_allowed_dt = normalize_dt_like(max_allowed_dt)
            if base_dt is not None and max_allowed_dt is not None and base_dt > max_allowed_dt:
                logger.info(
                    "[summary_recovery] preload base_dt clamped interval=%s base_dt=%s -> max_allowed_dt=%s",
                    interval,
                    base_dt,
                    max_allowed_dt,
                )
                base_dt = max_allowed_dt

        if base_dt is None:
            logger.warning(
                "[summary_recovery] recent history preload skipped: interval=%s base_dt unresolved",
                interval,
            )
            return pd.DataFrame()

        window = history_window_by_interval(interval, min_bars)
        start_dt = base_dt - window
        end_dt = base_dt + pd.Timedelta(minutes=max(int(interval), 1))

        hist_raw = load_summary_df_between(
            int(interval),
            start_dt,
            end_dt,
            target_dates=target_dates_ctx,
            anchor_day=anchor_day,
            max_allowed_dt=max_allowed_dt,
        )
        hist_raw = normalize_datetime_columns(hist_raw, interval=int(interval))

        if hist_raw.empty and fallback_snapshot:
            snap_raw = load_latest_summary_snapshot(int(interval))
            snap_raw = normalize_datetime_columns(snap_raw, interval=int(interval))
            logger.info(
                "[summary_recovery] recent history fallback snapshot interval=%s rows=%d",
                interval,
                len(snap_raw),
            )
            return snap_raw

        logger.info(
            "[summary_recovery] recent history loaded interval=%s raw_rows=%d start=%s end=%s anchor_day=%s max_allowed_dt=%s",
            interval,
            len(hist_raw),
            start_dt,
            end_dt,
            anchor_day,
            max_allowed_dt,
        )
        return hist_raw

    except Exception:
        logger.exception(
            "[summary_recovery] recent history preload failed interval=%s last_dt=%s",
            interval,
            last_dt,
        )
        return pd.DataFrame()


def restore_recent_persisted_summary_to_cache(
    interval: int,
    last_dt,
    *,
    min_bars: int,
    target_dates_ctx=None,
    anchor_day=None,
    max_allowed_dt=None,
) -> pd.DataFrame:
    """
    既存 summary DB から recent history を読み、cache へ戻す。
    skip rebuild 時はこちらを使う。

    重要:
      - restore 対象は全銘柄ではなく
        当日global ranking + 前日ranking DB の和集合
      - その対象 symbols に対してだけ tail restore する
      - cache 更新には raw を渡す
      - return は upsert 用整形済み DF
    """
    try:
        end_dt = normalize_dt_like(last_dt)
        if end_dt is None:
            end_dt = normalize_dt_like(max_allowed_dt)
        if end_dt is None:
            end_dt = normalize_dt_like(pd.Timestamp.now())

        restore_symbols = load_restore_target_symbols(
            target_dates=target_dates_ctx,
            include_previous_day_from_db=True,
        )

        hist_raw = load_recent_summary_tail_per_symbol(
            interval=int(interval),
            bars_per_symbol=max(int(min_bars), 1),
            end_dt=end_dt,
            target_dates=target_dates_ctx,
            anchor_day=anchor_day,
            max_allowed_dt=max_allowed_dt,
            symbols=restore_symbols,
        )

        hist_raw = normalize_datetime_columns(hist_raw, interval=interval)
        hist_upsert = finalize_for_upsert(hist_raw, interval)

        if hist_upsert.empty:
            snap_raw = load_latest_summary_snapshot(int(interval))
            snap_raw = normalize_datetime_columns(snap_raw, interval=int(interval))
            snap_upsert = finalize_for_upsert(snap_raw, int(interval))

            if not snap_upsert.empty:
                update_global_cache(snap_raw, int(interval))
                logger.info(
                    "[summary_recovery] restored persisted cache by snapshot fallback interval=%s rows=%d symbols=%d",
                    interval,
                    len(snap_upsert),
                    int(snap_upsert["symbol"].nunique()) if "symbol" in snap_upsert.columns and not snap_upsert.empty else 0,
                )
                return snap_upsert

            logger.info(
                "[summary_recovery] restored persisted cache interval=%s rows=0 symbols=0 restore_symbols=%d",
                interval,
                len(restore_symbols),
            )
            return pd.DataFrame()

        if not hist_raw.empty:
            update_global_cache(hist_raw, int(interval))

        logger.info(
            "[summary_recovery] restored persisted cache interval=%s raw_rows=%d upsert_rows=%d symbols=%d restore_symbols=%d",
            interval,
            len(hist_raw),
            len(hist_upsert),
            int(hist_upsert["symbol"].nunique()) if "symbol" in hist_upsert.columns and not hist_upsert.empty else 0,
            len(restore_symbols),
        )
        return hist_upsert

    except Exception:
        logger.exception("[summary_recovery] restore persisted cache failed interval=%s", interval)
        return pd.DataFrame()


def resample_htf_from_1m(preload_1m: pd.DataFrame, interval: int) -> pd.DataFrame:
    """
    1分足履歴から HTF を再構成する。

    重要:
      - 入力は raw 1m history を想定
      - resample 後にだけ finalize_for_upsert を適用
    """
    try:
        interval = int(interval)
        if interval not in (3, 5):
            return pd.DataFrame()

        base_raw = normalize_datetime_columns(preload_1m, interval=1)

        if base_raw.empty:
            logger.info(
                "[summary_recovery] resample htf skipped interval=%s reason=preload_1m empty",
                interval,
            )
            return pd.DataFrame()

        htf_raw = rebuild_higher_tf_from_1m(base_raw, interval)
        htf_raw = normalize_datetime_columns(htf_raw, interval=interval)
        htf_upsert = finalize_for_upsert(htf_raw, interval)

        logger.info(
            "[summary_recovery] resample htf from 1m done interval=%s raw_rows=%d upsert_rows=%d symbols=%d latest_dt=%s",
            interval,
            len(htf_raw),
            len(htf_upsert),
            int(htf_upsert["symbol"].nunique()) if "symbol" in htf_upsert.columns and not htf_upsert.empty else 0,
            htf_upsert["datetime"].max() if "datetime" in htf_upsert.columns and not htf_upsert.empty else None,
        )
        return htf_upsert
    except Exception:
        logger.exception("[summary_recovery] resample htf from 1m failed interval=%s", interval)
        return pd.DataFrame()


def trim_htf_cache_bars(df: pd.DataFrame, interval: int, keep_bars: int) -> pd.DataFrame:
    out = normalize_datetime_columns(df, interval=interval)
    if out.empty:
        return out

    if "symbol" not in out.columns or "datetime" not in out.columns:
        return out

    try:
        keep_bars = max(int(keep_bars), 1)
        out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
        out = (
            out.sort_values(["symbol", "datetime"])
            .groupby("symbol", group_keys=False)
            .tail(keep_bars)
            .reset_index(drop=True)
        )
        logger.info(
            "[summary_recovery] trim htf cache bars interval=%s keep_bars=%s rows=%d symbols=%d",
            interval,
            keep_bars,
            len(out),
            int(out["symbol"].nunique()) if "symbol" in out.columns and not out.empty else 0,
        )
        return out
    except Exception:
        logger.exception(
            "[summary_recovery] trim htf cache bars failed interval=%s keep_bars=%s",
            interval,
            keep_bars,
        )
        return out


def clamp_start_dt_to_recent_session(start_dt, now_dt):
    """
    higher TF 用 source window の開始を当日セッション寄りに制限する。
    """
    try:
        start_dt = normalize_dt_like(start_dt)
        now_dt = normalize_dt_like(now_dt)

        if start_dt is None:
            return start_dt
        if now_dt is None:
            return start_dt

        session_floor = pd.Timestamp(now_dt.date()).replace(
            hour=DELTA_SOURCE_SESSION_FLOOR_HOUR,
            minute=0,
            second=0,
            microsecond=0,
        )

        clamped = max(start_dt, session_floor)
        if clamped != start_dt:
            logger.info(
                "[summary_recovery] source window clamped start_dt=%s -> %s now_dt=%s",
                start_dt,
                clamped,
                now_dt,
            )
        return clamped

    except Exception:
        logger.exception(
            "[summary_recovery] clamp start dt failed start_dt=%s now_dt=%s",
            start_dt,
            now_dt,
        )
        return start_dt
