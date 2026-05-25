# ============================================================
# File   : trading/entry/tonosama/scoring.py
# Version: Ver1.1-TONOSAMA-FINAL-SCORE-MA-CONF-FALLBACK
# ------------------------------------------------------------
# Fix:
#   TONOSAMA 候補で raw_score は十分あるのに、ma25_conf/ma75_conf が
#   欠損して calc_final_entry_score() が 0 を返し、
#   final_score_low で全落ちする問題を修正。
#
# 背景:
#   trading.entry.final_entry_score.calc_final_entry_score() は
#   ma75_conf が None または 0.6 未満だと 0 を返す。
#   TONOSAMA は急騰/5秒足/出来高急増を拾う短期ルートなので、
#   MA信頼度がDB/summary側から補完できない場合でも raw_score を
#   最低限の最終スコアとして使う。
# ============================================================
from __future__ import annotations

import logging
import pandas as pd

from trading.entry.final_entry_score import calc_final_entry_score
from .utils import safe_float

logger = logging.getLogger(__name__)


def _is_missing_or_zero(v) -> bool:
    try:
        if v is None:
            return True
        if pd.isna(v):
            return True
        return abs(float(v)) <= 1.0e-12
    except Exception:
        return True


def prepare_entry_scores(x: pd.DataFrame) -> pd.DataFrame:
    if x is None or x.empty:
        return pd.DataFrame()
    out = x.copy()

    score_candidates = [
        c for c in [
            "final_score",
            "display_score",
            "disp_score",
            "score_total",
            "score_buy",
            "score",
            "ranking_score",
        ] if c in out.columns
    ]
    out["_raw_score"] = out[score_candidates].max(axis=1).fillna(0.0) if score_candidates else 0.0

    if "slope" in out.columns:
        out["_slope"] = pd.to_numeric(out["slope"], errors="coerce").fillna(0.0)
    elif "slope_atr_scaled" in out.columns:
        out["_slope"] = pd.to_numeric(out["slope_atr_scaled"], errors="coerce").fillna(0.0)
    else:
        out["_slope"] = 0.0

    out["_tonosama_score"] = (
        out["_raw_score"].fillna(0.0)
        + out["_max_volume_surge_ratio"].fillna(0.0).clip(upper=6.0) * 0.8
        + out["_max_price_change_pct"].fillna(0.0).clip(lower=0.0, upper=5.0) * 1.2
        + out["_slope"].fillna(0.0).clip(lower=-0.02, upper=0.10) * 5.0
    )

    if "has_5sec_bar" in out.columns:
        out["_tonosama_score"] = (
            out["_tonosama_score"]
            + out["price_change_5s_pct"].fillna(0.0).clip(lower=0.0, upper=1.0) * 2.0
        )

    return out


def calc_final_score_safe(row, *, raw_score: float, ai_prob: float) -> float:
    """
    TONOSAMA用の最終スコア。

    通常は共通 calc_final_entry_score() を使うが、MA信頼度が欠損して
    0になる場合は raw_score を採用する。TONOSAMAは短期急騰検知ルートで、
    ma75_conf 欠損だけで全候補を落とすと発注に到達しないため。
    """
    raw = safe_float(raw_score, 0.0)
    if raw <= 0:
        return 0.0

    try:
        ma25_conf = row.get("ma25_conf") if "ma25_conf" in row.index else None
        ma75_conf = row.get("ma75_conf") if "ma75_conf" in row.index else None
        ai_value = ai_prob if ai_prob > 0 else row.get("ai_prob", None)

        final = safe_float(
            calc_final_entry_score(
                raw_score=raw,
                ma25_conf=ma25_conf,
                ma75_conf=ma75_conf,
                source="TONOSAMA",
                ai_prob=ai_value,
            ),
            0.0,
        )

        if final > 0:
            return final

        # MA信頼度が欠損/ゼロで共通スコアが0になった場合のみ救済。
        if _is_missing_or_zero(ma75_conf):
            logger.warning(
                "[TONOSAMA SCORE FALLBACK] use raw_score because ma75_conf missing/zero raw=%.4f ma25_conf=%s ma75_conf=%s ai_prob=%s symbol=%s",
                raw,
                ma25_conf,
                ma75_conf,
                ai_value,
                row.get("symbol", ""),
            )
            return raw

        return final

    except Exception:
        logger.exception("[TONOSAMA SCORE FALLBACK] calc final failed -> use raw_score")
        return raw
