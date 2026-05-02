# ============================================================
# File   : trading/summary/postprocess/calc.py
# Version: Ver1.0-PRODUCTION-POSTPROCESS-CALC
# ------------------------------------------------------------
# ✔ interval 推定
# ✔ actual calc 要否判定
# ✔ indicator + scoring 再計算
# ✔ before/after profile log
# ============================================================

from __future__ import annotations

import logging
import pandas as pd

from .filtering import drop_outside_allowed_dates

logger = logging.getLogger(__name__)


def infer_interval_label(df: pd.DataFrame) -> str:
    try:
        if "interval" in df.columns:
            s = pd.to_numeric(df["interval"], errors="coerce").dropna()
            if not s.empty:
                iv = int(s.iloc[0])
                if iv in (1, 3, 5, 10, 15, 30, 60):
                    return f"{iv}min"
        if "time_range" in df.columns:
            sample = df["time_range"].dropna().astype(str)
            if not sample.empty:
                txt = sample.iloc[0]
                if "-" in txt:
                    a, b = txt.split("-", 1)
                    try:
                        t1 = pd.to_datetime(a, format="%H:%M")
                        t2 = pd.to_datetime(b, format="%H:%M")
                        mins = int((t2 - t1).total_seconds() // 60) + 1
                        if mins in (1, 3, 5, 10, 15, 30, 60):
                            return f"{mins}min"
                    except Exception:
                        pass
    except Exception:
        logger.debug("[POST.CALC] interval infer failed", exc_info=True)
    return "1min"


def needs_actual_calc(df: pd.DataFrame) -> bool:
    try:
        if df is None or df.empty:
            return False

        def _nz(col_names: list[str]) -> int:
            for c in col_names:
                if c in df.columns:
                    return int(pd.to_numeric(df[c], errors="coerce").fillna(0).ne(0).sum())
            return 0

        score_nonzero = _nz(["score", "score_total"])
        slope_nonzero = _nz(["slope", "slope_atr_scaled", "score_slope"])
        mtf_nonzero = _nz(["mtf", "mtf_alignment"])
        rsi_nonzero = _nz(["rsi"])
        macd_nonzero = _nz(["macd"])
        signal_nonzero = _nz(["signal"])

        technical_dead = (rsi_nonzero == 0 and macd_nonzero == 0 and signal_nonzero == 0)
        mtf_dead = (mtf_nonzero == 0)

        need = (
            (score_nonzero == 0)
            or technical_dead
            or (slope_nonzero == 0 and mtf_dead)
        )

        logger.info(
            "[POST.CALC] actual-calc need=%s "
            "score_nonzero=%s slope_nonzero=%s mtf_nonzero=%s "
            "rsi_nonzero=%s macd_nonzero=%s signal_nonzero=%s",
            need,
            score_nonzero,
            slope_nonzero,
            mtf_nonzero,
            rsi_nonzero,
            macd_nonzero,
            signal_nonzero,
        )
        return need

    except Exception:
        logger.exception("[POST.CALC] needs_actual_calc failed")
        return True


def log_post_numeric_profile(tag: str, df: pd.DataFrame) -> None:
    try:
        if df is None or df.empty:
            logger.info("[POST.CALC] %s empty", tag)
            return

        def _nz(col: str) -> int:
            if col not in df.columns:
                return -1
            return int(pd.to_numeric(df[col], errors="coerce").fillna(0).ne(0).sum())

        logger.info(
            "[POST.CALC] %s rows=%s score=%s slope=%s mtf=%s score_mtf=%s rsi=%s macd=%s signal=%s",
            tag,
            len(df),
            _nz("score"),
            _nz("slope"),
            _nz("mtf"),
            _nz("score_mtf"),
            _nz("rsi"),
            _nz("macd"),
            _nz("signal"),
        )
    except Exception:
        logger.exception("[POST.CALC] log profile failed tag=%s", tag)


def run_actual_indicator_and_scoring(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df

    out = df.copy()
    interval_label = infer_interval_label(out)

    log_post_numeric_profile("before_actual_indicator", out)

    try:
        from trading.summary.indicators.indicator_calculator import add_all_indicators
        logger.info("[POST.CALC] running add_all_indicators interval=%s rows=%s", interval_label, len(out))
        out = add_all_indicators(out, interval=interval_label)
        log_post_numeric_profile("after_add_all_indicators", out)
    except Exception:
        logger.exception("[POST.CALC] add_all_indicators failed interval=%s", interval_label)
        return df

    try:
        from trading.scoring.core.scoring_core import scoring_main
        logger.info("[POST.CALC] running scoring_main interval=%s rows=%s", interval_label, len(out))
        out = scoring_main(out, interval=interval_label, force=True)
        log_post_numeric_profile("after_scoring_main", out)
    except Exception:
        logger.exception("[POST.CALC] scoring_main failed interval=%s", interval_label)
        return out

    out = drop_outside_allowed_dates(out, "actual_indicator_scoring")
    log_post_numeric_profile("after_actual_indicator_scoring", out)
    return out