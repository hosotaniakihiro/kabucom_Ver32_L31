# ============================================================
# hybrid_1m_engine.py
# PRODUCTION-HYBRID-1M-ENGINE-FINAL-STABLE-RESOLVER-SCORING
# ------------------------------------------------------------
# ✔ Yahoo履歴ベース
# ✔ 最新20分はPUSH優先
# ✔ indicator一括計算
# ✔ scoring一括実行
# ✔ 二重計算完全排除
# ✔ SQLite Timestamp binding 完全防止
# ✔ scoring lazy import（循環import防止）
# ✔ scoring resolver（関数名差異吸収）
# ✔ 本番安定版
# ============================================================

from __future__ import annotations

import datetime as dt
import importlib
import logging
from functools import lru_cache
from typing import Any, Callable

import pandas as pd
from sqlalchemy import text

from database.session import get_summary_engine
from trading.summary.indicators.indicator_calculator import add_all_indicators

logger = logging.getLogger(__name__)

HYBRID_CUTOFF_MINUTES = 20


# ============================================================
# Lazy import / resolver helper
# ============================================================

@lru_cache(maxsize=1)
def _get_scoring_main() -> Callable[..., Any]:
    """
    循環 import 回避のため scoring 関数は遅延 import する。
    さらに関数名揺れを吸収する。
    """
    candidates: list[tuple[str, str]] = [
        ("trading.scoring.core.scoring_core", "scoring_main"),
        ("trading.scoring.core.scoring_core", "run_scoring"),
        ("trading.scoring.core.scoring_core", "apply_scoring"),
        ("trading.scoring.core.scoring_core", "score_dataframe"),
        ("trading.ranking.scoring.scoring_pipeline", "scoring_main"),
        ("trading.ranking.scoring.scoring_pipeline", "run_scoring"),
        ("trading.ranking.scoring.scoring_pipeline", "apply_scoring"),
        ("trading.ranking.scoring.scoring_pipeline", "score_dataframe"),
    ]

    errors: list[str] = []

    for module_name, attr_name in candidates:
        try:
            mod = importlib.import_module(module_name)
            fn = getattr(mod, attr_name, None)

            if callable(fn):
                logger.info(
                    "[HYBRID] resolved scoring function -> %s.%s",
                    module_name,
                    attr_name,
                )
                return fn

            errors.append(f"{module_name}.{attr_name}: not callable or missing")

        except Exception as e:
            errors.append(f"{module_name}.{attr_name}: {type(e).__name__}: {e}")

    raise RuntimeError("HYBRID scoring unresolved: " + " | ".join(errors))


def _apply_scoring_best_effort(
    fn: Callable[..., Any],
    df: pd.DataFrame,
    *,
    interval: int,
) -> pd.DataFrame:
    """
    scoring 関数のシグネチャ差異を吸収する。
    """
    attempts = [
        lambda: fn(df, interval=interval),
        lambda: fn(df, tf=interval),
        lambda: fn(df, timeframe=interval),
        lambda: fn(df, 1),
        lambda: fn(df),
    ]

    errors: list[str] = []

    for i, caller in enumerate(attempts, start=1):
        try:
            out = caller()
            if isinstance(out, pd.DataFrame):
                return out
            if out is None:
                return df
            return out
        except TypeError as e:
            errors.append(f"attempt{i}: TypeError: {e}")
        except Exception:
            raise

    raise TypeError("HYBRID scoring call failed: " + " | ".join(errors))


# ============================================================
# Timestamp安全変換
# ============================================================

def _safe_datetime(v):
    if v is None:
        return None

    if isinstance(v, pd.Timestamp):
        return v.to_pydatetime()

    if isinstance(v, dt.datetime):
        return v

    try:
        return pd.to_datetime(v).to_pydatetime()
    except Exception:
        return None


class Hybrid1MEngine:

    # ========================================================
    # Yahoo履歴ロード
    # ========================================================

    def _load_yahoo_history(self) -> pd.DataFrame:
        query = """
            SELECT *
            FROM stock_summary_1min
            WHERE source = 'yahoo'
            ORDER BY datetime ASC
        """

        try:
            with get_summary_engine().connect() as conn:
                df = pd.read_sql(text(query), conn)

            if df.empty:
                return pd.DataFrame()

            df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
            df = df.dropna(subset=["datetime"])

            return df

        except Exception:
            logger.exception("[HYBRID] Yahoo load failed")
            return pd.DataFrame()

    # ========================================================
    # PUSH確定ロード
    # ========================================================

    def _load_push_recent(self, cutoff: pd.Timestamp) -> pd.DataFrame:
        query = """
            SELECT *
            FROM stock_summary_1min
            WHERE source = 'push'
              AND datetime > :cutoff
            ORDER BY datetime ASC
        """

        try:
            cutoff = _safe_datetime(cutoff)

            with get_summary_engine().connect() as conn:
                df = pd.read_sql(
                    text(query),
                    conn,
                    params={"cutoff": cutoff},
                )

            if df.empty:
                return pd.DataFrame()

            df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
            df = df.dropna(subset=["datetime"])

            return df

        except Exception:
            logger.exception("[HYBRID] Push load failed")
            return pd.DataFrame()

    # ========================================================
    # ハイブリッド生成
    # ========================================================

    def build_hybrid_1m(self) -> pd.DataFrame:
        now = pd.Timestamp.now().floor("min")
        cutoff = now - pd.Timedelta(minutes=HYBRID_CUTOFF_MINUTES)

        logger.info(
            "[HYBRID] building hybrid 1min | cutoff=%s",
            cutoff,
        )

        df_yahoo = self._load_yahoo_history()
        df_push = self._load_push_recent(cutoff)

        if df_yahoo.empty and df_push.empty:
            logger.warning("[HYBRID] empty result")
            return pd.DataFrame()

        # ----------------------------------------------------
        # 履歴部分（Yahoo正）
        # ----------------------------------------------------
        if not df_yahoo.empty:
            df_hist = df_yahoo[df_yahoo["datetime"] <= cutoff].copy()
        else:
            df_hist = pd.DataFrame()

        # ----------------------------------------------------
        # 直近部分（Push正）
        # ----------------------------------------------------
        df_recent = df_push.copy() if isinstance(df_push, pd.DataFrame) else pd.DataFrame()

        # ----------------------------------------------------
        # 結合
        # ----------------------------------------------------
        df_merged = pd.concat(
            [df_hist, df_recent],
            ignore_index=True,
        )

        if "datetime" not in df_merged.columns:
            logger.warning("[HYBRID] datetime missing after concat")
            return pd.DataFrame()

        if "symbol" not in df_merged.columns:
            logger.warning("[HYBRID] symbol missing after concat")
            return pd.DataFrame()

        df_merged["datetime"] = pd.to_datetime(df_merged["datetime"], errors="coerce")
        df_merged = df_merged.dropna(subset=["symbol", "datetime"])

        df_merged = (
            df_merged
            .drop_duplicates(subset=["symbol", "datetime"], keep="last")
            .sort_values(["symbol", "datetime"])
            .reset_index(drop=True)
        )

        if df_merged.empty:
            logger.warning("[HYBRID] merged empty")
            return pd.DataFrame()

        # ----------------------------------------------------
        # indicator一括計算
        # ----------------------------------------------------
        try:
            df_merged = add_all_indicators(
                df_merged,
                interval=1,
            )
        except TypeError:
            try:
                df_merged = add_all_indicators(df_merged)
            except Exception:
                logger.exception("[HYBRID] indicator failed")
                return pd.DataFrame()
        except Exception:
            logger.exception("[HYBRID] indicator failed")
            return pd.DataFrame()

        if df_merged is None or df_merged.empty:
            logger.warning("[HYBRID] empty after indicators")
            return pd.DataFrame()

        # ----------------------------------------------------
        # scoring
        # ----------------------------------------------------
        try:
            scoring_main = _get_scoring_main()
            df_merged = _apply_scoring_best_effort(
                scoring_main,
                df_merged,
                interval=1,
            )
        except Exception:
            logger.exception("[HYBRID] scoring failed")

        if df_merged is None:
            return pd.DataFrame()

        logger.info(
            "[HYBRID] completed rows=%d",
            len(df_merged),
        )

        return df_merged


# ============================================================
# Singleton
# ============================================================

_hybrid_engine = None


def get_hybrid_1m_engine():
    global _hybrid_engine

    if _hybrid_engine is None:
        _hybrid_engine = Hybrid1MEngine()

    return _hybrid_engine


def build_confirmed_push_1min(df, now=None):
    if df is None or df.empty:
        return pd.DataFrame()

    df = df.copy()

    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
    df = df.dropna(subset=["datetime"])

    df = (
        df
        .sort_values("datetime")
        .drop_duplicates(["symbol", "datetime"], keep="last")
    )

    return df