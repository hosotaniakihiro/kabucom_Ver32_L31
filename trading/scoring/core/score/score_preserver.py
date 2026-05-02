# ============================================================
# File   : trading/scoring/core/score/score_preserver.py
# Version: Ver2.5-PRODUCTION-SCORE-PRESERVER-RAW-VS-SCORE-SEPARATED
#          -SLOPE-RESCUE-FINAL
# ------------------------------------------------------------
# ✔ Ver2.4 完全保持ベース
# ✔ existing score keep first
# ✔ no destructive zero overwrite
# ✔ alias fallback only if missing
# ✔ score/display_score/final_score を score_buy に流用しない
# ✔ score_total の fallback を安全化
# ✔ closed-day では ±14 固定値群の preserve を拒否
# ✔ score_slope / score_mtf と表示用 slope / mtf を完全分離
# ✔ slope / mtf は visibility field のみ
# ✔ score成分は score列だけを preserve
# ✔ FIX: slope visibility に score_slope を流用しない
# ✔ FIX: mtf visibility に score_mtf / mtf_score を流用しない
# ✔ NEW: score_slope / score_mtf / slope / mtf を NaN preserve
# ✔ NEW: visibility fields を fillna(0.0) しない
# ✔ NEW: score系だけ 0許容、表示系は未計算を保持
# ✔ NEW: slope -> score_slope rescue（score_slope が 0/NaN のときのみ）
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
from typing import Iterable, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# business-day helpers
# ============================================================

def _safe_previous_business_day(base_date: dt.date) -> dt.date:
    try:
        from utils.business_day_utils import get_previous_business_day
        return get_previous_business_day(base_date)
    except Exception:
        d = base_date - dt.timedelta(days=1)
        while d.weekday() >= 5:
            d -= dt.timedelta(days=1)
        return d


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


# ============================================================
# generic helpers
# ============================================================

def _first_existing(df: pd.DataFrame, candidates: Iterable[str]) -> Optional[str]:
    for c in candidates:
        if c in df.columns:
            return c
    return None


def _numeric_series(
    df: pd.DataFrame,
    col: str,
    default: float = 0.0,
    *,
    fillna: bool = True,
) -> pd.Series:
    if col not in df.columns:
        return pd.Series(default, index=df.index, dtype="float64")
    try:
        s = pd.to_numeric(df[col], errors="coerce")
        if fillna:
            s = s.fillna(default)
        return s.astype("float64")
    except Exception:
        logger.exception("[SCORING PRESERVER] numeric series failed col=%s", col)
        return pd.Series(default, index=df.index, dtype="float64")


def _numeric_nan_series(df: pd.DataFrame, col: str) -> pd.Series:
    if col not in df.columns:
        return pd.Series(np.nan, index=df.index, dtype="float64")
    try:
        s = pd.to_numeric(df[col], errors="coerce")
        return s.astype("float64")
    except Exception:
        logger.exception("[SCORING PRESERVER] numeric NaN series failed col=%s", col)
        return pd.Series(np.nan, index=df.index, dtype="float64")


def _ensure_numeric_series(
    df: pd.DataFrame,
    target: str,
    candidates: Iterable[str],
    *,
    default: float = 0.0,
    fillna: bool = True,
) -> pd.DataFrame:
    """
    fillna=True:
      score_buy / score_sell / score_total のような 0許容列向け
    fillna=False:
      score_slope / score_mtf / slope / mtf のような NaN保持列向け
    """
    if target in df.columns:
        s = pd.to_numeric(df[target], errors="coerce")
        if fillna:
            s = s.fillna(default)
        df[target] = s
        return df

    src = _first_existing(df, candidates)
    if src is not None:
        s = pd.to_numeric(df[src], errors="coerce")
        if fillna:
            s = s.fillna(default)
        df[target] = s
    else:
        if fillna:
            df[target] = pd.Series(default, index=df.index, dtype="float64")
        else:
            df[target] = pd.Series(np.nan, index=df.index, dtype="float64")
    return df


def _profile(df: pd.DataFrame, col: str) -> str:
    try:
        if col not in df.columns:
            return f"{col}=MISSING"
        s = pd.to_numeric(df[col], errors="coerce")
        return (
            f"{col}: nonnull={int(s.notna().sum())} "
            f"nonzero={int(s.fillna(0).ne(0).sum())} "
            f"eq14={int(s.fillna(0).abs().eq(14).sum())} "
            f"eq1000={int(s.fillna(0).eq(1000).sum())} "
            f"eq2000={int(s.fillna(0).eq(2000).sum())} "
            f"min={s.min()} max={s.max()}"
        )
    except Exception:
        return f"{col}=PROFILE_FAILED"


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

        bad = suspicious_14 or suspicious_ratio or suspicious_unique

        logger.info(
            "[SCORING PRESERVER] fixed cluster check rows=%d nz=%d top_val=%s top_ratio=%.3f unique_n=%d bad=%s",
            len(x),
            len(nz),
            top_val,
            top_ratio,
            unique_n,
            bad,
        )
        return bad
    except Exception:
        logger.exception("[SCORING PRESERVER] fixed cluster check failed")
        return False


def _sanitize_preserved_component(
    s: pd.Series,
    *,
    name: str,
    closed_day_mode: bool,
) -> pd.Series:
    """
    score成分だけに使う。
    ここでは 0許容でよいが、closed-day の固定値群は拒否する。
    """
    out = pd.to_numeric(s, errors="coerce").fillna(0.0)

    if closed_day_mode and _series_is_fixed_value_cluster(out):
        logger.warning(
            "[SCORING PRESERVER] closed-day suspicious preserved component dropped name=%s",
            name,
        )
        return pd.Series(0.0, index=out.index, dtype="float64")

    return out


# ============================================================
# main
# ============================================================

def preserve_existing_scores(df: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return df

    out = df.copy()
    closed_day_mode = _is_closed_day_mode(out)

    # --------------------------------------------------------
    # score_buy:
    # 絶対に score / final_score / display_score を流用しない
    # 0許容
    # --------------------------------------------------------
    out = _ensure_numeric_series(
        out,
        "score_buy",
        ["buy_score"],
        default=0.0,
        fillna=True,
    )

    # --------------------------------------------------------
    # score_sell:
    # sell_score のみ採用
    # 0許容
    # --------------------------------------------------------
    out = _ensure_numeric_series(
        out,
        "score_sell",
        ["sell_score"],
        default=0.0,
        fillna=True,
    )

    # --------------------------------------------------------
    # score_slope:
    # 基本は score列のみ preserve
    # ただし score_slope が 0/NaN で slope があるときだけ救済
    # --------------------------------------------------------
    out = _ensure_numeric_series(
        out,
        "score_slope",
        ["slope_score", "score_slope"],
        default=0.0,
        fillna=False,
    )

    # --------------------------------------------------------
    # score_mtf:
    # score列のみ preserve
    # mtf / mtf_alignment は使わない
    # NaN保持
    # --------------------------------------------------------
    out = _ensure_numeric_series(
        out,
        "score_mtf",
        ["mtf_score", "score_mtf"],
        default=0.0,
        fillna=False,
    )

    # --------------------------------------------------------
    # score_total:
    # combined/final が明示的にある時だけ拾う
    # "score" / "display_score" は除外
    # 0許容
    # --------------------------------------------------------
    out = _ensure_numeric_series(
        out,
        "score_total",
        ["combined_score", "final_score"],
        default=0.0,
        fillna=True,
    )

    # --------------------------------------------------------
    # visibility fields
    # NaN保持
    # --------------------------------------------------------
    out = _ensure_numeric_series(
        out,
        "slope",
        ["slope", "slope_atr_scaled", "slope_raw"],
        default=0.0,
        fillna=False,
    )

    out = _ensure_numeric_series(
        out,
        "mtf",
        ["mtf", "mtf_alignment"],
        default=0.0,
        fillna=False,
    )

    # --------------------------------------------------------
    # 型整理
    # --------------------------------------------------------
    for c in ("score_buy", "score_sell", "score_total"):
        out[c] = pd.to_numeric(out[c], errors="coerce").fillna(0.0)

    for c in ("score_slope", "score_mtf", "slope", "mtf"):
        out[c] = pd.to_numeric(out[c], errors="coerce")

    # --------------------------------------------------------
    # NEW: slope -> score_slope rescue
    # score_slope が 0/NaN のときだけ visibility slope から救済
    # closed-day 固定値 guard はこの後で適用
    # --------------------------------------------------------
    try:
        score_slope_raw = pd.to_numeric(out["score_slope"], errors="coerce")
        slope_raw = pd.to_numeric(out["slope"], errors="coerce")

        rescue_mask = (
            (score_slope_raw.isna() | score_slope_raw.fillna(0).eq(0))
            & slope_raw.notna()
            & slope_raw.fillna(0).ne(0)
        )

        rescued_count = int(rescue_mask.sum())
        if rescued_count > 0:
            out.loc[rescue_mask, "score_slope"] = slope_raw.loc[rescue_mask]
            logger.info(
                "[SCORING PRESERVER] slope -> score_slope rescued rows=%d",
                rescued_count,
            )
    except Exception:
        logger.exception("[SCORING PRESERVER] slope rescue failed")

    # --------------------------------------------------------
    # closed-day guard
    # preserve対象の score成分だけ止める
    # --------------------------------------------------------
    out["score_slope"] = _sanitize_preserved_component(
        out["score_slope"],
        name="score_slope",
        closed_day_mode=closed_day_mode,
    )
    out["score_mtf"] = _sanitize_preserved_component(
        out["score_mtf"],
        name="score_mtf",
        closed_day_mode=closed_day_mode,
    )

    # visibility は触らない
    out["slope"] = pd.to_numeric(out["slope"], errors="coerce")
    out["mtf"] = pd.to_numeric(out["mtf"], errors="coerce")

    logger.info(
        "[SCORING PRESERVER] rows=%d closed_day=%s | %s | %s | %s | %s | %s | %s | %s",
        len(out),
        closed_day_mode,
        _profile(out, "score_buy"),
        _profile(out, "score_sell"),
        _profile(out, "score_slope"),
        _profile(out, "score_mtf"),
        _profile(out, "score_total"),
        _profile(out, "slope"),
        _profile(out, "mtf"),
    )

    return out