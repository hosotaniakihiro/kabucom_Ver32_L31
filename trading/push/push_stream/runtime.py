# ============================================================
# File   : trading/push/push_stream/runtime.py
# Version: Ver1.1-PUSH-STREAM-RUNTIME-MERGE-DB-AND-WS-DF
# ------------------------------------------------------------
# Purpose:
#   - push_stream runtime helpers
#   - WebSocket memory-only df を global_data.push_df へ同期する
#   - 既存のDB復元済みpush_dfを上書きせず、WebSocket受信dfとマージする
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import threading
import time
from typing import Any, Optional

import pandas as pd

from .constants import DEFAULT_WS_URL
from . import state

logger = logging.getLogger(__name__)

try:
    from global_state import global_data
except Exception:
    global_data = None


def _now() -> dt.datetime:
    return dt.datetime.now()


def _safe_iso(v: Any) -> Optional[str]:
    if v is None:
        return None
    try:
        if isinstance(v, dt.datetime):
            return v.isoformat()
        if isinstance(v, dt.date):
            return dt.datetime.combine(v, dt.time()).isoformat()
        s = str(v).strip()
        return s if s else None
    except Exception:
        return None


def _safe_set_runtime(name: str, value: Any) -> None:
    try:
        if global_data is not None:
            setattr(global_data, name, value)
    except Exception:
        logger.debug("[push_stream] global_data setattr failed: %s", name, exc_info=True)


def _safe_get_runtime(name: str, default: Any = None) -> Any:
    try:
        if global_data is not None and hasattr(global_data, name):
            return getattr(global_data, name)
    except Exception:
        logger.debug("[push_stream] global_data getattr failed: %s", name, exc_info=True)
    return default


def _resolve_ws_url() -> str:
    for candidate in [
        _safe_get_runtime("push_ws_url"),
        os.getenv("PUSH_WS_URL"),
        DEFAULT_WS_URL,
    ]:
        if candidate:
            return str(candidate).strip()
    return DEFAULT_WS_URL


def _ensure_runtime_flags() -> None:
    _safe_set_runtime("ws_connected", False)
    _safe_set_runtime("push_stream_running", False)
    _safe_set_runtime("subscription_refresh_running", False)
    _safe_set_runtime("push_writer_running", False)
    _safe_set_runtime("last_push_received_at", None)
    _safe_set_runtime("last_push_db_flush_at", None)
    _safe_set_runtime("push_ws_url", _resolve_ws_url())


# ============================================================
# PUSH df merge helpers
# ============================================================

def _max_merged_rows() -> int:
    try:
        return int(os.environ.get("PUSH_MERGED_DF_MAX_ROWS", "100000"))
    except Exception:
        return 100000


def _as_dataframe(x: Any) -> pd.DataFrame:
    if isinstance(x, pd.DataFrame):
        return x.copy()
    if x is None:
        return pd.DataFrame()
    try:
        return pd.DataFrame(x)
    except Exception:
        return pd.DataFrame()


def _normalize_symbol_column(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    out = df.copy()
    out.columns = [str(c).strip().lower() for c in out.columns]

    if "symbol" not in out.columns:
        for c in ("code", "symbol_code", "銘柄コード"):
            if c in out.columns:
                out["symbol"] = out[c]
                break

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

    return out


def _normalize_time_column(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    out = df.copy()

    # DB復元は time、WebSocket normalize は datetime を持つことがある。
    if "datetime" not in out.columns:
        for c in ("time", "timestamp", "current_price_time", "received_at", "inserted_at"):
            if c in out.columns:
                out["datetime"] = out[c]
                break

    if "time" not in out.columns and "datetime" in out.columns:
        out["time"] = out["datetime"]

    for c in ("datetime", "time"):
        if c not in out.columns:
            continue
        try:
            out[c] = pd.to_datetime(out[c], errors="coerce")
            # tz付きが混在してもnaiveへ寄せる
            try:
                if getattr(out[c].dt, "tz", None) is not None:
                    out[c] = out[c].dt.tz_convert("Asia/Tokyo").dt.tz_localize(None)
            except Exception:
                pass
        except Exception:
            pass

    return out


def merge_push_db_and_runtime_df(db_df: Any, runtime_df: Any, *, left_normalized: bool = False) -> pd.DataFrame:
    """
    DB復元済みPUSH df と WebSocket受信中PUSH df をマージする。

    Policy:
      - 列名は小文字へ正規化
      - time / datetime を相互補完
      - symbol + datetime で重複排除
      - 価格列がある場合は symbol + datetime + price 系でも補助的に安定化
      - 最後は時刻順に並べ、メモリ保護で末尾だけ残す
    """
    # left_normalized=True は「left は前回この関数が返したもの (正規化済み)」の意味。
    # 2026-09-02 実測: 20,000 行で 1 回 5.484s -> 増分マージで 0.2s 台。
    # 旧実装は毎回 left/right 双方に copy + 全行 regex + to_datetime をかけていた。
    if left_normalized and isinstance(db_df, pd.DataFrame):
        left = db_df
    else:
        left = _normalize_time_column(_normalize_symbol_column(_as_dataframe(db_df)))
    right = _normalize_time_column(_normalize_symbol_column(_as_dataframe(runtime_df)))

    frames = [x for x in (left, right) if isinstance(x, pd.DataFrame) and not x.empty]
    if not frames:
        return pd.DataFrame()

    try:
        merged = pd.concat(frames, ignore_index=True, sort=False)
    except Exception:
        logger.exception("[push_stream] concat push db/runtime df failed")
        return right if not right.empty else left

    if merged.empty:
        return merged

    # datetimeがない行はsummary計算に使いにくいため、timeから補完済みでも欠損なら落とす。
    if "datetime" in merged.columns:
        try:
            merged = merged.dropna(subset=["datetime"])
        except Exception:
            pass

    dedup_cols: list[str] = []
    if "symbol" in merged.columns:
        dedup_cols.append("symbol")
    if "datetime" in merged.columns:
        dedup_cols.append("datetime")

    if len(dedup_cols) >= 2:
        try:
            merged = merged.sort_values("datetime").drop_duplicates(dedup_cols, keep="last")
        except Exception:
            logger.debug("[push_stream] dedupe by symbol/datetime failed", exc_info=True)

    try:
        if "datetime" in merged.columns:
            merged = merged.sort_values("datetime")
    except Exception:
        pass

    max_rows = max(1000, _max_merged_rows())
    if len(merged) > max_rows:
        merged = merged.tail(max_rows)

    return merged.reset_index(drop=True)


# ============================================================
# global_data.push_df 同期
# ------------------------------------------------------------
# 2026-09-02 P0: 旧実装はこの関数を on_message から **1 tick ごと** に
# 呼んでいた。中身は最大 10 万行に対する copy 8 回 + regex 2 回 +
# sort 2 回 + drop_duplicates で、tick あたりのコストが行数に比例する。
# その結果 WS 読み取りスレッドが詰まり kabu Station に切断され続けた。
#
# Ver2.0 では
#   - on_message からは呼ばない (dataframe._append_df は deque へ積むだけ)
#   - 専用スレッドが seq の変化を見て間引いて呼ぶ
#   - 1 回の同期が interval より長引いたら interval を自動で伸ばす
# とする。閾値・stale 判定・fail-closed には触れていない。
# ============================================================

_GSYNC_LOCK = threading.Lock()


def _gsync_interval_sec() -> float:
    try:
        return max(0.2, float(os.environ.get("PUSH_STREAM_GLOBAL_SYNC_INTERVAL_SEC", "2.0")))
    except Exception:
        return 2.0


def _gsync_max_interval_sec() -> float:
    try:
        return max(1.0, float(os.environ.get("PUSH_STREAM_GLOBAL_SYNC_MAX_INTERVAL_SEC", "15.0")))
    except Exception:
        return 15.0


def push_global_sync_stats() -> dict:
    """診断用。E2E 1 行ログが読む。"""
    try:
        return {
            "rows": len(state._push_rows),
            "seq": int(state._push_rows_seq),
            "synced_seq": int(state._gsync_last_seq),
            "last_sync_age": None if state._gsync_last_at <= 0 else round(time.monotonic() - state._gsync_last_at, 2),
            "last_sync_elapsed": round(float(state._gsync_last_elapsed), 3),
            "thread_alive": bool(state._gsync_thread is not None and state._gsync_thread.is_alive()),
        }
    except Exception:
        return {}


def _global_sync_worker() -> None:
    """global_data.push_df を間引いて更新する。WS スレッドは絶対に待たせない。"""
    logger.info("[push_stream] global sync worker started interval=%.2fs", _gsync_interval_sec())
    interval = _gsync_interval_sec()
    while not state._stop_event.is_set():
        try:
            state._stop_event.wait(interval)
            if state._stop_event.is_set():
                break
            with state._df_lock:
                seq = int(state._push_rows_seq)
            if seq == state._gsync_last_seq:
                continue
            t0 = time.perf_counter()
            _sync_push_df_to_global(reason="worker")
            el = time.perf_counter() - t0
            state._gsync_last_seq = seq
            state._gsync_last_at = time.monotonic()
            state._gsync_last_elapsed = el
            base = _gsync_interval_sec()
            # 1 回の同期が interval を超えるほど重いなら間引きを強める。
            want = base if el <= base else min(_gsync_max_interval_sec(), max(base, el * 2.0))
            if abs(want - interval) > 0.05:
                logger.warning(
                    "[push_stream] global sync interval %.2fs -> %.2fs (last sync %.3fs, rows=%d) "
                    "(同期が重いので間引く。WS 受信は止めない)",
                    interval, want, el, len(state._push_rows),
                )
                interval = want
        except Exception:
            logger.exception("[push_stream] global sync worker loop failed")
            state._stop_event.wait(1.0)
    logger.info("[push_stream] global sync worker stopped")


def _sync_push_df_to_global(reason: str = "") -> None:
    if not _GSYNC_LOCK.acquire(blocking=False):
        # 前回の同期がまだ走っている。重ねて回さない。
        logger.debug("[push_stream] global sync already running; skip reason=%s", reason)
        return
    try:
        _sync_push_df_to_global_inner()
    finally:
        try:
            _GSYNC_LOCK.release()
        except Exception:
            pass


def _sync_push_df_to_global_inner() -> None:
    try:
        if global_data is None:
            return

        setter = getattr(global_data, "set_push_df", None)
        if not callable(setter):
            logger.debug("[push_stream] global_data.set_push_df unavailable")
            return

        with state._df_lock:
            seq = int(state._push_rows_seq)
            rows = list(state._push_rows)

        # ---- 増分マージの可否判定 ----
        # get_push_df() は実装によって copy() を返すので同一性判定に使えない。
        # 素の属性 global_data.push_df は前回 setattr したオブジェクトそのものなので、
        # これが前回の出力と同一なら「誰も外から差し替えていない」と断定できる。
        # 同一でなければ従来どおり全体を作り直す (取りこぼしを作らない)。
        raw_attr = getattr(global_data, "push_df", None)
        incremental = (
            state._gsync_last_out is not None
            and raw_attr is state._gsync_last_out
            and state._gsync_merged_upto_seq >= 0
            and seq >= state._gsync_merged_upto_seq
        )
        if incremental:
            existing_df = state._gsync_last_out
        else:
            try:
                getter = getattr(global_data, "get_push_df", None)
                existing_df = getter() if callable(getter) else raw_attr
            except Exception:
                existing_df = raw_attr
            if existing_df is None:
                existing_df = pd.DataFrame()
        if incremental:
            n_new = min(len(rows), seq - state._gsync_merged_upto_seq)
            if n_new <= 0:
                return
            runtime_df = pd.DataFrame(rows[-n_new:])
        else:
            runtime_df = pd.DataFrame(rows) if rows else pd.DataFrame()

        merged = merge_push_db_and_runtime_df(
            existing_df, runtime_df, left_normalized=incremental
        )
        setter(merged)
        state._gsync_last_out = merged
        state._gsync_merged_upto_seq = seq

        # 旧コード互換: global_data.push_df 直参照にも同じものを置く。
        try:
            setattr(global_data, "push_df", merged)
        except Exception:
            pass

        _safe_set_runtime("push_merged_df_rows", int(len(merged)))
        _safe_set_runtime("push_runtime_df_rows", int(len(runtime_df)))

        logger.debug(
            "[push_stream] synced merged push_df to global db_existing=%s runtime=%s merged=%s",
            0 if not isinstance(existing_df, pd.DataFrame) else len(existing_df),
            len(runtime_df),
            len(merged),
        )

    except Exception:
        logger.exception("[push_stream] sync push_df to global failed")


__all__ = [
    "_now",
    "_safe_iso",
    "_safe_set_runtime",
    "_safe_get_runtime",
    "_resolve_ws_url",
    "_ensure_runtime_flags",
    "merge_push_db_and_runtime_df",
    "_sync_push_df_to_global",
    "_global_sync_worker",
    "push_global_sync_stats",
]
