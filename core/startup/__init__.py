# ============================================================
# File   : core/startup/__init__.py
# Ver    : PRODUCTION-STABLE-REV22-DB-CONTEXT-INIT-PATCH-GUARD
# ------------------------------------------------------------
# 【概要】
#   core.startup パッケージの公開入口。
#   各種 runtime/startup patch を自動適用する。
#
# REV22:
#   - main_database.py / data_collectors_runner.py 等の DB専用子プロセスが
#     core.startup 配下のどれか一つでも import すると、本ファイルが
#     entry/ranking/summary 系の runtime patch 13本 (+MA5早期クロス) を
#     無条件でインストールしてしまっていた問題を修正。
#   - sitecustomize.py の _is_database_process() と同じ argv 判定を
#     _core_startup_is_database_context() として複製し、DB専用プロセスでは
#     これらのライブ取引向け patch をスキップするようにした。
#   - main.py (本番取引プロセス) は対象外のため従来通り全て適用される。
#   - CORE_STARTUP_FORCE_FULL_INIT_PATCHES=1 で強制的に全パッチ適用に戻せる
#     (調査・緊急時のフォールバック用)。
#
# REV21.6:
#   - SUMMARY AI候補前に 3分/5分の5MA早期クロス条件を追加
#   - BUY : 3m/5mで close > 5MA かつ 5MA上向き、上抜け直後〜数本以内
#   - SELL: 3m/5mで close < 5MA かつ 5MA下向き、下抜け直後〜数本以内
#   - RANKING / TONOSAMA 由来には直接かけない
#   - ranking_summary_schedule_bg_patch 等も継続適用
# ============================================================

from __future__ import annotations

import logging
import os
import sys

logger = logging.getLogger(__name__)


def _core_startup_is_database_context() -> bool:
    """DB専用子プロセス (main_database.py 等) かどうかを argv から判定する。

    sitecustomize.py の _is_database_process() と同じプロセス名リストを使う。
    ここが True の場合、entry/ranking/summary 系の live-trading 向け runtime
    patch (このファイル下部の _install_entry_runtime_init_patches) はスキップする。
    main.py はこのリストに含まれないため対象外 (常に全パッチ適用)。
    """
    try:
        if str(os.getenv("CORE_STARTUP_FORCE_FULL_INIT_PATCHES", "")).strip().lower() in {
            "1", "true", "yes", "y", "on", "enable", "enabled",
        }:
            return False
        argv_text = " ".join(str(x) for x in sys.argv).replace("\\", "/").lower()
        db_process_names = (
            "main_database.py",
            "data_collectors_runner.py",
            "db_prepare_runner.py",
            "push_receiver_runner.py",
            "yahoo_complement_runner.py",
            "summary_database_runner.py",
            "ranking_collector_runner.py",
        )
        return any(name in argv_text for name in db_process_names)
    except Exception:
        return False


_SKIP_ENTRY_RUNTIME_INIT_PATCHES = _core_startup_is_database_context()


def _install_entry_runtime_init_patches() -> None:
    try:
        from .summary_scheduler_unified_stale_guard_patch import install as install_summary_scheduler_unified_stale_guard_patch

        install_summary_scheduler_unified_stale_guard_patch()
    except Exception:
        logger.exception("[core.startup] summary scheduler unified stale guard patch install failed")

    try:
        from .ranking_summary_schedule_bg_patch import install as install_ranking_summary_schedule_bg_patch

        install_ranking_summary_schedule_bg_patch()
    except Exception:
        logger.exception("[core.startup] ranking summary schedule bg patch install failed")

    try:
        from .summary_controller_soft_technical_ready_patch import install as install_summary_controller_soft_technical_ready_patch

        install_summary_controller_soft_technical_ready_patch()
    except Exception:
        logger.exception("[core.startup] summary controller soft technical ready patch install failed")

    # ============================================================
    # summary runner ready-fill-before-save patch
    # (PUSH由来サマリーで technical_ready=False のまま save/AI に渡る問題を補正)
    # ============================================================
    try:
        def _core_ready_fill_count(df) -> int:
            try:
                import pandas as _pd
                if not isinstance(df, _pd.DataFrame) or df.empty or "technical_ready" not in df.columns:
                    return 0
                return int(_pd.Series(df["technical_ready"]).fillna(False).astype(bool).sum())
            except Exception:
                return 0

        def _core_ready_fill_inplace(df, *, interval: int, source: str):
            import pandas as _pd
            if not isinstance(df, _pd.DataFrame) or df.empty:
                return df

            before_ready = _core_ready_fill_count(df)
            before_cols = set(df.columns)

            try:
                from trading.summary.pipeline.indicator_short_history_patch import add_short_history_indicators
                filled = add_short_history_indicators(df, interval=interval)
            except Exception:
                logger.exception("[SUMMARY RUNNER READY FILL] add_short_history_indicators failed interval=%s source=%s", interval, source)
                return df

            if not isinstance(filled, _pd.DataFrame) or filled.empty:
                return df

            try:
                df.drop(df.index, inplace=True)
                for c in list(df.columns):
                    if c not in filled.columns:
                        try:
                            df.drop(columns=[c], inplace=True)
                        except Exception:
                            pass
                for c in filled.columns:
                    df[c] = filled[c].values
                df.index = filled.index
            except Exception:
                logger.exception("[SUMMARY RUNNER READY FILL] inplace replace failed interval=%s source=%s", interval, source)
                return filled

            after_ready = _core_ready_fill_count(df)
            logger.warning(
                "[SUMMARY RUNNER READY FILL] applied interval=%s source=%s rows=%s technical_ready %s->%s added_cols=%s",
                interval, source, len(df), before_ready, after_ready,
                sorted([c for c in df.columns if c not in before_cols])[:20],
            )
            return df

        import scheduler_jobs.summary.runner_core as _runner_core

        _ready_fill_old_save = getattr(_runner_core, "_save_summary_if_owner", None)
        if not callable(_ready_fill_old_save):
            logger.warning("[SUMMARY RUNNER READY FILL] _save_summary_if_owner not callable")
        else:
            def _core_save_summary_if_owner_patched(df, interval: int, *, source: str) -> None:
                try:
                    _core_ready_fill_inplace(df, interval=int(interval), source=str(source))
                except Exception:
                    logger.exception("[SUMMARY RUNNER READY FILL] pre-save fill failed interval=%s source=%s", interval, source)
                return _ready_fill_old_save(df, interval, source=source)

            _runner_core._save_summary_if_owner = _core_save_summary_if_owner_patched
            logger.warning("[SUMMARY RUNNER READY FILL] installed")
    except Exception:
        logger.exception("[core.startup] summary runner ready fill before save patch install failed")

    try:
        from .ranking_entry_volume_unit_patch import install as install_ranking_entry_volume_unit_patch

        install_ranking_entry_volume_unit_patch()
    except Exception:
        logger.exception("[core.startup] ranking entry volume unit patch install failed")

    # ============================================================
    # SUMMARY AI 3m/5m 5MA early-cross runtime patch
    # ============================================================
    try:
        import os as _os
        from typing import Any as _Any

        import pandas as _pd

        _TRUE_SET = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
        _FALSE_SET = {"0", "false", "no", "n", "off", "ng", "disable", "disabled"}

        def _core_env_bool(name: str, default: bool = True) -> bool:
            try:
                raw = _os.getenv(name)
                if raw is None or str(raw).strip() == "":
                    return bool(default)
                s = str(raw).strip().lower()
                if s in _TRUE_SET:
                    return True
                if s in _FALSE_SET:
                    return False
            except Exception:
                pass
            return bool(default)

        def _core_env_float(name: str, default: float) -> float:
            try:
                raw = _os.getenv(name)
                if raw is not None and str(raw).strip() != "":
                    return float(raw)
            except Exception:
                pass
            return float(default)

        def _core_env_int(name: str, default: int) -> int:
            try:
                raw = _os.getenv(name)
                if raw is not None and str(raw).strip() != "":
                    return int(float(raw))
            except Exception:
                pass
            return int(default)

        def _core_ma5_intervals() -> set[int]:
            raw = str(_os.getenv("SUMMARY_AI_MA5_EARLY_INTERVALS") or "3,5")
            out: set[int] = set()
            for p in raw.replace(";", ",").split(","):
                try:
                    v = int(float(str(p).strip().lower().replace("min", "").replace("m", "")))
                    if v in {1, 3, 5}:
                        out.add(v)
                except Exception:
                    pass
            return out or {3, 5}

        def _core_to_interval(v: _Any) -> int:
            try:
                s = str(v).strip().lower().replace("minutes", "").replace("minute", "").replace("min", "").replace("m", "")
                return int(float(s))
            except Exception:
                return 1

        def _core_norm_symbol(v: _Any) -> str:
            s = str(v or "").strip()
            if s.endswith(".0") and s[:-2].isdigit():
                return s[:-2]
            return s

        def _core_source_excluded(source: _Any) -> bool:
            s = str(source or "").upper()
            return "RANKING" in s or "TONOSAMA" in s

        def _core_num_col(df: _pd.DataFrame, names: list[str]) -> _pd.Series | None:
            for name in names:
                if name in df.columns:
                    return _pd.to_numeric(df[name], errors="coerce")
            return None

        def _core_prepare_ma5_history(df: _pd.DataFrame, interval: int) -> _pd.DataFrame:
            if df is None or df.empty or "symbol" not in df.columns:
                return _pd.DataFrame()
            x = df.copy()
            x["symbol"] = x["symbol"].map(_core_norm_symbol)
            if "datetime" in x.columns:
                x["datetime"] = _pd.to_datetime(x["datetime"], errors="coerce")
            else:
                x["datetime"] = _pd.NaT
            close = _core_num_col(x, [f"close_{interval}m", "close", "close_price", "current_price", "price", "disp_close"])
            ma5 = _core_num_col(x, [f"ma5_{interval}m", "ma5", "MA5", "sma5", "ma_5", "moving_average_5"])
            if close is None:
                return _pd.DataFrame()
            x["_ma5_close"] = close
            if ma5 is None:
                x["_ma5"] = x.groupby("symbol")["_ma5_close"].transform(lambda s: s.rolling(5, min_periods=5).mean())
            else:
                x["_ma5"] = ma5
            x = x.dropna(subset=["symbol", "_ma5_close", "_ma5"])
            if x.empty:
                return _pd.DataFrame()
            return x.sort_values(["symbol", "datetime"], kind="stable")

        def _core_get_summary_history(interval: int, source: str) -> _pd.DataFrame:
            try:
                from core.global_context.context import global_context as GC
                for kwargs in ({"source": "push"}, {"source": source.lower()}, {}):
                    try:
                        got = GC.get_summary_history(interval, **kwargs)
                    except TypeError:
                        got = GC.get_summary_history(interval)
                    if isinstance(got, _pd.DataFrame) and not got.empty:
                        return got.copy()
            except Exception:
                logger.debug("[SUMMARY AI MA5 EARLY] global_context history unavailable", exc_info=True)
            return _pd.DataFrame()

        def _core_decide_want_side(row: _pd.Series) -> str:
            buy = 0.0
            sell = 0.0
            for c in ("score_buy", "buy_score", "disp_buy_score"):
                if c in row.index:
                    try:
                        buy = max(buy, float(row.get(c) or 0.0))
                    except Exception:
                        pass
            for c in ("score_sell", "sell_score", "disp_sell_score"):
                if c in row.index:
                    try:
                        sell = max(sell, float(row.get(c) or 0.0))
                    except Exception:
                        pass
            return "SELL" if sell > buy else "BUY"

        def _core_ma5_signal_symbols(df: _pd.DataFrame, *, interval: int, source: str) -> dict[str, str]:
            hist = _core_prepare_ma5_history(df, interval)
            if hist.empty or int(hist.groupby("symbol").size().max()) < 2:
                hist = _core_prepare_ma5_history(_core_get_summary_history(interval, source), interval)
            if hist.empty:
                return {}
            max_bars = max(1, _core_env_int("SUMMARY_AI_MA5_EARLY_MAX_BARS_AFTER_CROSS", 2))
            slope_min = _core_env_float("SUMMARY_AI_MA5_SLOPE_MIN", 0.0)
            out: dict[str, str] = {}
            for sym, g in hist.groupby("symbol", sort=False):
                try:
                    gg = g.sort_values("datetime", kind="stable").tail(max(10, max_bars + 3)).copy()
                    if len(gg) < 2:
                        continue
                    gg["_prev_close"] = gg["_ma5_close"].shift(1)
                    gg["_prev_ma5"] = gg["_ma5"].shift(1)
                    gg["_ma5_slope"] = gg["_ma5"] - gg["_prev_ma5"]
                    cur = gg.iloc[-1]
                    cur_close = float(cur.get("_ma5_close"))
                    cur_ma5 = float(cur.get("_ma5"))
                    cur_slope = float(cur.get("_ma5_slope") or 0.0)
                    buy_cross = (gg["_prev_close"] <= gg["_prev_ma5"]) & (gg["_ma5_close"] > gg["_ma5"])
                    sell_cross = (gg["_prev_close"] >= gg["_prev_ma5"]) & (gg["_ma5_close"] < gg["_ma5"])
                    buy_recent = bool(buy_cross.tail(max_bars).fillna(False).any())
                    sell_recent = bool(sell_cross.tail(max_bars).fillna(False).any())
                    buy_ok = bool(cur_close > cur_ma5 and cur_slope > slope_min and buy_recent)
                    sell_ok = bool(cur_close < cur_ma5 and cur_slope < -abs(slope_min) and sell_recent)
                    if buy_ok and sell_ok:
                        continue
                    if buy_ok:
                        out[str(sym)] = "BUY"
                    elif sell_ok:
                        out[str(sym)] = "SELL"
                except Exception:
                    continue
            return out

        def _core_apply_ma5_early_filter(df: _pd.DataFrame, *, interval: int, source: str) -> _pd.DataFrame:
            if df is None or df.empty or "symbol" not in df.columns:
                return _pd.DataFrame() if df is None else df
            signals = _core_ma5_signal_symbols(df, interval=interval, source=source)
            before = len(df)
            if not signals:
                fail_open = _core_env_bool("SUMMARY_AI_MA5_EARLY_FAIL_OPEN", False)
                logger.warning(
                    "[SUMMARY AI MA5 EARLY] no symbols interval=%s source=%s before=%s fail_open=%s",
                    interval, source, before, fail_open,
                )
                return df if fail_open else _pd.DataFrame()
            x = df.copy()
            x["_ma5_norm_symbol"] = x["symbol"].map(_core_norm_symbol)
            x["_ma5_want_side"] = x.apply(_core_decide_want_side, axis=1)
            x["_ma5_signal_side"] = x["_ma5_norm_symbol"].map(signals)
            x = x[(x["_ma5_signal_side"].notna()) & (x["_ma5_want_side"] == x["_ma5_signal_side"])].copy()
            try:
                x["ma5_early_entry"] = 1
                x["ma5_early_interval"] = interval
                x["entry_reason_ma5_early"] = x["_ma5_signal_side"].map(lambda s: f"{interval}m_5MA早期クロス_{s}")
            except Exception:
                pass
            x.drop(columns=["_ma5_norm_symbol", "_ma5_want_side", "_ma5_signal_side"], inplace=True, errors="ignore")
            logger.warning(
                "[SUMMARY AI MA5 EARLY] filtered interval=%s source=%s before=%s after=%s signals=%s",
                interval, source, before, len(x), dict(list(signals.items())[:30]),
            )
            return x

        def _install_summary_ai_ma5_early_patch() -> bool:
            if not _core_env_bool("SUMMARY_AI_MA5_EARLY_ENTRY", True):
                logger.warning("[SUMMARY AI MA5 EARLY] disabled by env")
                return False
            try:
                import trading.entry.summary_ai.runner as runner
                old = getattr(runner, "run_summary_ai_entry_from_df", None)
                if not callable(old):
                    return False
                if getattr(old, "_summary_ai_ma5_early_patch", False):
                    return True

                def _patched(summary_df=None, *, df=None, interval=1, source="SUMMARY", **kwargs):
                    src = str(kwargs.get("source", source) or source)
                    iv = _core_to_interval(interval)
                    if iv in _core_ma5_intervals() and not _core_source_excluded(src):
                        try:
                            target = summary_df if isinstance(summary_df, _pd.DataFrame) else df
                            if isinstance(target, _pd.DataFrame):
                                filtered = _core_apply_ma5_early_filter(target, interval=iv, source=src)
                                if isinstance(summary_df, _pd.DataFrame):
                                    summary_df = filtered
                                elif isinstance(df, _pd.DataFrame):
                                    df = filtered
                        except Exception:
                            logger.exception("[SUMMARY AI MA5 EARLY] filter failed interval=%s source=%s", iv, src)
                            if not _core_env_bool("SUMMARY_AI_MA5_EARLY_FAIL_OPEN", False):
                                return {
                                    "candidates": [], "ai_results": [], "ai_ok": [], "approved_rows": [],
                                    "execution": {"executed": False, "dry_run": bool(kwargs.get("dry_run", False)), "approved_rows": [], "result": None, "skip_reason": "ma5_early_filter_failed"},
                                    "dry_run": bool(kwargs.get("dry_run", False)), "skip_reason": "ma5_early_filter_failed",
                                }
                    return old(summary_df=summary_df, df=df, interval=interval, source=source, **kwargs)

                _patched._summary_ai_ma5_early_patch = True  # type: ignore[attr-defined]
                _patched._original = old  # type: ignore[attr-defined]
                runner.run_summary_ai_entry_from_df = _patched
                runner.run_summary_ai_entry = lambda summary_df=None, *, df=None, interval=1, **kwargs: _patched(summary_df=summary_df, df=df, interval=interval, **kwargs)
                logger.warning(
                    "[SUMMARY AI MA5 EARLY] installed intervals=%s max_bars=%s slope_min=%.4f fail_open=%s",
                    sorted(_core_ma5_intervals()),
                    _core_env_int("SUMMARY_AI_MA5_EARLY_MAX_BARS_AFTER_CROSS", 2),
                    _core_env_float("SUMMARY_AI_MA5_SLOPE_MIN", 0.0),
                    _core_env_bool("SUMMARY_AI_MA5_EARLY_FAIL_OPEN", False),
                )
                return True
            except Exception:
                logger.exception("[SUMMARY AI MA5 EARLY] install failed")
                return False

        _install_summary_ai_ma5_early_patch()
    except Exception:
        logger.exception("[core.startup] summary AI MA5 early patch install failed")

    # ============================================================
    # SUMMARY AI slope env patch (未設定時のみ slope gate を緩める)
    # ============================================================
    try:
        _SLOPE_ENV_DEFAULTS = {
            "ENTRY_MIN_BUY_SLOPE": "-999",
            "SUMMARY_AI_MIN_BUY_SLOPE": "-999",
            "ENTRY_MAX_SELL_SLOPE": "999",
            "SUMMARY_AI_MAX_SELL_SLOPE": "999",
        }

        def _core_env_blank(v: object) -> bool:
            try:
                return v is None or str(v).strip() == ""
            except Exception:
                return True

        _slope_applied: dict[str, str] = {}
        _slope_kept: dict[str, str] = {}
        for _key, _value in _SLOPE_ENV_DEFAULTS.items():
            _cur = os.environ.get(_key)
            if _core_env_blank(_cur):
                os.environ[_key] = _value
                _slope_applied[_key] = _value
            else:
                _slope_kept[_key] = str(_cur)
        logger.warning(
            "[SUMMARY AI SLOPE ENV PATCH] installed applied=%s kept=%s",
            _slope_applied, _slope_kept,
        )
    except Exception:
        logger.exception("[core.startup] summary AI slope env patch install failed")

    # ============================================================
    # SUMMARY AI score env patch (entry_controller の BUY 閾値を実運用値へ)
    # ============================================================
    try:
        def _core_env_float_for_score(name: str, default: float) -> float:
            try:
                v = os.environ.get(name)
                if _core_env_blank(v):
                    return float(default)
                return float(str(v).strip())
            except Exception:
                return float(default)

        _score_applied: dict[str, str] = {}
        _score_kept: dict[str, str] = {}
        for _key, _value in (
            ("MIN_ENTRY_SCORE", "3.0"),
            ("ENTRY_CONTROLLER_MIN_SUMMARY_SCORE_BUY", "3.0"),
            ("ENTRY_CONTROLLER_MIN_COMPOSITE_SCORE_BUY", "3.0"),
            ("ENTRY_CONTROLLER_MIN_AI_CONFIDENCE_BUY", "0.60"),
        ):
            _cur = os.environ.get(_key)
            if _core_env_blank(_cur):
                os.environ[_key] = _value
                _score_applied[_key] = _value
            else:
                _score_kept[_key] = str(_cur)

        _score_controller_patch: dict[str, object] = {"patched": False}
        try:
            import trading.handlers.entry_controller as _ec

            _min_buy_score = _core_env_float_for_score("ENTRY_CONTROLLER_MIN_SUMMARY_SCORE_BUY", 3.0)
            _min_buy_comp = _core_env_float_for_score("ENTRY_CONTROLLER_MIN_COMPOSITE_SCORE_BUY", 3.0)
            _min_buy_conf = _core_env_float_for_score("ENTRY_CONTROLLER_MIN_AI_CONFIDENCE_BUY", 0.60)

            _score_old = {
                "MIN_SUMMARY_SCORE_BUY": getattr(_ec, "MIN_SUMMARY_SCORE_BUY", None),
                "MIN_COMPOSITE_SCORE_BUY": getattr(_ec, "MIN_COMPOSITE_SCORE_BUY", None),
                "MIN_AI_CONFIDENCE_BUY": getattr(_ec, "MIN_AI_CONFIDENCE_BUY", None),
            }

            _ec.MIN_SUMMARY_SCORE_BUY = float(_min_buy_score)
            _ec.MIN_COMPOSITE_SCORE_BUY = float(_min_buy_comp)
            _ec.MIN_AI_CONFIDENCE_BUY = float(_min_buy_conf)

            _score_controller_patch.update(
                {
                    "patched": True,
                    "old": _score_old,
                    "new": {
                        "MIN_SUMMARY_SCORE_BUY": _ec.MIN_SUMMARY_SCORE_BUY,
                        "MIN_COMPOSITE_SCORE_BUY": _ec.MIN_COMPOSITE_SCORE_BUY,
                        "MIN_AI_CONFIDENCE_BUY": _ec.MIN_AI_CONFIDENCE_BUY,
                    },
                }
            )
        except Exception as _e:
            logger.exception("[SUMMARY AI SCORE ENV PATCH] entry_controller threshold patch failed")
            _score_controller_patch.update({"error": repr(_e)})

        logger.warning(
            "[SUMMARY AI SCORE ENV PATCH] installed applied=%s kept=%s entry_controller=%s",
            _score_applied, _score_kept, _score_controller_patch,
        )
    except Exception:
        logger.exception("[core.startup] summary AI score env patch install failed")

    try:
        from .summary_write_gate_runtime_patch import install_summary_write_gate_runtime_patch

        install_summary_write_gate_runtime_patch()
    except Exception:
        logger.exception("[core.startup] summary write gate runtime patch install failed")

    # ============================================================
    # ranking summary persistence lock patch (database is locked を warning+skip に緩和)
    # ============================================================
    try:
        def _core_ranking_lock_is_locked_error(exc: BaseException) -> bool:
            s = str(exc).lower()
            return "database is locked" in s or "database table is locked" in s or "locked" in s

        import trading.ranking.summary.persistence as _persistence

        _rlock_original_local = getattr(_persistence, "_ensure_local_ranking_summary_table", None)
        _rlock_original_ensure = getattr(_persistence, "ensure_ranking_summary_table", None)
        _rlock_original_save = getattr(_persistence, "save_ranking_summary", None)

        if not callable(_rlock_original_local) or not callable(_rlock_original_ensure):
            logger.warning("[RANKING SUMMARY LOCK PATCH] target functions missing")
        else:
            def _core_ranking_lock_patched_ensure_local(*, interval=1, date_yyyymmdd=None, db_path=None) -> bool:
                try:
                    return bool(_rlock_original_local(interval=interval, date_yyyymmdd=date_yyyymmdd, db_path=db_path))
                except Exception as exc:
                    if _core_ranking_lock_is_locked_error(exc):
                        logger.warning(
                            "[RANKING SUMMARY LOCK PATCH] ensure local skipped locked interval=%s db=%s err=%s",
                            interval, db_path, exc,
                        )
                        return False
                    logger.exception("[RANKING SUMMARY LOCK PATCH] ensure local failed interval=%s", interval)
                    return False

            def _core_ranking_lock_patched_ensure(interval=1, *args, **kwargs) -> bool:
                try:
                    return bool(_rlock_original_ensure(interval, *args, **kwargs))
                except Exception as exc:
                    if _core_ranking_lock_is_locked_error(exc):
                        logger.warning(
                            "[RANKING SUMMARY LOCK PATCH] ensure skipped locked interval=%s err=%s",
                            interval, exc,
                        )
                        return False
                    logger.exception("[RANKING SUMMARY LOCK PATCH] ensure failed interval=%s", interval)
                    return False

            def _core_ranking_lock_patched_save(df, *args, interval=1, source="ranking", **kwargs) -> int:
                ok = _core_ranking_lock_patched_ensure(interval=interval)
                if not ok:
                    try:
                        rows = 0 if df is None else len(df)
                    except Exception:
                        rows = 0
                    logger.warning(
                        "[RANKING SUMMARY LOCK PATCH] save skipped because ensure unavailable interval=%s source=%s rows=%s",
                        interval, source, rows,
                    )
                    return 0
                if not callable(_rlock_original_save):
                    return 0
                try:
                    return int(_rlock_original_save(df, *args, interval=interval, source=source, **kwargs) or 0)
                except Exception as exc:
                    if _core_ranking_lock_is_locked_error(exc):
                        logger.warning(
                            "[RANKING SUMMARY LOCK PATCH] save skipped locked interval=%s source=%s err=%s",
                            interval, source, exc,
                        )
                        return 0
                    logger.exception("[RANKING SUMMARY LOCK PATCH] save failed interval=%s source=%s", interval, source)
                    return 0

            _persistence._ensure_local_ranking_summary_table = _core_ranking_lock_patched_ensure_local
            _persistence.ensure_ranking_summary_table = _core_ranking_lock_patched_ensure
            _persistence.ensure_table = _core_ranking_lock_patched_ensure
            _persistence.save_ranking_summary = _core_ranking_lock_patched_save
            _persistence.save_ranking_summary_df = _core_ranking_lock_patched_save
            _persistence.persist_ranking_summary = _core_ranking_lock_patched_save
            _persistence.persist_ranking_summary_df = _core_ranking_lock_patched_save
            _persistence.save = _core_ranking_lock_patched_save
            _persistence.save_df = _core_ranking_lock_patched_save

            logger.warning(
                "[RANKING SUMMARY LOCK PATCH] installed locked_skip=True ensure_timeout_note=original timeout; error downgraded to warning"
            )
    except Exception:
        logger.exception("[core.startup] ranking summary persistence lock patch install failed")

    try:
        from .allow_orders_runtime_patch import install_allow_orders_runtime_patch

        install_allow_orders_runtime_patch()
    except Exception:
        logger.exception("[core.startup] allow orders runtime patch install failed")

    try:
        from .exit_limit_board_touch_runtime_patch import install as install_exit_limit_board_touch_patch

        install_exit_limit_board_touch_patch()
    except Exception:
        logger.exception("[core.startup] exit board touch limit patch install failed")

    try:
        from .summary_existing_null_repair_patch import install as install_summary_existing_null_repair_patch

        install_summary_existing_null_repair_patch()
    except Exception:
        logger.exception("[core.startup] summary existing null repair patch install failed")

    try:
        from .daily_signal_cache_fast_startup_patch import install as install_daily_signal_cache_fast_startup_patch

        install_daily_signal_cache_fast_startup_patch()
    except Exception:
        logger.exception("[core.startup] daily signal cache fast startup patch install failed")


if _SKIP_ENTRY_RUNTIME_INIT_PATCHES:
    logger.warning(
        "[core.startup] database-context process detected (argv=%s); skipping "
        "entry/ranking/summary runtime init patches (main.py live-trading only)",
        sys.argv,
    )
else:
    _install_entry_runtime_init_patches()

from .startup import system_startup
from .summary_bootstrap import (
    bootstrap_summary,
    run_bootstrap_incremental_rebuild_if_available,
)

__all__ = [
    "system_startup",
    "bootstrap_summary",
    "run_bootstrap_incremental_rebuild_if_available",
]
