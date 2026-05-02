# ============================================================
# File   : trading/summary/recovery/bootstrap_logging.py
# Ver    : PRODUCTION-STABLE-REV1.0-BOOTSTRAP-LOGGING
# ------------------------------------------------------------
# ✔ source dataframe の日別内訳ログ
# ✔ warmup 充足状況ログ
# ✔ split loaders / compat 両対応
# ============================================================

from __future__ import annotations

import logging
import pandas as pd

logger = logging.getLogger(__name__)

try:
    from trading.summary.recovery.loaders_common import log_df_date_breakdown
except Exception:
    try:
        from trading.summary.recovery.loaders import log_df_date_breakdown  # type: ignore
    except Exception:
        def log_df_date_breakdown(*args, **kwargs):
            return None


def log_source_date_breakdown(
    df: pd.DataFrame,
    *,
    label: str,
    target_dates_ctx=None,
    anchor_day=None,
    required_bars_per_symbol: int | None = None,
) -> None:
    """
    source dataframe の日別内訳をログ出力する。
    前日分が本当に入っているか、warmup が足りているか確認するための詳細ログ。
    """
    try:
        log_df_date_breakdown(df, label=label, datetime_col="datetime")
    except Exception:
        pass

    try:
        if df is None or df.empty:
            logger.info(
                "[summary_recovery] %s breakdown empty target_dates=%s anchor_day=%s required_bars_per_symbol=%s",
                label,
                [] if not target_dates_ctx else [str(x) for x in target_dates_ctx],
                anchor_day,
                required_bars_per_symbol,
            )
            return

        if "datetime" not in df.columns:
            logger.info(
                "[summary_recovery] %s breakdown skipped reason=no_datetime rows=%s cols=%s",
                label,
                len(df),
                list(df.columns),
            )
            return

        x = df.copy()
        x["datetime"] = pd.to_datetime(x["datetime"], errors="coerce")
        x = x[x["datetime"].notna()].copy()

        if x.empty:
            logger.info(
                "[summary_recovery] %s breakdown skipped reason=no_valid_datetime rows=%s",
                label,
                len(df),
            )
            return

        x["date_only"] = x["datetime"].dt.strftime("%Y-%m-%d")

        summary = (
            x.groupby("date_only")
            .agg(
                rows=("date_only", "size"),
                symbols=("symbol", "nunique") if "symbol" in x.columns else ("date_only", "size"),
                dt_min=("datetime", "min"),
                dt_max=("datetime", "max"),
            )
            .reset_index()
            .sort_values("date_only")
        )

        target_dates_str = []
        if target_dates_ctx:
            for d in target_dates_ctx:
                try:
                    target_dates_str.append(pd.to_datetime(d).strftime("%Y-%m-%d"))
                except Exception:
                    continue
        target_dates_str = sorted(set(target_dates_str))

        logger.info(
            "[summary_recovery] %s breakdown target_dates=%s anchor_day=%s required_bars_per_symbol=%s total_rows=%s total_symbols=%s",
            label,
            target_dates_str,
            anchor_day,
            required_bars_per_symbol,
            len(x),
            x["symbol"].nunique() if "symbol" in x.columns else 0,
        )

        if "symbol" in x.columns:
            counts = x.groupby("symbol")["datetime"].count()
            logger.info(
                "[summary_recovery] %s bars_per_symbol min=%s median=%s max=%s symbols=%s",
                label,
                int(counts.min()) if len(counts) else 0,
                int(counts.median()) if len(counts) else 0,
                int(counts.max()) if len(counts) else 0,
                int(counts.shape[0]) if len(counts) else 0,
            )
            if required_bars_per_symbol is not None and len(counts) > 0:
                short = int((counts < int(required_bars_per_symbol)).sum())
                logger.info(
                    "[summary_recovery] %s warmup_check required_bars_per_symbol=%s short_symbols=%s ok_symbols=%s",
                    label,
                    int(required_bars_per_symbol),
                    short,
                    int((counts >= int(required_bars_per_symbol)).sum()),
                )

        for _, row in summary.iterrows():
            logger.info(
                "[summary_recovery] %s by_date=%s rows=%s symbols=%s dt_min=%s dt_max=%s",
                label,
                row["date_only"],
                int(row["rows"]),
                int(row["symbols"]) if pd.notna(row["symbols"]) else 0,
                row["dt_min"],
                row["dt_max"],
            )

    except Exception:
        logger.exception("[summary_recovery] source breakdown log failed label=%s", label)