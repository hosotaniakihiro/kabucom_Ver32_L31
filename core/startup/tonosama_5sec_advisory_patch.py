# ============================================================
# File   : core/startup/tonosama_5sec_advisory_patch.py
# Version: V2.0-TONOSAMA-ADVISORY-NO-OVERRIDE-CLIMAX-SAFE
# ------------------------------------------------------------
# 【目的】
#   旧V1.0は trading.entry.tonosama.runner.iter_tonosama_candidate_rows を
#   runtime patch で上書きしていた。
#
# 【問題】
#   runner.py 側に BUY/SELL クライマックスガードを追加しても、
#   このpatchが古い候補生成ロジックで上書きするため、
#   upper_wick=90%超 / close_pos=3〜10% のBUY候補が残っていた。
#
# 【V2.0方針】
#   - iter_tonosama_candidate_rows は上書きしない
#   - runner.py 本体の最新ロジックを尊重する
#   - 念のため _apply_climax_guards だけ安全版に差し替える
#   - 5秒足任意化は runner.py 本体の _apply_5sec_filter に任せる
#
# 【期待ログ】
#   [TONOSAMA 5SEC ADVISORY PATCH] installed v2 no_override=True climax_safe=True
#   [TONOSAMA FILTER DROP] ... reason=buying_climax_or_upper_wick_reversal_guard
# ============================================================

from __future__ import annotations

import logging
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)
_PATCHED = False


def _safe_climax_guards(r: Any, x: pd.DataFrame, *, stage: str, sample_cols: list[str]) -> pd.DataFrame:
    """
    runner.py の BUY/SELL クライマックスガード安全版。

    BUY:
      - 上ヒゲが大きく、終値が安値圏なら、上昇率が小さくても除外
      - 高値圏張り付き + 出来高急増も除外

    SELL:
      - 下ヒゲが大きく、終値が高値圏なら、下落率が小さくても除外
      - 安値圏張り付き + 出来高急増も除外
    """
    if x is None or x.empty:
        return pd.DataFrame()

    buy_rejected_close_pos = float(getattr(r, "BUY_REJECTED_CLOSE_POSITION_PCT", 35.0))
    sell_rejected_close_pos = float(getattr(r, "SELL_REJECTED_CLOSE_POSITION_PCT", 65.0))

    surge = r._num_series(x, "_max_volume_surge_ratio")
    price_chg = r._num_series(x, "_max_price_change_pct")
    signed_body = r._num_series(x, "_signed_body_change_pct")
    close_pos = r._num_series(x, "_close_position_pct", 50.0)
    upper_wick = r._num_series(x, "_upper_wick_pct")
    slope = r._num_series(x, "_slope")

    buy_like = (price_chg > 0) | (signed_body > 0) | (slope > 0)
    buy_too_late = buy_like & (price_chg >= r.MAX_BUY_PRICE_CHANGE_PCT)
    buy_high_zone = buy_like & (close_pos >= r.MAX_BUY_CLOSE_POSITION_PCT) & (price_chg >= r.BUYING_CLIMAX_MIN_PRICE_CHANGE_PCT)
    buy_upper_wick_reversal = buy_like & (upper_wick >= r.MAX_BUY_UPPER_WICK_PCT) & (close_pos <= buy_rejected_close_pos)
    buying_climax = buy_like & (surge >= r.BUYING_CLIMAX_MIN_SURGE_RATIO) & (
        ((price_chg >= r.BUYING_CLIMAX_MIN_PRICE_CHANGE_PCT) & (close_pos >= r.MAX_BUY_CLOSE_POSITION_PCT))
        | ((upper_wick >= r.MAX_BUY_UPPER_WICK_PCT) & (close_pos <= buy_rejected_close_pos))
    )

    before = x.copy()
    x = x[~(buy_too_late | buy_high_zone | buy_upper_wick_reversal | buying_climax)]
    r._log_filter_step(
        stage=stage,
        before=before,
        after=x,
        reason="buying_climax_or_upper_wick_reversal_guard",
        threshold={
            "MAX_BUY_PRICE_CHANGE_PCT": r.MAX_BUY_PRICE_CHANGE_PCT,
            "MAX_BUY_CLOSE_POSITION_PCT": r.MAX_BUY_CLOSE_POSITION_PCT,
            "MAX_BUY_UPPER_WICK_PCT": r.MAX_BUY_UPPER_WICK_PCT,
            "BUY_REJECTED_CLOSE_POSITION_PCT": buy_rejected_close_pos,
            "BUYING_CLIMAX_MIN_SURGE_RATIO": r.BUYING_CLIMAX_MIN_SURGE_RATIO,
            "BUYING_CLIMAX_MIN_PRICE_CHANGE_PCT": r.BUYING_CLIMAX_MIN_PRICE_CHANGE_PCT,
        },
        sample_cols=sample_cols,
    )

    if x.empty:
        return x

    surge = r._num_series(x, "_max_volume_surge_ratio")
    price_chg = r._num_series(x, "_max_price_change_pct")
    signed_body = r._num_series(x, "_signed_body_change_pct")
    close_pos = r._num_series(x, "_close_position_pct", 50.0)
    lower_wick = r._num_series(x, "_lower_wick_pct")
    slope = r._num_series(x, "_slope")

    drop_abs = price_chg.abs()
    sell_like = (price_chg < 0) | (signed_body < 0) | (slope < 0)
    sell_too_late = sell_like & (drop_abs >= r.MAX_SELL_PRICE_DROP_PCT)
    sell_low_zone = sell_like & (close_pos <= r.MIN_SELL_CLOSE_POSITION_PCT) & (drop_abs >= r.SELLING_CLIMAX_MIN_PRICE_DROP_PCT)
    sell_lower_wick_reversal = sell_like & (lower_wick >= r.MAX_SELL_LOWER_WICK_PCT) & (close_pos >= sell_rejected_close_pos)
    selling_climax = sell_like & (surge >= r.SELLING_CLIMAX_MIN_SURGE_RATIO) & (
        ((drop_abs >= r.SELLING_CLIMAX_MIN_PRICE_DROP_PCT) & (close_pos <= r.MIN_SELL_CLOSE_POSITION_PCT))
        | ((lower_wick >= r.MAX_SELL_LOWER_WICK_PCT) & (close_pos >= sell_rejected_close_pos))
    )

    before = x.copy()
    x = x[~(sell_too_late | sell_low_zone | sell_lower_wick_reversal | selling_climax)]
    r._log_filter_step(
        stage=stage,
        before=before,
        after=x,
        reason="selling_climax_or_lower_wick_reversal_guard",
        threshold={
            "MAX_SELL_PRICE_DROP_PCT": r.MAX_SELL_PRICE_DROP_PCT,
            "MIN_SELL_CLOSE_POSITION_PCT": r.MIN_SELL_CLOSE_POSITION_PCT,
            "MAX_SELL_LOWER_WICK_PCT": r.MAX_SELL_LOWER_WICK_PCT,
            "SELL_REJECTED_CLOSE_POSITION_PCT": sell_rejected_close_pos,
            "SELLING_CLIMAX_MIN_SURGE_RATIO": r.SELLING_CLIMAX_MIN_SURGE_RATIO,
            "SELLING_CLIMAX_MIN_PRICE_DROP_PCT": r.SELLING_CLIMAX_MIN_PRICE_DROP_PCT,
        },
        sample_cols=sample_cols,
    )
    return x


def install() -> bool:
    global _PATCHED
    if _PATCHED:
        return True
    try:
        import trading.entry.tonosama.runner as r

        def _patched_apply_climax_guards(x: pd.DataFrame, *, stage: str, sample_cols: list[str]) -> pd.DataFrame:
            return _safe_climax_guards(r, x, stage=stage, sample_cols=sample_cols)

        _patched_apply_climax_guards._tonosama_climax_safe_v2 = True  # type: ignore[attr-defined]
        r._apply_climax_guards = _patched_apply_climax_guards

        # 重要: iter_tonosama_candidate_rows は上書きしない。
        _PATCHED = True
        logger.warning("[TONOSAMA 5SEC ADVISORY PATCH] installed v2 no_override=True climax_safe=True")
        return True
    except Exception:
        logger.exception("[TONOSAMA 5SEC ADVISORY PATCH] install failed")
        return False


__all__ = ["install"]
