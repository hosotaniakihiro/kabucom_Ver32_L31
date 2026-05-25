# ============================================================
# File   : core/startup/yahoo_complement_safety_patch.py
# Version: REV1-YAHOO-COMPLEMENT-NA-SCORE-GUARD
# ------------------------------------------------------------
# Purpose:
#   main_database.py 側の yahoo_complement_runner 用 runtime patch。
#
# Fixes:
#   1) trading/yahoo/pipeline/complement/compute.py の
#      ensure_actual_db_schema_columns() で default_value=pd.NA のとき
#      `default_value in (0, 0.0)` が TypeError になる問題を止める。
#      - datetime / last_update 系の不足列 default を pd.NA ではなく None にする。
#      - None は sqlite 保存時に NULL になり、bool判定エラーを起こさない。
#
#   2) 3分/5分 Yahoo 補完で OHLCV / RSI / MACD / slope は存在するのに
#      score / final_score / display_score が全行0のままになるケースを救済する。
#      - scoring_pipeline が列名不一致等で0点を返した場合でも、
#        slope / macd-hist / rsi から最低限の方向スコアを再構成する。
#      - これにより「データはあるのにエントリー判定項目が空/0」の状態を減らす。
# ============================================================

from __future__ import annotations

import logging
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

_INSTALLED = False


def _numeric(df: pd.DataFrame, col: str, default: float = 0.0) -> pd.Series:
    try:
        if col in df.columns:
            return pd.to_numeric(df[col], errors="coerce")
    except Exception:
        pass
    return pd.Series(default, index=df.index, dtype="float64")


def _all_zero_or_na(s: pd.Series) -> bool:
    try:
        return bool((pd.to_numeric(s, errors="coerce").fillna(0.0) == 0.0).all())
    except Exception:
        return True


def _fallback_direction_score(df: pd.DataFrame) -> pd.Series:
    """
    Yahoo補完用の最低限スコア。
    強すぎる点を付けず、AI/entry側で最終確認できるように 0～数点程度に抑える。
    """
    idx = df.index
    score = pd.Series(0.0, index=idx, dtype="float64")

    slope = _numeric(df, "slope").fillna(_numeric(df, "slope_atr_scaled")).fillna(0.0)
    hist = _numeric(df, "hist").fillna(0.0)
    macd = _numeric(df, "macd").fillna(0.0)
    signal = _numeric(df, "signal").fillna(0.0)
    rsi = _numeric(df, "rsi").fillna(50.0)

    macd_diff = hist.copy()
    try:
        if _all_zero_or_na(macd_diff) and ("macd" in df.columns and "signal" in df.columns):
            macd_diff = (macd - signal).fillna(0.0)
    except Exception:
        macd_diff = hist.fillna(0.0)

    # slope: 方向性。小さな値でも少しだけ反映。
    score = score.where(~slope.gt(0.01), score + 2.0)
    score = score.where(~slope.lt(-0.01), score - 2.0)
    score = score.where(~slope.gt(0.03), score + 1.0)
    score = score.where(~slope.lt(-0.03), score - 1.0)

    # MACD差分: 方向補助。
    score = score.where(~macd_diff.gt(0), score + 1.0)
    score = score.where(~macd_diff.lt(0), score - 1.0)

    # RSI: 過熱/売られすぎを軽く加点。
    score = score.where(~rsi.gt(55), score + 0.5)
    score = score.where(~rsi.lt(45), score - 0.5)

    return score.fillna(0.0)


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True

    try:
        from trading.yahoo.pipeline.complement import compute as comp
    except Exception:
        logger.exception("[YAHOO COMPLEMENT SAFETY PATCH] import compute failed")
        return False

    # --------------------------------------------------------
    # 1) pd.NA default を None に変更して membership 判定エラーを防ぐ
    # --------------------------------------------------------
    def _safe_default_value_for_missing_db_col(col: str) -> Any:
        c = str(col).lower()
        if c in {"symbol", "symbolname", "date", "time", "time_range", "start_time", "end_time", "source", "signal"}:
            return ""
        if c in {"datetime", "last_update", "created_at", "updated_at"}:
            return None
        if c in {"technical_ready", "display_ready", "is_ready", "ready"}:
            return 0
        return 0.0

    comp._default_value_for_missing_db_col = _safe_default_value_for_missing_db_col  # type: ignore[attr-defined]

    # --------------------------------------------------------
    # 2) score全ゼロ救済
    # --------------------------------------------------------
    original_ensure_score_columns = getattr(comp, "ensure_score_columns", None)

    def _patched_ensure_score_columns(df: pd.DataFrame) -> pd.DataFrame:
        if callable(original_ensure_score_columns):
            out = original_ensure_score_columns(df)
        else:
            out = df.copy() if isinstance(df, pd.DataFrame) else pd.DataFrame()

        if out is None or not isinstance(out, pd.DataFrame) or out.empty:
            return pd.DataFrame()

        try:
            score = _numeric(out, "score").fillna(0.0)
            final_score = _numeric(out, "final_score").fillna(0.0)
            display_score = _numeric(out, "display_score").fillna(0.0)

            all_score_zero = bool((score == 0).all() and (final_score == 0).all() and (display_score == 0).all())
            has_signal = False
            for c in ("slope", "slope_atr_scaled", "hist", "macd", "rsi"):
                if c in out.columns and not _all_zero_or_na(_numeric(out, c)):
                    has_signal = True
                    break

            if all_score_zero and has_signal:
                fb = _fallback_direction_score(out)
                buy = fb.clip(lower=0.0)
                sell = (-fb).clip(lower=0.0)

                out["score_buy"] = buy
                out["score_sell"] = sell
                out["score_total"] = fb
                out["score"] = fb
                out["final_score"] = fb
                out["display_score"] = fb

                if "buy_score" in out.columns:
                    out["buy_score"] = buy
                if "sell_score" in out.columns:
                    out["sell_score"] = sell

                logger.warning(
                    "[YAHOO COMPLEMENT SAFETY PATCH] zero-score fallback applied rows=%s nonzero=%s",
                    len(out),
                    int((fb.fillna(0.0) != 0.0).sum()),
                )
        except Exception:
            logger.exception("[YAHOO COMPLEMENT SAFETY PATCH] score fallback failed; use original result")

        return out

    comp.ensure_score_columns = _patched_ensure_score_columns  # type: ignore[assignment]

    _INSTALLED = True
    logger.warning("[YAHOO COMPLEMENT SAFETY PATCH] installed")
    return True


__all__ = ["install"]
