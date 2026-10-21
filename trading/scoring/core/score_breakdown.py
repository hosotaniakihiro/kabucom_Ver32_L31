# ============================================================
# File   : trading/scoring/core/score_breakdown.py
# Version: PRODUCTION-STABLE-REV1.1-SCORE-BREAKDOWN-IMPORT-FIX
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
# 【互換列】
#   breakdown_base
#   breakdown_trend
#   breakdown_mom
#   breakdown_vel
#   breakdown_pen
#
# 【重要】
#   - scoring_pipeline.py から
#       from trading.scoring.core.score_breakdown import attach_score_breakdown
#     で import できるようにする。
#
#   - 既存の score / score_buy / score_sell / score_slope / score_mtf は壊さない。
#
#   - score_config.ini / score_table.py の SCORE_TABLE を優先利用する。
#
#   - flag_* 列が DataFrame に存在する場合:
#       flag_* × SCORE_TABLE の重み
#     でカテゴリ別に集計する。
#
#   - flag_* 列が存在しない場合:
#       score / score_buy / score_sell / slope / mtf / rsi / macd / volume
#     からフォールバック計算する。
# ============================================================

from __future__ import annotations

import logging
import math
import os
from typing import Any, Dict, Mapping, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# category keywords
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
# basic helpers
# ============================================================

def _safe_df(df: Any) -> pd.DataFrame:
    try:
        if df is None:
            return pd.DataFrame()

        if isinstance(df, pd.DataFrame):
            out = df.copy()
        elif isinstance(df, pd.Series):
            out = pd.DataFrame([df.to_dict()])
        elif isinstance(df, dict):
            out = pd.DataFrame([df])
        else:
            out = pd.DataFrame(df).copy()

        if out.empty:
            return out

        try:
            out.columns = [str(c) for c in out.columns]
        except Exception:
            pass

        try:
            # object 列を含む DataFrame へ一括 replace すると
            #   FutureWarning: Downcasting behavior in `replace` is deprecated
            # になる。inf は数値列にしか存在しないので数値列だけを対象にする
            # (結果は従来と同じ)。
            num_cols = [c for c in out.columns if pd.api.types.is_numeric_dtype(out[c])]
            if num_cols:
                out[num_cols] = out[num_cols].replace([np.inf, -np.inf], np.nan)
        except Exception:
            pass

        return out.reset_index(drop=True)

    except Exception:
        logger.exception("[SCORE BREAKDOWN] _safe_df failed")
        return pd.DataFrame()


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        x = float(v)
        if math.isnan(x) or math.isinf(x):
            return default
        return x
    except Exception:
        return default


def _num_series(df: pd.DataFrame, col: str, default: float = 0.0) -> pd.Series:
    if col not in df.columns:
        return pd.Series(default, index=df.index, dtype="float64")

    try:
        s = pd.to_numeric(df[col], errors="coerce")
        s = s.replace([np.inf, -np.inf], np.nan)
        return s.fillna(default).astype("float64")
    except Exception:
        return pd.Series(default, index=df.index, dtype="float64")


def _first_num_series(df: pd.DataFrame, candidates: list[str], default: float = 0.0) -> pd.Series:
    for col in candidates:
        if col in df.columns:
            return _num_series(df, col, default=default)
    return pd.Series(default, index=df.index, dtype="float64")


def _normalize_flag_key(key: str) -> str:
    k = str(key or "").strip().lower()
    if not k:
        return ""
    if not k.startswith("flag_"):
        k = f"flag_{k}"
    return k


def _use_table_breakdown() -> bool:
    """SCORE_TABLE 方式の値を実際に採用するか。

    flag 列が繋がると used_flags は 71 になるが、table 方式は
    trend/mom の負値をほぼ 0 にするため、ranking_pipeline の
    direction_penalty が効かなくなり ENTRY 条件が緩む
    (2026-08-26 実測: mom の負値 104/300 -> 0/300)。
    値の切り替えは明示スイッチにする。既定は従来どおり fallback。
    """
    try:
        v = os.getenv("SCORE_BREAKDOWN_USE_TABLE")
        if v is None or str(v).strip() == "":
            return False
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}
    except Exception:
        return False


def _min_flags_for_table() -> int:
    """SCORE_TABLE 方式を採用するのに必要な最低一致数。

    0 個のときだけ fallback という判定だと、たまたま 1〜2 個だけ一致したときに
    「180 flag のうち 2 個だけで内訳を作る」という fallback より悪い状態へ
    黙って切り替わる。既定は 10 個。
    """
    try:
        return int(float(os.getenv("SCORE_BREAKDOWN_MIN_FLAGS", "10")))
    except Exception:
        return 10


def _classify_flag(flag_name: str, weight: float) -> str:
    name = str(flag_name or "").lower()

    # short_scoring 側は score_table では負の重みになる。
    # 負の重みは基本的に penalty へ寄せる。
    if float(weight) < 0:
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
# score table loader
# ============================================================

def _load_score_table() -> Dict[str, float]:
    """
    既存の score_table.py から SCORE_TABLE を読む。

    想定候補:
      - trading.scoring.config.score_table
      - trading.scoring.score_table
      - scoring.config.score_table
      - scoring.score_table

    アップロードされた score_table.py は score_config.ini を唯一の定義源として
    SCORE_TABLE を公開している。これを優先利用する。
    """
    candidates = [
        "trading.scoring.config.score_table",
        "trading.scoring.score_table",
        "scoring.config.score_table",
        "scoring.score_table",
    ]

    for mod_name in candidates:
        try:
            mod = __import__(mod_name, fromlist=["SCORE_TABLE"])
            table = getattr(mod, "SCORE_TABLE", None)
            if isinstance(table, dict) and table:
                out: Dict[str, float] = {}
                for k, v in table.items():
                    kk = _normalize_flag_key(k)
                    if kk:
                        out[kk] = _safe_float(v, 0.0)

                logger.info(
                    "[SCORE BREAKDOWN] SCORE_TABLE loaded module=%s flags=%s",
                    mod_name,
                    len(out),
                )
                return out
        except Exception:
            continue

    logger.warning("[SCORE BREAKDOWN] SCORE_TABLE import failed; fallback mode only")
    return {}


# ============================================================
# fallback breakdown
# ============================================================

def _nz(s) -> int:
    try:
        return int((pd.to_numeric(s, errors="coerce").fillna(0.0) != 0).sum())
    except Exception:
        return -1


def _neg(s) -> int:
    try:
        return int((pd.to_numeric(s, errors="coerce").fillna(0.0) < 0).sum())
    except Exception:
        return -1


def _log_table_vs_fallback(out, *, table_flags: int, used_flags: int, table_values, adopted: bool = True) -> None:
    """flag が十分そろったとき、table 値と fallback 値の差を 1 行で残す。"""
    try:
        fb = _fallback_breakdown(out)
        names = ("base", "trend", "mom", "vel", "pen")
        parts = []
        for i, nm in enumerate(names):
            parts.append(
                "%s[table nz=%d neg=%d / fallback nz=%d neg=%d]"
                % (nm, _nz(table_values[i]), _neg(table_values[i]), _nz(fb[i]), _neg(fb[i]))
            )
        logger.warning(
            "[SCORE BREAKDOWN] flag coverage OK rows=%s table_flags=%s used_flags=%s "
            "adopted=%s (SCORE_BREAKDOWN_USE_TABLE=%s) | %s",
            len(out), table_flags, used_flags,
            "SCORE_TABLE" if adopted else "fallback(既定: ENTRY条件を変えないため)",
            os.getenv("SCORE_BREAKDOWN_USE_TABLE") or "unset",
            " ".join(parts),
        )
    except Exception:
        logger.exception("[SCORE BREAKDOWN] table/fallback 比較ログに失敗")


def _fallback_breakdown(out: pd.DataFrame) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series, pd.Series]:
    """
    flag_* 列が使えない場合の保険。

    score_base:
        score_buy - score_sell を優先。無ければ score。
    score_trend:
        score_slope + score_mtf
    score_momentum:
        RSI と MACD の簡易評価
    score_velocity:
        volume の percentile
    score_penalty:
        -abs(score_sell)
    """
    score = _first_num_series(out, ["score", "display_score", "final_score"], default=0.0)
    score_buy = _first_num_series(out, ["score_buy", "buy_score", "buy"], default=0.0)
    score_sell = _first_num_series(out, ["score_sell", "sell_score", "sell"], default=0.0)

    if float(score_buy.abs().sum() + score_sell.abs().sum()) > 0.0:
        score_base = score_buy - score_sell.abs()
    else:
        score_base = score

    slope = _first_num_series(out, ["score_slope", "slope", "slope_atr_scaled"], default=0.0)
    mtf = _first_num_series(out, ["score_mtf", "mtf_score", "mtf"], default=0.0)
    score_trend = slope + mtf

    rsi = _first_num_series(out, ["rsi", "RSI"], default=50.0)
    macd = _first_num_series(out, ["macd", "MACD"], default=0.0)
    signal = _first_num_series(out, ["signal", "macd_signal", "SIGNAL"], default=0.0)

    rsi_part = ((rsi - 50.0) / 10.0).clip(-5.0, 5.0)
    macd_part = (macd - signal).clip(-5.0, 5.0)
    score_momentum = rsi_part + macd_part

    volume = _first_num_series(out, ["volume", "trading_volume"], default=0.0)
    try:
        score_velocity = (volume.rank(pct=True).fillna(0.0) * 5.0).clip(0.0, 5.0)
    except Exception:
        score_velocity = pd.Series(0.0, index=out.index, dtype="float64")

    score_penalty = -score_sell.abs()

    return score_base, score_trend, score_momentum, score_velocity, score_penalty


# ============================================================
# public API
# ============================================================

def attach_score_breakdown(
    df: pd.DataFrame,
    *,
    score_table: Optional[Mapping[str, float]] = None,
    round_digits: int = 4,
    debug: bool = True,
) -> pd.DataFrame:
    """
    scoring_pipeline から呼ぶ本体関数。

    Parameters
    ----------
    df:
        scoring 後の DataFrame
    score_table:
        直接渡す場合の flag -> weight テーブル。
        None の場合は既存 score_table.py の SCORE_TABLE を import する。
    round_digits:
        丸め桁
    debug:
        ログ出力の有無

    Returns
    -------
    pd.DataFrame
        以下の列が追加された DataFrame:
          - score_base
          - score_trend
          - score_momentum
          - score_velocity
          - score_penalty
          - breakdown_base
          - breakdown_trend
          - breakdown_mom
          - breakdown_vel
          - breakdown_pen
    """
    out = _safe_df(df)
    if out.empty:
        return out

    # 呼び出し側が table を明示指定した場合は、その意図どおり table 方式を使う。
    # (本番経路は score_table=None なので env ゲートの判断に従う)
    explicit_table = score_table is not None
    if score_table is None:
        score_table = _load_score_table()

    table: Dict[str, float] = {}
    for k, v in dict(score_table or {}).items():
        kk = _normalize_flag_key(k)
        if not kk:
            continue
        table[kk] = _safe_float(v, 0.0)

    score_base = pd.Series(0.0, index=out.index, dtype="float64")
    score_trend = pd.Series(0.0, index=out.index, dtype="float64")
    score_momentum = pd.Series(0.0, index=out.index, dtype="float64")
    score_velocity = pd.Series(0.0, index=out.index, dtype="float64")
    score_penalty = pd.Series(0.0, index=out.index, dtype="float64")

    used_flags = 0

    # score_config.ini のキーは _normalize_flag_key で必ず "flag_" 付きに揃えられるが、
    # DataFrame 側 (flag_builder_buy/sell や conditions_long 由来) の列名は
    # "bullish_engulfing" のように接頭辞なしで作られる。
    # 接頭辞つきの名前だけで突き合わせていたため、列が存在していても
    # used_flags=0 になっていた。両方の綴りを見る。
    for flag_col, weight in table.items():
        col = flag_col
        if col not in out.columns:
            bare = flag_col[5:] if flag_col.startswith("flag_") else flag_col
            if bare and bare in out.columns:
                col = bare
            else:
                continue

        flag_value = _num_series(out, col, default=0.0)
        contrib = flag_value * float(weight)

        category = _classify_flag(flag_col, weight)

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

        used_flags += 1

    # 一部しか一致しない状態で table 方式に切り替えると、
    # 180 flag 中 2 個だけで内訳を組み立てることになり fallback より劣化する。
    # 実運用では flag_builder_buy/sell が pipeline から呼ばれておらず
    # summary DF には ma5_above_ma25 / ma5_below_ma25 の 2 個しか存在しない
    # (builder を通せば 71 個そろう)。十分な数がそろうまでは fallback を使う。
    min_flags = max(1, _min_flags_for_table())
    if used_flags < min_flags:
        score_base, score_trend, score_momentum, score_velocity, score_penalty = _fallback_breakdown(out)
        if debug:
            logger.warning(
                "[SCORE BREAKDOWN] flag coverage too low; fallback used "
                "rows=%s table_flags=%s matched_flags=%s min_flags=%s df_cols=%s "
                "hint=flag_builder_buy/sell が pipeline 未接続の可能性",
                len(out),
                len(table),
                used_flags,
                min_flags,
                len(out.columns),
            )
    else:
        # flag は十分そろっている。table 値と fallback 値の差を必ず残す。
        if debug:
            _log_table_vs_fallback(
                out,
                table_flags=len(table),
                used_flags=used_flags,
                table_values=(score_base, score_trend, score_momentum, score_velocity, score_penalty),
                adopted=bool(explicit_table or _use_table_breakdown()),
            )
        if not (explicit_table or _use_table_breakdown()):
            # ENTRY 条件を勝手に緩めないため、値は従来どおり fallback を使う。
            # SCORE_BREAKDOWN_USE_TABLE=1 で設計どおりの table 方式になる。
            score_base, score_trend, score_momentum, score_velocity, score_penalty = _fallback_breakdown(out)

    out["score_base"] = pd.to_numeric(score_base, errors="coerce").fillna(0.0).round(round_digits)
    out["score_trend"] = pd.to_numeric(score_trend, errors="coerce").fillna(0.0).round(round_digits)
    out["score_momentum"] = pd.to_numeric(score_momentum, errors="coerce").fillna(0.0).round(round_digits)
    out["score_velocity"] = pd.to_numeric(score_velocity, errors="coerce").fillna(0.0).round(round_digits)
    out["score_penalty"] = pd.to_numeric(score_penalty, errors="coerce").fillna(0.0).round(round_digits)

    # 表示互換列
    out["breakdown_base"] = out["score_base"]
    out["breakdown_trend"] = out["score_trend"]
    out["breakdown_mom"] = out["score_momentum"]
    out["breakdown_vel"] = out["score_velocity"]
    out["breakdown_pen"] = out["score_penalty"]

    if debug:
        try:
            logger.info(
                "[SCORE BREAKDOWN] attached rows=%s table_flags=%s used_flags=%s "
                "base_nonzero=%s trend_nonzero=%s mom_nonzero=%s vel_nonzero=%s pen_nonzero=%s",
                len(out),
                len(table),
                used_flags,
                int((out["score_base"].abs() > 0).sum()),
                int((out["score_trend"].abs() > 0).sum()),
                int((out["score_momentum"].abs() > 0).sum()),
                int((out["score_velocity"].abs() > 0).sum()),
                int((out["score_penalty"].abs() > 0).sum()),
            )
        except Exception:
            logger.debug("[SCORE BREAKDOWN] debug log failed", exc_info=True)

    return out


def get_score_breakdown_values(row: Any) -> Dict[str, float]:
    """
    display.py などから使える補助関数。
    """
    def _get(*names: str) -> float:
        for name in names:
            try:
                if hasattr(row, "get"):
                    v = row.get(name, None)
                else:
                    v = getattr(row, name, None)
                if v is None:
                    continue
                if pd.isna(v):
                    continue
                return _safe_float(v, 0.0)
            except Exception:
                continue
        return 0.0

    return {
        "base": _get("score_base", "breakdown_base", "base_score", "base"),
        "trend": _get("score_trend", "breakdown_trend", "trend_score", "trend"),
        "mom": _get("score_momentum", "breakdown_mom", "score_mom", "momentum_score", "mom", "momentum"),
        "vel": _get("score_velocity", "breakdown_vel", "score_vel", "velocity_score", "vel", "velocity"),
        "pen": _get("score_penalty", "breakdown_pen", "score_pen", "penalty_score", "penalty", "pen"),
    }


def format_score_breakdown_line(row: Any) -> str:
    """
    ログ表示用の1行。
    """
    b = get_score_breakdown_values(row)
    return (
        f"    base={b['base']:7.2f} "
        f"trend={b['trend']:7.2f} "
        f"mom={b['mom']:7.2f} "
        f"vel={b['vel']:7.2f} "
        f"pen={b['pen']:7.2f}"
    )


# ============================================================
# backward compatible aliases
# ============================================================

def add_score_breakdown(df: pd.DataFrame, *args, **kwargs) -> pd.DataFrame:
    """
    旧名互換。
    """
    return attach_score_breakdown(df, *args, **kwargs)


def build_score_breakdown(df: pd.DataFrame, *args, **kwargs) -> pd.DataFrame:
    """
    旧名互換。
    """
    return attach_score_breakdown(df, *args, **kwargs)

