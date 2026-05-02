# ============================================================
# File   : trading/scoring/core/score/score_composer.py
# Version: Ver2.6-PRODUCTION-FORCE-DIRECTIONAL-COMPOSER-SCORE-SEPARATED
#          -NONZERO-PREFER-MTF-SLOPE-FINAL
# ------------------------------------------------------------
# ✔ Ver2.5 完全保持ベース
# ✔ BUY / SELL directional compose (BUY - SELL)
# ✔ score explosion prevention
# ✔ display_score = abs(score)
# ✔ backward compatibility aliases kept
# ✔ directional total all-zero fallback
# ✔ existing score/final/combined/display rescue
# ✔ score_buy / score_sell canonical rebuild from total when zero
# ✔ closed-day では ±14 固定値 cluster の score_mtf を total から除外
# ✔ slope / mtf の表示値を total 合成に使わない
# ✔ score_slope / score_mtf のみ total に加算
# ✔ suspicious cluster warning を score_slope にも追加
# ✔ score_slope / score_mtf の元列は NaN preserve
# ✔ 合成時のみ一時的に fillna(0.0)
# ✔ visibility field を 0 に潰さない
# ✔ NEW: existing 0 より fallback nonzero を優先
# ✔ NEW: score_mtf / mtf_score の nonzero rescue
# ✔ NEW: score_slope / slope_score の nonzero rescue
# ============================================================

from __future__ import annotations

import datetime as dt
import logging

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def _numeric(df: pd.DataFrame, col: str) -> pd.Series:
    """
    既存互換の 0許容 series。
    total / buy / sell / display 系向け。
    """
    if col not in df.columns:
        return pd.Series(0.0, index=df.index, dtype="float64")
    try:
        s = pd.to_numeric(df[col], errors="coerce")
        s = s.replace([np.inf, -np.inf], np.nan).fillna(0.0)
        return s.astype("float64")
    except Exception:
        logger.exception("[SCORE COMPOSER] numeric conversion failed col=%s", col)
        return pd.Series(0.0, index=df.index, dtype="float64")


def _numeric_nan(df: pd.DataFrame, col: str) -> pd.Series:
    """
    NaN preserve series。
    score_slope / score_mtf / slope / mtf などの未計算を保持したい列向け。
    """
    if col not in df.columns:
        return pd.Series(np.nan, index=df.index, dtype="float64")
    try:
        s = pd.to_numeric(df[col], errors="coerce")
        s = s.replace([np.inf, -np.inf], np.nan)
        return s.astype("float64")
    except Exception:
        logger.exception("[SCORE COMPOSER] numeric_nan conversion failed col=%s", col)
        return pd.Series(np.nan, index=df.index, dtype="float64")


def _text(df: pd.DataFrame, col: str) -> pd.Series:
    if col not in df.columns:
        return pd.Series("", index=df.index, dtype="object")
    try:
        return df[col].astype(str).fillna("")
    except Exception:
        return pd.Series("", index=df.index, dtype="object")


def _prefer_existing_nonzero(existing: pd.Series, fallback: pd.Series) -> pd.Series:
    try:
        keep = existing.notna() & (existing != 0)
        return existing.where(keep, fallback)
    except Exception:
        return fallback


def _prefer_existing_nonnull(existing: pd.Series, fallback: pd.Series) -> pd.Series:
    try:
        keep = existing.notna()
        return existing.where(keep, fallback)
    except Exception:
        return fallback


def _prefer_nonzero_then_nonnull(existing: pd.Series, fallback: pd.Series) -> pd.Series:
    """
    existing が 0/NaN なら、fallback の nonzero を優先して救済する。
    """
    try:
        existing_num = pd.to_numeric(existing, errors="coerce")
        fallback_num = pd.to_numeric(fallback, errors="coerce")

        out = existing_num.copy()

        replace_mask = (
            (out.isna() | (out == 0))
            & fallback_num.notna()
            & (fallback_num != 0)
        )
        out = out.where(~replace_mask, fallback_num)

        # nonzero がなければ nonnull fallback で最低限埋める
        out = out.where(out.notna(), fallback_num)
        return out.astype("float64")
    except Exception:
        return fallback


def _is_today_business_day() -> bool:
    try:
        from utils.business_day_utils import is_today_business_day
        return bool(is_today_business_day())
    except Exception:
        return dt.date.today().weekday() < 5


def _is_closed_day_mode(df: pd.DataFrame) -> bool:
    try:
        if _is_today_business_day():
            return False
        return True
    except Exception:
        return False


def _series_is_fixed_value_cluster(
    s: pd.Series,
    *,
    min_rows: int = 20,
    ratio_threshold: float = 0.6,
) -> bool:
    try:
        x = pd.to_numeric(s, errors="coerce").dropna()
        if len(x) < min_rows:
            return False

        nz = x[x != 0]
        if len(nz) < min_rows:
            return False

        vc = nz.round(6).value_counts(dropna=True)
        if vc.empty:
            return False

        top_val = float(vc.index[0])
        top_ratio = float(vc.iloc[0] / len(nz))
        unique_n = int(vc.size)

        suspicious_14 = abs(abs(top_val) - 14.0) <= 1e-6
        suspicious_ratio = top_ratio >= ratio_threshold
        suspicious_unique = unique_n <= 3 and top_ratio >= 0.5

        return suspicious_14 or suspicious_ratio or suspicious_unique
    except Exception:
        logger.exception("[SCORE COMPOSER] fixed cluster check failed")
        return False


def _should_ignore_existing_total(df: pd.DataFrame) -> bool:
    """
    既存 score_total が upstream で暴走しているケースを検知。
    特に ranking_history_1m / ranking 系 1min は既存 total を信用しない。
    """
    try:
        source = _text(df, "source").str.lower()
        interval = None
        if "interval" in df.columns:
            iv = pd.to_numeric(df["interval"], errors="coerce").fillna(0)
            if len(iv) > 0:
                interval = int(iv.iloc[0])

        source_hit = source.str.contains(r"ranking_history_1m|ranking", regex=True, na=False).any()
        interval_hit = interval == 1

        existing_total = _numeric(df, "score_total")
        suspicious_total = (
            (existing_total.abs() >= 1500)
            | (existing_total == 2000)
            | (existing_total == -2000)
        ).any()

        buy_score = _numeric(df, "buy_score")
        sell_score = _numeric(df, "sell_score")
        both_hot = ((buy_score >= 900) & (sell_score >= 900)).any()

        if (source_hit and interval_hit) or suspicious_total or both_hot:
            return True
        return False
    except Exception:
        logger.exception("[SCORE COMPOSER] ignore-existing-total check failed")
        return True


def compose_total_scores(df: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return df

    out = df.copy()
    closed_day_mode = _is_closed_day_mode(out)

    score_buy = _numeric(out, "score_buy")
    score_sell = _numeric(out, "score_sell")

    # --------------------------------------------------------
    # score成分は score列だけを使う
    # 表示用 slope / mtf は total 合成に流用しない
    # 元列は NaN preserve
    # existing 0 より fallback nonzero を優先
    # --------------------------------------------------------
    score_slope_existing = _numeric_nan(out, "score_slope")
    score_slope_fallback = _numeric_nan(out, "slope_score") if "slope_score" in out.columns else pd.Series(np.nan, index=out.index, dtype="float64")
    score_slope = _prefer_nonzero_then_nonnull(score_slope_existing, score_slope_fallback)

    score_mtf_existing = _numeric_nan(out, "score_mtf")
    score_mtf_fallback = _numeric_nan(out, "mtf_score") if "mtf_score" in out.columns else pd.Series(np.nan, index=out.index, dtype="float64")
    score_mtf = _prefer_nonzero_then_nonnull(score_mtf_existing, score_mtf_fallback)

    if _series_is_fixed_value_cluster(score_slope):
        logger.warning("[SCORE COMPOSER] suspicious score_slope cluster detected")

    if _series_is_fixed_value_cluster(score_mtf):
        logger.warning("[SCORE COMPOSER] suspicious score_mtf cluster detected")

    # closed-day では ±14 固定群の score_mtf / score_slope を total から除外
    if closed_day_mode and _series_is_fixed_value_cluster(score_mtf):
        logger.warning("[SCORE COMPOSER] closed-day suspicious score_mtf cluster detected -> zeroed for total compose")
        score_mtf_for_total = pd.Series(0.0, index=score_mtf.index, dtype="float64")
    else:
        score_mtf_for_total = score_mtf.fillna(0.0)

    if closed_day_mode and _series_is_fixed_value_cluster(score_slope):
        logger.warning("[SCORE COMPOSER] closed-day suspicious score_slope cluster detected -> zeroed for total compose")
        score_slope_for_total = pd.Series(0.0, index=score_slope.index, dtype="float64")
    else:
        score_slope_for_total = score_slope.fillna(0.0)

    buy_score = _numeric(out, "buy_score")
    sell_score = _numeric(out, "sell_score")

    # canonical BUY / SELL
    buy_mag = pd.concat(
        [score_buy.clip(lower=0), buy_score.clip(lower=0)],
        axis=1,
    ).max(axis=1)

    sell_mag = pd.concat(
        [score_sell.abs(), sell_score.abs()],
        axis=1,
    ).max(axis=1)

    # directional total
    directional_total = buy_mag - sell_mag + score_slope_for_total + score_mtf_for_total
    directional_total = directional_total.replace([np.inf, -np.inf], np.nan).fillna(0.0)

    existing_total = _numeric(out, "score_total")
    existing_final = _numeric(out, "final_score")
    existing_combined = _numeric(out, "combined_score")
    existing_score = _numeric(out, "score")
    display_score = _numeric(out, "display_score")

    ignore_existing = _should_ignore_existing_total(out)

    directional_nonzero = int((directional_total != 0).sum())
    total_nonzero = int((existing_total != 0).sum())
    final_nonzero = int((existing_final != 0).sum())
    combined_nonzero = int((existing_combined != 0).sum())
    score_nonzero = int((existing_score != 0).sum())
    display_nonzero = int((display_score != 0).sum())

    if not ignore_existing and total_nonzero > 0:
        total = existing_total
        source = "existing_score_total"
    elif not ignore_existing and final_nonzero > 0:
        total = existing_final
        source = "existing_final_score"
    elif not ignore_existing and combined_nonzero > 0:
        total = existing_combined
        source = "existing_combined_score"
    elif not ignore_existing and score_nonzero > 0:
        total = existing_score
        source = "existing_score"
    elif directional_nonzero > 0:
        total = directional_total
        source = "directional_compose"
    elif score_nonzero > 0:
        total = existing_score
        source = "fallback_score_after_zero_directional"
    elif final_nonzero > 0:
        total = existing_final
        source = "fallback_final_after_zero_directional"
    elif combined_nonzero > 0:
        total = existing_combined
        source = "fallback_combined_after_zero_directional"
    elif display_nonzero > 0:
        total = display_score
        source = "fallback_display_after_zero_directional"
    else:
        total = directional_total
        source = "all_zero_fallback"

    score_total_fixed = total.replace([np.inf, -np.inf], np.nan).fillna(0.0)

    # score_buy / score_sell が全ゼロのときは total から再構成
    if int((buy_mag != 0).sum()) == 0:
        buy_mag = score_total_fixed.clip(lower=0)

    if int((sell_mag != 0).sum()) == 0:
        sell_mag = (-score_total_fixed.clip(upper=0)).abs()

    # canonical fields
    out["score_buy"] = buy_mag
    out["score_sell"] = sell_mag
    out["buy_score"] = buy_mag
    out["sell_score"] = sell_mag

    # 元の score成分は NaN preserve
    out["score_slope"] = score_slope
    out["score_mtf"] = score_mtf

    out["score_total"] = score_total_fixed
    out["combined_score"] = score_total_fixed
    out["final_score"] = score_total_fixed
    out["score"] = score_total_fixed
    out["display_score"] = score_total_fixed.abs()

    if "mtf_score" not in out.columns:
        out["mtf_score"] = score_mtf
    else:
        out["mtf_score"] = _prefer_nonzero_then_nonnull(_numeric_nan(out, "mtf_score"), score_mtf)

    if "slope_score" not in out.columns:
        out["slope_score"] = score_slope
    else:
        out["slope_score"] = _prefer_nonzero_then_nonnull(_numeric_nan(out, "slope_score"), score_slope)

    if "base_score" not in out.columns:
        out["base_score"] = buy_mag - sell_mag + score_slope_for_total
    else:
        out["base_score"] = _prefer_existing_nonzero(
            _numeric(out, "base_score"),
            buy_mag - sell_mag + score_slope_for_total,
        )

    # visibility field は 0 に潰さない
    if "slope" in out.columns:
        out["slope"] = _numeric_nan(out, "slope")
    if "mtf" in out.columns:
        out["mtf"] = _numeric_nan(out, "mtf")

    try:
        both_hot = int(((buy_mag != 0) & (sell_mag != 0)).sum())
        logger.info(
            "[SCORING PIPELINE] composed totals rows=%d source=%s ignore_existing=%s closed_day=%s "
            "directional_nonzero=%d nonzero_total=%d buy_nonzero=%d sell_nonzero=%d "
            "score_slope_nonnull=%d score_slope_nonzero=%d score_mtf_nonnull=%d score_mtf_nonzero=%d "
            "slope_score_nonzero=%d mtf_score_nonzero=%d both_hot=%d min=%s max=%s",
            len(out),
            source,
            ignore_existing,
            closed_day_mode,
            directional_nonzero,
            int((out["score_total"] != 0).sum()),
            int((buy_mag != 0).sum()),
            int((sell_mag != 0).sum()),
            int(pd.to_numeric(out["score_slope"], errors="coerce").notna().sum()),
            int(pd.to_numeric(out["score_slope"], errors="coerce").fillna(0.0).ne(0).sum()),
            int(pd.to_numeric(out["score_mtf"], errors="coerce").notna().sum()),
            int(pd.to_numeric(out["score_mtf"], errors="coerce").fillna(0.0).ne(0).sum()),
            int(pd.to_numeric(out["slope_score"], errors="coerce").fillna(0.0).ne(0).sum()) if "slope_score" in out.columns else 0,
            int(pd.to_numeric(out["mtf_score"], errors="coerce").fillna(0.0).ne(0).sum()) if "mtf_score" in out.columns else 0,
            both_hot,
            out["score_total"].min() if not out.empty else None,
            out["score_total"].max() if not out.empty else None,
        )
    except Exception:
        logger.exception("[SCORE COMPOSER] logging failed")

    return out