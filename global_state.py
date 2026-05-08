# ============================================================
# File   : global_state.py
# Version: V59.2-PRODUCTION-THIN-COMPAT-LAYER-POSITIONS-API-FIX
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
# ✔ NEW: set/get_merged_summary に source 引数対応
# ✔ NEW: set/get_multi_summary に source 引数対応
# ✔ NEW: get_positions() 旧API互換を追加
# ✔ NEW: positions dataframe/dict/list fallback 対応
# ✔ FIX: entry_inflight_lock / push_lock / orderflow_lock の None fallback 強化
# ✔ FIX: positions 側 lock 未初期化時でも with 文で落ちない
# ✔ FIX: entry_pipeline の global_data.get_positions AttributeError 対応
# ============================================================

from __future__ import annotations

import threading
from typing import Any

import pandas as pd
from core.global_context.context import global_context as GC


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
                # {symbol: position_dict} 形式を優先して list[dict] にする
                vals = list(value.values())
                if vals and all(isinstance(v, dict) for v in vals):
                    return vals
                # 1ポジション分の dict の可能性
                if any(k in value for k in ("symbol", "Symbol", "銘柄コード", "code", "stock_code")):
                    return [value]
                return vals

            if isinstance(value, (list, tuple, set)):
                return list(value)

            return []

        except Exception:
            return []

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

    def set_merged_summary(self, interval, df, source=None):

        try:
            try:
                GC.set_merged_summary(interval, df, source=source)
            except TypeError:
                GC.set_merged_summary(interval, df)
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

        try:
            if hasattr(GC, "set_push_merged_summary"):
                GC.set_push_merged_summary(interval, df)
            else:
                self.set_merged_summary(interval, df, source="push")
        except Exception:
            pass

    def get_push_merged_summary(self, interval):

        try:
            if hasattr(GC, "get_push_merged_summary"):
                return GC.get_push_merged_summary(interval)
            return self.get_merged_summary(interval, source="push")
        except Exception:
            return None

    def set_ranking_merged_summary(self, interval, df):

        try:
            if hasattr(GC, "set_ranking_merged_summary"):
                GC.set_ranking_merged_summary(interval, df)
            else:
                self.set_merged_summary(interval, df, source="ranking")
        except Exception:
            pass

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

        try:
            return GC.positions.add_entry_inflight(symbol, order_id, side)
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
