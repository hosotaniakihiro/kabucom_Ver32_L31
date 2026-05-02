# ============================================================
# File   : trading/scoring/core/score_breakdown.py
# Version: PRODUCTION-STABLE-REV1.0-SCORE-BREAKDOWN
# ------------------------------------------------------------
# 【概要】
#   scoring_pipeline 後の DataFrame に、
#   スコア内訳列を追加する専用モジュール。
#
# 【目的】
#   scheduler_jobs/summary/display.py の表示:
#
#       base= ... trend= ... mom= ... vel= ... pen= ...
#
#   に実数値を入れる。
#
# 【生成する列】
#   score_base
#   score_trend
#   score_momentum
#   score_velocity
#   score_penalty
#
# 【設計】
#   - score_config.ini の [scoring] / [short_scoring] を読む
#   - DataFrame に存在する flag_* 列だけ使う
#   - 存在しない flag は無視
#   - 既存の score / score_buy / score_sell は壊さない
#   - 表示用の補助列として追加するだけ
#
# 【重要】
#   - 売り系 short_scoring のマイナス値は penalty 側に集約
#   - BUY 系 flag は category_map に従って base/trend/momentum/velocity へ分類
#   - 分類不能な flag は score_base に入れる
# ============================================================

from __future__ import annotations

import configparser
import logging
import math
from pathlib import Path
from typing import Dict, Iterable, Mapping, Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# Default config path
# ============================================================

DEFAULT_SCORE_CONFIG_PATH = (
    Path(__file__).resolve().parents[1] / "config" / "score_config.ini"
)


# ============================================================
# Category map
# ------------------------------------------------------------
# score_config.ini のコメント分類に合わせて、
# flag 名から内訳カテゴリへ寄せる。
# ============================================================

TREND_KEYWORDS = (
    "dir_",
    "slope",
    "trend",
    "ma",
    "perfect_order",
    "pullback",
    "rebound_on_ma",
    "above_vwap",
    "vwap",
    "breakout",
    "range",
    "window_up",
    "gap_up",
    "gap_down",
)

MOMENTUM_KEYWORDS = (
    "macd",
    "rsi",
    "stoch",
    "rci",
    "momentum",
)

VELOCITY_KEYWORDS = (
    "volume",
    "tick",
    "trade_count",
    "orderflow",
    "board_pressure",
    "bid_stack",
    "ask_stack",
    "bid_dominance",
    "ask_dominance",
    "spike",
    "surge",
    "expansion",
)

PENALTY_KEYWORDS = (
    "penalty",
    "reversal",
    "fail",
    "reject",
    "drop",
    "exhaustion",
    "breakdown",
    "down",
    "bearish",
    "dark_cloud",
    "evening_star",
    "shooting_star",
    "hanging_man",
    "three_black_crows",
    "gapdown",
    "window_down",
    "exit",
    "warning",
)


# ============================================================
# Utilities
# ============================================================

def _safe_float(v, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        x = float(v)
        if math.isnan(x) or math.isinf(x):
            return default
        return x
    except Exception:
        return default


def _to_numeric_series(df: pd.DataFrame, col: str, default: float = 0.0) -> pd.Series:
    if col not in df.columns:
        return pd.Series(default, index=df.index, dtype="float64")
    return pd.to_numeric(df[col], errors="coerce").fillna(default).astype("float64")


def _normalize_flag_name(name: str) -> str:
    name = str(name or "").strip()
    if not name:
        return ""
    if not name.startswith("flag_"):
        name = f"flag_{name}"
    return name


def _classify_flag(flag_name: str, *, is_short: bool = False) -> str:
    """
    flag 名から base/trend/momentum/velocity/penalty を判定する。

    short_scoring は基本的に penalty として扱う。
    ただし、今後 SELL 内訳を分けたくなった場合のために、
    keyword 判定も残している。
    """
    name = str(flag_name or "").lower()

    if is_short:
        return "penalty"

    if any(k in name for k in PENALTY_KEYWORDS):
        return "penalty"

    if any(k in name for k in MOMENTUM_KEYWORDS):
        return "momentum"

    if any(k in name for k in VELOCITY_KEYWORDS):
        return "velocity"

    if any(k in name for k in TREND_KEYWORDS):
        return "trend"

    return "base"


# ============================================================
# Config loader
# ============================================================

def load_score_weights(
    config_path: Optional[str | Path] = None,
) -> Tuple[Dict[str, float], Dict[str, float]]:
    """
    score_config.ini から BUY / SELL の flag 重みを読む。

    Returns
    -------
    buy_weights:
        [scoring] の flag_* 重み
    sell_weights:
        [short_scoring] の flag_* 重み
    """
    path = Path(config_path) if config_path else DEFAULT_SCORE_CONFIG_PATH

    cp = configparser.ConfigParser()
    cp.optionxform = str  # preserve case, although current keys are lower

    if not path.exists():
        logger.warning("[SCORE BREAKDOWN] config not found: %s", path)
        return {}, {}

    try:
        cp.read(path, encoding="utf-8")
    except UnicodeDecodeError:
        cp.read(path, encoding="cp932")
    except Exception:
        logger.exception("[SCORE BREAKDOWN] config read failed: %s", path)
        return {}, {}

    def _read_section(section: str) -> Dict[str, float]:
        out: Dict[str, float] = {}
        if not cp.has_section(section):
            return out

        for key, value in cp.items(section):
            key = _normalize_flag_name(key)
            if not key.startswith("flag_"):
                continue
            out[key] = _safe_float(value, 0.0)

        return out

    buy_weights = _read_section("scoring")
    sell_weights = _read_section("short_scoring")

    logger.info(
        "[SCORE BREAKDOWN] config loaded path=%s buy_flags=%s sell_flags=%s",
        path,
        len(buy_weights),
        len(sell_weights),
    )

    return buy_weights, sell_weights


# ============================================================
# Core calculation
# ============================================================

def _flag_contribution(
    df: pd.DataFrame,
    flag_col: str,
    weight: float,
) -> pd.Series:
    """
    DataFrame の flag 列 × score_config.ini の重み。

    flag 列は以下を想定:
      - bool
      - 0/1
      - 数値
      - NaN

    bool/0/1 なら weight がそのまま加算。
    数値が 2 などなら weight * 2 になる。
    """
    if flag_col not in df.columns:
        return pd.Series(0.0, index=df.index, dtype="float64")

    s = pd.to_numeric(df[flag_col], errors="coerce").fillna(0.0)

    # bool 的な列でも numeric 化されるのでそのまま使える
    return s.astype("float64") * float(weight)


def attach_score_breakdown(
    df: pd.DataFrame,
    *,
    config_path: Optional[str | Path] = None,
    buy_weights: Optional[Mapping[str, float]] = None,
    sell_weights: Optional[Mapping[str, float]] = None,
    round_digits: int = 4,
    debug: bool = True,
) -> pd.DataFrame:
    """
    DataFrame に score_base / score_trend / score_momentum /
    score_velocity / score_penalty を追加する。

    Parameters
    ----------
    df:
        scoring_pipeline 後の DataFrame
    config_path:
        score_config.ini のパス。省略時は trading/scoring/config/score_config.ini
    buy_weights:
        直接渡す場合の BUY flag 重み
    sell_weights:
        直接渡す場合の SELL flag 重み
    round_digits:
        丸め桁
    debug:
        True の場合、ログに有効 flag 数などを出す
    """
    if df is None or df.empty:
        return df

    out = df.copy()

    if buy_weights is None or sell_weights is None:
        loaded_buy, loaded_sell = load_score_weights(config_path)
        if buy_weights is None:
            buy_weights = loaded_buy
        if sell_weights is None:
            sell_weights = loaded_sell

    buy_weights = dict(buy_weights or {})
    sell_weights = dict(sell_weights or {})

    # 初期化
    score_base = pd.Series(0.0, index=out.index, dtype="float64")
    score_trend = pd.Series(0.0, index=out.index, dtype="float64")
    score_momentum = pd.Series(0.0, index=out.index, dtype="float64")
    score_velocity = pd.Series(0.0, index=out.index, dtype="float64")
    score_penalty = pd.Series(0.0, index=out.index, dtype="float64")

    used_buy_flags = 0
    used_sell_flags = 0

    # BUY 側
    for flag, weight in buy_weights.items():
        flag = _normalize_flag_name(flag)
        if flag not in out.columns:
            continue

        contrib = _flag_contribution(out, flag, weight)
        category = _classify_flag(flag, is_short=False)

        if category == "trend":
            score_trend = score_trend + contrib
        elif category == "momentum":
            score_momentum = score_momentum + contrib
        elif category == "velocity":
            score_velocity = score_velocity + contrib
        elif category == "penalty":
            score_penalty = score_penalty + contrib
        else:
            score_base = score_base + contrib

        used_buy_flags += 1

    # SELL 側
    # short_scoring は値が負なので、そのまま penalty に入れる。
    # 表示上は pen=-3.00 のように出る。
    for flag, weight in sell_weights.items():
        flag = _normalize_flag_name(flag)
        if flag not in out.columns:
            continue

        contrib = _flag_contribution(out, flag, weight)
        score_penalty = score_penalty + contrib
        used_sell_flags += 1

    # もし flag 列がほとんど存在しない場合のフォールバック
    # score_base を score から作る。
    if used_buy_flags == 0 and used_sell_flags == 0:
        score = _to_numeric_series(out, "score", 0.0)
        score_buy = _to_numeric_series(out, "score_buy", 0.0)
        score_sell = _to_numeric_series(out, "score_sell", 0.0)

        # score_buy / score_sell があるならそれを優先
        if score_buy.abs().sum() > 0 or score_sell.abs().sum() > 0:
            score_base = score_buy - score_sell
        else:
            score_base = score

        # 既存の slope / mtf / rsi / macd がある場合は補助的に分類
        slope = _to_numeric_series(out, "score_slope", 0.0)
        if "score_slope" not in out.columns:
            slope = _to_numeric_series(out, "slope", 0.0)

        mtf = _to_numeric_series(out, "score_mtf", 0.0)
        if "score_mtf" not in out.columns:
            mtf = _to_numeric_series(out, "mtf", 0.0)

        rsi = _to_numeric_series(out, "rsi", 50.0)
        macd = _to_numeric_series(out, "macd", 0.0)
        signal = _to_numeric_series(out, "signal", 0.0)
        volume = _to_numeric_series(out, "volume", 0.0)

        score_trend = slope + mtf
        score_momentum = ((rsi - 50.0) / 10.0).clip(-5.0, 5.0) + (macd - signal).clip(-5.0, 5.0)
        score_velocity = (volume.rank(pct=True).fillna(0.0) * 5.0).clip(0.0, 5.0)
        score_penalty = -score_sell.abs()

        logger.warning(
            "[SCORE BREAKDOWN] no flag columns matched; fallback used rows=%s",
            len(out),
        )

    out["score_base"] = score_base.round(round_digits)
    out["score_trend"] = score_trend.round(round_digits)
    out["score_momentum"] = score_momentum.round(round_digits)
    out["score_velocity"] = score_velocity.round(round_digits)
    out["score_penalty"] = score_penalty.round(round_digits)

    # 短縮名も付ける。display 側がどちらでも読めるようにする。
    out["breakdown_base"] = out["score_base"]
    out["breakdown_trend"] = out["score_trend"]
    out["breakdown_mom"] = out["score_momentum"]
    out["breakdown_vel"] = out["score_velocity"]
    out["breakdown_pen"] = out["score_penalty"]

    if debug:
        try:
            logger.info(
                "[SCORE BREAKDOWN] attached rows=%s used_buy_flags=%s used_sell_flags=%s "
                "base_nonzero=%s trend_nonzero=%s mom_nonzero=%s vel_nonzero=%s pen_nonzero=%s",
                len(out),
                used_buy_flags,
                used_sell_flags,
                int((out["score_base"].abs() > 0).sum()),
                int((out["score_trend"].abs() > 0).sum()),
                int((out["score_momentum"].abs() > 0).sum()),
                int((out["score_velocity"].abs() > 0).sum()),
                int((out["score_penalty"].abs() > 0).sum()),
            )
        except Exception:
            logger.exception("[SCORE BREAKDOWN] debug log failed")

    return out


# ============================================================
# Display helper
# ============================================================

def get_score_breakdown_values(row) -> Dict[str, float]:
    """
    display.py 側から使える補助関数。
    """
    def _get(*names: str) -> float:
        for name in names:
            try:
                if hasattr(row, "get"):
                    v = row.get(name, None)
                else:
                    v = getattr(row, name, None)
                if v is not None and not pd.isna(v):
                    return _safe_float(v, 0.0)
            except Exception:
                continue
        return 0.0

    return {
        "base": _get("score_base", "breakdown_base", "base_score"),
        "trend": _get("score_trend", "breakdown_trend", "trend_score"),
        "mom": _get("score_momentum", "breakdown_mom", "momentum_score", "score_mom"),
        "vel": _get("score_velocity", "breakdown_vel", "velocity_score", "score_vel"),
        "pen": _get("score_penalty", "breakdown_pen", "penalty_score", "score_pen"),
    }


def format_score_breakdown_line(row) -> str:
    """
    display.py でそのまま使える表示行。
    """
    b = get_score_breakdown_values(row)
    return (
        f"    base={b['base']:7.2f} "
        f"trend={b['trend']:7.2f} "
        f"mom={b['mom']:7.2f} "
        f"vel={b['vel']:7.2f} "
        f"pen={b['pen']:7.2f}"
    )