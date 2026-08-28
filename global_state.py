# ============================================================
# File   : global_state.py
# Version: V59.3-PRODUCTION-ENTRY-UNFILLED-CANCEL-WATCH
# ------------------------------------------------------------
# ✔ V59 全機能保持
# ✔ GlobalContext完全委譲
# ✔ 状態保持最小
# ✔ None崩壊完全防止
# ✔ PushState正式対応
# ✔ Summary互換完全維持
# ✔ RankingState API 完全互換
# ✔ ranking_snapshot dataframe生成
# ✔ PositionState API 修正対応
# ✔ RegimeState API 修正対応
# ✔ AIState API 修正対応
# ✔ orderflow互換安全化
# ✔ entry_inflight_lock 正規化
# ✔ fallback完全対応
# ✔ ranking engine compatibility
# ✔ set/get_merged_summary に source 引数対応
# ✔ set/get_multi_summary に source 引数対応
# ✔ get_positions() 旧API互換
# ✔ positions dataframe/dict/list fallback 対応
# ✔ entry_inflight_lock / push_lock / orderflow_lock の None fallback 強化
# ✔ positions 側 lock 未初期化時でも with 文で落ちない
# ✔ entry_pipeline の global_data.get_positions AttributeError 対応
# ✔ NEW: add_entry_inflight 時に未約定注文10秒取消監視を自動起動
# ============================================================

from __future__ import annotations

import logging
import os as _os
import threading
import threading as _threading
import time as _time
from typing import Any

import pandas as pd
from core.global_context.context import global_context as GC

_gs_logger = logging.getLogger(__name__)


# ------------------------------------------------------------
# 旧 GlobalData 型チェック互換
# ------------------------------------------------------------
try:
    from global_state_legacy import GlobalData as _LegacyGlobalData
except Exception:

    class _LegacyGlobalData:
        pass


# ============================================================
# GlobalDataCompat
# ============================================================

class GlobalDataCompat(_LegacyGlobalData):

    def __init__(self):

        # runtime軽量状態のみ保持
        self._trade_restricted = {}

        # fallback lock 群
        self._fallback_push_lock = threading.RLock()
        self._fallback_orderflow_lock = threading.RLock()
        self._fallback_entry_inflight_lock = threading.RLock()

    # ============================================================
    # internal helpers
    # ============================================================

    def _get_fallback_lock(self, name: str):
        try:
            lock = getattr(self, name, None)
            if lock is None:
                lock = threading.RLock()
                setattr(self, name, lock)
            return lock
        except Exception:
            return threading.RLock()

    @staticmethod
    def _normalize_position_collection(value: Any):
        """
        旧コード互換用に position collection を返す。

        entry_pipeline.py 旧実装は list[dict] を想定していたため、
        dict snapshot の場合は values() を list 化する。
        ただし DataFrame/list/tuple/set はそのまま扱える形で返す。
        """
        try:
            if value is None:
                return []

            if isinstance(value, pd.DataFrame):
                return value

            if isinstance(value, dict):
                vals = list(value.values())
                if vals and all(isinstance(v, dict) for v in vals):
                    return vals
                if any(k in value for k in ("symbol", "Symbol", "銘柄コード", "code", "stock_code")):
                    return [value]
                return vals

            if isinstance(value, (list, tuple, set)):
                return list(value)

            return []

        except Exception:
            return []

    @staticmethod
    def _schedule_unfilled_cancel_watch(symbol: str, order_id: str, side: str) -> None:
        """
        エントリー注文が一定秒数未約定なら取消する監視を起動する。
        import失敗や監視側例外で売買本体を止めない。
        """
        try:
            from trading.entry.unfilled_order_cancel_watch import schedule_cancel_unfilled_entry_order

            schedule_cancel_unfilled_entry_order(
                symbol=symbol,
                order_id=order_id,
                side=side,
            )
        except Exception:
            # global_state は薄い互換層なので、ここでは例外を握りつぶす。
            pass

    # ============================================================
    # PUSH DF 単調フレッシュネス保証 (P0-11)
    # ------------------------------------------------------------
    # 2026-08-27 のログ:
    #   15:04:41 [SUMMARY MAIN PUSH DF WRITEBACK] latest 14:08:10 -> 15:03:03
    #   15:11:09 [SUMMARY FORCE DIRECT 1M] reject stale push memory
    #            latest=14:08:10 age=3770.0s rows=644
    #   15:13:02 [SUMMARY MAIN PUSH DF WRITEBACK] latest 14:08:10 -> 15:11:04
    #   15:17:07 ... latest=14:08:10 rows=644
    # 書き戻すたびに「更新前」が 14:08:10 に戻っている。
    # つまり誰かが新しいフレームを古い 644 行フレームで上書きしていた。
    # そのせいで Summary の latest が 14:08 で固定され、
    # PUSH DB は 15:24:04 まで来ているのに merged が 73 分古いままだった。
    #
    # ここでは push_df をデータ記述子 (property) にして
    #   - 古いフレームでの上書きを拒否する
    #   - 拒否したときに書き手 (caller) を必ず名指しする
    # 逆流を潰すのが目的なので、同じ latest なら行数が多い方を採る
    # (履歴が削られた縮退フレームで置き換えられるのも防ぐ)。
    # ============================================================

    _PUSH_DF_LOCK = _threading.Lock()

    @staticmethod
    def _push_df_guard_enabled() -> bool:
        v = _os.getenv("PUSH_DF_MONOTONIC_GUARD")
        if v is None or str(v).strip() == "":
            return True
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}

    @staticmethod
    def _push_df_latest(df) -> Any:
        try:
            if not isinstance(df, pd.DataFrame) or df.empty:
                return None
            for col in ("datetime", "received_at", "time"):
                if col in df.columns:
                    ts = pd.to_datetime(df[col], errors="coerce").max()
                    if not pd.isna(ts):
                        return ts
            return None
        except Exception:
            return None

    @staticmethod
    def _push_df_caller() -> str:
        try:
            import inspect

            for fr in inspect.stack()[2:8]:
                mod = str(fr.frame.f_globals.get("__name__", ""))
                if mod and not mod.startswith("global_state"):
                    return "%s.%s:%d" % (mod, fr.function, fr.lineno)
        except Exception:
            pass
        return "<unknown>"

    @property
    def push_df(self):
        return getattr(self, "_push_df_value", None)

    @push_df.setter
    def push_df(self, value) -> None:
        if not GlobalDataCompat._push_df_guard_enabled():
            self._push_df_value = value
            return

        with GlobalDataCompat._PUSH_DF_LOCK:
            cur = getattr(self, "_push_df_value", None)
            cur_latest = GlobalDataCompat._push_df_latest(cur)
            new_latest = GlobalDataCompat._push_df_latest(value)

            # 現在が空 / 判定不能なら素直に採用する。
            if cur_latest is None or not isinstance(cur, pd.DataFrame) or cur.empty:
                self._push_df_value = value
                return

            # 新しい方が判定不能なら上書きしない (空/壊れたフレームで潰さない)。
            if new_latest is None:
                _gs_logger.warning(
                    "[PUSH DF GUARD] latest 不明のフレームで上書きしようとした -> 拒否 "
                    "caller=%s new_rows=%s current_latest=%s current_rows=%s",
                    GlobalDataCompat._push_df_caller(),
                    len(value) if isinstance(value, pd.DataFrame) else None,
                    cur_latest, len(cur),
                )
                return

            if new_latest < cur_latest:
                _gs_logger.warning(
                    "[PUSH DF GUARD] 古いフレームでの上書きを拒否 caller=%s "
                    "current_latest=%s current_rows=%s new_latest=%s new_rows=%s "
                    "(fresh を stale で潰さない。PUSH_DF_MONOTONIC_GUARD=0 で従来動作)",
                    GlobalDataCompat._push_df_caller(),
                    cur_latest, len(cur), new_latest,
                    len(value) if isinstance(value, pd.DataFrame) else None,
                )
                return

            if new_latest == cur_latest:
                new_rows = len(value) if isinstance(value, pd.DataFrame) else 0
                if new_rows < len(cur):
                    _gs_logger.warning(
                        "[PUSH DF GUARD] 同じ latest でより少ない行数の上書きを拒否 "
                        "caller=%s latest=%s current_rows=%s new_rows=%s",
                        GlobalDataCompat._push_df_caller(),
                        cur_latest, len(cur), new_rows,
                    )
                    return

            self._push_df_value = value

    # ============================================================
    # PUSH
    # ============================================================

    @property
    def push_lock(self):
        try:
            lock = getattr(GC.push, "_lock", None)
            return lock if lock is not None else self._get_fallback_lock("_fallback_push_lock")
        except Exception:
            return self._get_fallback_lock("_fallback_push_lock")

    @property
    def push_buffer(self):

        try:
            ticks = getattr(GC.push, "_latest_ticks", {})
            return list(ticks.values())
        except Exception:
            return []

    def get_latest_tick(self, symbol):
        try:
            return GC.push.get_tick(symbol)
        except Exception:
            return None

    def has_tick(self, symbol):
        try:
            return GC.push.get_tick(symbol) is not None
        except Exception:
            return False

    # ------------------------------------------------------------
    # push_df 互換
    # ------------------------------------------------------------

    def get_push_df(self):

        try:

            if hasattr(GC.summary, "get_push_df"):
                df = GC.summary.get_push_df()

                if isinstance(df, pd.DataFrame):
                    return df

            snapshot = GC.push.snapshot()

            if snapshot:
                return pd.DataFrame(snapshot.values())

            return pd.DataFrame()

        except Exception:
            return pd.DataFrame()

    def set_push_df(self, df):

        try:

            if df is None:
                return

            if hasattr(GC.summary, "set_push_df"):
                GC.summary.set_push_df(df)

        except Exception:
            pass

    # ============================================================
    # ORDERFLOW
    # ============================================================

    @property
    def orderflow_buffer(self):

        try:

            if hasattr(GC.push, "_orderflow_buffer"):
                return GC.push._orderflow_buffer

            if hasattr(GC, "orderflow") and hasattr(GC.orderflow, "_ticks"):
                return GC.orderflow._ticks

            return {}

        except Exception:
            return {}

    @property
    def orderflow_lock(self):

        try:

            if hasattr(GC.push, "_orderflow_lock"):
                lock = GC.push._orderflow_lock
                if lock is not None:
                    return lock

            if hasattr(GC, "orderflow") and hasattr(GC.orderflow, "_lock"):
                lock = GC.orderflow._lock
                if lock is not None:
                    return lock

            return self._get_fallback_lock("_fallback_orderflow_lock")

        except Exception:
            return self._get_fallback_lock("_fallback_orderflow_lock")

    # ============================================================
    # SUMMARY
    # ============================================================

    @property
    def summary_cache(self):
        return getattr(GC.summary, "_merged", {})

    def get_summary_cache(self, interval):
        try:
            return GC.summary.get_merged(interval)
        except Exception:
            return pd.DataFrame()

    def set_summary_cache(self, interval, df):

        try:
            GC.summary.set_merged(interval, df)
        except Exception:
            pass

    def get_summary(self, interval):

        try:

            df = GC.summary.to_dataframe(interval)

            return df if isinstance(df, pd.DataFrame) else pd.DataFrame()

        except Exception:
            return pd.DataFrame()

    def get_latest_summary_row(self, interval, symbol):

        try:

            df = GC.summary.to_dataframe(interval)

            if df is None or df.empty:
                return None

            rows = df[df["symbol"] == symbol]

            if rows.empty:
                return None

            return rows.iloc[-1]

        except Exception:
            return None

    # ============================================================
    # MULTI / MERGED SUMMARY
    # ============================================================

    def set_multi_summary(self, interval, df, source=None):

        try:
            try:
                GC.set_multi_summary(interval, df, source=source)
            except TypeError:
                GC.set_multi_summary(interval, df)
        except Exception:
            pass

    def get_multi_summary(self, interval, source=None):

        try:
            try:
                return GC.get_multi_summary(interval, source=source)
            except TypeError:
                return GC.get_multi_summary(interval)
        except Exception:
            return None

    # --------------------------------------------------------
    # setter レベル enrich の重複排除
    # --------------------------------------------------------
    _SETTER_ENRICH_LOCK = _threading.Lock()
    _SETTER_ENRICH_SEEN: dict = {}
    # P0-7: context ごとに走っている enrich スレッド (同時 1 本まで)
    _SETTER_ENRICH_INFLIGHT: dict = {}
    _SETTER_ENRICH_STARTED: dict = {}

    @staticmethod
    def _setter_enrich_env_bool(name, default):
        try:
            v = _os.getenv(name)
            if v is None or str(v).strip() == "":
                return bool(default)
            return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}
        except Exception:
            return bool(default)

    @staticmethod
    def _setter_enrich_max_rows():
        try:
            return int(float(_os.getenv("SUMMARY_SETTER_ENRICH_MAX_ROWS", "3000")))
        except Exception:
            return 3000

    @staticmethod
    def _setter_enrich_budget_sec():
        """setter 内 enrich の上限秒数。0 以下で無制限 (従来動作)。

        setter の目標は projection + atomic publish で 0.5 秒以内。
        enrich が cold cache で数秒かかるときは待たずに publish する。
        """
        try:
            return float(_os.getenv("SUMMARY_SETTER_ENRICH_BUDGET_SEC", "0.5"))
        except Exception:
            return 0.5

    @staticmethod
    def _setter_enrich_signature(interval, df):
        """同じ内容のフレームかどうかを安く判定する署名。"""
        try:
            latest = ""
            if "datetime" in df.columns:
                ts = pd.to_datetime(df["datetime"], errors="coerce").max()
                latest = "" if pd.isna(ts) else str(ts)
            nsym = int(df["symbol"].nunique()) if "symbol" in df.columns else -1
            ssum = 0.0
            for c in ("score", "score_buy", "score_sell", "close"):
                if c in df.columns:
                    v = pd.to_numeric(df[c], errors="coerce").sum()
                    if v == v:  # not NaN
                        ssum += float(v)
            return "%s|%d|%d|%d|%s|%.4f" % (interval, len(df), len(df.columns), nsym, latest, ssum)
        except Exception:
            return None

    @staticmethod
    def _projection_enabled():
        v = _os.getenv("SUMMARY_MERGED_REALTIME_PROJECTION")
        if v is None or str(v).strip() == "":
            return True
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}

    @staticmethod
    def _projection_max_rows():
        """この行数以下なら既に projection 済みとみなして触らない。"""
        try:
            return int(float(_os.getenv("SUMMARY_MERGED_PROJECTION_MAX_ROWS", "1200")))
        except Exception:
            return 1200

    @staticmethod
    def _projection_always():
        """行数が銘柄数を超えていれば必ず畳むか。既定 True。

        realtime merged の定義は「1 symbol = 最新完成足 1 行」なので、
        行数の大小に関係なく畳むのが正しい。
        SUMMARY_MERGED_PROJECTION_ALWAYS=0 で旧挙動 (閾値超えだけ畳む)。
        """
        v = _os.getenv("SUMMARY_MERGED_PROJECTION_ALWAYS")
        if v is None or str(v).strip() == "":
            return True
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}

    @staticmethod
    def _realtime_projection(df, interval, context):
        """publish 境界で realtime_projection に落とす。

        1 symbol = 当日の最新完成足 1 行。
        履歴フレーム (22,155 行など) を merged へ渡さないための最後の砦。
        """
        if not GlobalDataCompat._projection_enabled():
            return df
        # P0-9: 共通ガードを先に通し、REALTIME_HISTORY_LEAK として
        # caller / rows_before / rows_after / latest_before / latest_after を残す。
        try:
            from trading.summary.realtime_history_guard import to_latest_projection

            df = to_latest_projection(df, caller=context, interval=interval)
        except Exception:
            _gs_logger.debug("[REALTIME_HISTORY_LEAK] guard unavailable context=%s",
                             context, exc_info=True)
        try:
            if not isinstance(df, pd.DataFrame) or df.empty:
                return df
            if "symbol" not in df.columns or "datetime" not in df.columns:
                return df
            n_in = len(df)
            # P0-5: realtime merged は「1 symbol = 1 行」が定義。
            #
            # 旧実装は rows > SUMMARY_MERGED_PROJECTION_MAX_ROWS (1200) の
            # ときしか畳んでいなかった。2026-08-28 は
            #   rows=881 / symbols=214 (= 銘柄あたり平均 4.1 本)
            # が閾値を下回るのでそのまま enrich へ渡り、
            # setter enrich だけで 9.50 秒かかっていた。
            # 同じ enrich が 214 行なら 0.069 秒。
            #
            # 履歴が要るのは indicator / storage 側であって、
            # merged publish ではない。行数が銘柄数を超えていれば畳む。
            try:
                n_sym = int(df["symbol"].nunique())
            except Exception:
                n_sym = -1
            if n_sym <= 0 or n_in <= n_sym:
                return df
            if n_in <= GlobalDataCompat._projection_max_rows() and not GlobalDataCompat._projection_always():
                return df

            t0 = _time.perf_counter()
            out = df
            dtc = pd.to_datetime(out["datetime"], errors="coerce")

            # 1. 当日のみ (過去日は indicator seed 専用であって current projection ではない)
            n_before_today = len(out)
            try:
                today = pd.Timestamp.now().normalize()
                mask_today = dtc.dt.normalize() == today
                if mask_today.any():
                    out = out[mask_today]
                    dtc = dtc[mask_today]
            except Exception:
                _gs_logger.debug("[SUMMARY PROJECTION] today filter failed", exc_info=True)
            n_after_today = len(out)

            # 2. symbol ごとに最新 1 行
            try:
                work = out.assign(_dt=dtc.values)
                work = work.sort_values("_dt", kind="stable")
                work = work.drop_duplicates("symbol", keep="last")
                out = work.drop(columns=["_dt"], errors="ignore")
            except Exception:
                _gs_logger.debug("[SUMMARY PROJECTION] latest-per-symbol failed", exc_info=True)
                return df

            el = _time.perf_counter() - t0
            _gs_logger.warning(
                "[SUMMARY PROJECTION] context=%s interval=%s rows %d -> %d "
                "(today filter %d -> %d, non_today=%d) symbols=%d elapsed=%.3fs "
                "(履歴フレームを realtime projection へ落とした)",
                context, interval, n_in, len(out), n_before_today, n_after_today,
                n_before_today - n_after_today,
                int(out["symbol"].nunique()) if "symbol" in out.columns else -1, el,
            )
            return out.reset_index(drop=True)
        except Exception:
            _gs_logger.exception("[SUMMARY PROJECTION] failed context=%s interval=%s",
                                 context, interval)
            return df

    @staticmethod
    def _stage_ctx():
        """P0-1: setter を Summary worker の stage として計測する。"""
        try:
            from scheduler_jobs.summary import stage_tracker as _st
            return _st
        except Exception:
            return None

    @classmethod
    def _setter_enrich(cls, df, interval, context):
        """setter から呼ぶ enrich。重複と巨大フレームを避ける。

        _enrich は冪等なので、同じ内容を何度も enrich するのは無駄。
        実測で 1 ジョブあたり 56 回 / 169.6 秒かかっていた。
        """
        try:
            import trading.summary.summary_controller as sc
        except Exception:
            return df
        try:
            if getattr(sc, "_LATEST_ENRICH_REFRESHING_1M", False):
                return df
        except Exception:
            pass

        max_rows = cls._setter_enrich_max_rows()
        if max_rows > 0 and len(df) > max_rows:
            # 履歴フレーム。enrich が効くのは銘柄あたり最新行なので省く。
            # 黙って省かず必ず残す。
            _gs_logger.warning(
                "[SUMMARY SETTER ENRICH] skipped context=%s interval=%s rows=%s > max_rows=%s "
                "(履歴フレームには setter レベル enrich をかけない; "
                "SUMMARY_SETTER_ENRICH_MAX_ROWS=0 で従来動作)",
                context, interval, len(df), max_rows,
            )
            return df

        sig = None
        if cls._setter_enrich_env_bool("SUMMARY_SETTER_ENRICH_DEDUPE", True):
            sig = cls._setter_enrich_signature(interval, df)
            if sig is not None:
                with cls._SETTER_ENRICH_LOCK:
                    hit = cls._SETTER_ENRICH_SEEN.get(sig)
                if hit is not None:
                    _gs_logger.info(
                        "[SUMMARY SETTER ENRICH] dedupe hit context=%s interval=%s rows=%s "
                        "(同一内容は enrich 済み; 冪等なので省略)",
                        context, interval, len(df),
                    )
                    return df

        # P0-1: worker が cancel されていたら enrich に入らない。
        # enrich は NAS を触りうるので、ここが最後の降り場所になる。
        _st = cls._stage_ctx()
        if _st is not None:
            try:
                if _st.should_stop("setter_enrich:%s" % context):
                    _gs_logger.warning(
                        "[SUMMARY SETTER ENRICH] skipped by cooperative stop context=%s interval=%s rows=%s",
                        context, interval, len(df),
                    )
                    return df
            except Exception:
                _gs_logger.debug("[SUMMARY SETTER ENRICH] should_stop failed", exc_info=True)

        # ------------------------------------------------------------
        # P0-6: setter は「重い enrich 関数」であってはいけない。
        #
        # setter の責務は validate / latest projection / atomic publish まで。
        # ranking / daily enrich は NAS を読むので、cold cache のときは
        # 数秒〜十数秒かかる (2026-08-28 実測:
        #   rows=881 daily_mtf=6.682s ranking=2.410s total=9.093s
        #   rows=214 daily_mtf=0.012s ranking=0.057s total=0.069s
        # 差は行数ではなく cache が cold かどうか)。
        #
        # 予算を超えたら enrich を諦めて publish を先に通す。
        # enrich は冪等で、後段の before-display / cache-latest でも
        # 走るのでそこで cache が温まる。値が欠けるのではなく
        # 「今 tick では付かない」だけ。
        # ------------------------------------------------------------
        # ------------------------------------------------------------
        # P0-7: enrich スレッドを溜めない。
        #
        # 前ラウンドの実装は「呼び出しごとに Thread を作って join(budget)」
        # だった。予算超過したスレッドは **放置されたまま走り続ける**ので、
        # 毎 tick 積み上がる。2026-08-28 12:33 の実測:
        #   threads_top={'setter-enrich-global-set_merged': 4,
        #                'setter-enrich-global-set_push_merged': 3, ...}
        #   rss_mb 1774.6 -> 6282.0 (8 分)
        # 放置されたスレッドが DataFrame を掴んだまま NAS を読み続けるため、
        # スレッド数もメモリも増え続けていた。これは前ラウンドで入れた回帰。
        #
        # 対策: context ごとに **同時 1 本だけ**。
        # 前の enrich がまだ走っているなら新しく起動せず即 publish する。
        # enrich は冪等なので、走っている方が終われば次 tick で反映される。
        # ------------------------------------------------------------
        budget = cls._setter_enrich_budget_sec()
        t0 = _time.perf_counter()
        box: dict = {}

        def _run_enrich() -> None:
            try:
                if _st is not None:
                    with _st.stage(context):
                        box["out"] = sc._enrich(df, interval=interval, context=context)
                else:
                    box["out"] = sc._enrich(df, interval=interval, context=context)
            except BaseException as e:  # noqa: BLE001
                box["err"] = e

        if budget <= 0:
            _run_enrich()
        else:
            key = str(context)
            with cls._SETTER_ENRICH_LOCK:
                inflight = cls._SETTER_ENRICH_INFLIGHT.get(key)
                busy = inflight is not None and inflight.is_alive()
                if busy:
                    started = cls._SETTER_ENRICH_STARTED.get(key)
                    age = None if started is None else (_time.perf_counter() - started)
            if busy:
                _gs_logger.warning(
                    "[SUMMARY SETTER ENRICH] 前回の enrich がまだ走っているので起動しない "
                    "context=%s interval=%s rows=%s inflight_age=%s "
                    "(スレッドを積み上げない。enrich は冪等なので次 tick で反映される)",
                    context, interval, len(df),
                    None if age is None else round(age, 2),
                )
                return df

            th = _threading.Thread(
                target=_run_enrich, name="setter-enrich-%s" % context, daemon=True
            )
            with cls._SETTER_ENRICH_LOCK:
                cls._SETTER_ENRICH_INFLIGHT[key] = th
                cls._SETTER_ENRICH_STARTED[key] = _time.perf_counter()
            th.start()
            th.join(budget)
            if th.is_alive():
                _gs_logger.warning(
                    "[SUMMARY SETTER ENRICH] budget 超過のため enrich を待たずに publish する "
                    "context=%s interval=%s rows=%s budget=%.2fs "
                    "(setter は projection と atomic publish が責務。"
                    "enrich は後段の before-display / cache-latest で付く。"
                    "このスレッドは次回完了まで新規起動をブロックする。"
                    "SUMMARY_SETTER_ENRICH_BUDGET_SEC=0 で従来動作)",
                    context, interval, len(df), budget,
                )
                return df
            with cls._SETTER_ENRICH_LOCK:
                cls._SETTER_ENRICH_INFLIGHT.pop(key, None)
                cls._SETTER_ENRICH_STARTED.pop(key, None)

        if "err" in box:
            _gs_logger.exception(
                "[SUMMARY SETTER ENRICH] failed context=%s interval=%s err=%r",
                context, interval, box["err"],
            )
            return df
        out = box.get("out")
        el = _time.perf_counter() - t0
        log = _gs_logger.warning if el >= budget and budget > 0 else _gs_logger.info
        log(
            "[SUMMARY SETTER ENRICH] done context=%s interval=%s rows=%s elapsed=%.2fs budget=%.2fs",
            context, interval, len(df), el, budget,
        )
        if sig is not None:
            with cls._SETTER_ENRICH_LOCK:
                cls._SETTER_ENRICH_SEEN[sig] = _time.time()
                if len(cls._SETTER_ENRICH_SEEN) > 64:
                    for k in sorted(cls._SETTER_ENRICH_SEEN,
                                    key=lambda x: cls._SETTER_ENRICH_SEEN[x])[:32]:
                        cls._SETTER_ENRICH_SEEN.pop(k, None)
        return out if isinstance(out, pd.DataFrame) else df

    def set_merged_summary(self, interval, df, source=None):
        # 旧 core/startup/summary_controller_latest_enrich_patch.py の
        # _patch_global_data_setters から移設 (interval==1のenrich + 3/5mのMTF逆流refresh)。
        is_df = isinstance(df, pd.DataFrame) and not df.empty
        if is_df and interval == 1:
            df = GlobalDataCompat._realtime_projection(df, 1, "global-set_merged_summary")
            df = GlobalDataCompat._setter_enrich(df, 1, "global-set_merged_summary")

        try:
            try:
                GC.set_merged_summary(interval, df, source=source)
            except TypeError:
                GC.set_merged_summary(interval, df)
        except Exception:
            pass

        if is_df and interval in (3, 5):
            try:
                import trading.summary.summary_controller as sc
                sc._refresh_1m_mtf_from_global(f"global-set_merged_summary-{interval}m")
            except Exception:
                pass

    def get_merged_summary(self, interval, source=None):

        try:
            try:
                return GC.get_merged_summary(interval, source=source)
            except TypeError:
                return GC.get_merged_summary(interval)
        except Exception:
            return None

    # ------------------------------------------------------------
    # explicit separated helpers
    # ------------------------------------------------------------

    def set_push_merged_summary(self, interval, df):
        # 旧 core/startup/summary_controller_latest_enrich_patch.py の
        # _patch_global_data_setters から移設 (interval==1のenrich + 3/5mのMTF逆流refresh)。
        is_df = isinstance(df, pd.DataFrame) and not df.empty
        if is_df and interval == 1:
            df = GlobalDataCompat._realtime_projection(df, 1, "global-set_push_merged_summary")
            df = GlobalDataCompat._setter_enrich(df, 1, "global-set_push_merged_summary")

        try:
            if hasattr(GC, "set_push_merged_summary"):
                GC.set_push_merged_summary(interval, df)
            else:
                self.set_merged_summary(interval, df, source="push")
        except Exception:
            pass

        if is_df and interval in (3, 5):
            try:
                import trading.summary.summary_controller as sc
                sc._refresh_1m_mtf_from_global(f"global-set_push_merged_summary-{interval}m")
            except Exception:
                pass

    def get_push_merged_summary(self, interval):

        try:
            if hasattr(GC, "get_push_merged_summary"):
                return GC.get_push_merged_summary(interval)
            return self.get_merged_summary(interval, source="push")
        except Exception:
            return None

    def set_summary_history(self, tf, df, source="legacy", caller=None):
        # 旧 core/startup/summary_controller_latest_enrich_patch.py の
        # _patch_global_data_setters から移設。GlobalContext.set_summary_history 本体や
        # モジュール関数 core.global_context.context.set_summary_history 経由の直呼び出しには
        # 影響させず、global_data 経由の呼び出しだけをここで明示的に処理する。
        is_df = isinstance(df, pd.DataFrame) and not df.empty
        if is_df and tf == 1:
            df = GlobalDataCompat._setter_enrich(df, 1, "global-set_summary_history")

        try:
            GC.set_summary_history(tf, df, source=source, caller=caller)
        except Exception:
            pass

        if is_df and tf in (3, 5):
            try:
                import trading.summary.summary_controller as sc
                sc._refresh_1m_mtf_from_global(f"global-set_summary_history-{tf}m")
            except Exception:
                pass

    @staticmethod
    def _ranking_realtime_max_age_sec():
        """realtime ranking merged として認める最大齢。"""
        try:
            return float(_os.getenv("RANKING_REALTIME_MAX_AGE_SEC", "900"))
        except Exception:
            return 900.0

    def set_ranking_merged_summary(self, interval, df):
        # ============================================================
        # 追加P0-B: stale な bootstrap/history を realtime global へ出さない
        # ------------------------------------------------------------
        # 2026-08-28 14:00:41 の実測:
        #   [MERGED SET] tf=3 source=ranking rows=0 completed=False
        #   [RANKING SUMMARY BOOTSTRAP CACHE]
        #       global_data.set_ranking_merged_summary rows=33650
        #   built=33650 saved=12975 latest_dt=11:09:00
        # 現在時刻 14:00 に対して約 3 時間古い履歴フレームを
        # realtime global state へ publish していた。
        #
        # 履歴は indicator seed / historical cache 専用。
        # realtime へ出すのは
        #   1 symbol = 1 latest row / latest_dt が fresh
        # を満たすものだけにする。
        # stale の場合は historical 側 (ranking_history_merged_*) に置き、
        # Entry / Display / realtime merged には出さない。
        # 閾値は緩めない。stale は publish しないだけ。
        # ============================================================
        try:
            if isinstance(df, pd.DataFrame) and not df.empty:
                projected = GlobalDataCompat._realtime_projection(
                    df, interval, "global-set_ranking_merged_summary"
                )
                latest = GlobalDataCompat._push_df_latest(projected)
                max_age = GlobalDataCompat._ranking_realtime_max_age_sec()
                age = None
                if latest is not None:
                    try:
                        age = float(
                            (pd.Timestamp.now() - pd.Timestamp(latest)).total_seconds()
                        )
                    except Exception:
                        age = None

                if latest is None or (age is not None and age > max_age):
                    # historical 側にだけ残す。realtime へは出さない。
                    try:
                        setattr(self, "ranking_history_merged_%s" % interval, df)
                    except Exception:
                        _gs_logger.debug(
                            "[RANKING REALTIME GUARD] historical cache set failed interval=%s",
                            interval, exc_info=True,
                        )
                    _gs_logger.warning(
                        "[RANKING REALTIME GUARD] stale/unknown なので realtime へ publish しない "
                        "interval=%s rows=%s projected_rows=%s latest=%s age=%s max_age=%.0fs "
                        "(履歴は ranking_history_merged_%s へ退避。"
                        "Entry/Display/realtime merged には出さない。"
                        "RANKING_REALTIME_MAX_AGE_SEC で調整可)",
                        interval, len(df), len(projected), latest,
                        None if age is None else round(age, 1), max_age, interval,
                    )
                    return

                df = projected
        except Exception:
            _gs_logger.exception(
                "[RANKING REALTIME GUARD] 判定に失敗 interval=%s -> publish しない (fail-close)",
                interval,
            )
            return

        try:
            if hasattr(GC, "set_ranking_merged_summary"):
                GC.set_ranking_merged_summary(interval, df)
            else:
                self.set_merged_summary(interval, df, source="ranking")
        except Exception:
            _gs_logger.debug(
                "[RANKING REALTIME GUARD] publish failed interval=%s", interval, exc_info=True
            )

    def get_ranking_merged_summary(self, interval):

        try:
            if hasattr(GC, "get_ranking_merged_summary"):
                return GC.get_ranking_merged_summary(interval)
            return self.get_merged_summary(interval, source="ranking")
        except Exception:
            return None

    # ============================================================
    # RANKING
    # ============================================================

    def get_latest_ranking(self, symbol):

        try:
            return GC.ranking.get(symbol)
        except Exception:
            return None

    def set_latest_ranking_snapshot(self, snapshot: dict, snapshot_time=None):

        try:
            return GC.ranking.set_latest_snapshot(snapshot, snapshot_time)
        except Exception:
            return None

    def get_latest_ranking_snapshot(self):

        try:
            return GC.ranking.get_latest_snapshot()
        except Exception:
            return {}

    # ------------------------------------------------------------
    # ranking snapshot dataframe
    # ------------------------------------------------------------

    def get_ranking_snapshot(self):

        """
        ranking engine互換API
        """

        try:

            snapshot = GC.ranking.get_latest_snapshot()

            if not snapshot:
                return pd.DataFrame()

            if isinstance(snapshot, dict):
                return pd.DataFrame(snapshot.values())

            if isinstance(snapshot, list):
                return pd.DataFrame(snapshot)

            return pd.DataFrame()

        except Exception:
            return pd.DataFrame()

    def get_ranking_snapshot_1min(self):

        try:

            snapshot = GC.ranking.get_latest_snapshot()

            if not snapshot:
                return pd.DataFrame()

            return pd.DataFrame(snapshot.values())

        except Exception:
            return pd.DataFrame()

    # ============================================================
    # POSITION
    # ============================================================

    @property
    def open_positions(self):

        try:
            return GC.positions.snapshot_dict()
        except Exception:
            return {}

    def get_positions(self):
        """
        旧 GlobalData 互換API。

        entry_pipeline.py など既存コードが
            global_data.get_positions()
        を呼ぶため、GlobalDataCompat でも必ず提供する。

        戻り値は旧実装に寄せて list[dict] を基本とする。
        DataFrame で保持されている場合は DataFrame のまま返す。
        何も取得できない場合は [] を返し、発注処理を AttributeError で止めない。
        """
        candidates = []

        try:
            if hasattr(GC.positions, "snapshot_dict"):
                candidates.append(GC.positions.snapshot_dict())
        except Exception:
            pass

        try:
            if hasattr(GC.positions, "snapshot"):
                candidates.append(GC.positions.snapshot())
        except Exception:
            pass

        for attr in (
            "positions",
            "open_positions",
            "current_positions",
            "holdings",
            "position_cache",
            "positions_cache",
            "position_df",
            "positions_df",
            "_positions",
            "_open_positions",
            "_holdings",
            "_position_cache",
        ):
            try:
                if hasattr(GC.positions, attr):
                    candidates.append(getattr(GC.positions, attr))
            except Exception:
                pass

        for value in candidates:
            normalized = self._normalize_position_collection(value)
            try:
                if isinstance(normalized, pd.DataFrame):
                    if not normalized.empty:
                        return normalized
                elif normalized:
                    return normalized
            except Exception:
                continue

        return []

    def get_position(self, symbol):

        try:
            return GC.positions.get(symbol)
        except Exception:
            return None

    @property
    def entry_inflight(self):
        try:
            return getattr(GC.positions, "_entry_inflight", set())
        except Exception:
            return set()

    @property
    def entry_inflight_lock(self):
        """
        重要:
        呼び出し側に with global_data.entry_inflight_lock があるため、
        None を返さず必ず lock object を返す。
        """
        try:
            lock = getattr(GC.positions, "_entry_inflight_lock", None)
            if lock is not None:
                return lock

            if hasattr(GC.positions, "entry_inflight_lock"):
                lock = getattr(GC.positions, "entry_inflight_lock", None)
                if lock is not None:
                    return lock

            return self._get_fallback_lock("_fallback_entry_inflight_lock")

        except Exception:
            return self._get_fallback_lock("_fallback_entry_inflight_lock")

    def add_entry_inflight(self, symbol: str, order_id: str, side: str):
        """
        エントリー注文のinflight登録。

        NEW:
          登録後に未約定取消ウォッチを起動する。
          ENTRY_UNFILLED_CANCEL_SECONDS の既定値は10秒。
        """
        try:
            ret = GC.positions.add_entry_inflight(symbol, order_id, side)
            self._schedule_unfilled_cancel_watch(symbol, order_id, side)
            return ret
        except Exception:
            try:
                with self.entry_inflight_lock:
                    inflight = getattr(GC.positions, "_entry_inflight", None)
                    if inflight is None:
                        inflight = set()
                        setattr(GC.positions, "_entry_inflight", inflight)
                    inflight.add(symbol)
            except Exception:
                pass
            self._schedule_unfilled_cancel_watch(symbol, order_id, side)

    def remove_entry_inflight(self, symbol: str):

        try:
            return GC.positions.release_entry_inflight(symbol)
        except Exception:
            try:
                with self.entry_inflight_lock:
                    inflight = getattr(GC.positions, "_entry_inflight", None)
                    if isinstance(inflight, set):
                        inflight.discard(symbol)
            except Exception:
                pass

    def is_entry_inflight(self, symbol: str) -> bool:

        try:
            return GC.positions.is_entry_inflight(symbol)
        except Exception:
            try:
                inflight = getattr(GC.positions, "_entry_inflight", None)
                return symbol in inflight if isinstance(inflight, set) else False
            except Exception:
                return False

    # ============================================================
    # RATE LIMIT
    # ============================================================

    @property
    def trade_restricted(self):
        return self._trade_restricted

    # ============================================================
    # FORCE EXIT
    # ============================================================

    @property
    def force_exit(self):
        return GC.ai.get_force_exit()

    @force_exit.setter
    def force_exit(self, value):
        GC.ai.set_force_exit(value)

    # ============================================================
    # REGIME
    # ============================================================

    def get_regime(self, regime_model, market_state):

        try:

            GC.regime.set_model(regime_model)

            return GC.regime.predict(market_state)

        except Exception:
            return None

    # ============================================================
    # AI
    # ============================================================

    def ai_enabled(self):

        try:
            return GC.ai.get_feature_cache() is not None
        except Exception:
            return False

    # ============================================================
    # COOLDOWN
    # ============================================================

    @property
    def entry_cooldown_until(self):

        try:
            return getattr(GC.positions, "_cooldown_until", {})
        except Exception:
            return {}

    # ============================================================
    # 透過委譲
    # ============================================================

    def __getattr__(self, item):

        if hasattr(GC, item):
            return getattr(GC, item)

        raise AttributeError(
            f"'GlobalDataCompat' has no attribute '{item}'"
        )


# ============================================================
# Singleton
# ============================================================

global_data = GlobalDataCompat()
