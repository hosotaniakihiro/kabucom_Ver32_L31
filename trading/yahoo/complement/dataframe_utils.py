from __future__ import annotations
import datetime as dt, logging, time
import pandas as pd
from utils.datetime_guard import ensure_datetime
from trading.yahoo.storage.yahoo_1min_store import save_yahoo_1min
from trading.yahoo.normalize.yahoo_normalizer_resolver import normalize_yahoo_df
from .logging_utils import log_df_profile, log_step
logger = logging.getLogger(__name__)

def normalize_downloaded_df(df: pd.DataFrame, *, label: str) -> pd.DataFrame:
    if df is None or df.empty: return pd.DataFrame()
    ts = time.time()
    try:
        log_df_profile(f"{label}:raw_before_normalize", df)
        out = normalize_yahoo_df(df)
        if out is None or not isinstance(out, pd.DataFrame) or out.empty:
            logger.warning("%s: normalize empty", label); return pd.DataFrame()
        log_df_profile(f"{label}:after_normalize_yahoo_df", out)
        out = ensure_datetime(out)
        if out is None or not isinstance(out, pd.DataFrame) or out.empty:
            logger.warning("%s: datetime repair failed", label); return pd.DataFrame()
        try: out = out.drop_duplicates(subset=["symbol", "datetime"], keep="last").sort_values(["symbol", "datetime"]).reset_index(drop=True)
        except Exception: pass
        log_df_profile(f"{label}:after_ensure_datetime", out); log_step(f"{label}:normalize_done", ts, rows=len(out)); return out
    except Exception: logger.exception("%s: normalize failed", label); return pd.DataFrame()

def save_intraday_by_date(df: pd.DataFrame, *, fallback_date: dt.date, label: str) -> None:
    if df is None or df.empty: return
    ts = time.time()
    try: by_date = {d: g.copy() for d, g in df.groupby(pd.to_datetime(df["datetime"], errors="coerce").dt.date, sort=False)}
    except Exception: logger.exception("%s: date grouping failed", label); by_date = {}
    if by_date:
        logger.info("[YAHOO COMPLEMENT] %s save_intraday_by_date groups=%s dates=%s", label, len(by_date), list(by_date.keys()))
        for day, g in by_date.items():
            try:
                if isinstance(g, pd.DataFrame) and not g.empty:
                    log_df_profile(f"{label}:save_group:{day}", g); save_yahoo_1min(g, target_date=day); logger.info("[YAHOO COMPLEMENT] %s save_yahoo_1min done day=%s rows=%s", label, day, len(g))
            except Exception: logger.exception("❌ yahoo_1min 保存失敗 day=%s label=%s", day, label)
    else:
        try: log_df_profile(f"{label}:save_fallback:{fallback_date}", df); save_yahoo_1min(df, target_date=fallback_date)
        except Exception: logger.exception("❌ yahoo_1min 保存失敗 day=%s label=%s", fallback_date, label)
    log_step(f"{label}:save_intraday_done", ts)
__all__ = ["normalize_downloaded_df", "save_intraday_by_date"]
