# ============================================================
# File   : AI/entry_gate.py
# Version: Ver26.32-FINAL-ENTRY-GATE-SUMMARY-MTF-UNREADY-FAILOPEN
# ------------------------------------------------------------
# ✔ ENTRY 最終ゲート（唯一の判断場所）
# ✔ 副作用ゼロ（pending_entries を絶対に触らない）
# ✔ 戻り値は AI_RESULT 固定スキーマ
# ✔ score → 構造 → 流動性 → RANKING → MTF → 即益 → 勢い
# ✔ → confidence → 3m/5m補正 → bias → lot
# ✔ SUMMARY / RANKING / EARLY_SCALP を source で完全分離
# ✔ ranking_score_direct を hard gate + final_score に直結
# ✔ EARLY_SCALP は AI を BLOCK 専用で使用
# ✔ None / NaN / 未供給フィールド完全防御
# ✔ SUMMARYのscore_low閾値を候補生成側 min_buy=4.0 と整合
# ✔ scoreをint丸めせず小数のまま判定（4.39を4に落とさない）
# ✔ SUMMARY_AI pending の score_buy/score_sell 欠落を score から復元
# ✔ SUMMARY_AI pending の dominant_ratio 欠落は 1.0 として fail-open
# ✔ SUMMARY SELL は BUY と別閾値 MIN_ENTRY_SCORE_SELL_SUMMARY で判定
# ✔ SUMMARY 3m/5m の technical_ready=False / hist不足時は MTF hard block しない
# ============================================================

import logging
import math
import datetime as dt
import os

from AI.predict_mtf import predict_mtf
from AI.train.entry.entry_immediate_profit import predict_immediate_profit
from AI.inference.ranking_entry_predictor import (
    predict_entry as predict_ranking_entry,
)

# ------------------------------------------------------------
# ranking score direct
# ------------------------------------------------------------
from AI.features.ranking_score_direct import build_ranking_direct_score

# ------------------------------------------------------------
# confidence bias（実損益ベース）
# ------------------------------------------------------------
try:
    from AI.confidence.confidence_bias import apply_confidence_bias
except Exception:
    apply_confidence_bias = None

# ------------------------------------------------------------
# EARLY SCALP config
# ------------------------------------------------------------
try:
    from config.early_scalp_config import EARLY_SCALP_CONFIG
except Exception:
    EARLY_SCALP_CONFIG = None

from config import global_config
from global_state import global_data

logger = logging.getLogger(__name__)
_WARNED_MISSING_RANKING_FIELDS = set()


# ============================================================
# helpers
# ============================================================

def _cfg(key: str, default):
    try:
        return global_config.get(key, default)
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == "":
            return float(default)
        x = float(v)
        return x if math.isfinite(x) else float(default)
    except Exception:
        return float(default)


def _cfg_float(key: str, default: float, *, env_name: str | None = None) -> float:
    """
    global_config と環境変数の両対応。
    環境変数があれば環境変数を優先する。
    """
    if env_name:
        v = os.environ.get(env_name)
        if v is not None and str(v).strip() != "":
            return _env_float(env_name, default)
    try:
        x = float(_cfg(key, default))
        return x if math.isfinite(x) else float(default)
    except Exception:
        return float(default)


def _safe_float(v, default: float = 0.0) -> float:
    try:
        v = float(v)
        return v if math.isfinite(v) else default
    except Exception:
        return default


def _norm_side(v):
    if v is None:
        return None
    try:
        s = str(v).upper()
    except Exception:
        return None
    return s if s in ("BUY", "SELL") else None


def _norm_text(v) -> str:
    try:
        if v is None:
            return ""
        return str(v).strip().upper()
    except Exception:
        return ""


def _is_market_open(now: dt.datetime | None = None) -> bool:
    now = now or dt.datetime.now()
    t = now.time()
    return (
        (dt.time(9, 0) <= t <= dt.time(11, 30)) or
        (dt.time(12, 30) <= t <= dt.time(15, 30))
    )


def _warn_missing_ranking_field(symbol: str, field: str, row: dict):
    key = (symbol, field)
    if key in _WARNED_MISSING_RANKING_FIELDS:
        return
    _WARNED_MISSING_RANKING_FIELDS.add(key)
    logger.warning(
        "[RANKING FEATURE MISSING] symbol=%s field=%s source=%s keys=%s",
        symbol,
        field,
        row.get("source"),
        sorted(row.keys()),
    )


def _coalesce_float(row: dict, keys: tuple[str, ...], default: float = 0.0) -> float:
    for key in keys:
        try:
            if key in row and row.get(key) not in (None, ""):
                v = _safe_float(row.get(key), default)
                if v != default or str(row.get(key)).strip() in ("0", "0.0"):
                    return v
        except Exception:
            pass
    return default


def _bool_like(v, default: bool = False) -> bool:
    try:
        if isinstance(v, bool):
            return v
        if v is None:
            return default
        s = str(v).strip().lower()
        if s in ("1", "true", "yes", "y", "on", "ok"):
            return True
        if s in ("0", "false", "no", "n", "off", "ng", ""):
            return False
        return default
    except Exception:
        return default


def _is_summary_ai_pending(row: dict, source: str) -> bool:
    entry_type = _norm_text(row.get("entry_type") or row.get("entryType"))
    raw_source = _norm_text(row.get("source"))
    return source == "SUMMARY" and (entry_type == "SUMMARY_AI" or raw_source == "SUMMARY")


def _resolve_summary_scores(row: dict, *, source: str, decision: str | None) -> tuple[float, float, float, float]:
    """
    SUMMARY_AI pending では build_entry_row 後に buy_score/sell_score が欠け、
    score だけが -6.0 のように残る場合がある。
    その場合、SELL は abs(score) を sell_score として復元する。
    """
    buy_score = _coalesce_float(row, ("buy_score", "score_buy"), 0.0)
    sell_score = _coalesce_float(row, ("sell_score", "score_sell"), 0.0)
    total_score = _coalesce_float(row, ("score_total", "total_score"), 0.0)
    raw_score = _coalesce_float(row, ("score", "final_score", "display_score"), 0.0)

    if source == "SUMMARY":
        if decision == "BUY":
            if buy_score <= 0 and raw_score > 0:
                buy_score = raw_score
        elif decision == "SELL":
            if sell_score <= 0:
                if raw_score < 0:
                    sell_score = abs(raw_score)
                elif raw_score > 0:
                    sell_score = raw_score

        if total_score <= 0:
            total_score = max(buy_score, sell_score, abs(raw_score))

    return buy_score, sell_score, total_score, raw_score


def _resolve_min_score_for_side(*, source: str, is_ranking: bool, decision: str | None) -> float:
    """
    BUY と SELL で最終ゲートの score 閾値を分ける。

    SUMMARY AI runner は SELL 側を max_sell=2.00 で候補化しているため、
    entry_gate 側で BUY と同じ 3〜4点を必須にすると SELL が全滅する。
    既定値:
      - RANKING: 2.0
      - SUMMARY BUY : 4.0
      - SUMMARY SELL: 1.0
    必要なら環境変数または global_config で上書き可能。
    """
    if is_ranking:
        if decision == "SELL":
            return _cfg_float(
                "MIN_ENTRY_SCORE_SELL_RANKING",
                _cfg_float("MIN_ENTRY_SCORE_RANKING", 2.0, env_name="MIN_ENTRY_SCORE_RANKING"),
                env_name="MIN_ENTRY_SCORE_SELL_RANKING",
            )
        if decision == "BUY":
            return _cfg_float(
                "MIN_ENTRY_SCORE_BUY_RANKING",
                _cfg_float("MIN_ENTRY_SCORE_RANKING", 2.0, env_name="MIN_ENTRY_SCORE_RANKING"),
                env_name="MIN_ENTRY_SCORE_BUY_RANKING",
            )
        return _cfg_float("MIN_ENTRY_SCORE_RANKING", 2.0, env_name="MIN_ENTRY_SCORE_RANKING")

    if source == "SUMMARY" and decision == "SELL":
        return _cfg_float(
            "MIN_ENTRY_SCORE_SELL_SUMMARY",
            _cfg_float("MIN_ENTRY_SCORE_SELL", 1.0, env_name="MIN_ENTRY_SCORE_SELL"),
            env_name="MIN_ENTRY_SCORE_SELL_SUMMARY",
        )

    if source == "SUMMARY" and decision == "BUY":
        return _cfg_float(
            "MIN_ENTRY_SCORE_BUY_SUMMARY",
            _cfg_float("MIN_ENTRY_SCORE", 4.0, env_name="MIN_ENTRY_SCORE"),
            env_name="MIN_ENTRY_SCORE_BUY_SUMMARY",
        )

    return _cfg_float("MIN_ENTRY_SCORE", 4.0, env_name="MIN_ENTRY_SCORE")


def _should_fail_open_summary_mtf(row: dict, *, source: str, interval: int) -> tuple[bool, str]:
    """
    SUMMARY 3m/5m で履歴不足・テクニカル未成熟の場合、MTFをhard blockにしない。

    最新ログでは ready_rows=0 / technical_ready=False / macd=0 / slope=0 / hist_len=1 の状態で
    3m候補が全て mtf_low になっていた。これは「まだテクニカルが作れていない」状態であり、
    銘柄の方向性が否定されたわけではないため、score/流動性/信用売りガードへ後続判断を任せる。
    """
    if source != "SUMMARY":
        return False, "not_summary"

    if _env_float("SUMMARY_ENTRY_FORCE_MTF_CHECK", 0.0) > 0:
        return False, "force_mtf_check"

    if interval == 1:
        return True, "summary_1min_skip_mtf"

    technical_ready = _bool_like(row.get("technical_ready"), False)
    hist_len = _safe_float(row.get("symbol_hist_len"), 0.0)
    rsi = _safe_float(row.get("rsi"), 0.0)
    macd = _safe_float(row.get("macd"), 0.0)
    signal = _safe_float(row.get("signal"), 0.0)
    slope = _safe_float(row.get("slope"), 0.0)
    slope_atr = _safe_float(row.get("slope_atr_scaled"), 0.0)
    mtf_score = _safe_float(row.get("score_mtf") or row.get("mtf_score") or row.get("mtf"), 0.0)

    if not technical_ready:
        return True, "technical_not_ready"

    if hist_len and hist_len < 14:
        return True, f"hist_short:{hist_len:.0f}"

    if rsi in (0.0, 50.0) and macd == 0.0 and signal == 0.0 and slope == 0.0 and slope_atr == 0.0 and mtf_score == 0.0:
        return True, "indicators_flat_or_missing"

    return False, "mtf_available"


# ============================================================
# AI RESULT schema
# ============================================================

def _block(reason: str, confidence: float = 0.0, model_used: str = "NONE") -> dict:
    return {
        "allow": False,
        "confidence": float(confidence),
        "reason": str(reason),
        "model_used": model_used,
    }


def _allow(
    *,
    confidence: float,
    lot_multiplier: float,
    reason: str,
    model_used: str,
) -> dict:
    return {
        "allow": True,
        "confidence": float(confidence),
        "lot_multiplier": float(lot_multiplier),
        "reason": str(reason),
        "model_used": model_used,
    }


# ============================================================
# ranking momentum boost（confidence 専用）
# ============================================================

def _ranking_momentum_boost(row: dict) -> float:
    rsi = _safe_float(row.get("ranking_rsi"))
    rsi_prev = _safe_float(row.get("ranking_rsi_prev"), rsi)

    ma5 = _safe_float(row.get("ranking_ma5"))
    ma25 = _safe_float(row.get("ranking_ma25"))

    rsi_level = 1.10 if rsi >= 70 else 1.05 if rsi >= 60 else 1.00 if rsi >= 50 else 0.90
    rsi_delta = rsi - rsi_prev
    rsi_slope = 1.05 if rsi_delta >= 2.0 else 0.95 if rsi_delta <= -2.0 else 1.00

    if ma5 > 0 and ma25 > 0:
        ma_trend = 1.05 if ma5 >= ma25 else 0.95
        ma_diff_boost = min(1.10, 1.0 + abs(ma5 - ma25) / max(ma25, 1e-6))
    else:
        ma_trend = ma_diff_boost = 1.00

    boost = rsi_level * rsi_slope * ma_trend * ma_diff_boost
    return max(0.80, min(1.30, boost))


# ============================================================
# EARLY SCALP 判定（独立・BLOCK専用）
# ============================================================

def _early_scalp_entry_ok(row: dict) -> tuple[bool, str]:
    if not EARLY_SCALP_CONFIG:
        return False, "early_cfg_missing"

    e = EARLY_SCALP_CONFIG["ENTRY"]

    volume_speed = _safe_float(row.get("volume_speed"))
    fast_return = _safe_float(row.get("fast_return"))
    price = _safe_float(row.get("close_price") or row.get("price"))
    vwap = _safe_float(row.get("vwap"))
    ma5 = _safe_float(row.get("ma5"))
    spread = _safe_float(row.get("spread"))

    if volume_speed < e["MIN_VOLUME_SPEED"]:
        return False, "es_volume_low"
    if fast_return < e["MIN_FAST_RETURN"]:
        return False, "es_fast_return_low"
    if e["REQUIRE_ABOVE_VWAP"] and price < vwap:
        return False, "es_below_vwap"
    if e["REQUIRE_ABOVE_MA5"] and price < ma5:
        return False, "es_below_ma5"
    if spread > e["MAX_SPREAD"]:
        return False, "es_spread_wide"

    return True, "early_scalp_ok"


def apply_mtf_boost(entry_row, mtf_summary):
    symbol = entry_row["symbol"]

    mtf_score = 0.0
    if symbol in mtf_summary:
        mtf_score = mtf_summary[symbol]["mtf_score"]

    # 攻撃型倍率
    boost = 1 + (mtf_score * 0.05)

    entry_row["final_score"] *= boost
    entry_row["mtf_boost"] = boost

    return entry_row


# ============================================================
# ENTRY FINAL GATE
# ============================================================

def ai_final_entry_check(row: dict) -> dict:

    if not isinstance(row, dict):
        return _block("invalid_row")

    symbol = str(row.get("symbol") or "")
    if not symbol:
        return _block("no_symbol")

    interval = int(row.get("interval") or 1)
    raw_source = str(row.get("source") or "SUMMARY").upper()

    # ========================================================
    # EARLY SCALP（最初に分岐）
    # ========================================================
    if raw_source == "EARLY_SCALP":

        ok, reason = _early_scalp_entry_ok(row)
        if not ok:
            return _block(reason, 0.0, "EARLY_SCALP_RULE")

        feats = getattr(global_data, "latest_features", {}).get(symbol)

        if feats:
            imm_p = _safe_float(predict_immediate_profit(feats), 1.0)
            if imm_p < 0.4:
                return _block("es_immediate_low", imm_p, "IMMEDIATE")

        return _allow(
            confidence=0.65,
            lot_multiplier=EARLY_SCALP_CONFIG["RISK"]["LOT_RATIO"],
            reason="early_scalp_entry",
            model_used="EARLY_SCALP",
        )

    # ========================================================
    # SUMMARY / RANKING
    # ========================================================
    source = "RANKING" if raw_source == "RANKING" else "SUMMARY"
    is_ranking = source == "RANKING"

    if is_ranking:
        MIN_TURNOVER = _cfg_float("MIN_TURNOVER_RANKING", 1_000_000, env_name="MIN_TURNOVER_RANKING")
        MIN_DOM = _cfg_float("MIN_DOMINANT_RATIO_RANKING", 0.0, env_name="MIN_DOMINANT_RATIO_RANKING")
        MIN_MTF = _cfg_float("MIN_MTF_CONFIDENCE_RANKING", 0.55, env_name="MIN_MTF_CONFIDENCE_RANKING")
        MIN_RANK_SCORE = _cfg_float("MIN_RANKING_DIRECT_SCORE", 0.15, env_name="MIN_RANKING_DIRECT_SCORE")
    else:
        MIN_TURNOVER = _cfg_float("MIN_TURNOVER_1M", 3_000_000, env_name="MIN_TURNOVER_1M")
        MIN_DOM = _cfg_float("MIN_DOMINANT_RATIO_SUMMARY", 0.58, env_name="MIN_DOMINANT_RATIO_SUMMARY")
        MIN_MTF = _cfg_float("MIN_MTF_CONFIDENCE", 0.55, env_name="MIN_MTF_CONFIDENCE")

    decision = _norm_side(row.get("entry_decision") or row.get("side"))
    MIN_SCORE = _resolve_min_score_for_side(source=source, is_ranking=is_ranking, decision=decision)

    # ========================================================
    # NaN完全防御 score取得
    #   - 旧実装は int() 化で 4.39 -> 4 に丸めていた。
    #   - SUMMARY_AI pending では score_buy/score_sell が欠けることがあるため、
    #     score / final_score / display_score から復元する。
    # ========================================================
    buy_score, sell_score, total_score, raw_score = _resolve_summary_scores(
        row,
        source=source,
        decision=decision,
    )

    score_total = (
        total_score
        if is_ranking
        else max(
            buy_score,
            sell_score,
            total_score,
            abs(raw_score),
        )
    )

    turnover = _safe_float(row.get("turnover"))
    dominant_ratio = _safe_float(row.get("dominant_ratio"))

    summary_ai_pending = _is_summary_ai_pending(row, source)
    if summary_ai_pending and dominant_ratio <= 0:
        dominant_ratio = 1.0
        logger.info(
            "[ENTRY GATE] SUMMARY_AI dominant_ratio fail-open symbol=%s side=%s score=%.4f buy=%.4f sell=%.4f min_score=%.4f",
            symbol,
            decision,
            score_total,
            buy_score,
            sell_score,
            MIN_SCORE,
        )

    if score_total < MIN_SCORE:
        logger.info(
            "[ENTRY GATE] block score_low symbol=%s source=%s interval=%s side=%s score=%.4f min_score=%.4f buy=%.4f sell=%.4f total=%.4f raw=%.4f entry_type=%s",
            symbol,
            source,
            interval,
            decision,
            score_total,
            MIN_SCORE,
            buy_score,
            sell_score,
            total_score,
            raw_score,
            row.get("entry_type"),
        )
        return _block(f"score_low:{score_total:.3f}<{MIN_SCORE:.3f}", score_total)

    if interval == 1 and _is_market_open() and turnover < MIN_TURNOVER:
        return _block("low_turnover", 0.0, "TURNOVER")

    if dominant_ratio < MIN_DOM:
        return _block("dominant_low", dominant_ratio)

    dominant_side = _norm_side(row.get("dominant_side"))

    if not decision:
        return _block("decision_none")

    if dominant_side and decision != dominant_side:
        return _block("direction_mismatch")

    # ========================================================
    # RANKING AI
    # ========================================================
    ranking_conf = 1.0

    if is_ranking:
        try:
            r = predict_ranking_entry(row)

            if r.get("action") and r.get("action") != decision:
                return _block(
                    "ranking_ai_mismatch",
                    r.get("confidence"),
                    "RANKING_LGBM",
                )

            ranking_conf = _safe_float(r.get("confidence"), 1.0)

        except Exception as e:
            logger.warning("[RANKING AI SKIP] %s: %s", symbol, e)

    # ========================================================
    # MTF AI
    # ========================================================
    mtf_conf = 1.0
    summary_mtf_fail_open, summary_mtf_reason = _should_fail_open_summary_mtf(row, source=source, interval=interval)

    if summary_mtf_fail_open:
        logger.info(
            "[ENTRY GATE] SUMMARY MTF fail-open symbol=%s interval=%s side=%s reason=%s score=%.4f buy=%.4f sell=%.4f",
            symbol,
            interval,
            decision,
            summary_mtf_reason,
            score_total,
            buy_score,
            sell_score,
        )
    else:
        pred = predict_mtf(
            symbol,
            row.get("close_price"),
            interval,
            row.get("datetime"),
        )

        mtf_conf = _safe_float(
            pred.get("prob_up" if decision == "BUY" else "prob_down")
        )

        if mtf_conf < MIN_MTF:
            return _block("mtf_low", mtf_conf, "MTF")

    # ========================================================
    # IMMEDIATE PROFIT AI
    # ========================================================
    feats = getattr(global_data, "latest_features", {}).get(symbol)

    immediate_p = (
        _safe_float(predict_immediate_profit(feats), 1.0)
        if feats
        else 1.0
    )

    # ========================================================
    # RANKING DIRECT SCORE
    # ========================================================
    ranking_score_final = 0.0

    if is_ranking:

        rank_feats = {
            "ranking_session_rank_ret":
                _safe_float(row.get("ranking_session_rank_ret")),

            "ranking_session_quality":
                _safe_float(row.get("ranking_session_quality"), 0.3),

            "ranking_mtf_alignment":
                _safe_float(row.get("ranking_mtf_alignment")),
        }

        pack = build_ranking_direct_score(rank_feats)

        ranking_score_final = _safe_float(
            pack.get("ranking_score_final")
        )

        if ranking_score_final < MIN_RANK_SCORE:
            return _block(
                "ranking_direct_low",
                ranking_score_final,
                "RANKING_DIRECT",
            )

    # ========================================================
    # momentum boost
    # ========================================================
    momentum_boost = _ranking_momentum_boost(row) if is_ranking else 1.0

    final_conf = (
        dominant_ratio
        * ranking_conf
        * mtf_conf
        * immediate_p
        * momentum_boost
    )

    # ========================================================
    # 3m / 5m confidence補正
    # ========================================================
    score_3m = _safe_float(row.get("score_3m"))
    score_5m = _safe_float(row.get("score_5m"))

    if score_3m >= 4.0:
        final_conf *= 1.10

    if score_5m >= 3.5:
        final_conf *= 1.10

    final_conf = min(final_conf, 1.50)

    # ========================================================
    # confidence bias
    # ========================================================
    if apply_confidence_bias:
        try:
            final_conf = apply_confidence_bias(
                symbol=symbol,
                confidence=final_conf,
            )
        except Exception:
            pass

    lot_multiplier = max(0.5, min(2.0, 0.5 + final_conf))

    return _allow(
        confidence=final_conf,
        lot_multiplier=lot_multiplier,
        reason=(
            f"rankScore={ranking_score_final:.3f}|"
            f"mtf={mtf_conf:.2f}|"
            f"rank={ranking_conf:.2f}|"
            f"imm={immediate_p:.2f}|"
            f"dom={dominant_ratio:.2f}|"
            f"boost={momentum_boost:.2f}|"
            f"3m={score_3m:.2f}|"
            f"5m={score_5m:.2f}|"
            f"src={source}|"
            f"minScore={MIN_SCORE:.2f}"
        ),
        model_used=("RANKING_LGBM+MTF" if is_ranking else "MTF"),
    )
