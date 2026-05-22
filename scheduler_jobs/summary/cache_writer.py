# ============================================================
# File   : scheduler_jobs/summary/cache_writer.py
# Ver    : PRODUCTION-STABLE-SUMMARY-CACHE-WRITER-V1.5-MAIN-ENTRY-ONLY-SKIP-DB
# ------------------------------------------------------------
# ✔ merged cache 保存
# ✔ uncomputed DF の cache 汚染防止
# ✔ summary DB upsert を明示実行
# ✔ DB保存後に global_data cache 保存
# ✔ PUSH / RANKING の source を保持
# ✔ 例外安全化
# ✔ interval=1 のDB保存ロック詰まり対策
# ✔ bulk_upsert_summary に lock_timeout_sec / skip_if_busy / latest_only を渡す
# ✔ lock_timeout 旧名を使わず、summary_saver_bulk の正式名 lock_timeout_sec に統一
# ✔ 関数内部の TypeError を signature 不明と誤判定しない
# ✔ fallback 呼び出しでも interval を positional で渡して欠落を防ぐ
# ✔ main.py entry_only 実行時は DB upsert だけスキップし、cache保存は継続
# ============================================================

from __future__ import annotations

import inspect
import logging
import os
from typing import Any

import pandas as pd

from .display_prepare import latest_dt_str
from .quality_guards import looks_uncomputed_push_df, looks_uncomputed_ranking_df

logger = logging.getLogger(__name__)

try:
    from global_state import global_data  # type: ignore
except Exception:
    try:
        from core.global_context import global_data  # type: ignore
    except Exception:
        class _FallbackGlobalData:
            pass

        global_data = _FallbackGlobalData()


# ============================================================
# helpers
# ============================================================

def _env_bool(name: str, default: bool = False) -> bool:
    try:
        v = os.environ.get(name)
        if v is None:
            return bool(default)
        return str(v).strip().lower() in ("1", "true", "yes", "on", "y")
    except Exception:
        return bool(default)


def _skip_db_save_for_entry_only_main() -> bool:
    """
    main.py はサマリー計算結果をエントリー判定・表示・cache用に使うだけ。
    正式な summary DB 保存は main_database.py 側へ寄せる。

    main.py で以下が設定されている場合だけ DB upsert を止める:
      SUMMARY_SKIP_DB_SAVE_IN_MAIN=1
      SUMMARY_MAIN_ENTRY_ONLY=1
      SUMMARY_DB_WRITER_ROLE=entry_only
    """
    if _env_bool("SUMMARY_SKIP_DB_SAVE_IN_MAIN", False):
        return True
    if _env_bool("SUMMARY_MAIN_ENTRY_ONLY", False):
        return True
    role = str(os.environ.get("SUMMARY_DB_WRITER_ROLE") or "").strip().lower()
    return role in ("entry_only", "main_entry_only", "read_only", "no_save")


def _safe_rows(df: Any) -> int:
    try:
        return len(df) if isinstance(df, pd.DataFrame) else 0
    except Exception:
        return 0


def _safe_symbols(df: Any) -> int:
    try:
        if isinstance(df, pd.DataFrame) and not df.empty and "symbol" in df.columns:
            return int(df["symbol"].astype(str).nunique())
    except Exception:
        pass
    return 0


def _safe_latest_dt(df: Any):
    try:
        if not isinstance(df, pd.DataFrame) or df.empty:
            return None

        if "datetime" in df.columns:
            s = pd.to_datetime(df["datetime"], errors="coerce").dropna()
            if not s.empty:
                return s.max()

        for c in ("dt", "timestamp", "end_time", "snapshot_time"):
            if c in df.columns:
                s = pd.to_datetime(df[c], errors="coerce").dropna()
                if not s.empty:
                    return s.max()

    except Exception:
        pass

    return None


def _normalize_source(source: str) -> str:
    s = str(source or "").strip().lower()
    if s in ("push", "summary", "push_summary", "push-stream", "push_stream"):
        return "push"
    if s in ("ranking", "ranking_summary"):
        return "ranking"
    return s or "unknown"


def _call_with_supported_kwargs(func: Any, *args: Any, **kwargs: Any) -> Any:
    try:
        sig = inspect.signature(func)
    except (TypeError, ValueError):
        logger.debug(
            "[summary.cache_writer] signature unavailable func=%s; call with original args/kwargs",
            getattr(func, "__name__", str(func)),
            exc_info=True,
        )
        return func(*args, **kwargs)

    params = sig.parameters
    accepts_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())

    if accepts_var_kw:
        return func(*args, **kwargs)

    filtered = {k: v for k, v in kwargs.items() if k in params}
    dropped = sorted(set(kwargs) - set(filtered))

    if dropped:
        logger.info(
            "[summary.cache_writer] dropped unsupported kwargs func=%s dropped=%s",
            getattr(func, "__name__", str(func)),
            dropped,
        )

    return func(*args, **filtered)


def _db_upsert_options(interval: int, source: str) -> dict[str, Any]:
    interval = int(interval)
    source = _normalize_source(source)

    if interval == 1:
        return {
            "lock_timeout_sec": 3.0,
            "skip_if_busy": True,
            "latest_only": True,
            "save_reason": f"cache_writer_{source}",
        }

    if interval == 3:
        return {
            "lock_timeout_sec": 8.0,
            "skip_if_busy": True,
            "latest_only": False,
            "save_reason": f"cache_writer_{source}",
        }

    if interval == 5:
        return {
            "lock_timeout_sec": 10.0,
            "skip_if_busy": True,
            "latest_only": False,
            "save_reason": f"cache_writer_{source}",
        }

    return {
        "lock_timeout_sec": 5.0,
        "skip_if_busy": True,
        "latest_only": False,
        "save_reason": f"cache_writer_{source}",
    }


def _prepare_df_for_db(df: pd.DataFrame, interval: int, source: str) -> pd.DataFrame:
    out = df.copy()

    if "datetime" not in out.columns:
        for c in ("dt", "timestamp", "end_time", "snapshot_time"):
            if c in out.columns:
                out["datetime"] = out[c]
                break

    if "datetime" in out.columns:
        out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
        try:
            out["datetime"] = out["datetime"].dt.tz_localize(None)
        except Exception:
            pass

    if "symbol" in out.columns:
        out["symbol"] = out["symbol"].astype(str).str.strip()

    before = len(out)

    if "symbol" in out.columns and "datetime" in out.columns:
        out = out.dropna(subset=["symbol", "datetime"]).copy()
        out = out[out["symbol"].astype(str).str.strip() != ""].copy()

    after = len(out)
    if after != before:
        logger.warning(
            "[summary.cache_writer] db prepare dropped rows interval=%s source=%s before=%s after=%s",
            interval,
            source,
            before,
            after,
        )

    if out.empty:
        return out.reset_index(drop=True)

    if "datetime" in out.columns:
        dt_ser = pd.to_datetime(out["datetime"], errors="coerce")
        out["datetime"] = dt_ser.dt.strftime("%Y-%m-%d %H:%M:%S")

        if "date" not in out.columns:
            out["date"] = dt_ser.dt.strftime("%Y-%m-%d")
        else:
            mask = out["date"].isna() | (out["date"].astype(str).str.strip() == "")
            if mask.any():
                out.loc[mask, "date"] = dt_ser.loc[mask].dt.strftime("%Y-%m-%d")

        if "time" not in out.columns:
            out["time"] = dt_ser.dt.strftime("%H:%M:%S")
        else:
            mask = out["time"].isna() | (out["time"].astype(str).str.strip() == "")
            if mask.any():
                out.loc[mask, "time"] = dt_ser.loc[mask].dt.strftime("%H:%M:%S")

        if "time_range" not in out.columns:
            out["time_range"] = dt_ser.dt.strftime("%H:%M")
        else:
            mask = out["time_range"].isna() | (out["time_range"].astype(str).str.strip() == "")
            if mask.any():
                out.loc[mask, "time_range"] = dt_ser.loc[mask].dt.strftime("%H:%M")

    if "source" not in out.columns:
        out["source"] = source
    else:
        mask = out["source"].isna() | (out["source"].astype(str).str.strip() == "")
        if mask.any():
            out.loc[mask, "source"] = source

    if "interval" not in out.columns:
        out["interval"] = int(interval)
    else:
        mask = out["interval"].isna() | (out["interval"].astype(str).str.strip() == "")
        if mask.any():
            out.loc[mask, "interval"] = int(interval)

    alias_pairs = {
        "open": "open_price",
        "high": "high_price",
        "low": "low_price",
        "close": "close_price",
    }

    for src, dst in alias_pairs.items():
        if src in out.columns and dst not in out.columns:
            out[dst] = out[src]
        elif dst in out.columns and src not in out.columns:
            out[src] = out[dst]

    if "symbol" in out.columns and "datetime" in out.columns:
        before_dedupe = len(out)
        out = out.sort_values(["symbol", "datetime"]).drop_duplicates(["symbol", "datetime"], keep="last")
        after_dedupe = len(out)

        if before_dedupe != after_dedupe:
            logger.info(
                "[summary.cache_writer] db prepare dedupe interval=%s source=%s rows=%s -> %s dropped=%s",
                interval,
                source,
                before_dedupe,
                after_dedupe,
                before_dedupe - after_dedupe,
            )

    return out.reset_index(drop=True)


def _try_db_upsert(df: pd.DataFrame, interval: int, source: str) -> int:
    interval = int(interval)
    source = _normalize_source(source)

    work = _prepare_df_for_db(df, interval, source)

    if work.empty:
        logger.warning(
            "[summary.cache_writer] db upsert skipped interval=%s source=%s reason=prepared_empty",
            interval,
            source,
        )
        return 0

    opts = _db_upsert_options(interval, source)

    logger.info(
        "[summary.cache_writer] db upsert start interval=%s source=%s rows=%s symbols=%s latest_dt=%s opts=%s",
        interval,
        source,
        len(work),
        _safe_symbols(work),
        _safe_latest_dt(work),
        opts,
    )

    try:
        from trading.summary.persistence.summary_saver_bulk import bulk_upsert_summary  # type: ignore

        ret = _call_with_supported_kwargs(bulk_upsert_summary, work, interval, **opts)
        saved = int(ret) if isinstance(ret, (int, float)) else len(work)

        logger.info(
            "[summary.cache_writer] db upsert done via bulk_upsert_summary interval=%s source=%s saved=%s",
            interval,
            source,
            saved,
        )
        return saved

    except ImportError:
        logger.info("[summary.cache_writer] bulk_upsert_summary unavailable -> fallback interval=%s source=%s", interval, source)
    except TimeoutError:
        logger.warning(
            "[summary.cache_writer] bulk_upsert_summary lock timeout interval=%s source=%s rows=%s opts=%s -> fallback",
            interval,
            source,
            len(work),
            opts,
            exc_info=True,
        )
    except Exception:
        logger.exception("[summary.cache_writer] bulk_upsert_summary failed interval=%s source=%s -> fallback", interval, source)

    try:
        from trading.summary.persistence.summary_saver_bulk import save_summary_bulk  # type: ignore

        ret = _call_with_supported_kwargs(save_summary_bulk, work, interval, **opts)
        saved = int(ret) if isinstance(ret, (int, float)) else len(work)

        logger.info(
            "[summary.cache_writer] db upsert done via save_summary_bulk interval=%s source=%s saved=%s",
            interval,
            source,
            saved,
        )
        return saved

    except ImportError:
        logger.info("[summary.cache_writer] save_summary_bulk unavailable -> fallback interval=%s source=%s", interval, source)
    except TimeoutError:
        logger.warning(
            "[summary.cache_writer] save_summary_bulk lock timeout interval=%s source=%s rows=%s opts=%s -> fallback",
            interval,
            source,
            len(work),
            opts,
            exc_info=True,
        )
    except Exception:
        logger.exception("[summary.cache_writer] save_summary_bulk failed interval=%s source=%s -> fallback", interval, source)

    try:
        from trading.summary.persistence.core.upsert_executor import execute_upsert  # type: ignore

        rows = work.to_dict(orient="records")
        executor_opts = {"skip_if_busy": False}
        saved = int(_call_with_supported_kwargs(execute_upsert, rows, interval, **executor_opts))

        logger.info(
            "[summary.cache_writer] db upsert done via execute_upsert interval=%s source=%s saved=%s",
            interval,
            source,
            saved,
        )
        return saved

    except TimeoutError:
        logger.warning(
            "[summary.cache_writer] execute_upsert lock timeout interval=%s source=%s rows=%s opts=%s",
            interval,
            source,
            len(work),
            opts,
            exc_info=True,
        )
        return 0
    except Exception:
        logger.exception("[summary.cache_writer] db upsert failed interval=%s source=%s rows=%s", interval, source, len(work))
        return 0


def _save_cache(df: pd.DataFrame, interval: int, source: str) -> bool:
    try:
        if source == "push" and hasattr(global_data, "set_push_merged_summary"):
            global_data.set_push_merged_summary(interval, df.copy())
            logger.info("[summary.cache_writer] push merged cache saved interval=%s rows=%s", interval, len(df))
            return True

        if source == "ranking" and hasattr(global_data, "set_ranking_merged_summary"):
            global_data.set_ranking_merged_summary(interval, df.copy())
            logger.info("[summary.cache_writer] ranking merged cache saved interval=%s rows=%s", interval, len(df))
            return True

    except Exception:
        logger.exception("[summary.cache_writer] separated merged cache save failed source=%s interval=%s", source, interval)

    try:
        if hasattr(global_data, "set_merged_summary"):
            try:
                global_data.set_merged_summary(interval, df.copy(), source=source)
            except TypeError:
                global_data.set_merged_summary(interval, df.copy())

            logger.info("[summary.cache_writer] merged cache saved interval=%s source=%s rows=%s", interval, source, len(df))
            return True

    except Exception:
        logger.exception("[summary.cache_writer] merged cache save failed source=%s interval=%s", source, interval)

    logger.warning(
        "[summary.cache_writer] merged cache save skipped source=%s interval=%s reason=no_global_cache_writer",
        source,
        interval,
    )
    return False


# ============================================================
# public
# ============================================================

def save_merged_summary(df: pd.DataFrame, interval: int, *, source: str) -> None:
    interval = int(interval)
    source = _normalize_source(source)

    if df is None or df.empty:
        logger.warning("[summary.cache_writer] save skipped source=%s interval=%s reason=empty_df", source, interval)
        return

    if source == "push" and looks_uncomputed_push_df(df):
        logger.warning(
            "[summary.cache_writer] skip merged summary save source=%s interval=%s reason=uncomputed-zero-df latest_dt=%s",
            source,
            interval,
            latest_dt_str(df),
        )
        return

    if source == "ranking" and looks_uncomputed_ranking_df(df):
        logger.warning(
            "[summary.cache_writer] skip merged summary save source=%s interval=%s reason=uncomputed-zero-df latest_dt=%s",
            source,
            interval,
            latest_dt_str(df),
        )
        return

    logger.info(
        "[summary.cache_writer] save start source=%s interval=%s rows=%s symbols=%s latest_dt=%s",
        source,
        interval,
        _safe_rows(df),
        _safe_symbols(df),
        _safe_latest_dt(df),
    )

    db_save_skipped = _skip_db_save_for_entry_only_main()
    if db_save_skipped:
        saved_rows = 0
        logger.warning(
            "[summary.cache_writer] db upsert skipped interval=%s source=%s reason=main_entry_only rows=%s symbols=%s env_skip=%s role=%s",
            interval,
            source,
            len(df),
            _safe_symbols(df),
            os.environ.get("SUMMARY_SKIP_DB_SAVE_IN_MAIN"),
            os.environ.get("SUMMARY_DB_WRITER_ROLE"),
        )
    else:
        saved_rows = _try_db_upsert(df, interval, source)
        if saved_rows <= 0:
            logger.warning(
                "[summary.cache_writer] db upsert saved zero rows source=%s interval=%s input_rows=%s",
                source,
                interval,
                len(df),
            )

    cache_ok = _save_cache(df, interval, source)

    logger.info(
        "[summary.cache_writer] save done source=%s interval=%s input_rows=%s db_saved=%s db_skipped=%s cache_ok=%s",
        source,
        interval,
        len(df),
        saved_rows,
        db_save_skipped,
        cache_ok,
    )
