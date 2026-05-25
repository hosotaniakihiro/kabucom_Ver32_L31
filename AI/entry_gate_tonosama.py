# ============================================================
# File: AI/entry_gate_tonosama.py
# Version: PRODUCTION-STABLE-V3-MODEL-MISSING-HEURISTIC-FALLBACK
# ------------------------------------------------------------
# 殿様イナゴ（BUY）専用 ENTRY ゲート
#
# ✔ LightGBM 60秒分類モデルを使用
# ✔ feature_row dict / symbol str の両方を受け付ける
# ✔ entry_controller が allow_tonosama_entry(symbol) と呼ぶ既存実装に対応
# ✔ pending_entries の TONOSAMA entry から最低限の特徴量を復元
# ✔ モデルが存在しない場合は、短期スキャルピング用の保守的 heuristic fallback で判定
# ✔ fallback は環境変数 TONOSAMA_MODEL_MISSING_FAIL_OPEN で制御可能
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import math
import os
from typing import Any, Dict

import lightgbm as lgb
import numpy as np

logger = logging.getLogger(__name__)


# ============================================================
# 設定
# ============================================================

MODEL_PATH = os.environ.get("TONOSAMA_MODEL_PATH", "tonosama_lgbm.txt")

FEATURES = [
    "price_velocity",
    "volume_speed",
    "rank_jump",
    "rank_strength",
    "dominant_ratio",
    "spread_ratio",
    "minute_from_open",
]

MIN_PROB = float(os.getenv("TONOSAMA_MIN_PROB", "0.55"))
MIN_SCORE = float(os.getenv("TONOSAMA_MIN_SCORE", "1.20"))
MAX_SPREAD_RATIO = float(os.getenv("TONOSAMA_MAX_SPREAD_RATIO", "0.003"))

# モデルファイルがない場合の運用。
# 0: 従来どおり fail-closed
# 1: heuristic fallback で最低限判定して通す
MODEL_MISSING_FAIL_OPEN = str(os.getenv("TONOSAMA_MODEL_MISSING_FAIL_OPEN", "1")).strip().lower() not in {"0", "false", "no", "off", "ng"}

# fallback 用の保守的な基準。
# TONOSAMA pending 側で既に volume surge / price change / 5秒足を確認している前提。
FALLBACK_MIN_PRICE_VELOCITY = float(os.getenv("TONOSAMA_FALLBACK_MIN_PRICE_VELOCITY", "-0.003"))
FALLBACK_MIN_VOLUME_SPEED = float(os.getenv("TONOSAMA_FALLBACK_MIN_VOLUME_SPEED", "1.0"))
FALLBACK_MIN_DOMINANT_RATIO = float(os.getenv("TONOSAMA_FALLBACK_MIN_DOMINANT_RATIO", "0.80"))
FALLBACK_MIN_RANK_STRENGTH = float(os.getenv("TONOSAMA_FALLBACK_MIN_RANK_STRENGTH", "0.0"))

_model: lgb.Booster | None = None
_model_missing_logged = False


# ============================================================
# helpers
# ============================================================

def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        if isinstance(v, float) and math.isnan(v):
            return default
        if isinstance(v, str) and v.strip() == "":
            return default
        x = float(v)
        return x if math.isfinite(x) else default
    except Exception:
        return default


def _safe_str(v: Any, default: str = "") -> str:
    try:
        if v is None:
            return default
        return str(v).strip()
    except Exception:
        return default


def _minute_from_open(now: dt.datetime | None = None) -> int:
    now = now or dt.datetime.now()
    open_dt = now.replace(hour=9, minute=0, second=0, microsecond=0)
    return max(int((now - open_dt).total_seconds() // 60), 0)


def _normalize_symbol(v: Any) -> str:
    return _safe_str(v).upper().replace(".T", "").replace(".0", "")


def _first(row: Dict[str, Any], keys: tuple[str, ...], default: Any = None) -> Any:
    for k in keys:
        try:
            if k in row and row.get(k) is not None and str(row.get(k)).strip() != "":
                return row.get(k)
        except Exception:
            pass
    return default


def _get_pending_entry_for_symbol(symbol: str) -> Dict[str, Any]:
    try:
        from global_state import global_data

        sym = _normalize_symbol(symbol)
        root = getattr(global_data, "pending_entries", {}) or {}
        bucket = root.get(sym) or root.get(str(symbol)) or []
        if isinstance(bucket, dict):
            bucket = [bucket]
        if not isinstance(bucket, list):
            return {}

        # TONOSAMA を優先
        for e in bucket:
            if not isinstance(e, dict):
                continue
            src = _safe_str(e.get("source")).upper()
            typ = _safe_str(e.get("entry_type")).upper()
            side = _safe_str(e.get("side") or e.get("entry_decision")).upper()
            if side == "BUY" and (src == "TONOSAMA" or typ == "TONOSAMA"):
                return dict(e)

        for e in bucket:
            if isinstance(e, dict):
                return dict(e)
    except Exception:
        logger.debug("[TONOSAMA BUY] pending lookup failed symbol=%s", symbol, exc_info=True)
    return {}


def _entry_conditions(row: Dict[str, Any]) -> Dict[str, Any]:
    try:
        ec = row.get("entry_conditions")
        if isinstance(ec, dict):
            return ec
    except Exception:
        pass
    return {}


def _feature_from_entry_row(row: Dict[str, Any]) -> Dict[str, float]:
    if not isinstance(row, dict) or not row:
        return {}

    ec = _entry_conditions(row)

    # すでに学習特徴量が全部ある場合はそのまま使う
    if all(f in row for f in FEATURES):
        out = {f: _safe_float(row.get(f), 0.0) for f in FEATURES}
        out["symbol"] = _normalize_symbol(row.get("symbol"))  # type: ignore[assignment]
        return out

    close = _safe_float(_first(row, ("close", "close_price", "current_price", "price"), 0.0), 0.0)
    rank_now = _safe_float(_first(row, ("rank_now", "rank", "rank_position", "ranking_rank"), 0.0), 0.0)
    rank_prev = _safe_float(_first(row, ("rank_prev", "prev_rank", "rank_previous"), 0.0), 0.0)

    # 価格速度は 5秒足変化率 → slope 系 → price_velocity の順に使う。
    price_change_5s_pct = _safe_float(_first(ec, ("price_change_5s_pct",), None), None)  # type: ignore[arg-type]
    if price_change_5s_pct is None:
        price_change_5s_pct = _safe_float(_first(row, ("price_change_5s_pct",), 0.0), 0.0)

    price_velocity = price_change_5s_pct / 100.0 if price_change_5s_pct is not None else 0.0
    if abs(price_velocity) <= 1.0e-12:
        price_velocity = _safe_float(
            _first(row, ("price_velocity", "slope", "slope_atr_scaled", "score_slope"), _first(ec, ("slope",), 0.0)),
            0.0,
        )
    if abs(price_velocity) > 1.0:
        # score_slope 等が 5.0 のようなスコア値なら比率へ縮小
        price_velocity = price_velocity / 100.0

    # volume_speed は 5秒足比率 → entry_conditions の急増比率 → row のvolume系の順。
    volume_speed = _safe_float(
        _first(
            row,
            ("volume_speed", "volume_ratio", "volume_surge_ratio_5s", "出来高速度"),
            _first(ec, ("volume_surge_ratio_5s", "max_volume_surge_ratio", "volume_surge_ratio_3m", "volume_surge_ratio_5m"), 1.0),
        ),
        1.0,
    )
    if volume_speed <= 0:
        volume_speed = _safe_float(_first(ec, ("max_volume_surge_ratio",), 1.0), 1.0)
    if volume_speed <= 0:
        volume_speed = 1.0

    if rank_now > 0 and rank_prev > 0:
        rank_jump = rank_prev - rank_now
    else:
        rank_jump = _safe_float(row.get("rank_jump"), 0.0)

    if rank_now > 0:
        rank_strength = 1.0 / max(rank_now, 1.0)
    else:
        rank_strength = _safe_float(row.get("rank_strength"), 0.0)

    dominant_ratio = _safe_float(_first(row, ("dominant_ratio", "buy_pressure", "board_dominant_ratio"), 1.0), 1.0)
    if dominant_ratio <= 0:
        dominant_ratio = 1.0

    spread_ratio = _safe_float(_first(row, ("spread_ratio", "board_spread_ratio"), 0.0), 0.0)
    spread = _safe_float(_first(row, ("spread", "board_spread"), 0.0), 0.0)
    if spread_ratio <= 0 and spread > 0 and close > 0:
        spread_ratio = spread / close

    out = {
        "price_velocity": float(price_velocity),
        "volume_speed": float(volume_speed),
        "rank_jump": float(rank_jump),
        "rank_strength": float(rank_strength),
        "dominant_ratio": float(dominant_ratio),
        "spread_ratio": float(spread_ratio),
        "minute_from_open": float(_safe_float(row.get("minute_from_open"), _minute_from_open())),
    }
    out["symbol"] = _normalize_symbol(row.get("symbol"))  # type: ignore[assignment]
    return out


def _resolve_feature_row(feature_or_symbol: Any) -> Dict[str, float]:
    if isinstance(feature_or_symbol, dict):
        return _feature_from_entry_row(feature_or_symbol)

    symbol = _normalize_symbol(feature_or_symbol)
    if not symbol:
        return {}

    pending = _get_pending_entry_for_symbol(symbol)
    if pending:
        return _feature_from_entry_row(pending)

    logger.info("[TONOSAMA BUY] feature row unavailable symbol=%s", symbol)
    return {}


# ============================================================
# model / fallback
# ============================================================

def _load_model() -> lgb.Booster:
    global _model
    if _model is None:
        if not os.path.exists(MODEL_PATH):
            raise FileNotFoundError(f"model not found: {MODEL_PATH}")
        _model = lgb.Booster(model_file=MODEL_PATH)
    return _model


def _heuristic_fallback(row: Dict[str, float], *, reason: str) -> bool:
    """
    モデルがない時の最低限判定。
    TONOSAMA runner 側ですでに候補化済みなので、ここでは
    強すぎる逆方向・スプレッド過大だけを止める。
    """
    price_velocity = _safe_float(row.get("price_velocity"), 0.0)
    volume_speed = _safe_float(row.get("volume_speed"), 1.0)
    dominant_ratio = _safe_float(row.get("dominant_ratio"), 1.0)
    spread_ratio = _safe_float(row.get("spread_ratio"), 0.0)
    rank_strength = _safe_float(row.get("rank_strength"), 0.0)

    ok = bool(
        MODEL_MISSING_FAIL_OPEN
        and price_velocity >= FALLBACK_MIN_PRICE_VELOCITY
        and volume_speed >= FALLBACK_MIN_VOLUME_SPEED
        and dominant_ratio >= FALLBACK_MIN_DOMINANT_RATIO
        and spread_ratio <= MAX_SPREAD_RATIO
        and rank_strength >= FALLBACK_MIN_RANK_STRENGTH
    )

    logger.warning(
        "[TONOSAMA BUY FALLBACK] ok=%s reason=%s model_path=%s fail_open=%s price_velocity=%.6f volume_speed=%.4f dominant_ratio=%.4f spread_ratio=%.6f rank_strength=%.6f thresholds={pv>=%.6f,vol>=%.4f,dom>=%.4f,spread<=%.6f,rank>=%.6f} symbol=%s",
        ok,
        reason,
        MODEL_PATH,
        MODEL_MISSING_FAIL_OPEN,
        price_velocity,
        volume_speed,
        dominant_ratio,
        spread_ratio,
        rank_strength,
        FALLBACK_MIN_PRICE_VELOCITY,
        FALLBACK_MIN_VOLUME_SPEED,
        FALLBACK_MIN_DOMINANT_RATIO,
        MAX_SPREAD_RATIO,
        FALLBACK_MIN_RANK_STRENGTH,
        row.get("symbol", ""),
    )
    return ok


# ============================================================
# public API
# ============================================================

def allow_tonosama_entry(feature_row: Dict[str, float] | str) -> bool:
    """
    殿様イナゴ BUY の ENTRY 可否を判定する。

    互換性:
      - allow_tonosama_entry(feature_row_dict)
      - allow_tonosama_entry(symbol_str)

    symbol_str の場合は pending_entries から TONOSAMA entry を探し、
    entry_row に含まれる slope / volume_speed / rank / dominant_ratio などから
    推論特徴量を復元する。
    """
    global _model_missing_logged

    row = _resolve_feature_row(feature_row)
    if not row:
        return False

    for f in FEATURES:
        if f not in row:
            logger.info("[TONOSAMA BUY] missing feature=%s row=%s", f, row)
            return False

    try:
        x = np.array([[float(row[f]) for f in FEATURES]], dtype=float)
        model = _load_model()
        prob = float(model.predict(x)[0])

        volume_speed = _safe_float(row.get("volume_speed"), 0.0)
        dominant_ratio = _safe_float(row.get("dominant_ratio"), 0.0)
        spread_ratio = _safe_float(row.get("spread_ratio"), 0.0)
        score = prob * volume_speed * dominant_ratio

        ok = bool(prob >= MIN_PROB and score >= MIN_SCORE and spread_ratio <= MAX_SPREAD_RATIO)
        logger.info(
            "[TONOSAMA BUY] gate symbol=%s ok=%s prob=%.4f score=%.4f volume_speed=%.4f dominant_ratio=%.4f spread_ratio=%.6f",
            row.get("symbol", ""),
            ok,
            prob,
            score,
            volume_speed,
            dominant_ratio,
            spread_ratio,
        )
        return ok

    except FileNotFoundError as e:
        if not _model_missing_logged:
            logger.warning("[TONOSAMA BUY] model missing -> heuristic fallback enabled=%s err=%s", MODEL_MISSING_FAIL_OPEN, e)
            _model_missing_logged = True
        return _heuristic_fallback(row, reason="MODEL_MISSING")

    except Exception as e:
        logger.exception("[TONOSAMA BUY] gate failed row=%s", row)
        # 想定外エラーは基本fail-closed。ただし明示的にfail_openが有効なら保守的fallback。
        return _heuristic_fallback(row, reason=f"MODEL_ERROR:{type(e).__name__}")


__all__ = ["allow_tonosama_entry"]
