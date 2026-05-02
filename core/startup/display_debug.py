# ============================================================
# File   : core/startup/display_debug.py
# Version: REV1.0-STARTUP-DISPLAY-DEBUG
# ------------------------------------------------------------
# ✔ startup display debug / closed-day profile を分離
# ✔ symbolname / name を統一
# ✔ datetime を表示から除外
# ✔ 価格は小数1位、指標は小数2位
# ============================================================

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def log_closed_day_summary_state(label: str, df) -> None:
    try:
        if df is None:
            logger.warning("[CLOSED DAY] %s: df is None", label)
            return

        rows = len(df)
        cols = len(df.columns) if hasattr(df, "columns") else 0

        latest_dt = None
        if hasattr(df, "columns") and "datetime" in df.columns and not df.empty:
            try:
                import pandas as pd
                s = pd.to_datetime(df["datetime"], errors="coerce").dropna()
                if not s.empty:
                    latest_dt = s.max()
            except Exception:
                pass

        logger.info(
            "[CLOSED DAY] %s rows=%s cols=%s latest_dt=%s",
            label,
            rows,
            cols,
            latest_dt,
        )

        if hasattr(df, "columns") and not df.empty:
            for c in (
                "score",
                "slope",
                "mtf",
                "score_buy",
                "score_sell",
                "score_slope",
                "score_mtf",
                "slope_atr_scaled",
                "mtf_score",
                "final_score",
                "display_score",
                "rsi",
                "macd",
                "signal",
            ):
                if c in df.columns:
                    try:
                        import pandas as pd
                        s = pd.to_numeric(df[c], errors="coerce").fillna(0)
                        logger.info(
                            "[CLOSED DAY] %s %s nonzero=%s",
                            label,
                            c,
                            int((s != 0).sum()),
                        )
                    except Exception:
                        logger.exception("[CLOSED DAY] failed nonzero count: %s %s", label, c)

    except Exception:
        logger.exception("[CLOSED DAY] state log failed: %s", label)


def log_display_input(tf: int, df) -> None:
    try:
        if df is None:
            logger.warning("[STARTUP DISPLAY INPUT tf=%s] df is None", tf)
            return

        if not hasattr(df, "empty"):
            logger.warning("[STARTUP DISPLAY INPUT tf=%s] not a dataframe", tf)
            return

        if df.empty:
            logger.warning("[STARTUP DISPLAY INPUT tf=%s] df empty", tf)
            return

        import pandas as pd

        x = df.copy()

        if "symbolname" not in x.columns:
            x["symbolname"] = None

        if "name" in x.columns:
            sym_s = x["symbolname"].astype("string").fillna("").str.strip()
            name_s = x["name"].astype("string").fillna("").str.strip()
            x["symbolname"] = sym_s.where(sym_s != "", name_s)

        drop_cols = [c for c in ("name", "datetime") if c in x.columns]
        if drop_cols:
            x = x.drop(columns=drop_cols, errors="ignore")

        sample_cols = [
            c for c in [
                "symbol",
                "symbolname",
                "score",
                "score_total",
                "final_score",
                "display_score",
                "score_buy",
                "score_sell",
                "slope",
                "slope_atr_scaled",
                "score_slope",
                "mtf",
                "mtf_alignment",
                "score_mtf",
                "mtf_score",
                "open",
                "high",
                "low",
                "close",
                "rsi",
                "macd",
                "signal",
            ]
            if c in x.columns
        ]

        logger.info(
            "[STARTUP DISPLAY INPUT tf=%s] rows=%s cols=%s sample_cols=%s",
            tf,
            len(x),
            len(x.columns),
            sample_cols,
        )

        price_cols = [c for c in ("open", "high", "low", "close") if c in x.columns]
        metric_cols = [
            c for c in (
                "score", "score_total", "final_score", "display_score",
                "score_buy", "score_sell",
                "slope", "slope_atr_scaled", "score_slope",
                "mtf", "mtf_alignment", "score_mtf", "mtf_score",
                "rsi", "macd", "signal",
            )
            if c in x.columns
        ]

        for c in price_cols:
            x[c] = pd.to_numeric(x[c], errors="coerce").round(1)

        for c in metric_cols:
            x[c] = pd.to_numeric(x[c], errors="coerce").round(2)

        try:
            logger.info(
                "[STARTUP DISPLAY INPUT tf=%s]\n%s",
                tf,
                x[sample_cols].head(20).to_string(index=False),
            )
        except Exception:
            logger.exception("[STARTUP DISPLAY INPUT tf=%s] sample render failed", tf)

        try:
            def _nz(col: str) -> int:
                if col not in df.columns:
                    return -1
                return int(pd.to_numeric(df[col], errors="coerce").fillna(0).ne(0).sum())

            logger.info(
                "[STARTUP DISPLAY INPUT tf=%s] nonzero "
                "score=%s slope=%s slope_atr_scaled=%s score_slope=%s "
                "mtf=%s mtf_alignment=%s score_mtf=%s mtf_score=%s "
                "rsi=%s macd=%s signal=%s",
                tf,
                _nz("score"),
                _nz("slope"),
                _nz("slope_atr_scaled"),
                _nz("score_slope"),
                _nz("mtf"),
                _nz("mtf_alignment"),
                _nz("score_mtf"),
                _nz("mtf_score"),
                _nz("rsi"),
                _nz("macd"),
                _nz("signal"),
            )
        except Exception:
            logger.exception("[STARTUP DISPLAY INPUT tf=%s] nonzero profile failed", tf)

    except Exception:
        logger.exception("[STARTUP DISPLAY INPUT tf=%s] fatal", tf)