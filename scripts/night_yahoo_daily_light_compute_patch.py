# ============================================================
# File   : scripts/night_yahoo_daily_light_compute_patch.py
# Version: V1-NIGHT-YAHOO-DAILY-LIGHT-COMPUTE
# ------------------------------------------------------------
# 夜間Yahoo日足フル作成で、外部の重い日足計算パイプラインを使わず、
# fixed full columns patch の enrich_daily_columns だけで計算する。
#
# 目的:
#   - compute begin のまま固まる問題を回避
#   - 1995年から全銘柄を安定して作成する
# ============================================================

from __future__ import annotations

import logging
import os
import time
from typing import Any

import pandas as pd

LOG = logging.getLogger("night_yahoo_daily_light_compute_patch")
VERSION = "V1-NIGHT-YAHOO-DAILY-LIGHT-COMPUTE"
_INSTALLED = False


def _enabled() -> bool:
    return str(os.environ.get("NIGHT_YAHOO_DAILY_LIGHT_COMPUTE", "1")).strip().lower() not in {"0", "false", "no", "off"}


def _normalize_symbol(value: Any) -> str:
    s = str(value or "").strip().upper()
    if s.endswith(".T"):
        s = s[:-2]
    if s.endswith(".0"):
        s = s[:-2]
    return s.zfill(4) if s.isdigit() and len(s) < 4 else s


def install(daily_mod: Any) -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    if daily_mod is None:
        return False
    if not _enabled():
        LOG.warning("[NIGHT YAHOO DAILY LIGHT COMPUTE] disabled by env")
        return False

    try:
        from scripts import night_yahoo_daily_full_columns_patch as fullcol
    except Exception:
        LOG.warning("[NIGHT YAHOO DAILY LIGHT COMPUTE] import full columns patch failed", exc_info=True)
        return False

    def light_pipeline(price_df: pd.DataFrame, rec: dict[str, str]) -> pd.DataFrame:
        t0 = time.time()
        if price_df is None or price_df.empty:
            return pd.DataFrame()
        p = price_df.copy()
        p["date"] = pd.to_datetime(p["date"], errors="coerce")
        p = p.dropna(subset=["date"]).copy()
        symbol = _normalize_symbol((rec or {}).get("symbol") or (p["symbol"].iloc[0] if "symbol" in p.columns and not p.empty else ""))
        stock_name = str((rec or {}).get("symbolname") or (rec or {}).get("stock_name") or "")
        market = str((rec or {}).get("market") or "")

        # full_columns側が期待する基本列へ正規化
        p["stock_code"] = symbol
        p["stock_name"] = stock_name
        p["market"] = market
        if "adj_close" not in p.columns and "close" in p.columns:
            p["adj_close"] = p["close"]
        for c in ["open", "high", "low", "close", "adj_close", "volume"]:
            if c in p.columns:
                p[c] = pd.to_numeric(p[c], errors="coerce")
        base_cols = ["stock_code", "stock_name", "market", "date", "open", "high", "low", "close", "adj_close", "volume"]
        p = p[[c for c in base_cols if c in p.columns]].copy()
        out = fullcol.enrich_daily_columns(p)
        LOG.info(
            "[NIGHT YAHOO DAILY LIGHT COMPUTE] done symbol=%s rows=%s cols=%s elapsed=%.1fs",
            symbol, len(out), len(out.columns), time.time() - t0,
        )
        return out

    daily_mod._run_indicator_pipeline = light_pipeline
    daily_mod._NIGHT_YAHOO_DAILY_LIGHT_COMPUTE_PATCHED = True
    _INSTALLED = True
    LOG.warning(
        "[NIGHT YAHOO DAILY LIGHT COMPUTE] installed version=%s enabled=%s",
        VERSION,
        os.environ.get("NIGHT_YAHOO_DAILY_LIGHT_COMPUTE", "1"),
    )
    return True
