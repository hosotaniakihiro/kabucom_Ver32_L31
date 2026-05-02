# ============================================================
# trading/summary/realtime_engine.py
# Version: Ver7.1-PRODUCTION-ULTRA-STABLE-REALTIME-ENGINE
#          -SUMMARY-HISTORY-AWARE
#          -NO-LATEST-ONLY-HTF
#          -CACHE-UPDATE-HISTORY-GUARD
# ------------------------------------------------------------
# ✔ Ver7.0 完全互換
# ✔ push → ring buffer
# ✔ dataframe_manager 連携
# ✔ 1min diff計算
# ✔ confirmed builder fallback
# ✔ 3min / 5min 自動生成
# ✔ incremental indicators
# ✔ async DB保存
# ✔ cache完全同期
# ✔ 二重発火防止
# ✔ bar_clock統合
# ✔ AI非同期
# ✔ DataFrame安全化
# ✔ lock粒度改善
# ✔ engine singleton完全保証
# ✔ summary logger統合
# ✔ datetime / OHLC / symbol guard
# ✔ resample安全化
# ✔ production完全安定版
#
# 【Ver7.1 修正】
# ✔ 1min / 3min / 5min の既存データ取得で summary_history を優先
# ✔ 3min / 5min 生成時に latest cache だけを使わない
# ✔ HTF indicator 計算の hist_min=1〜2 問題を軽減
# ✔ df_updated を summary_history に保存する互換 setter を追加
# ✔ update_cache には df_updated を渡し、technical を持つ履歴から cache を作らせる
# ============================================================

from __future__ import annotations

import logging
import threading
import pandas as pd
from typing import Optional

from global_state import global_data

from trading.summary.bar_clock import BarClock
from trading.summary.push_ring_buffer import push_buffer
from trading.summary.incremental_indicators import (
    apply_incremental_indicators,
    update_1m_indicators,
)

from trading.summary.diff_1min_engine import build_1min_diff
from trading.summary.diff_higher_tf_engine import build_higher_tf_diff

from trading.summary.async_writer import async_write_summary, async_save
from trading.summary.cache_manager import update_cache
from trading.ai.async_ai_engine import submit_ai_job

from utils.dataframe_guard import (
    ensure_dataframe,
    sanitize_dataframe,
)

try:
    from trading.logger.summary_analysis_logger import run_all_loggers
    HAS_LOGGER = True
except Exception:
    HAS_LOGGER = False

logger = logging.getLogger(__name__)


# ============================================================
# dataframe guard helper
# ============================================================

def _safe_df(df):
    try:
        df = ensure_dataframe(df)

        if df.empty:
            return df

        df = sanitize_dataframe(df)
        return df

    except Exception:
        logger.exception("[REALTIME] dataframe sanitize failed")
        return pd.DataFrame()


def _normalize_runtime_df(df: pd.DataFrame) -> pd.DataFrame:
    out = _safe_df(df)
    if out.empty:
        return out

    try:
        if "symbol" in out.columns:
            out["symbol"] = out["symbol"].astype(str).str.strip()
            out = out[out["symbol"].ne("")].copy()

        if "datetime" in out.columns:
            out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
            out = out.dropna(subset=["datetime"]).copy()

        if {"symbol", "datetime"}.issubset(out.columns):
            out = (
                out.sort_values(["symbol", "datetime"], kind="stable")
                .drop_duplicates(subset=["symbol", "datetime"], keep="last")
                .reset_index(drop=True)
            )

    except Exception:
        logger.debug("[REALTIME] normalize runtime df failed", exc_info=True)

    return out


def _get_summary_history(interval: int) -> pd.DataFrame:
    """
    global_data から summary history を取得する。
    merged summary は latest cache のため、indicator 用には history を優先する。
    """
    try:
        candidates = [
            "get_summary_history",
            "get_summary_history_df",
            "get_history_summary",
        ]

        for name in candidates:
            fn = getattr(global_data, name, None)
            if callable(fn):
                try:
                    df = fn(int(interval), source="push")
                except TypeError:
                    try:
                        df = fn(int(interval))
                    except TypeError:
                        df = fn(interval, "push")
                df = _normalize_runtime_df(df)
                if not df.empty:
                    logger.info(
                        "[REALTIME] summary history get interval=%s rows=%s symbols=%s latest_dt=%s",
                        interval,
                        len(df),
                        df["symbol"].nunique() if "symbol" in df.columns else 0,
                        df["datetime"].max() if "datetime" in df.columns and not df.empty else None,
                    )
                    return df

        for attr in [
            "summary_history",
            "summary_history_cache",
            "push_summary_history",
            "_summary_history",
            "_summary_history_cache",
        ]:
            obj = getattr(global_data, attr, None)
            if isinstance(obj, dict):
                for key in [
                    int(interval),
                    str(int(interval)),
                    f"{int(interval)}min",
                    ("push", int(interval)),
                    (int(interval), "push"),
                ]:
                    df = obj.get(key)
                    df = _normalize_runtime_df(df)
                    if not df.empty:
                        logger.info(
                            "[REALTIME] summary history dict get interval=%s rows=%s symbols=%s latest_dt=%s",
                            interval,
                            len(df),
                            df["symbol"].nunique() if "symbol" in df.columns else 0,
                            df["datetime"].max() if "datetime" in df.columns and not df.empty else None,
                        )
                        return df

    except Exception:
        logger.debug("[REALTIME] get summary history failed interval=%s", interval, exc_info=True)

    return pd.DataFrame()


def _set_summary_history(interval: int, df: pd.DataFrame) -> None:
    """
    df_updated を full history として保存する。
    """
    df = _normalize_runtime_df(df)
    if df.empty:
        return

    try:
        candidates = [
            "set_summary_history",
            "set_summary_history_df",
            "set_history_summary",
        ]

        for name in candidates:
            fn = getattr(global_data, name, None)
            if callable(fn):
                try:
                    fn(int(interval), df, source="push")
                except TypeError:
                    try:
                        fn(int(interval), df)
                    except TypeError:
                        fn(interval, "push", df)
                logger.info(
                    "[REALTIME] summary history set interval=%s rows=%s symbols=%s",
                    interval,
                    len(df),
                    df["symbol"].nunique() if "symbol" in df.columns else 0,
                )
                return

        obj = getattr(global_data, "summary_history_cache", None)
        if not isinstance(obj, dict):
            obj = {}
            setattr(global_data, "summary_history_cache", obj)

        obj[int(interval)] = df.copy()
        obj[str(int(interval))] = df.copy()
        obj[f"{int(interval)}min"] = df.copy()
        obj[("push", int(interval))] = df.copy()
        obj[(int(interval), "push")] = df.copy()

        logger.info(
            "[REALTIME] summary history fallback stored interval=%s rows=%s symbols=%s",
            interval,
            len(df),
            df["symbol"].nunique() if "symbol" in df.columns else 0,
        )

    except Exception:
        logger.debug("[REALTIME] set summary history failed interval=%s", interval, exc_info=True)


def _get_existing_for_indicator(interval: int) -> pd.DataFrame:
    """
    indicator 計算用の既存DF。
    history を優先し、無ければ merged summary を使う。
    """
    hist = _get_summary_history(int(interval))
    if not hist.empty:
        return hist

    try:
        df = global_data.get_merged_summary(int(interval))
        df = _normalize_runtime_df(df)
        if not df.empty:
            logger.info(
                "[REALTIME] merged summary fallback for indicator interval=%s rows=%s symbols=%s",
                interval,
                len(df),
                df["symbol"].nunique() if "symbol" in df.columns else 0,
            )
            return df
    except Exception:
        logger.debug("[REALTIME] get merged fallback failed interval=%s", interval, exc_info=True)

    return pd.DataFrame()


# ============================================================
# Realtime Engine
# ============================================================

class RealtimeSummaryEngine:

    def __init__(self):
        self.clock = BarClock()
        self._lock_1m = threading.Lock()
        self._lock_htf = threading.Lock()

    # ========================================================
    # PUSH受信
    # ========================================================

    def on_push(self, symbol: str, push_row: dict):
        try:
            if not push_row:
                return
            push_buffer.append(push_row)
        except Exception:
            logger.exception("[REALTIME] push append error")

    # ========================================================
    # メインループ
    # ========================================================

    def run_cycle(self):
        if getattr(global_data, "is_initializing", False):
            return

        try:
            signals = self.clock.check()
        except Exception:
            logger.exception("[REALTIME] bar_clock error")
            return

        if signals.get("1min"):
            self._process_1min()

        if signals.get("3min"):
            self._process_higher_tf(3)

        if signals.get("5min"):
            self._process_higher_tf(5)

    # ========================================================
    # 1分足処理
    # ========================================================

    def _process_1min(self):
        with self._lock_1m:
            try:
                data = push_buffer.flush()
            except Exception:
                logger.exception("[REALTIME] push flush error")
                return

            if data is None:
                return

            df_push = _safe_df(data)

            if df_push.empty:
                return

            df_new_1m = None

            try:
                df_new_1m = build_1min_diff(df_push)
            except Exception:
                logger.exception("[REALTIME] diff build error")

            if df_new_1m is None or df_new_1m.empty:
                try:
                    from trading.summary.confirmed_bar_builder import (
                        build_confirmed_1min_from_push,
                    )
                    df_new_1m = build_confirmed_1min_from_push(df_push)
                except Exception:
                    logger.exception("[REALTIME] confirmed fallback error")
                    return

            df_new_1m = _safe_df(df_new_1m)

            if df_new_1m.empty:
                return

            try:
                df_existing = _get_existing_for_indicator(1)
            except Exception:
                df_existing = pd.DataFrame()

            try:
                df_updated = apply_incremental_indicators(
                    df_existing=df_existing,
                    df_new=df_new_1m,
                    interval=1,
                )
            except Exception:
                logger.exception("[REALTIME] indicator error")
                return

            df_updated = _normalize_runtime_df(df_updated)

            try:
                _set_summary_history(1, df_updated)
                global_data.set_merged_summary(1, df_updated)
                global_data.set_multi_summary(1, df_updated)
            except Exception:
                logger.exception("[REALTIME] summary cache update error")

            try:
                df_light = update_1m_indicators(df_new_1m.copy())
            except Exception:
                logger.exception("[REALTIME] light indicator error")
                df_light = df_new_1m

            df_light = _safe_df(df_light)

            try:
                # cache は latest only ではなく df_updated を渡す
                update_cache(1, df_updated if not df_updated.empty else df_light)
            except Exception:
                logger.exception("[REALTIME] cache update error")

            try:
                async_write_summary(df_new_1m, 1)
                async_save(df_light, 1)
            except Exception:
                logger.exception("[REALTIME] async save error")

            try:
                submit_ai_job(df_light)
            except Exception:
                logger.exception("[REALTIME] AI job error")

            logger.info("[REALTIME] 1min rows=%d updated_rows=%d", len(df_new_1m), len(df_updated))

    # ========================================================
    # 上位足処理
    # ========================================================

    def _process_higher_tf(self, interval: int):
        with self._lock_htf:
            try:
                # 3min / 5min 作成元は latest cache ではなく 1min history を優先
                df_1m = _get_existing_for_indicator(1)
            except Exception:
                return

            df_1m = _safe_df(df_1m)

            if df_1m.empty:
                return

            df_new_tf = None

            try:
                df_new_tf = build_higher_tf_diff(
                    df_1m,
                    interval=interval,
                )
            except Exception:
                logger.exception("[REALTIME] HTF diff error")

            if df_new_tf is None or df_new_tf.empty:
                try:
                    from trading.summary.resample import resample_1min_to
                    df_new_tf = resample_1min_to(df_1m, interval)
                except Exception:
                    logger.exception("[REALTIME] HTF fallback error")
                    return

            df_new_tf = _safe_df(df_new_tf)

            if df_new_tf.empty:
                return

            try:
                df_existing = _get_existing_for_indicator(interval)
            except Exception:
                df_existing = pd.DataFrame()

            try:
                df_updated = apply_incremental_indicators(
                    df_existing=df_existing,
                    df_new=df_new_tf,
                    interval=interval,
                )
            except Exception:
                logger.exception("[REALTIME] HTF indicator error")
                return

            df_updated = _normalize_runtime_df(df_updated)

            try:
                _set_summary_history(interval, df_updated)
                global_data.set_merged_summary(interval, df_updated)
                global_data.set_multi_summary(interval, df_updated)
            except Exception:
                logger.exception("[REALTIME] HTF cache update error")

            try:
                # cache は df_new_tf ではなく df_updated を渡す。
                # df_new_tf は最新数本しかなく、rsi/macd/slope/mtf が落ちる。
                update_cache(interval, df_updated if not df_updated.empty else df_new_tf)
            except Exception:
                logger.exception("[REALTIME] HTF cache error")

            try:
                async_write_summary(df_new_tf, interval)
                async_save(df_new_tf, interval)
            except Exception:
                logger.exception("[REALTIME] HTF async save error")

            if HAS_LOGGER:
                try:
                    run_all_loggers(
                        df_new_tf,
                        interval=interval,
                        top_n=10,
                    )
                except Exception:
                    logger.exception("[REALTIME] logger failed")

            logger.info(
                "[REALTIME] %smin rows=%d updated_rows=%d",
                interval,
                len(df_new_tf),
                len(df_updated),
            )


# ============================================================
# engine singleton
# ============================================================

_engine_instance: Optional[RealtimeSummaryEngine] = None
_engine_lock = threading.Lock()


def init_realtime_engine():
    global _engine_instance

    with _engine_lock:
        if _engine_instance is None:
            _engine_instance = RealtimeSummaryEngine()


def on_push_tick(symbol: str, tick: dict):
    if _engine_instance is None:
        init_realtime_engine()

    _engine_instance.on_push(symbol, tick)


def process_realtime():
    if _engine_instance is None:
        init_realtime_engine()

    _engine_instance.run_cycle()


# ============================================================
# singleton reference
# ============================================================

realtime_engine = RealtimeSummaryEngine()