# ============================================================
# File   : core/startup/push_bootstrap_fast_restore_patch.py
# Version: Ver02-LIGHTWEIGHT-PUSH-BOOTSTRAP-MEMORY-PUBLISH
# ------------------------------------------------------------
# main.py 起動時の pushDB 復元が NAS + SELECT * + 8000行で重い問題を回避する。
#
# 症状:
#   pushDB recent restore query rows=7619 ... elapsed=91.440s
#   ただし復元後に main.py 側の push_df / PUSH memory が空のままになり、
#   [SUMMARY MAIN MEMORY 1M] no usable PUSH memory rows raw_rows=0
#   [PUSH SUMMARY ENGINE] resolved push source | rows=0
#   となって summary / merged_summary が空になる。
#
# 方針:
#   - main.py の起動復元では、サマリー計算に必要な最小列だけ読む
#   - 板10本・raw_json・IV/Greek等は起動復元から除外
#   - 行数/Lookbackを短縮
#   - 失敗時のみ元の _load_push_db にフォールバック
#   - bootstrap_push() 後に global_data / GlobalContext へ push_df を強制 publish
#   - global_state.GlobalDataCompat.get_push_df() が GC.get_push_df() を見ない旧実装でも、
#     runtime monkey patch で non-empty PUSH memory を返す
#
# ENV:
#   PUSH_BOOTSTRAP_FAST_RESTORE=1
#   PUSH_BOOTSTRAP_FAST_MAX_ROWS=3000
#   PUSH_BOOTSTRAP_FAST_LOOKBACK_MINUTES=45
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

_INSTALLED = False
_ORIG_LOAD = None
_ORIG_BOOTSTRAP = None
_ORIG_GET_PUSH_DF = None

_MIN_COL_CANDIDATES = [
    "symbol", "symbolname", "datetime", "date", "time",
    "price", "current_price", "close", "close_price",
    "volume", "trading_value", "turnover", "turnover_yen",
    "vwap", "previousclose", "opening_price", "high_price", "low_price",
]

_DT_COL_CANDIDATES = ["datetime", "timestamp", "current_price_time", "received_at", "inserted_at", "time"]


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok"}
    except Exception:
        return bool(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return max(1, int(float(v)))
    except Exception:
        return int(default)


def _table_cols(conn: sqlite3.Connection, table: str = "stream_data") -> list[str]:
    try:
        return [str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall() if len(r) >= 2]
    except Exception:
        return []


def _q(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _select_cols(cols: list[str]) -> list[str]:
    lower_to_real = {c.lower(): c for c in cols}
    out: list[str] = []
    for c in _MIN_COL_CANDIDATES:
        real = lower_to_real.get(c.lower())
        if real and real not in out:
            out.append(real)
    # normalize側が boardを必要とする場面もあるが、起動復元では重くなるため板列は読まない。
    return out


def _dt_col(cols: list[str]) -> str | None:
    lower_to_real = {c.lower(): c for c in cols}
    for c in _DT_COL_CANDIDATES:
        if c.lower() in lower_to_real:
            return lower_to_real[c.lower()]
    return None


def _df_rows(value: Any) -> int:
    try:
        return int(len(value)) if isinstance(value, pd.DataFrame) else 0
    except Exception:
        return 0


def _is_nonempty_df(value: Any) -> bool:
    return isinstance(value, pd.DataFrame) and not value.empty


def _copy_df(value: Any) -> pd.DataFrame:
    try:
        return value.copy() if isinstance(value, pd.DataFrame) else pd.DataFrame()
    except Exception:
        return pd.DataFrame()


def _normalize_publish_df(df: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame()
    out = df.copy()
    try:
        out.columns = [str(c).strip().lower() for c in out.columns]
    except Exception:
        pass

    if "symbol" in out.columns:
        try:
            out["symbol"] = (
                out["symbol"]
                .astype(str)
                .str.strip()
                .str.upper()
                .str.replace(r"\.T$", "", regex=True)
                .str.replace(r"\.0$", "", regex=True)
            )
        except Exception:
            pass

    if "price" not in out.columns:
        for c in ("current_price", "close", "close_price", "last_price"):
            if c in out.columns:
                out["price"] = out[c]
                break
    if "current_price" not in out.columns and "price" in out.columns:
        out["current_price"] = out["price"]
    if "close" not in out.columns and "price" in out.columns:
        out["close"] = out["price"]

    if "datetime" in out.columns:
        try:
            out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
            try:
                out["datetime"] = out["datetime"].dt.tz_localize(None)
            except Exception:
                pass
        except Exception:
            pass

    if "symbol" in out.columns:
        out = out[out["symbol"].fillna("").astype(str).str.strip() != ""].copy()
    if "price" in out.columns:
        try:
            price = pd.to_numeric(out["price"], errors="coerce")
            out = out[price.fillna(0) > 0].copy()
        except Exception:
            pass
    return out.reset_index(drop=True)


def _publish_push_df(df: pd.DataFrame, *, reason: str) -> bool:
    """
    PUSH復元DFを main.py 内の全互換キャッシュへ publish する。
    summary_main_memory_latest_1m_patch / push_summary_engine は参照先が複数あるため、
    push_df だけでなく raw/latest 系キーにも入れておく。
    """
    out = _normalize_publish_df(df)
    if out.empty:
        logger.warning("[PUSH BOOTSTRAP FAST] publish skipped empty reason=%s input_rows=%s", reason, _df_rows(df))
        return False

    gd = None
    gc = None
    try:
        from global_state import global_data as gd  # type: ignore
    except Exception:
        gd = None
    try:
        from core.global_context.context import global_context as gc  # type: ignore
    except Exception:
        gc = None

    for obj_name, obj in (("global_data", gd), ("global_context", gc)):
        if obj is None:
            continue
        for attr in ("push_df", "push_raw_df", "latest_push_df", "stream_data", "push_snapshot_df"):
            try:
                setattr(obj, attr, out.copy())
            except Exception:
                pass
        for method_name in ("set_push_df", "set_raw_push_df", "set_latest_push_df"):
            try:
                fn = getattr(obj, method_name, None)
                if callable(fn):
                    fn(out.copy())
            except Exception:
                logger.debug("[PUSH BOOTSTRAP FAST] %s.%s publish failed", obj_name, method_name, exc_info=True)

    latest = None
    try:
        latest = out["datetime"].max() if "datetime" in out.columns else None
    except Exception:
        latest = None

    logger.warning(
        "[PUSH BOOTSTRAP FAST] published push memory reason=%s rows=%s symbols=%s latest=%s",
        reason,
        len(out),
        int(out["symbol"].nunique()) if "symbol" in out.columns else 0,
        latest,
    )
    return True


def _install_global_data_getter_patch() -> bool:
    """
    global_state.GlobalDataCompat.get_push_df() の旧実装は GC.get_push_df() を見ず、
    GC.summary / GC.push.snapshot() だけを見て empty を返す場合がある。
    main.py の PUSH memory bridge が切れないよう、non-empty 候補を順に返す。
    """
    global _ORIG_GET_PUSH_DF
    try:
        import global_state

        gd = getattr(global_state, "global_data", None)
        if gd is None:
            return False
        cur = getattr(gd, "get_push_df", None)
        if getattr(cur, "_push_memory_getter_v2", False):
            return True
        _ORIG_GET_PUSH_DF = cur

        def _patched_get_push_df():
            candidates: list[Any] = []
            for attr in ("push_df", "push_raw_df", "latest_push_df", "stream_data", "push_snapshot_df"):
                try:
                    value = getattr(gd, attr, None)
                    if value is not None:
                        candidates.append(value)
                except Exception:
                    pass
            try:
                from core.global_context.context import global_context as gc
                for attr in ("push_df", "push_raw_df", "latest_push_df", "stream_data", "push_snapshot_df"):
                    try:
                        value = getattr(gc, attr, None)
                        if value is not None:
                            candidates.append(value)
                    except Exception:
                        pass
                try:
                    fn = getattr(gc, "get_push_df", None)
                    if callable(fn):
                        candidates.append(fn())
                except Exception:
                    pass
            except Exception:
                pass
            if callable(_ORIG_GET_PUSH_DF):
                try:
                    candidates.append(_ORIG_GET_PUSH_DF())
                except Exception:
                    pass
            for value in candidates:
                if isinstance(value, pd.DataFrame) and not value.empty:
                    return value.copy()
            return pd.DataFrame()

        _patched_get_push_df._push_memory_getter_v2 = True  # type: ignore[attr-defined]
        gd.get_push_df = _patched_get_push_df
        logger.warning("[PUSH BOOTSTRAP FAST] global_data.get_push_df patched for memory bridge")
        return True
    except Exception as e:
        logger.warning("[PUSH BOOTSTRAP FAST] get_push_df patch failed err=%s", e, exc_info=True)
        return False


def _fast_load_push_db(db_path: str) -> pd.DataFrame:
    if not _env_bool("PUSH_BOOTSTRAP_FAST_RESTORE", True):
        if callable(_ORIG_LOAD):
            return _ORIG_LOAD(db_path)
        return pd.DataFrame()

    started = dt.datetime.now()
    max_rows = _env_int("PUSH_BOOTSTRAP_FAST_MAX_ROWS", 3000)
    lookback_min = _env_int("PUSH_BOOTSTRAP_FAST_LOOKBACK_MINUTES", 45)
    cutoff_dt = started - dt.timedelta(minutes=lookback_min)
    cutoff_text = cutoff_dt.strftime("%Y-%m-%d %H:%M:%S")

    try:
        with sqlite3.connect(db_path, timeout=3.0) as conn:
            try:
                conn.execute("PRAGMA busy_timeout=3000")
                conn.execute("PRAGMA query_only=ON")
            except Exception:
                pass

            cols = _table_cols(conn, "stream_data")
            selected = _select_cols(cols)
            dcol = _dt_col(cols)
            if not selected:
                raise RuntimeError("no selectable columns")

            select_sql = ", ".join(_q(c) for c in selected)
            if dcol and dcol.lower() != "time":
                sql = f"""
                    SELECT {select_sql}
                    FROM stream_data
                    WHERE {_q(dcol)} >= ?
                    ORDER BY rowid DESC
                    LIMIT ?
                """
                params: tuple[Any, ...] = (cutoff_text, max_rows)
                df = pd.read_sql(sql, conn, params=params)
                mode = f"dt_col={dcol} cutoff={cutoff_text}"
            else:
                sql = f"""
                    SELECT {select_sql}
                    FROM stream_data
                    ORDER BY rowid DESC
                    LIMIT ?
                """
                params = (max_rows,)
                df = pd.read_sql(sql, conn, params=params)
                mode = "latest_rowid"

        logger.warning(
            "[PUSH BOOTSTRAP FAST] restore done rows=%s cols=%s max_rows=%s lookback=%s mode=%s elapsed=%.3fs db=%s",
            len(df) if isinstance(df, pd.DataFrame) else 0,
            list(df.columns) if isinstance(df, pd.DataFrame) else [],
            max_rows,
            lookback_min,
            mode,
            (dt.datetime.now() - started).total_seconds(),
            db_path,
        )
        return df if isinstance(df, pd.DataFrame) else pd.DataFrame()

    except Exception as e:
        logger.warning("[PUSH BOOTSTRAP FAST] failed err=%s -> fallback original", e, exc_info=False)
        if callable(_ORIG_LOAD):
            return _ORIG_LOAD(db_path)
        return pd.DataFrame()


def _install_bootstrap_publish_wrapper(pb) -> bool:
    global _ORIG_BOOTSTRAP
    try:
        cur = getattr(pb, "bootstrap_push", None)
        if not callable(cur):
            return False
        if getattr(cur, "_push_bootstrap_publish_wrapper_v2", False):
            return True
        _ORIG_BOOTSTRAP = cur

        def _wrapped_bootstrap_push(push_dir: str):
            ret = None
            if callable(_ORIG_BOOTSTRAP):
                ret = _ORIG_BOOTSTRAP(push_dir)

            df = pd.DataFrame()
            try:
                gd = getattr(pb, "global_data", None)
                for attr in ("push_df", "push_raw_df", "latest_push_df", "stream_data", "push_snapshot_df"):
                    value = getattr(gd, attr, None) if gd is not None else None
                    if _is_nonempty_df(value):
                        df = _copy_df(value)
                        break
            except Exception:
                pass

            if df.empty:
                try:
                    today_str = dt.datetime.now().strftime("%Y%m%d")
                    db_path = os.path.join(push_dir, f"push{today_str}.db")
                    if os.path.exists(db_path):
                        raw = _fast_load_push_db(db_path)
                        normalizer = getattr(pb, "_normalize_push_df_for_summary", None)
                        if callable(normalizer):
                            df = normalizer(raw)
                        else:
                            df = raw
                except Exception:
                    logger.warning("[PUSH BOOTSTRAP FAST] publish wrapper reload failed", exc_info=True)

            _publish_push_df(df, reason="bootstrap_wrapper")
            return ret

        _wrapped_bootstrap_push._push_bootstrap_publish_wrapper_v2 = True  # type: ignore[attr-defined]
        pb.bootstrap_push = _wrapped_bootstrap_push
        logger.warning("[PUSH BOOTSTRAP FAST] bootstrap_push memory publish wrapper installed")
        return True
    except Exception as e:
        logger.warning("[PUSH BOOTSTRAP FAST] bootstrap wrapper install failed err=%s", e, exc_info=True)
        return False


def install() -> bool:
    global _INSTALLED, _ORIG_LOAD
    if _INSTALLED:
        return True
    try:
        import core.startup.push_bootstrap as pb
        cur = getattr(pb, "_load_push_db", None)
        if not getattr(cur, "_push_bootstrap_fast_restore_v2", False):
            _ORIG_LOAD = cur
            _fast_load_push_db._push_bootstrap_fast_restore_v2 = True  # type: ignore[attr-defined]
            pb._load_push_db = _fast_load_push_db
        _install_global_data_getter_patch()
        _install_bootstrap_publish_wrapper(pb)
        _INSTALLED = True
        logger.warning(
            "[PUSH BOOTSTRAP FAST] installed enabled=%s max_rows=%s lookback=%s memory_publish=True",
            _env_bool("PUSH_BOOTSTRAP_FAST_RESTORE", True),
            _env_int("PUSH_BOOTSTRAP_FAST_MAX_ROWS", 3000),
            _env_int("PUSH_BOOTSTRAP_FAST_LOOKBACK_MINUTES", 45),
        )
        return True
    except Exception as e:
        logger.exception("[PUSH BOOTSTRAP FAST] install failed err=%s", e)
        return False


try:
    install()
except Exception as e:
    logger.exception("[PUSH BOOTSTRAP FAST] auto install failed err=%s", e)

__all__ = ["install"]
