# -*- coding: utf-8 -*-
# ============================================================
# File   : trading/summary/push_raw_1m_source.py
# Version: V2-MAIN-ENTRY-FRESH-RAW-SUMMARYAI
# ------------------------------------------------------------
# 旧 core/startup/main_entry_runtime_operator_fix_patch.py のデータ取得部を
# 切り出したもの。
#
# main.py は判断プロセスで、PUSH DB の保存は main_database.py が持つ。
# その結果 main.py 側の in-process merged summary だけが古くなり、
# Summary-AI / Tonosama が止まることがある。ここはその救済のために
# 「メモリ上の PUSH フレーム」と「PUSH raw DB の当日 1 分足」を読む。
#
# 読み手:
#   trading/entry/tonosama/history_missing_guard._load_push_raw_db_1m_history()
#       メモリ履歴 -> DB -> fresh raw の順に落とす (本文化済み)
#   core/startup/main_entry_operator.py
#       summary_ai hook へ fresh raw を overlay する
# ============================================================
from __future__ import annotations

import datetime as dt
import glob
import logging
import os
import sqlite3
import sys
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V2-MAIN-ENTRY-FRESH-RAW-SUMMARYAI"
_TRUE = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}


def _env_on(name: str, default: bool = False) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in _TRUE
    except Exception:
        return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return float(default)


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, str(default))))
    except Exception:
        return int(default)


def _is_df_like(obj: Any) -> bool:
    try:
        import pandas as pd
        return isinstance(obj, pd.DataFrame)
    except Exception:
        return False


def _first_col(df: Any, names: tuple[str, ...]) -> str | None:
    try:
        cols = set(getattr(df, "columns", []))
        lower_map = {str(c).lower(): c for c in getattr(df, "columns", [])}
        for name in names:
            if name in cols:
                return name
            low = str(name).lower()
            if low in lower_map:
                return str(lower_map[low])
    except Exception:
        pass
    return None


def _normalize_memory_push_frame(df: Any, *, label: str) -> Any:
    try:
        import pandas as pd
        if not isinstance(df, pd.DataFrame) or df.empty:
            return pd.DataFrame()
        max_rows = _env_int("TONOSAMA_PUSH_MEMORY_HISTORY_MAX_ROWS", 20000)
        if len(df) > max_rows:
            df = df.tail(max_rows)
        x = df.copy()
        symbol_col = _first_col(x, ("symbol", "Symbol", "code", "Code", "銘柄コード", "stock_code"))
        price_col = _first_col(x, ("price", "current_price", "CurrentPrice", "close", "close_price", "last_price", "CurrentPrice"))
        dt_col = _first_col(x, ("datetime", "received_at", "timestamp", "time", "updated_at", "last_update", "DateTime"))
        if symbol_col is None or price_col is None or dt_col is None:
            return pd.DataFrame()

        x["symbol"] = x[symbol_col].astype(str).str.strip()
        x["datetime"] = pd.to_datetime(x[dt_col], errors="coerce")
        x["price"] = pd.to_numeric(x[price_col], errors="coerce")
        x = x.dropna(subset=["symbol", "datetime", "price"])
        x = x[(x["symbol"] != "") & (x["price"] > 0)].copy()
        if x.empty:
            return pd.DataFrame()

        lookback_min = max(5, _env_int("TONOSAMA_PUSH_MEMORY_HISTORY_LOOKBACK_MIN", 30))
        latest = x["datetime"].max()
        x = x[x["datetime"] >= latest - pd.Timedelta(minutes=lookback_min)].copy()
        if x.empty:
            return pd.DataFrame()

        vol_col = _first_col(x, ("volume", "Volume", "trading_volume", "cum_volume", "売買高", "TradingVolume"))
        tv_col = _first_col(x, ("trading_value", "TradingValue", "turnover", "売買代金"))
        name_col = _first_col(x, ("symbolname", "symbol_name", "name", "銘柄名", "SymbolName"))
        x["volume_raw"] = pd.to_numeric(x[vol_col], errors="coerce").fillna(0.0) if vol_col else 0.0
        x["trading_value_raw"] = pd.to_numeric(x[tv_col], errors="coerce").fillna(0.0) if tv_col else 0.0
        x["symbolname"] = x[name_col].astype(str) if name_col else ""
        x["slot"] = x["datetime"].dt.floor("1min")

        grouped = x.sort_values(["symbol", "slot", "datetime"]).groupby(["symbol", "slot"], sort=False)
        out = pd.DataFrame({
            "symbol": grouped["symbol"].last(),
            "symbolname": grouped["symbolname"].last(),
            "datetime": grouped["slot"].last(),
            "open": grouped["price"].first(),
            "high": grouped["price"].max(),
            "low": grouped["price"].min(),
            "close": grouped["price"].last(),
            "cum_volume": grouped["volume_raw"].max(),
            "cum_trading_value": grouped["trading_value_raw"].max(),
        }).reset_index(drop=True)
        if out.empty:
            return pd.DataFrame()

        out = out.sort_values(["symbol", "datetime"])
        out["volume"] = out.groupby("symbol")["cum_volume"].diff().fillna(0.0)
        out.loc[(out["volume"] < 0) | out["volume"].isna(), "volume"] = 0.0
        raw_positive = pd.to_numeric(out["cum_volume"], errors="coerce").fillna(0.0) > 0
        delta_positive = pd.to_numeric(out["volume"], errors="coerce").fillna(0.0) > 0
        if int(delta_positive.sum()) < max(1, int(raw_positive.sum() * 0.05)):
            out["volume"] = pd.to_numeric(out["cum_volume"], errors="coerce").fillna(0.0)
            volume_mode = "raw_max"
        else:
            volume_mode = "cum_diff"

        out["trading_value"] = out.groupby("symbol")["cum_trading_value"].diff().fillna(0.0)
        out.loc[out["trading_value"] < 0, "trading_value"] = 0.0
        out["price"] = out["close"]
        out["current_price"] = out["close"]
        out["open_price"] = out["open"]
        out["high_price"] = out["high"]
        out["low_price"] = out["low"]
        out["close_price"] = out["close"]
        out["interval"] = 1
        out["source"] = f"push_memory_history_1m:{label}"
        out = out.dropna(subset=["datetime", "close"]).reset_index(drop=True)
        if not out.empty:
            logger.warning(
                "[TONOSAMA PUSH MEMORY HISTORY] usable source=%s rows=%s symbols=%s latest=%s volume_nonzero=%s volume_mode=%s",
                label, len(out), out["symbol"].nunique(), out["datetime"].max(),
                int((pd.to_numeric(out.get("volume", 0), errors="coerce").fillna(0) > 0).sum()), volume_mode,
            )
        return out
    except Exception:
        logger.debug("[TONOSAMA PUSH MEMORY HISTORY] normalize failed source=%s", label, exc_info=True)
        try:
            import pandas as pd
            return pd.DataFrame()
        except Exception:
            return None


def _candidate_memory_frames() -> list[tuple[str, Any]]:
    frames: list[tuple[str, Any]] = []
    for mod_name in (
        "trading.push.push_stream.monitor",
        "trading.push.push_stream",
        "trading.push.push_manager",
        "global_data",
        "global_state",
        "core.global_context.context",
    ):
        try:
            mod = __import__(mod_name, fromlist=["*"])
        except Exception:
            continue
        try:
            for attr, val in vars(mod).items():
                if _is_df_like(val):
                    frames.append((f"{mod_name}.{attr}", val))
                elif attr in {"global_data", "global_context", "GC"}:
                    try:
                        for sub_attr, sub_val in vars(val).items():
                            if _is_df_like(sub_val):
                                frames.append((f"{mod_name}.{attr}.{sub_attr}", sub_val))
                    except Exception:
                        pass
        except Exception:
            pass
    for mod_name, mod in list(sys.modules.items()):
        if mod is None:
            continue
        low = str(mod_name).lower()
        if not any(k in low for k in ("push", "global_context", "global_data")):
            continue
        try:
            for attr, val in vars(mod).items():
                if _is_df_like(val):
                    frames.append((f"{mod_name}.{attr}", val))
        except Exception:
            continue
    return frames


def _load_push_memory_1m_history() -> Any:
    try:
        import pandas as pd
        if not _env_on("TONOSAMA_PUSH_MEMORY_HISTORY_ENABLED", True):
            return pd.DataFrame()
        parts = []
        for label, frame in _candidate_memory_frames():
            norm = _normalize_memory_push_frame(frame, label=label)
            if isinstance(norm, pd.DataFrame) and not norm.empty:
                parts.append(norm)
        if not parts:
            return pd.DataFrame()
        out = pd.concat(parts, ignore_index=True, sort=False)
        out = out.drop_duplicates(subset=["symbol", "datetime"], keep="last").sort_values(["symbol", "datetime"])
        nonzero = int((pd.to_numeric(out.get("volume", 0), errors="coerce").fillna(0) > 0).sum()) if "volume" in out.columns else 0
        if nonzero < _env_int("TONOSAMA_PUSH_MEMORY_HISTORY_MIN_VOLUME_NONZERO", 1):
            logger.warning("[TONOSAMA PUSH MEMORY HISTORY] rejected no usable volume rows=%s symbols=%s nonzero=%s", len(out), out["symbol"].nunique() if "symbol" in out.columns else 0, nonzero)
            return pd.DataFrame()
        logger.warning("[TONOSAMA PUSH MEMORY HISTORY] loaded rows=%s symbols=%s latest=%s volume_nonzero=%s", len(out), out["symbol"].nunique() if "symbol" in out.columns else 0, out["datetime"].max() if "datetime" in out.columns else None, nonzero)
        return out.reset_index(drop=True)
    except Exception:
        logger.debug("[TONOSAMA PUSH MEMORY HISTORY] load failed", exc_info=True)
        try:
            import pandas as pd
            return pd.DataFrame()
        except Exception:
            return None


def _qident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _candidate_push_db_paths(now: dt.datetime | None = None) -> list[str]:
    now = now or dt.datetime.now()
    ymds = [now.strftime("%Y%m%d")]
    paths: list[str] = []
    for key in (
        "PUSH_DB_PATH", "PUSH_RAW_DB_PATH", "KABU_PUSH_DB_PATH", "SUMMARY_PUSH_RAW_DB_PATH",
    ):
        v = os.getenv(key)
        if v:
            paths.append(v)
    dirs = []
    for key in (
        "PUSH_DB_DIR", "PUSH_RAW_DB_DIR", "KABU_PUSH_DB_DIR", "SUMMARY_PUSH_RAW_DB_DIR", "AUTOSTOCK_PUSH_DB_DIR",
    ):
        v = os.getenv(key)
        if v:
            dirs.append(v)
    dirs.extend([
        r"\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\push",
        r"\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station",
        r"\\192.168.0.22\AutoStockBuyAndSell\raw_data",
        os.path.join(os.getcwd(), "raw_data", "kabu_station", "push"),
        os.path.join(os.getcwd(), "raw_data", "kabu_station"),
        os.getcwd(),
    ])
    for d in dirs:
        if not d:
            continue
        for ymd in ymds:
            paths.append(os.path.join(d, f"push{ymd}.db"))
        try:
            paths.extend(glob.glob(os.path.join(d, "push*.db")))
        except Exception:
            pass
    out = []
    seen = set()
    for p in paths:
        try:
            p2 = os.path.abspath(os.path.expanduser(str(p))) if not str(p).startswith("\\\\") else str(p)
            if p2 not in seen and os.path.exists(p2):
                seen.add(p2)
                out.append(p2)
        except Exception:
            continue
    return out


def _read_push_db_1m(path: str, *, now: dt.datetime | None = None) -> Any:
    try:
        import pandas as pd
        raw_limit = max(500, _env_int("SUMMARY_AI_FRESH_RAW_DB_READ_LIMIT", 8000))
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1.0)
        try:
            tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            parts = []
            for table in tables:
                try:
                    info = con.execute(f"PRAGMA table_info({_qident(table)})").fetchall()
                    cols = [str(r[1]) for r in info]
                    if not cols:
                        continue
                    cols_l = {c.lower(): c for c in cols}
                    def pick(names: tuple[str, ...]) -> str | None:
                        for n in names:
                            if n in cols:
                                return n
                            if n.lower() in cols_l:
                                return cols_l[n.lower()]
                        return None
                    sym = pick(("symbol", "code", "stock_code", "Symbol", "銘柄コード"))
                    dcol = pick(("datetime", "received_at", "timestamp", "time", "updated_at", "DateTime"))
                    price = pick(("price", "current_price", "CurrentPrice", "close", "close_price", "last_price"))
                    if not (sym and dcol and price):
                        continue
                    vol = pick(("volume", "Volume", "trading_volume", "cum_volume", "TradingVolume", "売買高"))
                    tv = pick(("trading_value", "TradingValue", "turnover", "売買代金"))
                    name = pick(("symbolname", "symbol_name", "name", "SymbolName", "銘柄名"))
                    select = [f"{_qident(sym)} AS symbol", f"{_qident(dcol)} AS datetime", f"{_qident(price)} AS price"]
                    if vol:
                        select.append(f"{_qident(vol)} AS volume")
                    if tv:
                        select.append(f"{_qident(tv)} AS trading_value")
                    if name:
                        select.append(f"{_qident(name)} AS symbolname")
                    sql = f"SELECT {', '.join(select)} FROM {_qident(table)} ORDER BY {_qident(dcol)} DESC LIMIT {int(raw_limit)}"
                    df = pd.read_sql_query(sql, con)
                    norm = _normalize_memory_push_frame(df, label=f"rawdb:{os.path.basename(path)}:{table}")
                    if isinstance(norm, pd.DataFrame) and not norm.empty:
                        parts.append(norm)
                except Exception:
                    logger.debug("[SUMMARY AI FRESH RAW OVERLAY] table read skipped db=%s table=%s", path, table, exc_info=True)
            if not parts:
                return pd.DataFrame()
            out = pd.concat(parts, ignore_index=True, sort=False)
            out = out.drop_duplicates(subset=["symbol", "datetime"], keep="last").sort_values(["symbol", "datetime"])
            logger.warning("[SUMMARY AI FRESH RAW OVERLAY] loaded raw db path=%s rows=%s symbols=%s latest=%s", path, len(out), out["symbol"].nunique(), out["datetime"].max())
            return out.reset_index(drop=True)
        finally:
            try:
                con.close()
            except Exception:
                pass
    except Exception:
        logger.debug("[SUMMARY AI FRESH RAW OVERLAY] db read failed path=%s", path, exc_info=True)
        try:
            import pandas as pd
            return pd.DataFrame()
        except Exception:
            return None


def _load_fresh_raw_1m(now: dt.datetime | None = None) -> Any:
    try:
        import pandas as pd
        candidates = []
        mem = _load_push_memory_1m_history()
        if isinstance(mem, pd.DataFrame) and not mem.empty:
            candidates.append(("memory", mem))
        for p in _candidate_push_db_paths(now):
            db = _read_push_db_1m(p, now=now)
            if isinstance(db, pd.DataFrame) and not db.empty:
                candidates.append((f"db:{p}", db))
        if not candidates:
            return pd.DataFrame()
        best_label, best = max(candidates, key=lambda x: pd.to_datetime(x[1]["datetime"], errors="coerce").max())
        logger.warning("[SUMMARY AI FRESH RAW OVERLAY] selected source=%s rows=%s symbols=%s latest=%s", best_label, len(best), best["symbol"].nunique(), best["datetime"].max())
        return best.reset_index(drop=True)
    except Exception:
        logger.debug("[SUMMARY AI FRESH RAW OVERLAY] load fresh raw failed", exc_info=True)
        try:
            import pandas as pd
            return pd.DataFrame()
        except Exception:
            return None


def _latest_dt(df: Any) -> Any:
    try:
        import pandas as pd
        if not isinstance(df, pd.DataFrame) or df.empty or "datetime" not in df.columns:
            return None
        s = pd.to_datetime(df["datetime"], errors="coerce")
        if s.notna().any():
            return s.max()
    except Exception:
        pass
    return None


def _overlay_summary_ai_df_with_fresh_raw(df: Any, *, interval: int, now: dt.datetime | None = None) -> Any:
    try:
        import pandas as pd
        if not _env_on("SUMMARY_AI_FRESH_RAW_OVERLAY_ENABLED", True):
            return df
        if int(interval) != 1 or not isinstance(df, pd.DataFrame) or df.empty or "symbol" not in df.columns:
            return df
        now = now or dt.datetime.now()
        stale_sec = _env_float("SUMMARY_AI_FRESH_RAW_OVERLAY_STALE_SEC", 180.0)
        max_age_sec = _env_float("SUMMARY_AI_FRESH_RAW_OVERLAY_MAX_AGE_SEC", 180.0)
        cur_latest = _latest_dt(df)
        if cur_latest is not None:
            age = abs((pd.Timestamp(now) - pd.Timestamp(cur_latest).tz_localize(None)).total_seconds())
            if age <= stale_sec:
                return df
        raw = _load_fresh_raw_1m(now)
        if not isinstance(raw, pd.DataFrame) or raw.empty:
            logger.warning("[SUMMARY AI FRESH RAW OVERLAY] skipped no fresh raw cur_latest=%s", cur_latest)
            return df
        raw_latest = _latest_dt(raw)
        if raw_latest is None:
            return df
        raw_age = abs((pd.Timestamp(now) - pd.Timestamp(raw_latest).tz_localize(None)).total_seconds())
        if raw_age > max_age_sec:
            logger.warning("[SUMMARY AI FRESH RAW OVERLAY] skipped raw stale raw_latest=%s age=%.1fs max=%.1fs", raw_latest, raw_age, max_age_sec)
            return df

        latest_raw = raw.sort_values(["symbol", "datetime"]).groupby("symbol", as_index=False).tail(1).copy()
        latest_raw = latest_raw[[c for c in latest_raw.columns if c in {
            "symbol", "symbolname", "datetime", "open", "high", "low", "close", "price", "current_price",
            "open_price", "high_price", "low_price", "close_price", "volume", "trading_value", "turnover",
        }]].copy()
        if latest_raw.empty:
            return df

        out = df.copy()
        out["symbol"] = out["symbol"].astype(str).str.strip()
        latest_raw["symbol"] = latest_raw["symbol"].astype(str).str.strip()
        merged = out.merge(latest_raw.add_suffix("_freshraw"), left_on="symbol", right_on="symbol_freshraw", how="left")
        hit = merged["datetime_freshraw"].notna() if "datetime_freshraw" in merged.columns else pd.Series(False, index=merged.index)
        for col in ("datetime", "open", "high", "low", "close", "price", "current_price", "open_price", "high_price", "low_price", "close_price", "volume", "trading_value", "turnover", "symbolname"):
            fcol = f"{col}_freshraw"
            if fcol not in merged.columns:
                continue
            if col not in merged.columns:
                merged[col] = merged[fcol]
            else:
                merged.loc[hit, col] = merged.loc[hit, fcol]
        drop_cols = [c for c in merged.columns if str(c).endswith("_freshraw")]
        merged = merged.drop(columns=drop_cols, errors="ignore")
        merged["summary_ai_fresh_raw_overlay"] = hit.astype(bool)
        merged["summary_ai_fresh_raw_latest"] = raw_latest
        merged["source"] = "summary_ai_fresh_raw_overlay"
        logger.warning(
            "[SUMMARY AI FRESH RAW OVERLAY] applied interval=%s rows=%s hit=%s cur_latest=%s raw_latest=%s raw_age=%.1fs score_nonzero=%s buy_nonzero=%s sell_nonzero=%s",
            interval, len(merged), int(hit.sum()), cur_latest, raw_latest, raw_age,
            _nonzero_count(merged, "score"), _nonzero_count(merged, "score_buy") or _nonzero_count(merged, "buy_score"),
            _nonzero_count(merged, "score_sell") or _nonzero_count(merged, "sell_score"),
        )
        _publish_push_merged_summary(merged, interval=interval)
        return merged.reset_index(drop=True)
    except Exception:
        logger.exception("[SUMMARY AI FRESH RAW OVERLAY] overlay failed interval=%s", interval)
        return df


def _nonzero_count(df: Any, col: str) -> int:
    try:
        import pandas as pd
        if col not in df.columns:
            return 0
        return int((pd.to_numeric(df[col], errors="coerce").fillna(0).abs() > 0).sum())
    except Exception:
        return 0


def _publish_push_merged_summary(df: Any, *, interval: int) -> None:
    try:
        if int(interval) != 1 or not _is_df_like(df):
            return
        import core.global_context.context as gc
        for name in ("set_merged_summary", "set_push_merged_summary", "set_summary", "update_merged_summary"):
            fn = getattr(gc, name, None)
            if not callable(fn):
                continue
            for kwargs in (
                {"tf": 1, "source": "push", "df": df},
                {"interval": 1, "source": "push", "df": df},
                {"interval": 1, "df": df},
                {"df": df},
            ):
                try:
                    fn(**kwargs)
                    logger.warning("[SUMMARY AI FRESH RAW OVERLAY] published to global_context via %s kwargs=%s rows=%s", name, list(kwargs.keys()), len(df))
                    return
                except TypeError:
                    continue
                except Exception:
                    logger.debug("[SUMMARY AI FRESH RAW OVERLAY] publish failed fn=%s kwargs=%s", name, kwargs, exc_info=True)
    except Exception:
        logger.debug("[SUMMARY AI FRESH RAW OVERLAY] publish failed", exc_info=True)


__all__ = [
    "VERSION",
    "load_push_memory_1m_history",
    "load_fresh_raw_1m",
    "overlay_summary_ai_df_with_fresh_raw",
    "publish_push_merged_summary",
]


# 公開名 (旧パッチ内では _ 付きの内部名だった)
load_push_memory_1m_history = _load_push_memory_1m_history
load_fresh_raw_1m = _load_fresh_raw_1m
overlay_summary_ai_df_with_fresh_raw = _overlay_summary_ai_df_with_fresh_raw
publish_push_merged_summary = _publish_push_merged_summary
