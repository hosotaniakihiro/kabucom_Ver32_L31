# ============================================================
# File   : core/startup/summary_ai_volatility_rescue_patch.py
# Version: V2-SUMMARY-AI-STRONG-VOL-RESCUE-DB-FRESH-INPUT
# ------------------------------------------------------------
# SUMMARY_AIの強い候補が、entry_controller内の ATR_1M_FILTER_NG /
# RANGE_5M_FILTER_NG だけで全落ちする問題を救済する。
#
# V2:
#   - main.py の PUSH memory が古い場合、Summary AI が
#     no_fresh_scored_df で投入されないため、summaryYYYYMMDD.db の
#     stock_summary_1min 最新scored行を AI input の代替として使う。
#   - DBも古い場合は発注せず、MAIN_DB/PUSH鮮度不足を明確にログへ出す。
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)
VERSION = "V2-SUMMARY-AI-STRONG-VOL-RESCUE-DB-FRESH-INPUT"
_INSTALLED = False
_WATCHER_STARTED = False
_MAIN_AI_INPUT_PATCHED = False


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(str(v).replace(",", "")))
    except Exception:
        return int(default)


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _row_to_dict(v: Any) -> dict[str, Any]:
    try:
        if isinstance(v, dict):
            d = dict(v)
        elif hasattr(v, "to_dict"):
            tmp = v.to_dict()
            d = dict(tmp) if isinstance(tmp, dict) else {}
        else:
            d = {}
        raw = d.get("_raw")
        if hasattr(raw, "to_dict"):
            try:
                raw = raw.to_dict()
            except Exception:
                raw = None
        if isinstance(raw, dict):
            for k, val in raw.items():
                if k not in d or d.get(k) in (None, ""):
                    d[k] = val
        return d
    except Exception:
        return {}


def _first(row: dict[str, Any], keys: tuple[str, ...], default: Any = None) -> Any:
    for k in keys:
        try:
            v = row.get(k)
            if v is not None and str(v).strip() != "":
                return v
        except Exception:
            pass
    return default


def _norm(v: Any) -> str:
    try:
        return str(v or "").strip().upper()
    except Exception:
        return ""


def _is_summary_ai(row: dict[str, Any]) -> bool:
    src = _norm(_first(row, ("source", "entry_source", "pipeline_source", "src"), ""))
    et = _norm(_first(row, ("entry_type", "type", "strategy"), ""))
    reason = _norm(_first(row, ("reason", "ai_reason", "entry_reason"), ""))
    model = _norm(_first(row, ("model", "model_used"), ""))
    if et in {"SUMMARY_AI", "AI_SUMMARY"}:
        return True
    if src in {"SUMMARY", "SUMMARY_AI", "PUSH"} and ("SUMMARY" in reason or "MTF" in model):
        return True
    return False


def _side(row: dict[str, Any]) -> str:
    s = _norm(_first(row, ("side", "entry_decision", "ai_side", "dominant_side"), ""))
    return s if s in {"BUY", "SELL"} else ""


def _score(row: dict[str, Any]) -> float:
    side = _side(row)
    if side == "BUY":
        keys = ("score_buy", "buy_score", "ai_buy_score", "score", "final_score", "display_score", "score_total")
    elif side == "SELL":
        keys = ("score_sell", "sell_score", "ai_sell_score", "score", "final_score", "display_score", "score_total")
    else:
        keys = ("score", "final_score", "display_score", "score_total", "score_buy", "score_sell")
    return abs(_safe_float(_first(row, keys, 0.0), 0.0))


def _strong_summary_ai_ok(entry_row: Any, label: str) -> bool:
    if not _env_bool("SUMMARY_AI_VOL_RESCUE_ENABLED", True):
        return False
    row = _row_to_dict(entry_row)
    if not _is_summary_ai(row):
        return False

    symbol = str(_first(row, ("symbol", "code", "stock_code", "銘柄コード"), "")).strip()
    price = _safe_float(_first(row, ("close_price", "close", "price", "current_price"), 0.0), 0.0)
    volume = _safe_float(_first(row, ("volume", "Volume", "trading_volume", "TradingVolume", "出来高"), 0.0), 0.0)
    turnover = _safe_float(_first(row, ("turnover", "trading_value", "TradingValue", "売買代金", "amount"), 0.0), 0.0)
    if turnover <= 0 and price > 0 and volume > 0:
        turnover = price * volume
    score = _score(row)
    slopes = [
        abs(_safe_float(row.get(k), 0.0))
        for k in ("slope_atr_scaled", "slope", "score_slope", "disp_slope")
        if k in row
    ]
    slope_abs = max(slopes, default=0.0)

    min_score = _env_float("SUMMARY_AI_VOL_RESCUE_MIN_SCORE", 3.0)
    min_turnover = _env_float("SUMMARY_AI_VOL_RESCUE_MIN_TURNOVER", 10000000.0)
    min_volume = _env_float("SUMMARY_AI_VOL_RESCUE_MIN_VOLUME", 3000.0)
    min_slope = _env_float("SUMMARY_AI_VOL_RESCUE_MIN_ABS_SLOPE", 0.001)
    min_price = _env_float("SUMMARY_AI_VOL_RESCUE_MIN_PRICE", 200.0)
    max_price = _env_float("SUMMARY_AI_VOL_RESCUE_MAX_PRICE", 7000.0)

    ok = (
        price >= min_price
        and price <= max_price
        and score >= min_score
        and turnover >= min_turnover
        and (volume >= min_volume or turnover >= min_turnover)
        and slope_abs >= min_slope
    )
    if ok:
        logger.warning(
            "[SUMMARY AI VOL RESCUE] allow original_%s_NG symbol=%s side=%s price=%.1f score=%.3f turnover=%.0f volume=%.0f slope_abs=%.6f min_score=%.2f min_turnover=%.0f min_slope=%.6f version=%s",
            label, symbol, _side(row), price, score, turnover, volume, slope_abs, min_score, min_turnover, min_slope, VERSION,
        )
        return True

    logger.info(
        "[SUMMARY AI VOL RESCUE] keep NG symbol=%s label=%s price=%.1f score=%.3f/%s turnover=%.0f/%s volume=%.0f/%s slope_abs=%.6f/%s",
        symbol, label, price, score, min_score, turnover, min_turnover, volume, min_volume, slope_abs, min_slope,
    )
    return False


def _wrap_filter(fn: Any, label: str):
    if not callable(fn):
        return fn
    if getattr(fn, f"_summary_ai_vol_rescue_{label}_v2", False):
        return fn

    def _wrapped(entry_row: Any = None, *args: Any, **kwargs: Any):
        ret = fn(entry_row, *args, **kwargs)
        if isinstance(ret, tuple):
            return ret
        if bool(ret):
            return ret
        if _strong_summary_ai_ok(entry_row, label):
            return True
        return ret

    setattr(_wrapped, f"_summary_ai_vol_rescue_{label}_v1", True)
    setattr(_wrapped, f"_summary_ai_vol_rescue_{label}_v2", True)
    _wrapped._original = fn  # type: ignore[attr-defined]
    return _wrapped


def _summary_db_paths() -> list[str]:
    ymd = dt.datetime.now().strftime("%Y%m%d")
    candidates = [
        os.getenv("SUMMARY_MAIN_FRESH_DB_PATH"),
        os.getenv("SUMMARY_DB_PATH"),
        os.getenv("SUMMARY_DB_FILE"),
    ]
    dirs = [
        os.getenv("SUMMARY_MAIN_FRESH_DB_DIR"),
        os.getenv("SUMMARY_DB_DIR"),
        os.getenv("AUTOSTOCK_SUMMARY_DIR"),
        r"\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\summary",
        r"\\192.168.0.22\AutoStockBuyAndSell\raw_data\summary",
        r"\\192.168.0.22\AutoStockBuyAndSell\summary",
    ]
    out: list[str] = []
    for p in candidates:
        if not p:
            continue
        s = str(p).replace("YYYYMMDD", ymd)
        out.append(s)
    for d in dirs:
        if d:
            out.append(str(Path(str(d)) / f"summary{ymd}.db"))
    seen, uniq = set(), []
    for p in out:
        if p and p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def _norm_df(df: Any) -> pd.DataFrame:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame()
    try:
        x = df.copy()
        x = x.loc[:, ~pd.Index(x.columns).duplicated()].copy()
        if "symbol" not in x.columns:
            for c in ("Symbol", "code", "Code", "stock_code"):
                if c in x.columns:
                    x["symbol"] = x[c]
                    break
        if "datetime" not in x.columns:
            for c in ("Datetime", "date_time", "timestamp", "created_at", "updated_at"):
                if c in x.columns:
                    x["datetime"] = x[c]
                    break
        if "symbol" not in x.columns or "datetime" not in x.columns:
            return pd.DataFrame()
        x["symbol"] = x["symbol"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
        x["datetime"] = pd.to_datetime(x["datetime"], errors="coerce")
        try:
            x["datetime"] = x["datetime"].dt.tz_localize(None)
        except Exception:
            pass
        x = x.dropna(subset=["symbol", "datetime"])
        x = x[x["symbol"].ne("")]
        return x.reset_index(drop=True)
    except Exception:
        return pd.DataFrame()


def _score_profile_df(df: pd.DataFrame) -> dict[str, Any]:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty:
            return {"rows": 0, "score_nonzero": 0, "latest": None, "age": None}
        def col(names: tuple[str, ...]) -> pd.Series:
            for n in names:
                if n in df.columns:
                    return pd.to_numeric(df[n], errors="coerce").fillna(0.0)
            return pd.Series(0.0, index=df.index)
        buy = col(("score_buy", "buy_score", "ai_disp_buy_score", "config_buy_score"))
        sell = col(("score_sell", "sell_score", "ai_disp_sell_score", "config_sell_score")).abs()
        total = col(("score_total", "final_score", "display_score", "score"))
        latest = pd.to_datetime(df["datetime"], errors="coerce").max() if "datetime" in df.columns else None
        age = None
        if latest is not None and pd.notna(latest):
            try:
                age = (dt.datetime.now().replace(tzinfo=None) - latest.to_pydatetime().replace(tzinfo=None)).total_seconds()
            except Exception:
                age = None
        return {
            "rows": len(df),
            "score_nonzero": int(((buy.abs() > 0) | (sell.abs() > 0) | (total.abs() > 0)).sum()),
            "buy_pos": int((buy > 0).sum()),
            "sell_pos": int((sell > 0).sum()),
            "latest": latest,
            "age": age,
        }
    except Exception:
        return {"rows": 0, "score_nonzero": 0, "latest": None, "age": None}


def _load_fresh_summary_db_scored() -> pd.DataFrame:
    if not _env_bool("SUMMARY_AI_DB_FRESH_INPUT_ENABLED", True):
        return pd.DataFrame()
    table = os.getenv("SUMMARY_AI_DB_FRESH_INPUT_TABLE", "stock_summary_1min")
    max_age = _env_float("SUMMARY_AI_DB_FRESH_INPUT_MAX_AGE_SEC", _env_float("SUMMARY_MAIN_AI_MAX_SCORE_AGE_SEC", 300.0))
    lookback_min = _env_int("SUMMARY_AI_DB_FRESH_INPUT_LOOKBACK_MIN", 20)
    limit = _env_int("SUMMARY_AI_DB_FRESH_INPUT_LIMIT", 5000)
    since = (dt.datetime.now() - dt.timedelta(minutes=lookback_min)).strftime("%Y-%m-%d %H:%M:%S")
    for path in _summary_db_paths():
        try:
            if not path or not os.path.exists(path):
                continue
            with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.8) as con:
                sql = f'SELECT * FROM "{table}" WHERE datetime >= ? ORDER BY datetime DESC LIMIT ?'
                df = pd.read_sql_query(sql, con, params=[since, int(limit)])
            x = _norm_df(df)
            if x.empty:
                continue
            x = x.sort_values(["symbol", "datetime"], kind="stable").drop_duplicates("symbol", keep="last").reset_index(drop=True)
            prof = _score_profile_df(x)
            if int(prof.get("score_nonzero", 0) or 0) <= 0:
                logger.warning("[SUMMARY AI DB FRESH INPUT] db has no scored rows path=%s rows=%s latest=%s", path, prof.get("rows"), prof.get("latest"))
                continue
            age = prof.get("age")
            if age is not None and float(age) > max_age:
                logger.warning(
                    "[SUMMARY AI DB FRESH INPUT] stale db scored summary skipped path=%s rows=%s latest=%s age=%.1fs max_age=%.1fs score_nonzero=%s START_MAIN_DATABASE_OR_CHECK_PUSH",
                    path, prof.get("rows"), prof.get("latest"), float(age), max_age, prof.get("score_nonzero"),
                )
                continue
            logger.warning(
                "[SUMMARY AI DB FRESH INPUT] selected db scored summary path=%s rows=%s latest=%s age=%s score_nonzero=%s buy_pos=%s sell_pos=%s version=%s",
                path, prof.get("rows"), prof.get("latest"), prof.get("age"), prof.get("score_nonzero"), prof.get("buy_pos"), prof.get("sell_pos"), VERSION,
            )
            return x
        except Exception as e:
            logger.debug("[SUMMARY AI DB FRESH INPUT] db candidate failed path=%s err=%s", path, e, exc_info=True)
    logger.warning("[SUMMARY AI DB FRESH INPUT] no fresh scored summary db available START_MAIN_DATABASE_OR_CHECK_PUSH paths=%s", _summary_db_paths()[:3])
    return pd.DataFrame()


def _patch_summary_main_ai_input(reason: str = "install") -> bool:
    global _MAIN_AI_INPUT_PATCHED
    try:
        import core.startup.summary_main_1m_light_tick_patch as light
        cur = getattr(light, "_get_scored_context_summary", None)
        if not callable(cur):
            return False
        if getattr(cur, "_summary_ai_db_fresh_input_v2", False):
            _MAIN_AI_INPUT_PATCHED = True
            return True
        orig = getattr(cur, "_original", cur)

        def _patched_get_scored_context_summary(*args: Any, **kwargs: Any):
            try:
                ret = orig(*args, **kwargs)
                if isinstance(ret, pd.DataFrame) and not ret.empty:
                    return ret
            except Exception:
                logger.debug("[SUMMARY AI DB FRESH INPUT] original scored context failed", exc_info=True)
            return _load_fresh_summary_db_scored()

        _patched_get_scored_context_summary._summary_ai_db_fresh_input_v2 = True  # type: ignore[attr-defined]
        _patched_get_scored_context_summary._original = orig  # type: ignore[attr-defined]
        light._get_scored_context_summary = _patched_get_scored_context_summary
        _MAIN_AI_INPUT_PATCHED = True
        logger.warning("[SUMMARY AI DB FRESH INPUT] patched summary_main_1m_light_tick scored context reason=%s version=%s", reason, VERSION)
        return True
    except Exception:
        logger.debug("[SUMMARY AI DB FRESH INPUT] patch failed reason=%s", reason, exc_info=True)
        return False


def _apply_once(reason: str = "install") -> bool:
    ok = False
    try:
        import trading.handlers.entry_controller as ec
        changed = False
        old_atr = getattr(ec, "atr_1m_filter", None)
        new_atr = _wrap_filter(old_atr, "ATR")
        if new_atr is not old_atr:
            ec.atr_1m_filter = new_atr
            changed = True
        old_range = getattr(ec, "range_5m_filter", None)
        new_range = _wrap_filter(old_range, "RANGE")
        if new_range is not old_range:
            ec.range_5m_filter = new_range
            changed = True
        if changed:
            logger.warning("[SUMMARY AI VOL RESCUE] patched entry_controller filters reason=%s version=%s", reason, VERSION)
        ok = True
    except Exception:
        logger.exception("[SUMMARY AI VOL RESCUE] apply failed reason=%s", reason)
    input_ok = _patch_summary_main_ai_input(reason)
    return bool(ok or input_ok)


def _watcher() -> None:
    loops = int(max(1, _env_float("SUMMARY_AI_VOL_RESCUE_WATCH_LOOPS", 80)))
    interval = max(0.2, _env_float("SUMMARY_AI_VOL_RESCUE_WATCH_INTERVAL_SEC", 0.5))
    for i in range(loops):
        try:
            _apply_once(reason=f"watcher:{i}")
        except Exception:
            logger.debug("[SUMMARY AI VOL RESCUE] watcher apply failed", exc_info=True)
        time.sleep(interval)


def install() -> bool:
    global _INSTALLED, _WATCHER_STARTED
    os.environ.setdefault("SUMMARY_AI_VOL_RESCUE_ENABLED", "1")
    os.environ.setdefault("SUMMARY_AI_VOL_RESCUE_MIN_SCORE", "3.0")
    os.environ.setdefault("SUMMARY_AI_VOL_RESCUE_MIN_TURNOVER", "10000000")
    os.environ.setdefault("SUMMARY_AI_VOL_RESCUE_MIN_VOLUME", "3000")
    os.environ.setdefault("SUMMARY_AI_VOL_RESCUE_MIN_ABS_SLOPE", "0.001")
    os.environ.setdefault("SUMMARY_AI_DB_FRESH_INPUT_ENABLED", "1")
    os.environ.setdefault("SUMMARY_AI_DB_FRESH_INPUT_MAX_AGE_SEC", "300")
    ok = _apply_once("install")
    _INSTALLED = bool(ok)
    if ok and not _WATCHER_STARTED:
        _WATCHER_STARTED = True
        threading.Thread(target=_watcher, name="summary-ai-vol-rescue-watch", daemon=True).start()
    logger.warning("[SUMMARY AI VOL RESCUE] installed=%s watcher=%s db_fresh_input=%s version=%s", ok, _WATCHER_STARTED, _MAIN_AI_INPUT_PATCHED, VERSION)
    return bool(ok)


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI VOL RESCUE] auto install failed")


__all__ = ["install"]
