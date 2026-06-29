# ============================================================
# File   : core/startup/summary_mtf_push_raw_fallback_patch.py
# Version: V1-MAIN-PUSH-RAW-MTF-FALLBACK
# ------------------------------------------------------------
# main.py は split 運用で NAS SQLite 直読みの3m/5m差分更新を避ける。
# その際、summary_1m cache がまだ空だと既存の
# summary_mtf_diff_from_1m_patch でも 3m/5m を生成できず、
#   MERGED GET tf=3 source=push rows=0
#   summary db fallback stale interval=3
# が残る。
#
# このパッチは global_data.push_df などの raw PUSH メモリを1m相当の履歴として
# 既存 summary_mtf_diff_from_1m_patch に追加供給し、main.py 側だけで軽量に
# 3分/5分PUSH summaryを作れるようにする。DB保存はしない。
# ============================================================

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

_INSTALLED = False
_ORIG_CACHED_1M = None
_BOOTSTRAP_STARTED = False


def _env_bool(name: str, default: bool) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "enable", "enabled", "ok"}
    except Exception:
        return bool(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(str(v).strip()))
    except Exception:
        return int(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).strip())
    except Exception:
        return float(default)


def _is_main_py() -> bool:
    try:
        argv = " ".join(str(x).replace("\\", "/").lower() for x in sys.argv)
        return "main.py" in argv
    except Exception:
        return False


def _to_df(x: Any) -> pd.DataFrame:
    if isinstance(x, pd.DataFrame):
        return x.copy()
    return pd.DataFrame()


def _pick_col(df: pd.DataFrame, names: tuple[str, ...]) -> str | None:
    for name in names:
        if name in df.columns:
            return name
    lower = {str(c).lower(): c for c in df.columns}
    for name in names:
        c = lower.get(name.lower())
        if c is not None:
            return str(c)
    return None


def _normalize_raw_push_df(df: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame()
    try:
        out = df.copy()
        sym_col = _pick_col(out, ("symbol", "Symbol", "code", "Code"))
        dt_col = _pick_col(out, ("datetime", "received_at", "current_price_time", "CurrentPriceTime", "time", "Time"))
        price_col = _pick_col(out, ("close", "price", "current_price", "CurrentPrice", "close_price", "Close"))
        if not sym_col or not dt_col or not price_col:
            return pd.DataFrame()

        ret = pd.DataFrame()
        ret["symbol"] = out[sym_col].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
        ret["datetime"] = pd.to_datetime(out[dt_col], errors="coerce")
        ret["close"] = pd.to_numeric(out[price_col], errors="coerce")
        ret = ret.dropna(subset=["symbol", "datetime", "close"])
        ret = ret[ret["symbol"] != ""].copy()
        if ret.empty:
            return pd.DataFrame()

        for dst, src_names in {
            "open": ("open", "opening_price", "OpenPrice", "open_price"),
            "high": ("high", "high_price", "HighPrice"),
            "low": ("low", "low_price", "LowPrice"),
        }.items():
            src = _pick_col(out, src_names)
            if src:
                ret[dst] = pd.to_numeric(out.loc[ret.index, src], errors="coerce").fillna(ret["close"])
            else:
                ret[dst] = ret["close"]

        vol_col = _pick_col(out, ("volume", "Volume", "vol", "CumVolume"))
        if vol_col:
            ret["volume"] = pd.to_numeric(out.loc[ret.index, vol_col], errors="coerce").fillna(0.0)
        else:
            ret["volume"] = 0.0

        name_col = _pick_col(out, ("symbolname", "SymbolName", "name", "Name"))
        if name_col:
            ret["symbolname"] = out.loc[ret.index, name_col].astype(str)
        else:
            ret["symbolname"] = ret["symbol"]

        ret["price"] = ret["close"]
        ret["current_price"] = ret["close"]
        ret["source"] = "main_push_raw_mtf_fallback"
        ret["interval"] = 1
        limit = _env_int("SUMMARY_MTF_PUSH_RAW_FALLBACK_LIMIT", 5000)
        ret = ret.sort_values(["symbol", "datetime"], kind="stable")
        ret = ret.drop_duplicates(subset=["symbol", "datetime"], keep="last")
        if len(ret) > limit:
            ret = ret.tail(limit)
        return ret.reset_index(drop=True)
    except Exception:
        logger.debug("[SUMMARY MTF PUSH RAW FALLBACK] normalize raw push failed", exc_info=True)
        return pd.DataFrame()


def _raw_push_history_from_global() -> pd.DataFrame:
    try:
        from global_state import global_data
        candidates: list[pd.DataFrame] = []
        for name in (
            "push_df",
            "push_stream_df",
            "raw_push_df",
            "stream_df",
            "current_push_df",
            "push_data_df",
            "latest_push_df",
        ):
            try:
                df = _normalize_raw_push_df(_to_df(getattr(global_data, name, None)))
                if not df.empty:
                    candidates.append(df)
            except Exception:
                pass
        for method_name in ("get_push_df", "get_raw_push_df", "get_push_stream_df"):
            try:
                fn = getattr(global_data, method_name, None)
                if callable(fn):
                    df = _normalize_raw_push_df(_to_df(fn()))
                    if not df.empty:
                        candidates.append(df)
            except Exception:
                pass
        if not candidates:
            return pd.DataFrame()
        out = pd.concat(candidates, ignore_index=True, sort=False)
        out = out.drop_duplicates(subset=["symbol", "datetime"], keep="last")
        out = out.sort_values(["symbol", "datetime"], kind="stable")
        logger.warning(
            "[SUMMARY MTF PUSH RAW FALLBACK] raw push history rows=%s symbols=%s latest_dt=%s",
            len(out),
            int(out["symbol"].nunique()) if "symbol" in out.columns else 0,
            out["datetime"].max() if "datetime" in out.columns and not out.empty else None,
        )
        return out.reset_index(drop=True)
    except Exception:
        logger.exception("[SUMMARY MTF PUSH RAW FALLBACK] raw push history load failed")
        return pd.DataFrame()


def _patch_cached_1m_loader() -> bool:
    global _ORIG_CACHED_1M
    try:
        import core.startup.summary_mtf_diff_from_1m_patch as base
        orig = getattr(base, "_cached_1m_history_from_global", None)
        if not callable(orig):
            logger.warning("[SUMMARY MTF PUSH RAW FALLBACK] base cached 1m loader unavailable")
            return False
        if getattr(orig, "_push_raw_fallback_patched", False):
            return True
        _ORIG_CACHED_1M = orig

        def _cached_1m_with_raw_push() -> pd.DataFrame:
            dfs: list[pd.DataFrame] = []
            try:
                old = orig()
                if isinstance(old, pd.DataFrame) and not old.empty:
                    dfs.append(old.copy())
            except Exception:
                logger.debug("[SUMMARY MTF PUSH RAW FALLBACK] original cached 1m failed", exc_info=True)
            raw = _raw_push_history_from_global()
            if isinstance(raw, pd.DataFrame) and not raw.empty:
                dfs.append(raw)
            if not dfs:
                return pd.DataFrame()
            out = pd.concat(dfs, ignore_index=True, sort=False)
            if "datetime" in out.columns:
                out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
                out = out.dropna(subset=["datetime"])
            if "symbol" in out.columns and "datetime" in out.columns:
                out["symbol"] = out["symbol"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
                out = out[out["symbol"] != ""]
                out = out.drop_duplicates(subset=["symbol", "datetime"], keep="last")
                out = out.sort_values(["symbol", "datetime"], kind="stable")
            return out.reset_index(drop=True)

        _cached_1m_with_raw_push._push_raw_fallback_patched = True  # type: ignore[attr-defined]
        setattr(base, "_cached_1m_history_from_global", _cached_1m_with_raw_push)
        return True
    except Exception:
        logger.exception("[SUMMARY MTF PUSH RAW FALLBACK] patch base cached loader failed")
        return False


def _bootstrap_worker() -> None:
    try:
        if not _is_main_py() and not _env_bool("SUMMARY_MTF_PUSH_RAW_BOOTSTRAP_FORCE", False):
            return
        import core.startup.summary_mtf_diff_from_1m_patch as base
        repeats = max(1, _env_int("SUMMARY_MTF_PUSH_RAW_BOOTSTRAP_REPEATS", 4))
        delay = max(0.5, _env_float("SUMMARY_MTF_PUSH_RAW_BOOTSTRAP_DELAY_SEC", 4.0))
        gap = max(2.0, _env_float("SUMMARY_MTF_PUSH_RAW_BOOTSTRAP_GAP_SEC", 8.0))
        for i in range(repeats):
            time.sleep(delay if i == 0 else gap)
            for interval in (3, 5):
                try:
                    fn = getattr(base, "_resample_cached_1m_to_mtf", None)
                    df = fn(interval) if callable(fn) else pd.DataFrame()
                    logger.warning(
                        "[SUMMARY MTF PUSH RAW FALLBACK] bootstrap resample interval=%s rows=%s i=%s/%s",
                        interval,
                        len(df) if isinstance(df, pd.DataFrame) else 0,
                        i + 1,
                        repeats,
                    )
                except Exception:
                    logger.debug("[SUMMARY MTF PUSH RAW FALLBACK] bootstrap resample failed interval=%s", interval, exc_info=True)
    except Exception:
        logger.exception("[SUMMARY MTF PUSH RAW FALLBACK] bootstrap worker failed")


def _start_bootstrap() -> None:
    global _BOOTSTRAP_STARTED
    if _BOOTSTRAP_STARTED:
        return
    _BOOTSTRAP_STARTED = True
    threading.Thread(target=_bootstrap_worker, name="SummaryMtfPushRawFallbackBootstrap", daemon=True).start()


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    if not _env_bool("SUMMARY_MTF_PUSH_RAW_FALLBACK_ENABLED", True):
        logger.warning("[SUMMARY MTF PUSH RAW FALLBACK] disabled by env")
        return False
    ok = _patch_cached_1m_loader()
    if ok:
        _INSTALLED = True
        _start_bootstrap()
    logger.warning(
        "[SUMMARY MTF PUSH RAW FALLBACK] installed ok=%s main_py=%s version=V1",
        ok,
        _is_main_py(),
    )
    return ok


try:
    install()
except Exception:
    logger.exception("[SUMMARY MTF PUSH RAW FALLBACK] auto install failed")

__all__ = ["install"]
