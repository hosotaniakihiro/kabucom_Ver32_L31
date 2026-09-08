# ============================================================
# File   : trading/entry/tonosama/runner.py
# Version: Ver1.9-TONOSAMA-FINAL-SLOPE-STRONG-MOVE-FAILOPEN
# ------------------------------------------------------------
# ✔ 5秒足は必須にしない。
# ✔ REQUIRE_5SEC_BAR=False の場合、5秒足が取れていても 0.0% 横ばいだけでは落とさない。
# ✔ 5秒足で MAX_5SEC_DROP_PCT 以下の強い逆行だけ落とす。
# ✔ REQUIRE_5SEC_BAR=True の場合のみ、従来通り 5秒足の正方向変化を要求する。
# ✔ 出来高急増だけで高値掴み/安値売りしない。
# ✔ Ver1.8:
#   - BUY: 上ヒゲ大 + 終値が安値圏なら、上昇率が小さくても除外
#   - SELL: 下ヒゲ大 + 終値が高値圏なら、下落率が小さくても除外
#   - 11:09ログの upper_wick=90%超 / close_pos=3〜10% のBUY通過を防止
# ✔ Ver1.9:
#   - final段階の slope_abs_too_small で _slope=0.0 の候補が全落ちする問題を補正
#   - 価格変化・値幅・出来高が十分なら slope filter を fail-open
# ============================================================
from __future__ import annotations

import datetime as dt
import logging
import os
import sys
import time
from typing import Any

import numpy as np
import pandas as pd

try:
    from trading.ranking.active_symbol_manager import update_active_symbols
except Exception:
    update_active_symbols = None

from .config import (
    MIN_PRICE,
    MIN_FINAL_SCORE,
    MIN_VOLUME_SURGE_RATIO,
    MIN_PRICE_CHANGE_PCT,
    MIN_SLOPE,
    MIN_BODY_CHANGE_PCT,
    MIN_INTRABAR_RANGE_PCT,
    MIN_LATEST_VOLUME,
    MIN_LATEST_TURNOVER,
    MIN_5SEC_PRICE_CHANGE_PCT,
    MAX_5SEC_DROP_PCT,
    REQUIRE_5SEC_BAR,
    USE_5SEC_CONFIRM,
    MIN_RAW_SCORE,
    MAX_PENDING_PER_LOOP,
    MAX_CANDIDATES,
    MAX_5SEC_FEATURE_SYMBOLS,
    MAX_BUY_PRICE_CHANGE_PCT,
    MAX_BUY_CLOSE_POSITION_PCT,
    MAX_BUY_UPPER_WICK_PCT,
    BUYING_CLIMAX_MIN_SURGE_RATIO,
    BUYING_CLIMAX_MIN_PRICE_CHANGE_PCT,
    MAX_SELL_PRICE_DROP_PCT,
    MIN_SELL_CLOSE_POSITION_PCT,
    MAX_SELL_LOWER_WICK_PCT,
    SELLING_CLIMAX_MIN_SURGE_RATIO,
    SELLING_CLIMAX_MIN_PRICE_DROP_PCT,
)
from .time_guard import is_market_time
# 旧 tonosama_history_missing_guard_patch は runner のこの束縛
# (= import 時に掴んだ素の実装。volume_surge 側の failclose ラッパーとは
#  別束縛であることを実測済み) を包んでいた。本文化でも core を束縛し、
# 下の build_scalping_feature_df がガード層になる。
from .volume_surge import build_scalping_feature_df as _build_scalping_feature_df_core
from .five_sec_features import build_5sec_features
from .scoring import prepare_entry_scores, calc_final_score_safe
from . import fast_score_prefilter as _fast_score
from .ai_gate import ai_check_tonosama_entry
from .pending_writer import (
    has_tonosama_pending,
    build_pending_entry,
    add_tonosama_pending,
    prune_expired_tonosama_pending,
)
from .notifier import notify_discord_tonosama_pending
from .utils import normalize_symbol, safe_float

logger = logging.getLogger(__name__)
_last_loop_at: dt.datetime | None = None
_LAST_FILTER_DIAG: dict[str, Any] = {}

BUY_REJECTED_CLOSE_POSITION_PCT = 35.0
SELL_REJECTED_CLOSE_POSITION_PCT = 65.0


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


def _num_series_core(df: pd.DataFrame, col: str, default: float = 0.0) -> pd.Series:
    if df is None or df.empty or col not in df.columns:
        return pd.Series(default, index=df.index if df is not None else None, dtype="float64")
    return pd.to_numeric(df[col], errors="coerce").fillna(default)


def _num_series(df: pd.DataFrame, col: str, default: float = 0.0) -> pd.Series:
    """数値列の取得。opt-in の TONOSAMA extra rescue が有効な列だけ補正する。

    旧 core/startup の rescue パッチ 3 種がこの関数を多段ラップしていた。
    詳細は下の「TONOSAMA extra rescue」節を参照。
    """
    s = _num_series_core(df, col, default)
    if col not in _TONOSAMA_RESCUE_COLS:
        return s
    if not _tonosama_extra_rescue_enabled():
        return s
    _apply_tonosama_rescue_env_defaults()
    try:
        if col == "_max_price_change_pct" and _rescue_patch_on("DISABLE_TONOSAMA_PRICE_RANGE_PATCH"):
            return _rescue_max_price_change_pct(df, s)
        if col == "_max_volume_surge_ratio" and _rescue_patch_on("DISABLE_TONOSAMA_VOLUME_SURGE_ZERO_RESCUE_PATCH"):
            return _rescue_max_volume_surge_ratio(df, s)
        if col == "_slope" and _rescue_patch_on("DISABLE_TONOSAMA_SLOPE_RANGE_RESCUE_PATCH"):
            return _rescue_slope(df, s)
    except Exception:
        logger.exception("[TONOSAMA EXTRA RESCUE] failed -> original series col=%s", col)
    return s


def _bool_series(df: pd.DataFrame, col: str, default: bool = False) -> pd.Series:
    if df is None or df.empty or col not in df.columns:
        return pd.Series(default, index=df.index if df is not None else None, dtype="bool")
    return df[col].fillna(default).astype(bool)


def _first_existing(df: pd.DataFrame, names: list[str]) -> str | None:
    try:
        for n in names:
            if n in df.columns:
                return n
    except Exception:
        pass
    return None


# ============================================================
# 履歴からの値幅補修 (旧 core/startup/tonosama_range_from_history_patch.py)
# ------------------------------------------------------------
# 最新の PUSH 1分足が one-tick bar (high == low == close) になると
# _intrabar_range_pct が 0 になり、実際には 3m/5m の履歴で値動きが
# 証明できている銘柄まで strict filter で落ちてしまう。
# volume_surge.py が既に作っている履歴カラムからだけ補修する。
# fail-open ではない。履歴にも値動きの根拠が無い行はそのまま落とす。
# ============================================================
_RANGE_REPAIR_VERSION = "V1-TONOSAMA-RANGE-FROM-HISTORY-STRICT"

_RANGE_REPAIR_DB_PROCESS_NAMES = (
    "main_database.py",
    "data_collectors_runner.py",
    "db_prepare_runner.py",
    "push_receiver_runner.py",
    "yahoo_complement_runner.py",
    "summary_database_runner.py",
    "ranking_collector_runner.py",
)

_RANGE_REPAIR_HISTORY_COLS = (
    "intrabar_range_pct_3m",
    "intrabar_range_pct_5m",
    "range_pct_3m",
    "range_pct_5m",
    "price_change_pct_3m",
    "price_change_pct_5m",
    "prev_3m_last_delta_pct",
    "prev_5m_last_delta_pct",
    "_max_price_change_pct",
)


def _range_repair_enabled() -> bool:
    """このプロセスで履歴からの値幅補修を行うか。

    旧パッチは sitecustomize の BACKGROUND_MAIN_PATCHES 経由 = main.py 専用
    (SITECUSTOMIZE_ENABLE_FULL_NONMAIN=1 の明示時を含む) で install されていた。
    そのスコープと install 条件 2 つをそのまま再現する。
    TONOSAMA_RANGE_REPAIR_SCOPE_ENABLED=1/0 で明示的に上書きできる。
    """
    try:
        raw = os.getenv("TONOSAMA_RANGE_REPAIR_SCOPE_ENABLED")
        if raw is not None and str(raw).strip() != "":
            return _env_bool("TONOSAMA_RANGE_REPAIR_SCOPE_ENABLED", True)
        # sitecustomize._install_module の disabled_env は "1" 完全一致で判定していた
        if str(os.environ.get("DISABLE_TONOSAMA_FAST_SCORE_PREFILTER_PATCH", "")).strip() == "1":
            return False
        if not _env_bool("TONOSAMA_RANGE_FROM_HISTORY_PATCH_ENABLED", True):
            return False
        if _env_bool("CORE_STARTUP_FORCE_FULL_INIT_PATCHES", False):
            return True
        argv = [str(x).replace("\\", "/").lower() for x in sys.argv]
        if any(a.endswith("/main.py") or a == "main.py" for a in argv):
            return True
        if any(name in " ".join(argv) for name in _RANGE_REPAIR_DB_PROCESS_NAMES):
            return False
        return _env_bool("SITECUSTOMIZE_ENABLE_FULL_NONMAIN", False)
    except Exception:
        return False


def _range_repair_num(df: pd.DataFrame, col: str, default: float = 0.0) -> pd.Series:
    """inf を落としてから fillna する。runner._num_series とは意図的に別実装。"""
    if df is None or df.empty or col not in df.columns:
        return pd.Series(default, index=df.index if df is not None else None, dtype="float64")
    return pd.to_numeric(df[col], errors="coerce").replace([float("inf"), -float("inf")], pd.NA).fillna(default)


def _best_history_move_pct(x: pd.DataFrame) -> pd.Series:
    vals = []
    for col in _RANGE_REPAIR_HISTORY_COLS:
        if col in x.columns:
            vals.append(_range_repair_num(x, col, 0.0).abs())
    if not vals:
        return pd.Series(0.0, index=x.index, dtype="float64")
    m = vals[0]
    for s in vals[1:]:
        m = m.combine(s, max)
    return m.fillna(0.0)


def _repair_intrabar_range_from_history(x: pd.DataFrame) -> pd.DataFrame:
    if x is None or x.empty:
        return x
    y = x.copy()
    if "_intrabar_range_pct" not in y.columns:
        return y
    cur = _range_repair_num(y, "_intrabar_range_pct", 0.0).abs()
    hist = _best_history_move_pct(y)
    # _intrabar_range_pct も price_change_pct_* も percent 単位なのでそのまま入れ替えてよい
    needs = cur <= 0.0
    repairable = needs & (hist > 0.0)
    if repairable.any():
        y.loc[repairable, "_intrabar_range_pct"] = hist.loc[repairable]
        try:
            logger.warning(
                "[TONOSAMA RANGE REPAIR] repaired rows=%s version=%s sample=%s",
                int(repairable.sum()),
                _RANGE_REPAIR_VERSION,
                y.loc[repairable, [c for c in ["symbol", "symbolname", "close", "_intrabar_range_pct", "_max_price_change_pct", "price_change_pct_3m", "price_change_pct_5m"] if c in y.columns]].head(12).to_dict("records"),
            )
        except Exception:
            pass
    return y


# ============================================================
# TONOSAMA extra rescue
#   旧 core/startup/tonosama_price_change_or_range_patch.py   (V2)
#   旧 core/startup/tonosama_volume_surge_zero_rescue_patch.py (V1)
#   旧 core/startup/tonosama_slope_range_rescue_patch.py       (V1)
# ------------------------------------------------------------
# 3 パッチとも runner._num_series() を多段ラップしていたが、補正対象カラムが
#   _max_price_change_pct / _max_volume_surge_ratio / _slope
# と排他なので、_num_series 側で col ごとに分岐すれば等価になる。
# (install 順は price -> volume -> slope だったが、互いに素なので順序非依存)
#
# これらは sitecustomize の TONOSAMA_EXTRA_RESCUE_PATCHES = opt-in グループで、
# 既定 OFF。install されたときだけ効いていたので、有効条件とプロセススコープを
# _tonosama_extra_rescue_enabled() で再現する。
# TONOSAMA_EXTRA_RESCUE_SCOPE_ENABLED=1/0 で明示的に上書きできる。
# ============================================================
_TONOSAMA_RESCUE_COLS = frozenset({"_max_price_change_pct", "_max_volume_surge_ratio", "_slope"})

# sitecustomize._is_database_process() の env 側判定
_TONOSAMA_RESCUE_DB_PROCESS_ENVS = (
    "AUTOSTOCK_DATA_COLLECTORS_PROCESS",
    "AUTOSTOCK_SUMMARY_DB_WRITER",
    "AUTOSTOCK_MAIN_DATABASE_PROCESS",
    "AUTOSTOCK_RANKING_COLLECTOR_PROCESS",
)

_TONOSAMA_RESCUE_ENV_APPLIED = False


def _rescue_patch_on(disable_env: str) -> bool:
    """sitecustomize._install_module の disabled_env 判定 ("1" 完全一致) を再現する。"""
    try:
        return str(os.environ.get(disable_env, "")).strip() != "1"
    except Exception:
        return True


def _tonosama_rescue_process_scope_ok() -> bool:
    """sitecustomize が optional rescue を install するプロセスか。

    _install_optional_rescue_patches() の呼び出し元は
      - main lite (argv に main.py かつ SITECUSTOMIZE_MAIN_LITE) の background loop
      - SITECUSTOMIZE_ENABLE_FULL_NONMAIN=1
    の 2 経路のみ。DB プロセスは先に分岐して除外される。
    """
    try:
        argv_list = [str(x).replace("\\", "/").lower() for x in sys.argv]
        argv = " ".join(argv_list)
        if any(name in argv for name in _RANGE_REPAIR_DB_PROCESS_NAMES):
            return False
        if any(_env_bool(name, False) for name in _TONOSAMA_RESCUE_DB_PROCESS_ENVS):
            return False
        if any(a.endswith("/main.py") or a == "main.py" for a in argv_list) and _env_bool("SITECUSTOMIZE_MAIN_LITE", True):
            return True
        return _env_bool("SITECUSTOMIZE_ENABLE_FULL_NONMAIN", False)
    except Exception:
        return False


def _tonosama_extra_rescue_enabled() -> bool:
    try:
        raw = os.getenv("TONOSAMA_EXTRA_RESCUE_SCOPE_ENABLED")
        if raw is not None and str(raw).strip() != "":
            return _env_bool("TONOSAMA_EXTRA_RESCUE_SCOPE_ENABLED", False)
        if not (
            _env_bool("SITECUSTOMIZE_ENABLE_RESCUE_PATCHES", False)
            or _env_bool("SITECUSTOMIZE_ENABLE_TONOSAMA_EXTRA_RESCUE_PATCHES", False)
            or _env_bool("USERCUSTOMIZE_ENABLE_TONOSAMA_RESCUE_PATCHES", False)
        ):
            return False
        return _tonosama_rescue_process_scope_ok()
    except Exception:
        return False


def _apply_tonosama_rescue_env_defaults() -> None:
    """旧 install() が持っていた env 副作用を、有効なときだけ 1 度だけ再現する。

    旧構成では sitecustomize の background loop で install した時点で設定していた。
    これらのキーを読むのは本節のコードだけなので、初回参照時に遅延適用してよい。
    """
    global _TONOSAMA_RESCUE_ENV_APPLIED
    if _TONOSAMA_RESCUE_ENV_APPLIED:
        return
    _TONOSAMA_RESCUE_ENV_APPLIED = True
    try:
        if _rescue_patch_on("DISABLE_TONOSAMA_PRICE_RANGE_PATCH"):
            # V2 は surge 不要。runtime_env_defaults の 3.0 をここで 0 へ倒していた。
            os.environ["TONOSAMA_PRICE_CHANGE_OR_RANGE_MIN_SURGE"] = os.environ.get(
                "TONOSAMA_PRICE_CHANGE_OR_RANGE_MIN_SURGE_FORCE", "0.0"
            )
        if _rescue_patch_on("DISABLE_TONOSAMA_SLOPE_RANGE_RESCUE_PATCH"):
            os.environ.setdefault("TONOSAMA_SLOPE_RANGE_RESCUE_ENABLED", "1")
            os.environ.setdefault("TONOSAMA_SLOPE_RANGE_RESCUE_MIN_RANGE_PCT", "4.0")
            os.environ.setdefault("TONOSAMA_SLOPE_RANGE_RESCUE_MIN_VOLUME", "500000")
            os.environ.setdefault("TONOSAMA_SLOPE_RANGE_RESCUE_MIN_SURGE", "0.0")
            os.environ.setdefault("TONOSAMA_SLOPE_RANGE_RESCUE_MIN_ABS_SCORE", "0.0")
    except Exception:
        logger.exception("[TONOSAMA EXTRA RESCUE] env defaults failed")


def _rescue_series(df: pd.DataFrame, col: str, default: float = 0.0) -> pd.Series:
    """rescue パッチ 3 種が持っていた _series()。_num_series と違い astype(float) する。"""
    if df is None or df.empty or col not in df.columns:
        return pd.Series(default, index=df.index if df is not None else None, dtype="float64")
    return pd.to_numeric(df[col], errors="coerce").fillna(default).astype(float)


def _rescue_series_safe(df: pd.DataFrame, col: str, default: float = 0.0) -> pd.Series:
    """volume surge rescue の _series() だけは例外を握り潰す実装だった。差異を残す。"""
    try:
        return _rescue_series(df, col, default)
    except Exception:
        return pd.Series(default, index=df.index if df is not None else None, dtype="float64")


def _rescue_max_price_change_pct(df: pd.DataFrame, s: pd.Series) -> pd.Series:
    """レンジ・出来高・終値位置が有効なら price_change_low_abs を通して後段guardへ渡す。"""
    if not _env_bool("TONOSAMA_PRICE_CHANGE_OR_RANGE_ENABLED", True):
        return s
    if df is None or df.empty:
        return s

    min_price_change = float(MIN_PRICE_CHANGE_PCT or 0.2)

    range_pct = _rescue_series(df, "_intrabar_range_pct", 0.0)
    volume = _rescue_series(df, "_latest_volume", 0.0)
    surge = _rescue_series(df, "_max_volume_surge_ratio", 0.0)
    close_pos = _rescue_series(df, "_close_position_pct", 50.0)
    signed_body = _rescue_series(df, "_signed_body_change_pct", 0.0)
    slope = _rescue_series(df, "_slope", 0.0)
    score = _rescue_series(df, "score", 0.0)
    final_score = _rescue_series(df, "final_score", 0.0)

    min_range = _env_float("TONOSAMA_PRICE_CHANGE_OR_RANGE_MIN_RANGE_PCT", 3.0)
    min_volume = _env_float("TONOSAMA_PRICE_CHANGE_OR_RANGE_MIN_VOLUME", 50000.0)
    min_surge = _env_float("TONOSAMA_PRICE_CHANGE_OR_RANGE_MIN_SURGE", 0.0)
    min_abs_slope = _env_float("TONOSAMA_PRICE_CHANGE_OR_RANGE_MIN_ABS_SLOPE", 0.0)
    min_abs_score = _env_float("TONOSAMA_PRICE_CHANGE_OR_RANGE_MIN_ABS_SCORE", 0.0)

    range_ok = range_pct >= min_range
    volume_ok = volume >= min_volume
    surge_ok = surge >= min_surge
    slope_ok = slope.abs() >= min_abs_slope
    score_ok = (score.abs() >= min_abs_score) | (final_score.abs() >= min_abs_score)

    # close_pos条件: 極端に上下へ寄っている候補だけでなく、レンジが十分なら中間でも後段guardへ渡す。
    body_or_position_ok = (signed_body.abs() > 0) | (close_pos >= 20.0) | (close_pos <= 80.0) | range_ok
    rescue = (s.abs() < min_price_change) & range_ok & volume_ok & surge_ok & slope_ok & score_ok & body_or_position_ok
    if not rescue.any():
        return s

    out = s.copy()
    # signed_bodyが0なら score/final_score の符号を使う。全部0なら正方向で閾値だけ満たす。
    sign_base = signed_body.copy()
    sign_base = sign_base.where(sign_base != 0, score)
    sign_base = sign_base.where(sign_base != 0, final_score)
    sign = sign_base.apply(lambda x: 1.0 if float(x) >= 0 else -1.0)
    out.loc[rescue] = sign.loc[rescue] * min_price_change
    try:
        sample_cols = [
            "symbol", "symbolname", "close", "_latest_volume", "_intrabar_range_pct",
            "_max_volume_surge_ratio", "_max_price_change_pct", "_close_position_pct",
            "_signed_body_change_pct", "score", "final_score",
        ]
        sample = df.loc[rescue, [c for c in sample_cols if c in df.columns]].head(10).to_dict("records")
    except Exception:
        sample = []
    logger.warning(
        "[TONOSAMA PRICE/RANGE RESCUE] rescued=%s min_price_change=%.3f min_range=%.3f min_volume=%.0f min_surge=%.2f min_abs_score=%.3f sample=%s",
        int(rescue.sum()), min_price_change, min_range, min_volume, min_surge, min_abs_score, sample,
    )
    return out


def _rescue_max_volume_surge_ratio(df: pd.DataFrame, s: pd.Series) -> pd.Series:
    """surge計算だけが 0.0 になり volume_surge_low で全落ちするケースを救済する。"""
    if not _env_bool("TONOSAMA_VOLUME_SURGE_ZERO_RESCUE_ENABLED", True):
        return s
    if df is None or df.empty:
        return s

    min_surge = float(MIN_VOLUME_SURGE_RATIO or 3.0)

    latest_volume = _rescue_series_safe(df, "_latest_volume", 0.0)
    intrabar_range = _rescue_series_safe(df, "_intrabar_range_pct", 0.0)
    body_change = _rescue_series_safe(df, "_body_change_pct", 0.0)
    mtf = _rescue_series_safe(df, "mtf", 0.0)
    score_mtf = _rescue_series_safe(df, "score_mtf", 0.0)
    final_score = _rescue_series_safe(df, "final_score", 0.0)
    score = _rescue_series_safe(df, "score", 0.0)

    min_volume = _env_float("TONOSAMA_VOLUME_SURGE_ZERO_RESCUE_MIN_VOLUME", 500000.0)
    min_range = _env_float("TONOSAMA_VOLUME_SURGE_ZERO_RESCUE_MIN_RANGE_PCT", 4.0)
    min_body = _env_float("TONOSAMA_VOLUME_SURGE_ZERO_RESCUE_MIN_BODY_PCT", 0.0)
    min_abs_score = _env_float("TONOSAMA_VOLUME_SURGE_ZERO_RESCUE_MIN_ABS_SCORE", 0.8)
    min_mtf = _env_float("TONOSAMA_VOLUME_SURGE_ZERO_RESCUE_MIN_MTF", 1.0)

    volume_ok = latest_volume >= min_volume
    range_ok = intrabar_range >= min_range
    body_ok = body_change >= min_body
    score_ok = final_score.abs().combine(score.abs(), max) >= min_abs_score
    mtf_ok = mtf.abs().combine(score_mtf.abs(), max) >= min_mtf
    rescue = (s <= 0) & volume_ok & range_ok & body_ok & score_ok & mtf_ok
    if not rescue.any():
        return s

    out = s.copy()
    out.loc[rescue] = min_surge
    try:
        sample_cols = [
            "symbol", "symbolname", "close", "_latest_volume", "_intrabar_range_pct",
            "_body_change_pct", "_max_volume_surge_ratio", "score", "final_score", "mtf", "score_mtf",
        ]
        sample = df.loc[rescue, [c for c in sample_cols if c in df.columns]].head(10).to_dict("records")
    except Exception:
        sample = []
    logger.warning(
        "[TONOSAMA VOLUME SURGE ZERO RESCUE] rescued=%s min_surge=%.2f min_volume=%.0f min_range=%.3f min_abs_score=%.3f min_mtf=%.3f sample=%s",
        int(rescue.sum()), min_surge, min_volume, min_range, min_abs_score, min_mtf, sample,
    )
    return out


def _rescue_slope(df: pd.DataFrame, s: pd.Series) -> pd.Series:
    """PUSH/ランキング由来の1分特徴で slope が微小なとき slope_abs_too_small を救済する。"""
    if not _env_bool("TONOSAMA_SLOPE_RANGE_RESCUE_ENABLED", True):
        return s
    if df is None or df.empty:
        return s

    min_slope = float(MIN_SLOPE or 0.001)

    range_pct = _rescue_series(df, "_intrabar_range_pct", 0.0)
    volume = _rescue_series(df, "_latest_volume", 0.0)
    close_pos = _rescue_series(df, "_close_position_pct", 50.0)
    signed_body = _rescue_series(df, "_signed_body_change_pct", 0.0)
    score = _rescue_series(df, "score", 0.0)
    final_score = _rescue_series(df, "final_score", 0.0)
    price_chg = _rescue_series(df, "_max_price_change_pct", 0.0)
    surge = _rescue_series(df, "_max_volume_surge_ratio", 0.0)

    min_range = _env_float("TONOSAMA_SLOPE_RANGE_RESCUE_MIN_RANGE_PCT", 4.0)
    min_volume = _env_float("TONOSAMA_SLOPE_RANGE_RESCUE_MIN_VOLUME", 500000.0)
    min_abs_score = _env_float("TONOSAMA_SLOPE_RANGE_RESCUE_MIN_ABS_SCORE", 0.0)
    min_surge = _env_float("TONOSAMA_SLOPE_RANGE_RESCUE_MIN_SURGE", 0.0)
    close_pos_low = _env_float("TONOSAMA_SLOPE_RANGE_RESCUE_CLOSE_POS_LOW", 15.0)
    close_pos_high = _env_float("TONOSAMA_SLOPE_RANGE_RESCUE_CLOSE_POS_HIGH", 85.0)

    range_ok = range_pct >= min_range
    volume_ok = volume >= min_volume
    score_ok = (score.abs() >= min_abs_score) | (final_score.abs() >= min_abs_score)
    surge_ok = surge >= min_surge
    close_pos_ok = (close_pos <= close_pos_low) | (close_pos >= close_pos_high) | range_ok

    rescue = (s.abs() < min_slope) & range_ok & volume_ok & score_ok & surge_ok & close_pos_ok
    if not rescue.any():
        return s

    out = s.copy()
    sign_base = signed_body.copy()
    sign_base = sign_base.where(sign_base != 0, price_chg)
    sign_base = sign_base.where(sign_base != 0, score)
    sign_base = sign_base.where(sign_base != 0, final_score)
    # close_pos が高値圏ならBUY方向、安値圏ならSELL方向の仮符号。
    sign_base = sign_base.where(sign_base != 0, close_pos - 50.0)
    sign = sign_base.apply(lambda x: 1.0 if float(x) >= 0 else -1.0)
    out.loc[rescue] = sign.loc[rescue] * min_slope
    try:
        sample_cols = [
            "symbol", "symbolname", "close", "_latest_volume", "_intrabar_range_pct",
            "_max_volume_surge_ratio", "_max_price_change_pct", "_slope",
            "_close_position_pct", "_signed_body_change_pct", "score", "final_score",
        ]
        sample = df.loc[rescue, [c for c in sample_cols if c in df.columns]].head(10).to_dict("records")
    except Exception:
        sample = []
    logger.warning(
        "[TONOSAMA SLOPE/RANGE RESCUE] rescued=%s min_slope=%.6f min_range=%.3f min_volume=%.0f min_surge=%.2f min_abs_score=%.3f sample=%s",
        int(rescue.sum()), min_slope, min_range, min_volume, min_surge, min_abs_score, sample,
    )
    return out


def _ensure_actual_movement_cols(df: pd.DataFrame) -> pd.DataFrame:
    out = _ensure_actual_movement_cols_core(df)
    if _range_repair_enabled():
        return _repair_intrabar_range_from_history(out)
    return out


def _ensure_actual_movement_cols_core(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    x = df.copy()
    close_col = _first_existing(x, ["close", "close_price", "current_price", "price", "close_1m"])
    open_col = _first_existing(x, ["open", "open_price", "open_1m"])
    high_col = _first_existing(x, ["high", "high_price", "high_1m"])
    low_col = _first_existing(x, ["low", "low_price", "low_1m"])
    volume_col = _first_existing(x, ["volume", "volume_1m", "latest_volume", "latest_1m_volume"])

    x["_tonosama_close_for_move"] = pd.to_numeric(x[close_col], errors="coerce") if close_col else 0.0

    if open_col:
        open_s = pd.to_numeric(x[open_col], errors="coerce")
        close_s = pd.to_numeric(x["_tonosama_close_for_move"], errors="coerce")
        x["_body_change_pct"] = ((close_s - open_s).abs() / open_s.replace(0, pd.NA) * 100.0).replace([float("inf"), -float("inf")], pd.NA).fillna(0.0)
        x["_signed_body_change_pct"] = ((close_s - open_s) / open_s.replace(0, pd.NA) * 100.0).replace([float("inf"), -float("inf")], pd.NA).fillna(0.0)
    else:
        x["_body_change_pct"] = _num_series(x, "_max_price_change_pct", 0.0).abs()
        x["_signed_body_change_pct"] = _num_series(x, "_max_price_change_pct", 0.0)

    if high_col and low_col:
        high_s = pd.to_numeric(x[high_col], errors="coerce")
        low_s = pd.to_numeric(x[low_col], errors="coerce")
        close_s = pd.to_numeric(x["_tonosama_close_for_move"], errors="coerce")
        open_s = pd.to_numeric(x[open_col], errors="coerce") if open_col else close_s
        # float64 の Series へ pd.NA を入れると object dtype に昇格し、
        # 続く fillna() が暗黙ダウンキャストで FutureWarning になる。
        # np.nan なら float64 のままで、結果値は同じ。
        rng = (high_s - low_s).abs().replace(0, np.nan)
        x["_intrabar_range_pct"] = ((high_s - low_s).abs() / close_s.where(close_s > 0, np.nan) * 100.0).replace([np.inf, -np.inf], np.nan).fillna(0.0)
        x["_close_position_pct"] = ((close_s - low_s) / rng * 100.0).replace([np.inf, -np.inf], np.nan).fillna(50.0).clip(0.0, 100.0)
        x["_upper_wick_pct"] = ((high_s - pd.concat([open_s, close_s], axis=1).max(axis=1)) / rng * 100.0).replace([np.inf, -np.inf], np.nan).fillna(0.0).clip(lower=0.0)
        x["_lower_wick_pct"] = ((pd.concat([open_s, close_s], axis=1).min(axis=1) - low_s) / rng * 100.0).replace([np.inf, -np.inf], np.nan).fillna(0.0).clip(lower=0.0)
    else:
        x["_intrabar_range_pct"] = 0.0
        x["_close_position_pct"] = 50.0
        x["_upper_wick_pct"] = 0.0
        x["_lower_wick_pct"] = 0.0

    x["_latest_volume"] = pd.to_numeric(x[volume_col], errors="coerce").fillna(0.0) if volume_col else 0.0

    # ------------------------------------------------------------
    # _latest_turnover: その 1 分足の売買代金
    # ------------------------------------------------------------
    # turnover 列があればそれを使い、無ければ volume x close で作る。
    # 「出来高」と「売買代金」を同じ列名で持ち回らないため、
    # 名前を _latest_volume / _latest_turnover に分ける。
    turnover_col = _first_existing(
        x, ["turnover", "turnover_1m", "trading_value", "trade_value", "value_1m"]
    )
    if turnover_col:
        x["_latest_turnover"] = pd.to_numeric(x[turnover_col], errors="coerce").fillna(0.0)
        x["_latest_turnover_source"] = turnover_col
    else:
        x["_latest_turnover"] = (
            _num_series(x, "_latest_volume", 0.0)
            * pd.to_numeric(x["_tonosama_close_for_move"], errors="coerce").fillna(0.0)
        )
        x["_latest_turnover_source"] = "volume_x_close"

    # ------------------------------------------------------------
    # _slope: 正規列名への 1 回だけの互換変換 (入力境界)
    # ------------------------------------------------------------
    # 2026-09-01 のログでは base summary に slope 列があるのに
    #   [TONOSAMA FILTER WARN] stage=primary missing _slope -> slope filter skipped
    # が出続け、傾きフィルターが一度も効いていなかった。
    # 下流 (フィルタ / サンプル / rescue) は _slope を正規名として使うので、
    # ここで 1 回だけ寄せる。値の変換はしない (別名の付け替えだけ)。
    if "_slope" not in x.columns or not pd.to_numeric(x["_slope"], errors="coerce").notna().any():
        slope_col = _first_existing(
            x, ["slope", "slope_atr_scaled", "score_slope", "ma75_slope", "slope_1m"]
        )
        if slope_col:
            x["_slope"] = pd.to_numeric(x[slope_col], errors="coerce")
            x["_slope_source"] = slope_col
            logger.warning(
                "[TONOSAMA SLOPE ALIAS] _slope が無かったので %s から作った rows=%s nonnull=%s "
                "(正規列名は _slope。互換変換は入力境界のここだけで行う)",
                slope_col, len(x), int(x["_slope"].notna().sum()),
            )
        else:
            x["_slope_source"] = "missing"

    return x


def _sample_rows(df: pd.DataFrame, cols: list[str], limit: int = 8) -> list[dict[str, Any]]:
    if df is None or df.empty:
        return []
    out: list[dict[str, Any]] = []
    use_cols = [c for c in cols if c in df.columns]
    for _, row in df.head(limit).iterrows():
        item: dict[str, Any] = {}
        for c in use_cols:
            v = row.get(c)
            try:
                if pd.isna(v):
                    v = None
            except Exception:
                pass
            if isinstance(v, float):
                v = round(v, 6)
            item[c] = v
        out.append(item)
    return out


def _log_filter_step(*, stage: str, before: pd.DataFrame, after: pd.DataFrame, reason: str, threshold: Any, sample_cols: list[str]) -> None:
    try:
        before_rows = 0 if before is None else len(before)
        after_rows = 0 if after is None else len(after)
        dropped = max(0, before_rows - after_rows)
        if dropped <= 0:
            logger.info("[TONOSAMA FILTER PASS] stage=%s reason=%s before=%s after=%s threshold=%s", stage, reason, before_rows, after_rows, threshold)
            return
        after_index = set(after.index) if after is not None and not after.empty else set()
        dropped_df = before.loc[[idx for idx in before.index if idx not in after_index]].copy() if before is not None and not before.empty else pd.DataFrame()
        logger.warning("[TONOSAMA FILTER DROP] stage=%s reason=%s before=%s after=%s dropped=%s threshold=%s sample=%s", stage, reason, before_rows, after_rows, dropped, threshold, _sample_rows(dropped_df, sample_cols, limit=10))
    except Exception:
        logger.debug("[TONOSAMA FILTER] step log failed stage=%s reason=%s", stage, reason, exc_info=True)


def _strong_move_slope_failopen_mask(x: pd.DataFrame) -> pd.Series:
    """_slope が0/欠損でも、値動き・値幅・出来高が十分ならfinal slope filterを通す。"""
    if x is None or x.empty or not _env_bool("TONOSAMA_FINAL_SLOPE_STRONG_MOVE_FAILOPEN", True):
        return pd.Series(False, index=x.index if x is not None else None, dtype="bool")
    price_abs = _num_series(x, "_max_price_change_pct", 0.0).abs()
    body_abs = _num_series(x, "_body_change_pct", 0.0).abs()
    range_pct = _num_series(x, "_intrabar_range_pct", 0.0).abs()
    latest_vol = _num_series(x, "_latest_volume", 0.0).combine(_num_series(x, "volume", 0.0), max)
    surge = _num_series(x, "_max_volume_surge_ratio", 0.0)
    slope_abs = _num_series(x, "_slope", 0.0).abs()

    min_price_change = _env_float("TONOSAMA_FINAL_SLOPE_FAILOPEN_MIN_PRICE_CHANGE_PCT", 0.5)
    min_body = _env_float("TONOSAMA_FINAL_SLOPE_FAILOPEN_MIN_BODY_PCT", 0.0)
    min_range = _env_float("TONOSAMA_FINAL_SLOPE_FAILOPEN_MIN_RANGE_PCT", 1.0)
    min_volume = _env_float("TONOSAMA_FINAL_SLOPE_FAILOPEN_MIN_VOLUME", 50000.0)
    min_surge = _env_float("TONOSAMA_FINAL_SLOPE_FAILOPEN_MIN_SURGE", 0.0)
    max_slope_abs = _env_float("TONOSAMA_FINAL_SLOPE_FAILOPEN_MAX_SLOPE_ABS", max(MIN_SLOPE, 0.001))

    strong_move = (
        (slope_abs < max_slope_abs)
        & ((price_abs >= min_price_change) | (body_abs >= min_body if min_body > 0 else price_abs >= min_price_change))
        & (range_pct >= min_range)
        & (latest_vol >= min_volume)
        & (surge >= min_surge)
    )
    try:
        if strong_move.any():
            logger.warning(
                "[TONOSAMA FINAL SLOPE FAILOPEN] rescued=%s min_price_change=%.3f min_body=%.3f min_range=%.3f min_volume=%.0f min_surge=%.2f sample=%s",
                int(strong_move.sum()), min_price_change, min_body, min_range, min_volume, min_surge,
                _sample_rows(x.loc[strong_move], ["symbol", "symbolname", "close", "_latest_volume", "_max_price_change_pct", "_body_change_pct", "_intrabar_range_pct", "_max_volume_surge_ratio", "_slope", "_tonosama_score"], limit=10),
            )
    except Exception:
        logger.debug("[TONOSAMA FINAL SLOPE FAILOPEN] log failed", exc_info=True)
    return strong_move.fillna(False).astype(bool)


def _diagnose_base_frame(x: pd.DataFrame) -> None:
    try:
        if x is None or x.empty:
            logger.warning("[TONOSAMA FILTER DIAG] base empty")
            return
        summary = {
            "rows": len(x),
            "symbols": int(x["symbol"].astype(str).nunique()) if "symbol" in x.columns else 0,
            "cols": list(x.columns)[:80],
            "min_price": MIN_PRICE,
            "min_volume_surge": MIN_VOLUME_SURGE_RATIO,
            "min_price_change_pct": MIN_PRICE_CHANGE_PCT,
            "min_slope": MIN_SLOPE,
            "min_body_change_pct": MIN_BODY_CHANGE_PCT,
            "min_intrabar_range_pct": MIN_INTRABAR_RANGE_PCT,
            "min_latest_volume": MIN_LATEST_VOLUME,
            "min_raw_score": MIN_RAW_SCORE,
            "max_buy_price_change_pct": MAX_BUY_PRICE_CHANGE_PCT,
            "max_sell_price_drop_pct": MAX_SELL_PRICE_DROP_PCT,
            "buy_rejected_close_position_pct": BUY_REJECTED_CLOSE_POSITION_PCT,
            "sell_rejected_close_position_pct": SELL_REJECTED_CLOSE_POSITION_PCT,
            "use_5sec_confirm": USE_5SEC_CONFIRM,
            "require_5sec_bar": REQUIRE_5SEC_BAR,
            "min_5sec_price_change_pct": MIN_5SEC_PRICE_CHANGE_PCT,
            "max_5sec_drop_pct": MAX_5SEC_DROP_PCT,
        }
        metrics = {}
        for col in ["close", "_max_volume_surge_ratio", "_max_price_change_pct", "_body_change_pct", "_signed_body_change_pct", "_intrabar_range_pct", "_close_position_pct", "_upper_wick_pct", "_lower_wick_pct", "_latest_volume", "_slope", "_tonosama_score"]:
            if col in x.columns:
                s = pd.to_numeric(x[col], errors="coerce")
                metrics[col] = {"nonnull": int(s.notna().sum()), "min": round(float(s.min()), 6) if s.notna().any() else None, "max": round(float(s.max()), 6) if s.notna().any() else None, "mean": round(float(s.mean()), 6) if s.notna().any() else None, "zero": int((s.fillna(0.0) == 0.0).sum())}
        logger.warning("[TONOSAMA FILTER DIAG] base summary=%s metrics=%s head=%s", summary, metrics, _sample_rows(x, ["symbol", "symbolname", "close", "_max_volume_surge_ratio", "_max_price_change_pct", "_body_change_pct", "_intrabar_range_pct", "_close_position_pct", "_upper_wick_pct", "_lower_wick_pct", "_latest_volume", "_slope", "score", "final_score", "score_mtf"], limit=15))
    except Exception:
        logger.debug("[TONOSAMA FILTER DIAG] base diagnose failed", exc_info=True)


def _apply_climax_guards(x: pd.DataFrame, *, stage: str, sample_cols: list[str]) -> pd.DataFrame:
    """クライマックス/ヒゲ反転ガード。

    旧 core/startup/tonosama_5sec_advisory_patch V3 がこの関数をラップして
    fail-open (例外時は入力をそのまま返す) にしていた。ガードで例外が出たときに
    tonosama_loop ごと落として殿様エントリーを丸ごと止めないための保険なので、
    その挙動を本文へ取り込んだ。
    判定ロジック自体は既に本体側でベクトル化されており、パッチが持っていた
    「Series 真偽値エラー時のベクトル化フォールバック」は本文と同一だったため
    不要になった (同じ式なので再実行しても同じ例外になる)。
    """
    try:
        return _apply_climax_guards_core(x, stage=stage, sample_cols=sample_cols)
    except ValueError as e:
        # Series 真偽値の曖昧さは本文がベクトル化済みで起きないはずなので、
        # 握りつぶさず表に出す (旧パッチも同じ式で再実行して結局伝播していた)。
        if "truth value of a Series is ambiguous" in str(e):
            raise
        logger.exception("[TONOSAMA] climax guard failed stage=%s -> fail-open", stage)
        return x if x is not None else pd.DataFrame()
    except Exception:
        logger.exception("[TONOSAMA] climax guard failed stage=%s -> fail-open", stage)
        return x if x is not None else pd.DataFrame()


def _apply_climax_guards_core(x: pd.DataFrame, *, stage: str, sample_cols: list[str]) -> pd.DataFrame:
    if x is None or x.empty:
        return pd.DataFrame()

    surge = _num_series(x, "_max_volume_surge_ratio")
    price_chg = _num_series(x, "_max_price_change_pct")
    signed_body = _num_series(x, "_signed_body_change_pct")
    close_pos = _num_series(x, "_close_position_pct", 50.0)
    upper_wick = _num_series(x, "_upper_wick_pct")
    slope = _num_series(x, "_slope")

    buy_like = (price_chg > 0) | (signed_body > 0) | (slope > 0)
    buy_too_late = buy_like & (price_chg >= MAX_BUY_PRICE_CHANGE_PCT)
    buy_high_zone = buy_like & (close_pos >= MAX_BUY_CLOSE_POSITION_PCT) & (price_chg >= BUYING_CLIMAX_MIN_PRICE_CHANGE_PCT)
    buy_upper_wick_reversal = buy_like & (upper_wick >= MAX_BUY_UPPER_WICK_PCT) & (close_pos <= BUY_REJECTED_CLOSE_POSITION_PCT)
    buying_climax = buy_like & (surge >= BUYING_CLIMAX_MIN_SURGE_RATIO) & (
        ((price_chg >= BUYING_CLIMAX_MIN_PRICE_CHANGE_PCT) & (close_pos >= MAX_BUY_CLOSE_POSITION_PCT))
        | ((upper_wick >= MAX_BUY_UPPER_WICK_PCT) & (close_pos <= BUY_REJECTED_CLOSE_POSITION_PCT))
    )
    before = x.copy()
    x = x[~(buy_too_late | buy_high_zone | buy_upper_wick_reversal | buying_climax)]
    _log_filter_step(
        stage=stage,
        before=before,
        after=x,
        reason="buying_climax_or_upper_wick_reversal_guard",
        threshold={
            "MAX_BUY_PRICE_CHANGE_PCT": MAX_BUY_PRICE_CHANGE_PCT,
            "MAX_BUY_CLOSE_POSITION_PCT": MAX_BUY_CLOSE_POSITION_PCT,
            "MAX_BUY_UPPER_WICK_PCT": MAX_BUY_UPPER_WICK_PCT,
            "BUY_REJECTED_CLOSE_POSITION_PCT": BUY_REJECTED_CLOSE_POSITION_PCT,
            "BUYING_CLIMAX_MIN_SURGE_RATIO": BUYING_CLIMAX_MIN_SURGE_RATIO,
            "BUYING_CLIMAX_MIN_PRICE_CHANGE_PCT": BUYING_CLIMAX_MIN_PRICE_CHANGE_PCT,
        },
        sample_cols=sample_cols,
    )

    if x.empty:
        return x

    surge = _num_series(x, "_max_volume_surge_ratio")
    price_chg = _num_series(x, "_max_price_change_pct")
    signed_body = _num_series(x, "_signed_body_change_pct")
    close_pos = _num_series(x, "_close_position_pct", 50.0)
    lower_wick = _num_series(x, "_lower_wick_pct")
    slope = _num_series(x, "_slope")

    drop_abs = price_chg.abs()
    sell_like = (price_chg < 0) | (signed_body < 0) | (slope < 0)
    sell_too_late = sell_like & (drop_abs >= MAX_SELL_PRICE_DROP_PCT)
    sell_low_zone = sell_like & (close_pos <= MIN_SELL_CLOSE_POSITION_PCT) & (drop_abs >= SELLING_CLIMAX_MIN_PRICE_DROP_PCT)
    sell_lower_wick_reversal = sell_like & (lower_wick >= MAX_SELL_LOWER_WICK_PCT) & (close_pos >= SELL_REJECTED_CLOSE_POSITION_PCT)
    selling_climax = sell_like & (surge >= SELLING_CLIMAX_MIN_SURGE_RATIO) & (
        ((drop_abs >= SELLING_CLIMAX_MIN_PRICE_DROP_PCT) & (close_pos <= MIN_SELL_CLOSE_POSITION_PCT))
        | ((lower_wick >= MAX_SELL_LOWER_WICK_PCT) & (close_pos >= SELL_REJECTED_CLOSE_POSITION_PCT))
    )
    before = x.copy()
    x = x[~(sell_too_late | sell_low_zone | sell_lower_wick_reversal | selling_climax)]
    _log_filter_step(
        stage=stage,
        before=before,
        after=x,
        reason="selling_climax_or_lower_wick_reversal_guard",
        threshold={
            "MAX_SELL_PRICE_DROP_PCT": MAX_SELL_PRICE_DROP_PCT,
            "MIN_SELL_CLOSE_POSITION_PCT": MIN_SELL_CLOSE_POSITION_PCT,
            "MAX_SELL_LOWER_WICK_PCT": MAX_SELL_LOWER_WICK_PCT,
            "SELL_REJECTED_CLOSE_POSITION_PCT": SELL_REJECTED_CLOSE_POSITION_PCT,
            "SELLING_CLIMAX_MIN_SURGE_RATIO": SELLING_CLIMAX_MIN_SURGE_RATIO,
            "SELLING_CLIMAX_MIN_PRICE_DROP_PCT": SELLING_CLIMAX_MIN_PRICE_DROP_PCT,
        },
        sample_cols=sample_cols,
    )
    return x


def _apply_primary_filters(x: pd.DataFrame) -> pd.DataFrame:
    global _LAST_FILTER_DIAG
    _LAST_FILTER_DIAG = {}
    if x is None or x.empty:
        _LAST_FILTER_DIAG = {"stage": "primary", "base_rows": 0, "primary_rows": 0, "empty_reason": "base_empty"}
        return pd.DataFrame()
    x = _ensure_actual_movement_cols(x)
    base_rows = len(x)
    try:
        from .config import log_effective_config_once

        log_effective_config_once()
    except Exception:
        logger.debug("[TONOSAMA CONFIG] effective config log failed", exc_info=True)
    _diagnose_base_frame(x)
    sample_cols = ["symbol", "symbolname", "close", "_latest_volume", "_body_change_pct", "_signed_body_change_pct", "_intrabar_range_pct", "_close_position_pct", "_upper_wick_pct", "_lower_wick_pct", "_max_volume_surge_ratio", "_max_price_change_pct", "_slope", "score", "final_score", "score_mtf", "mtf"]
    if "close" in x.columns:
        before = x.copy(); x = x[_num_series(x, "close") > MIN_PRICE]
        _log_filter_step(stage="primary", before=before, after=x, reason="close_below_min_price", threshold={"MIN_PRICE": MIN_PRICE}, sample_cols=sample_cols)
    before = x.copy(); x = x[_num_series(x, "_latest_volume") >= MIN_LATEST_VOLUME]
    _log_filter_step(stage="primary", before=before, after=x, reason="latest_volume_low_flat_alert_guard", threshold={"MIN_LATEST_VOLUME": MIN_LATEST_VOLUME}, sample_cols=sample_cols)
    # 売買代金 (最新1分足) の下限。出来高だけだと低位株が通ってしまう。
    # _latest_turnover は _ensure_actual_movement_cols_core が必ず作るので
    # 「列が無くて全件落ちる」ことは起きない。
    before = x.copy(); x = x[_num_series(x, "_latest_turnover") >= MIN_LATEST_TURNOVER]
    _log_filter_step(stage="primary", before=before, after=x, reason="latest_turnover_low", threshold={"MIN_LATEST_TURNOVER": MIN_LATEST_TURNOVER}, sample_cols=sample_cols)
    before = x.copy(); x = x[_num_series(x, "_body_change_pct") >= MIN_BODY_CHANGE_PCT]
    _log_filter_step(stage="primary", before=before, after=x, reason="body_change_low_flat_alert_guard", threshold={"MIN_BODY_CHANGE_PCT": MIN_BODY_CHANGE_PCT}, sample_cols=sample_cols)
    before = x.copy(); x = x[_num_series(x, "_intrabar_range_pct") >= MIN_INTRABAR_RANGE_PCT]
    _log_filter_step(stage="primary", before=before, after=x, reason="intrabar_range_low_flat_alert_guard", threshold={"MIN_INTRABAR_RANGE_PCT": MIN_INTRABAR_RANGE_PCT}, sample_cols=sample_cols)
    if "_max_volume_surge_ratio" in x.columns:
        before = x.copy(); x = x[_num_series(x, "_max_volume_surge_ratio") >= MIN_VOLUME_SURGE_RATIO]
        _log_filter_step(stage="primary", before=before, after=x, reason="volume_surge_low", threshold={"MIN_VOLUME_SURGE_RATIO": MIN_VOLUME_SURGE_RATIO}, sample_cols=sample_cols)
    if "_max_price_change_pct" in x.columns:
        before = x.copy(); x = x[_num_series(x, "_max_price_change_pct").abs() >= MIN_PRICE_CHANGE_PCT]
        _log_filter_step(stage="primary", before=before, after=x, reason="price_change_low_abs", threshold={"MIN_PRICE_CHANGE_PCT": MIN_PRICE_CHANGE_PCT}, sample_cols=sample_cols)
    if "_slope" in x.columns:
        before = x.copy(); x = x[_num_series(x, "_slope").abs() >= MIN_SLOPE]
        _log_filter_step(stage="primary", before=before, after=x, reason="slope_abs_too_small", threshold={"MIN_SLOPE": MIN_SLOPE}, sample_cols=sample_cols)
    else:
        logger.warning("[TONOSAMA FILTER WARN] stage=primary missing _slope -> slope filter skipped cols=%s", list(x.columns))

    x = _apply_climax_guards(x, stage="primary", sample_cols=sample_cols)

    _LAST_FILTER_DIAG = {"stage": "primary", "base_rows": base_rows, "primary_rows": len(x), "thresholds": {"MIN_PRICE": MIN_PRICE, "MIN_LATEST_VOLUME": MIN_LATEST_VOLUME, "MIN_LATEST_TURNOVER": MIN_LATEST_TURNOVER, "MIN_BODY_CHANGE_PCT": MIN_BODY_CHANGE_PCT, "MIN_INTRABAR_RANGE_PCT": MIN_INTRABAR_RANGE_PCT, "MIN_VOLUME_SURGE_RATIO": MIN_VOLUME_SURGE_RATIO, "MIN_PRICE_CHANGE_PCT": MIN_PRICE_CHANGE_PCT, "MIN_SLOPE": MIN_SLOPE, "MAX_BUY_PRICE_CHANGE_PCT": MAX_BUY_PRICE_CHANGE_PCT, "MAX_SELL_PRICE_DROP_PCT": MAX_SELL_PRICE_DROP_PCT, "BUY_REJECTED_CLOSE_POSITION_PCT": BUY_REJECTED_CLOSE_POSITION_PCT, "SELL_REJECTED_CLOSE_POSITION_PCT": SELL_REJECTED_CLOSE_POSITION_PCT}, "survivors": _sample_rows(x, sample_cols, limit=12)}
    logger.warning("[TONOSAMA FILTER SUMMARY] stage=primary base_rows=%s primary_rows=%s survivors=%s thresholds=%s", base_rows, len(x), _LAST_FILTER_DIAG.get("survivors"), _LAST_FILTER_DIAG.get("thresholds"))
    return x


# ============================================================
# HISTORY MISSING GUARD
# (旧 core/startup/tonosama_history_missing_guard_patch.py の
#  runner.build_scalping_feature_df 差し替えを本文化)
# ------------------------------------------------------------
# 履歴欠損 / failopen 行を落とす。旧 install はこのラップだけを行い、
# TONOSAMA_HISTORY_MISSING_GUARD_ENABLED が偽なら掛けずに return して
# いたので、その判定を guard_enabled() で再現する。
# DISABLE_TONOSAMA_HISTORY_MISSING_GUARD_PATCH=1 でモジュールごと
# 無効化されていた分も、import 失敗時は core へ落とすことで揃う。
# ============================================================


def build_scalping_feature_df() -> pd.DataFrame:
    try:
        from trading.entry.tonosama.history_missing_guard import (
            build_scalping_feature_df_guarded,
            guard_enabled,
        )
    except Exception:
        logger.exception("[TONOSAMA HISTORY GUARD] import failed; use core build_scalping_feature_df")
        return _build_scalping_feature_df_core()
    if not guard_enabled():
        return _build_scalping_feature_df_core()
    return build_scalping_feature_df_guarded(_build_scalping_feature_df_core)


def build_feature_df_with_5sec() -> pd.DataFrame:
    started = time.perf_counter()
    x = build_scalping_feature_df()
    if x is None or x.empty:
        logger.info("[TONOSAMA ENTRY] base feature empty")
        return pd.DataFrame()
    base_rows = len(x)
    x = _apply_primary_filters(x)
    primary_rows = len(x)
    if x.empty:
        logger.info("[TONOSAMA ENTRY] no candidates after primary filters base_rows=%s primary_rows=%s diag=%s elapsed=%.3fs", base_rows, primary_rows, _LAST_FILTER_DIAG, time.perf_counter() - started)
        return pd.DataFrame()
    # 旧 core/startup/tonosama_fast_score_prefilter_patch V5.2 から移設。
    # 5秒足の特徴量を作る前にスコアで足切りし、zero-surge 銘柄は救済する。
    _fsp_on = _fast_score.prefilter_enabled()
    _fsp_th = _fast_score.score_threshold(sys.modules[__name__]) if _fsp_on else 0.0
    if _fsp_on:
        x = _fast_score.zero_surge_score_rescue(x, threshold=_fsp_th)

    try:
        x = prepare_entry_scores(x)
        if _fsp_on:
            x = _fast_score.zero_surge_score_rescue(x, threshold=_fsp_th)
        if "_tonosama_score" in x.columns:
            x = x.sort_values("_tonosama_score", ascending=False)
    except Exception:
        logger.warning("[TONOSAMA ENTRY] pre 5sec prepare_entry_scores failed", exc_info=True)

    if _fsp_on and "_tonosama_score" in x.columns:
        _fsp_sample_cols = [
            "symbol", "symbolname", "close", "_latest_volume",
            "_body_change_pct", "_signed_body_change_pct", "_intrabar_range_pct",
            "_close_position_pct", "_upper_wick_pct", "_lower_wick_pct",
            "_max_volume_surge_ratio", "_max_price_change_pct", "_slope",
            "_tonosama_score", "score", "final_score", "score_mtf", "mtf", "_volume_surge_failopen",
        ]
        before = x.copy()
        x = x[pd.to_numeric(x["_tonosama_score"], errors="coerce").fillna(0.0) >= _fsp_th]
        try:
            _log_filter_step(
                stage="pre_5sec_score",
                before=before,
                after=x,
                reason="tonosama_score_below_min_final_pre_5sec",
                threshold={"MIN_FINAL_SCORE": MIN_FINAL_SCORE, "effective_threshold": _fsp_th},
                sample_cols=_fsp_sample_cols,
            )
        except Exception:
            logger.warning(
                "[TONOSAMA FAST SCORE PREFILTER] pre_5sec before=%s after=%s threshold=%.3f",
                len(before), len(x), _fsp_th,
            )
        if x.empty:
            logger.info(
                "[TONOSAMA ENTRY] no candidates after fast score prefilter base_rows=%s primary_rows=%s "
                "threshold=%.3f elapsed=%.3fs",
                base_rows, primary_rows, _fsp_th, time.perf_counter() - started,
            )
            return pd.DataFrame()

    max_5sec = int(MAX_5SEC_FEATURE_SYMBOLS or 0)
    if max_5sec <= 0:
        max_5sec = int(MAX_CANDIDATES or 80)
    before_head = _sample_rows(x, ["symbol", "symbolname", "close", "_latest_volume", "_body_change_pct", "_signed_body_change_pct", "_intrabar_range_pct", "_close_position_pct", "_upper_wick_pct", "_lower_wick_pct", "_max_volume_surge_ratio", "_max_price_change_pct", "_slope", "_tonosama_score"], limit=12)
    x = x.head(min(max_5sec, MAX_CANDIDATES)).reset_index(drop=True)
    features = []
    feature_missing = 0
    for _, row in x.iterrows():
        sym = normalize_symbol(row.get("symbol"))
        if not sym:
            continue
        try:
            f = build_5sec_features(sym)
            if not isinstance(f, dict):
                f = {}
            if not f:
                feature_missing += 1
            f["symbol"] = sym
            features.append(f)
        except Exception:
            feature_missing += 1
            logger.warning("[TONOSAMA ENTRY] build_5sec_features failed symbol=%s", sym, exc_info=True)
            features.append({"symbol": sym})
    if features:
        x = x.merge(pd.DataFrame(features), on="symbol", how="left")
    for c in ["price_change_5s_pct", "volume_surge_ratio_5s", "latest_5sec_close", "latest_5sec_volume"]:
        if c in x.columns:
            x[c] = pd.to_numeric(x[c], errors="coerce")
    x = prepare_entry_scores(x)
    if _fsp_on:
        x = _fast_score.zero_surge_score_rescue(x, threshold=_fsp_th)
    logger.info("[TONOSAMA ENTRY] feature build done base_rows=%s primary_rows=%s five_sec_rows=%s feature_missing=%s pre_5sec_head=%s post_5sec_head=%s elapsed=%.3fs", base_rows, primary_rows, len(x), feature_missing, before_head, _sample_rows(x, ["symbol", "symbolname", "close", "_latest_volume", "_body_change_pct", "_signed_body_change_pct", "_intrabar_range_pct", "_close_position_pct", "_upper_wick_pct", "_lower_wick_pct", "_max_volume_surge_ratio", "_max_price_change_pct", "_slope", "_tonosama_score", "has_5sec_bar", "price_change_5s_pct", "volume_surge_ratio_5s"], limit=12), time.perf_counter() - started)
    return x


def _apply_5sec_filter(x: pd.DataFrame, sample_cols: list[str]) -> pd.DataFrame:
    if not USE_5SEC_CONFIRM or "has_5sec_bar" not in x.columns:
        return x
    if REQUIRE_5SEC_BAR:
        before = x.copy(); x = x[_bool_series(x, "has_5sec_bar")]
        _log_filter_step(stage="5sec", before=before, after=x, reason="missing_5sec_bar", threshold={"REQUIRE_5SEC_BAR": REQUIRE_5SEC_BAR}, sample_cols=sample_cols)
        before = x.copy(); has_bar = _bool_series(x, "has_5sec_bar"); chg_5s = _num_series(x, "price_change_5s_pct")
        x = x[(~has_bar) | (chg_5s.abs() >= MIN_5SEC_PRICE_CHANGE_PCT)]
        _log_filter_step(stage="5sec", before=before, after=x, reason="five_sec_price_change_abs_strict_ng", threshold={"MIN_5SEC_PRICE_CHANGE_PCT": MIN_5SEC_PRICE_CHANGE_PCT, "MAX_5SEC_DROP_PCT": MAX_5SEC_DROP_PCT, "REQUIRE_5SEC_BAR": REQUIRE_5SEC_BAR}, sample_cols=sample_cols)
        return x

    before = x.copy(); has_bar = _bool_series(x, "has_5sec_bar"); chg_5s = _num_series(x, "price_change_5s_pct")
    x = x[(~has_bar) | (chg_5s > MAX_5SEC_DROP_PCT)]
    _log_filter_step(stage="5sec", before=before, after=x, reason="five_sec_advisory_drop_only_strong_reverse", threshold={"MAX_5SEC_DROP_PCT": MAX_5SEC_DROP_PCT, "REQUIRE_5SEC_BAR": REQUIRE_5SEC_BAR, "MIN_5SEC_PRICE_CHANGE_PCT_IGNORED_WHEN_OPTIONAL": MIN_5SEC_PRICE_CHANGE_PCT}, sample_cols=sample_cols)
    return x


def iter_tonosama_candidate_rows() -> pd.DataFrame:
    started = time.perf_counter()
    x = build_feature_df_with_5sec()
    if x.empty:
        return pd.DataFrame()
    x = _ensure_actual_movement_cols(x)
    sample_cols = ["symbol", "symbolname", "close", "_latest_volume", "_body_change_pct", "_signed_body_change_pct", "_intrabar_range_pct", "_close_position_pct", "_upper_wick_pct", "_lower_wick_pct", "_max_volume_surge_ratio", "_max_price_change_pct", "_slope", "_tonosama_score", "has_5sec_bar", "price_change_5s_pct", "volume_surge_ratio_5s"]
    for reason, col, threshold in [
        ("close_below_min_price", "close", MIN_PRICE),
        ("latest_volume_low_flat_alert_guard", "_latest_volume", MIN_LATEST_VOLUME),
        ("body_change_low_flat_alert_guard", "_body_change_pct", MIN_BODY_CHANGE_PCT),
        ("intrabar_range_low_flat_alert_guard", "_intrabar_range_pct", MIN_INTRABAR_RANGE_PCT),
        ("volume_surge_low", "_max_volume_surge_ratio", MIN_VOLUME_SURGE_RATIO),
    ]:
        before = x.copy()
        if reason == "close_below_min_price":
            x = x[_num_series(x, col) > threshold]
        else:
            x = x[_num_series(x, col) >= threshold]
        _log_filter_step(stage="final", before=before, after=x, reason=reason, threshold={col: threshold}, sample_cols=sample_cols)

    before = x.copy(); x = x[_num_series(x, "_max_price_change_pct").abs() >= MIN_PRICE_CHANGE_PCT]
    _log_filter_step(stage="final", before=before, after=x, reason="price_change_low_abs", threshold={"MIN_PRICE_CHANGE_PCT": MIN_PRICE_CHANGE_PCT}, sample_cols=sample_cols)
    before = x.copy()
    slope_ok = _num_series(x, "_slope").abs() >= MIN_SLOPE
    slope_failopen = _strong_move_slope_failopen_mask(x)
    x = x[slope_ok | slope_failopen]
    _log_filter_step(
        stage="final",
        before=before,
        after=x,
        reason="slope_abs_too_small",
        threshold={
            "MIN_SLOPE": MIN_SLOPE,
            "failopen": "strong_move",
            "min_price_change_pct": _env_float("TONOSAMA_FINAL_SLOPE_FAILOPEN_MIN_PRICE_CHANGE_PCT", 0.5),
            "min_range_pct": _env_float("TONOSAMA_FINAL_SLOPE_FAILOPEN_MIN_RANGE_PCT", 1.0),
            "min_volume": _env_float("TONOSAMA_FINAL_SLOPE_FAILOPEN_MIN_VOLUME", 50000.0),
        },
        sample_cols=sample_cols,
    )
    x = _apply_climax_guards(x, stage="final", sample_cols=sample_cols)

    x = _apply_5sec_filter(x, sample_cols)
    before = x.copy(); x = x[_num_series(x, "_tonosama_score") >= MIN_RAW_SCORE]
    _log_filter_step(stage="score", before=before, after=x, reason="raw_score_low", threshold={"MIN_RAW_SCORE": MIN_RAW_SCORE}, sample_cols=sample_cols)
    if x.empty:
        logger.info("[TONOSAMA ENTRY] no scalping candidates after surge/5sec/actual-move/climax filters diag=%s elapsed=%.3fs", _LAST_FILTER_DIAG, time.perf_counter() - started)
        return pd.DataFrame()
    out = x.sort_values("_tonosama_score", ascending=False).head(MAX_CANDIDATES).reset_index(drop=True)
    logger.info("[TONOSAMA ENTRY] candidates ready rows=%s head=%s elapsed=%.3fs", len(out), _sample_rows(out, sample_cols, limit=12), time.perf_counter() - started)
    return out


# ============================================================
# candidate優先度ソート (旧 core/startup/tonosama_runtime_budget_patch.py)
# ------------------------------------------------------------
# 予算内で評価しきれる件数に絞る前に、購入可能価格帯を優先しつつ
# score/出来高/値幅/出来高急増率が高い順へ並べ替える。
# ============================================================
def _prioritize_candidates(candidates: pd.DataFrame) -> pd.DataFrame:
    try:
        if candidates is None or candidates.empty:
            return candidates
        df = candidates.copy()

        price = pd.to_numeric(df.get("close"), errors="coerce").fillna(0.0)
        min_price = _env_float("ENTRY_MIN_PRICE", _env_float("TONOSAMA_ENTRY_MIN_PRICE", 1500.0))
        max_price = _env_float("ENTRY_MAX_PRICE", _env_float("TONOSAMA_ENTRY_MAX_PRICE", 7000.0))
        budget = _env_float("ENTRY_BUDGET_YEN", _env_float("TRADE_BUDGET_YEN", 700000.0))
        lot_size = max(1.0, _env_float("ENTRY_LOT_SIZE", 100.0))
        affordable_max = min(max_price, budget / lot_size) if budget > 0 else max_price

        if _env_bool("TONOSAMA_RUNTIME_PRIORITIZE_PRICE_ELIGIBLE", True):
            df["__prio_price_ok"] = (price >= min_price) & (price <= affordable_max)
        else:
            df["__prio_price_ok"] = True

        df["__prio_score"] = df.get("_tonosama_score", 0).map(lambda x: abs(safe_float(x, 0.0)))
        df["__prio_volume"] = df.get("_latest_volume", 0).map(lambda x: safe_float(x, 0.0))
        df["__prio_range"] = df.get("_intrabar_range_pct", 0).map(lambda x: safe_float(x, 0.0))
        df["__prio_surge"] = df.get("_max_volume_surge_ratio", 0).map(lambda x: safe_float(x, 0.0))
        eligible = int(df["__prio_price_ok"].sum())
        df = df.sort_values(
            ["__prio_price_ok", "__prio_score", "__prio_volume", "__prio_range", "__prio_surge"],
            ascending=[False, False, False, False, False],
            kind="mergesort",
        )
        logger.warning(
            "[TONOSAMA RUNTIME BUDGET] priority rows=%s price_eligible=%s min_price=%.1f max_price=%.1f affordable_max=%.1f",
            len(df), eligible, min_price, max_price, affordable_max,
        )
        return df.drop(columns=[c for c in ("__prio_price_ok", "__prio_score", "__prio_volume", "__prio_range", "__prio_surge") if c in df.columns])
    except Exception:
        logger.debug("[TONOSAMA RUNTIME BUDGET] prioritize failed", exc_info=True)
        return candidates


def build_tonosama_entries() -> int:
    """殿様候補をAI評価しpendingへ登録する (旧 core/startup/tonosama_runtime_budget_patch.py 統合済み)。

    timeout wrapper では実行中のPythonスレッドを強制停止できないため、
    ここで時間予算 (TONOSAMA_LOOP_TIME_BUDGET_SEC, 下限25秒) と評価件数上限
    (TONOSAMA_RUNTIME_MAX_EVAL_CANDIDATES, 下限12件) を見ながら自分で早期終了する。
    registered=0 の間は時間切れだけでは止めず、最低1件登録できるまで続ける。
    """
    build_started = time.perf_counter()
    raw_budget = _env_float("TONOSAMA_LOOP_TIME_BUDGET_SEC", 25.0)
    raw_max_eval = _env_int("TONOSAMA_RUNTIME_MAX_EVAL_CANDIDATES", 12)
    budget_sec = max(25.0, raw_budget)
    max_eval = max(12, raw_max_eval)
    min_remain = max(1.5, _env_float("TONOSAMA_RUNTIME_MIN_REMAIN_SEC", 1.5))
    return_after_first = _env_bool("TONOSAMA_RUNTIME_RETURN_AFTER_FIRST_PENDING", True)

    _wg = _tono_worker_guard()
    if _wg is not None and _wg.check(label="tonosama_before_candidates"):
        logger.warning("[TONOSAMA RUNTIME BUDGET] cancelled before candidate build")
        return 0
    try:
        candidates = iter_tonosama_candidate_rows()
    except Exception:
        logger.exception("[TONOSAMA RUNTIME BUDGET] iter_tonosama_candidate_rows failed")
        return 0

    candidate_elapsed = time.perf_counter() - build_started
    logger.warning(
        "[TONOSAMA RUNTIME BUDGET] phase candidate_build=%.2fs rows=%s",
        candidate_elapsed,
        0 if candidates is None else (len(candidates) if hasattr(candidates, "__len__") else -1),
    )
    if _wg is not None and _wg.check(label="tonosama_after_candidates"):
        logger.warning(
            "[TONOSAMA RUNTIME BUDGET] cancelled after candidate build candidate_elapsed=%.2fs",
            candidate_elapsed,
        )
        return 0
    try:
        empty = bool(candidates is None or candidates.empty)
    except Exception:
        empty = True
    if empty:
        logger.info("[TONOSAMA RUNTIME BUDGET] build done candidates=0 registered=0 candidate_elapsed=%.3fs", candidate_elapsed)
        return 0

    try:
        total_candidates = int(len(candidates))
        candidates = _prioritize_candidates(candidates).head(max_eval).reset_index(drop=True)
    except Exception:
        total_candidates = 0
        try:
            candidates = candidates.head(max_eval).reset_index(drop=True)
        except Exception:
            pass

    eval_started = time.perf_counter()
    deadline = eval_started + budget_sec
    registered = 0
    ai_ng = 0
    duplicate = 0
    low_score = 0
    no_symbol = 0
    evaluated = 0
    time_budget_stop = False
    final_low_samples: list[dict[str, Any]] = []
    add_fail_samples: list[dict[str, Any]] = []

    def _remaining() -> float:
        return deadline - time.perf_counter()

    def _log_done() -> None:
        logger.info(
            "[TONOSAMA RUNTIME BUDGET] build done total_candidates=%s candidates=%s evaluated=%s evaluated_max=%s registered=%s duplicate=%s ai_ng=%s low_score=%s no_symbol=%s stopped=%s budget_sec=%.1f raw_budget=%.1f max_eval=%s raw_max_eval=%s candidate_elapsed=%.3fs eval_elapsed=%.3fs total_elapsed=%.3fs return_after_first=%s low_score_samples=%s add_fail_samples=%s",
            total_candidates, len(candidates), evaluated, max_eval, registered, duplicate, ai_ng, low_score, no_symbol, time_budget_stop,
            budget_sec, raw_budget, max_eval, raw_max_eval, candidate_elapsed, time.perf_counter() - eval_started, time.perf_counter() - build_started,
            return_after_first, final_low_samples[:10], add_fail_samples[:10],
        )

    for idx, row in candidates.iterrows():
        if _wg is not None and _wg.check(label="tonosama_candidate_loop"):
            time_budget_stop = True
            logger.warning(
                "[TONOSAMA RUNTIME BUDGET] cancelled in candidate loop "
                "eval_elapsed=%.2fs registered=%s evaluated=%s",
                time.perf_counter() - eval_started, registered, evaluated,
            )
            break
        now = time.perf_counter()
        # registered=0 の間は、時間切れだけでは止めない。
        if now >= deadline and registered > 0:
            time_budget_stop = True
            logger.warning(
                "[TONOSAMA RUNTIME BUDGET] stop before candidate budget_sec=%.1f raw_budget=%.1f eval_elapsed=%.3fs registered=%s idx=%s evaluated_limit=%s total_candidates=%s",
                budget_sec, raw_budget, now - eval_started, registered, idx, max_eval, total_candidates,
            )
            break

        if registered >= MAX_PENDING_PER_LOOP:
            break

        symbol = normalize_symbol(row.get("symbol"))
        if not symbol:
            no_symbol += 1
            continue
        evaluated += 1

        try:
            if has_tonosama_pending(symbol):
                duplicate += 1
                continue
        except Exception:
            logger.debug("[TONOSAMA RUNTIME BUDGET] has_pending failed symbol=%s", symbol, exc_info=True)

        raw_score = safe_float(row.get("_tonosama_score"), 0.0)
        if raw_score <= 0:
            low_score += 1
            final_low_samples.append({"symbol": symbol, "reason": "raw_score_le_zero", "raw_score": raw_score})
            continue

        if registered > 0 and _remaining() <= min_remain:
            time_budget_stop = True
            logger.warning(
                "[TONOSAMA RUNTIME BUDGET] stop before AI due reserve symbol=%s remaining=%.3fs registered=%s min_remain=%.3fs",
                symbol, _remaining(), registered, min_remain,
            )
            break

        try:
            ai_ok, ai_prob, ai_reason = ai_check_tonosama_entry(row)
        except Exception:
            ai_ng += 1
            logger.warning("[TONOSAMA RUNTIME BUDGET] ai_check failed symbol=%s", symbol, exc_info=True)
            continue

        if not ai_ok:
            ai_ng += 1
            logger.info(
                "[TONOSAMA ENTRY AI NG] symbol=%s prob=%.3f reason=%s surge=%.2f price_chg=%.2f body=%.3f range=%.3f close_pos=%.1f upper_wick=%.1f lower_wick=%.1f vol=%.0f 5s=%.3f slope=%.6f",
                symbol, ai_prob, ai_reason,
                safe_float(row.get("_max_volume_surge_ratio"), 0.0),
                safe_float(row.get("_max_price_change_pct"), 0.0),
                safe_float(row.get("_body_change_pct"), 0.0),
                safe_float(row.get("_intrabar_range_pct"), 0.0),
                safe_float(row.get("_close_position_pct"), 50.0),
                safe_float(row.get("_upper_wick_pct"), 0.0),
                safe_float(row.get("_lower_wick_pct"), 0.0),
                safe_float(row.get("_latest_volume"), 0.0),
                safe_float(row.get("price_change_5s_pct"), 0.0),
                safe_float(row.get("_slope"), 0.0),
            )
            continue

        try:
            final_score = calc_final_score_safe(row, raw_score=raw_score, ai_prob=ai_prob)
        except Exception:
            logger.warning("[TONOSAMA RUNTIME BUDGET] final_score failed symbol=%s", symbol, exc_info=True)
            low_score += 1
            continue

        if final_score < MIN_FINAL_SCORE:
            low_score += 1
            final_low_samples.append({
                "symbol": symbol,
                "reason": "final_score_low",
                "final_score": round(final_score, 4),
                "min_final_score": MIN_FINAL_SCORE,
                "raw_score": round(raw_score, 4),
                "ai_prob": round(ai_prob, 4),
            })
            continue

        try:
            entry = build_pending_entry(row, final_score=final_score, ai_prob=ai_prob, ai_reason=ai_reason)
            ok = bool(add_tonosama_pending(entry))
            if ok:
                registered += 1
                logger.info(
                    "🔥 TONOSAMA PENDING %s score=%.2f price=%.1f vol=%.0f body=%.3f%% range=%.3f%% close_pos=%.1f%% upper_wick=%.1f%% lower_wick=%.1f%% surge=%.2fx price_chg=%.2f%% tf=%s 5s=%.3f%% slope=%.6f ai_prob=%.3f",
                    symbol, final_score,
                    safe_float(row.get("close"), 0.0),
                    safe_float(row.get("_latest_volume"), 0.0),
                    safe_float(row.get("_body_change_pct"), 0.0),
                    safe_float(row.get("_intrabar_range_pct"), 0.0),
                    safe_float(row.get("_close_position_pct"), 50.0),
                    safe_float(row.get("_upper_wick_pct"), 0.0),
                    safe_float(row.get("_lower_wick_pct"), 0.0),
                    safe_float(row.get("_max_volume_surge_ratio"), 0.0),
                    safe_float(row.get("_max_price_change_pct"), 0.0),
                    str(row.get("_surge_tf", "")),
                    safe_float(row.get("price_change_5s_pct"), 0.0),
                    safe_float(row.get("_slope"), 0.0),
                    ai_prob,
                )
                try:
                    notify_discord_tonosama_pending(entry)
                except Exception:
                    logger.warning("[TONOSAMA RUNTIME BUDGET] notify failed symbol=%s", symbol, exc_info=True)
                if return_after_first:
                    time_budget_stop = True
                    logger.warning(
                        "[TONOSAMA RUNTIME BUDGET] return after first pending symbol=%s registered=%s evaluated=%s elapsed=%.3fs",
                        symbol, registered, evaluated, time.perf_counter() - build_started,
                    )
                    break
            else:
                add_fail_samples.append({"symbol": symbol, "side": entry.get("side"), "score": round(final_score, 4)})
        except Exception:
            add_fail_samples.append({"symbol": symbol, "reason": "exception"})
            logger.warning("[TONOSAMA RUNTIME BUDGET] add failed symbol=%s", symbol, exc_info=True)
            continue

    _log_done()
    return registered


def _tono_worker_guard():
    try:
        from trading.entry_exit import worker_guard
        return worker_guard
    except Exception:
        return None


def tonosama_loop() -> int:
    global _last_loop_at
    _last_loop_at = dt.datetime.now(); started = time.perf_counter()
    try:
        logger.info("[TONOSAMA LOOP] start at=%s", _last_loop_at.strftime("%Y-%m-%d %H:%M:%S"))
        if not is_market_time():
            logger.info("[TONOSAMA ENTRY] market closed skip")
            return 0
        pruned = prune_expired_tonosama_pending(reason="TONOSAMA_LOOP_START_EXPIRED")
        if pruned:
            logger.warning("[TONOSAMA LOOP] expired pending pruned at start removed=%s", pruned)
        if callable(update_active_symbols):
            try:
                update_active_symbols()
            except Exception:
                logger.warning("[TONOSAMA ENTRY] update_active_symbols skipped/failed", exc_info=True)
        _wg = _tono_worker_guard()
        if _wg is not None and _wg.check(label="tonosama_loop_start"):
            logger.warning(
                "[TONOSAMA LOOP] cancelled before build elapsed=%.3fs",
                time.perf_counter() - started,
            )
            return 0
        registered = build_tonosama_entries()
        logger.info("[TONOSAMA LOOP] done registered=%s elapsed=%.3fs", registered, time.perf_counter() - started)
        return registered
    except Exception:
        logger.exception("[TONOSAMA ENTRY] tonosama_loop failed")
        return 0


__all__ = ["tonosama_loop", "build_tonosama_entries", "iter_tonosama_candidate_rows", "build_feature_df_with_5sec"]
