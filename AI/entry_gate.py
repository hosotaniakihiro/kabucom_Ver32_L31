# ============================================================
# File   : AI/entry_gate.py
# Version: Ver26.33-FINAL-ENTRY-GATE-SUMMARY-MTF-UNREADY-FAILCLOSED
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
# ✔ V26.33: SUMMARY 3m/5m technical_ready=False は fail-open せず block
# ============================================================

import logging
import math
import datetime as dt
import os
from typing import Any

from AI.predict_mtf import predict_mtf
from AI.train.entry.entry_immediate_profit import predict_immediate_profit
from AI.inference.ranking_entry_predictor import (
    predict_entry as predict_ranking_entry,
)

from AI.features.ranking_score_direct import build_ranking_direct_score

try:
    from AI.confidence.confidence_bias import apply_confidence_bias
except Exception:
    apply_confidence_bias = None

try:
    from config.early_scalp_config import EARLY_SCALP_CONFIG
except Exception:
    EARLY_SCALP_CONFIG = None

from config import global_config
from global_state import global_data

logger = logging.getLogger(__name__)
_WARNED_MISSING_RANKING_FIELDS = set()


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


def _env_bool(name: str, default: bool) -> bool:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on", "ok"}:
            return True
        if s in {"0", "false", "no", "n", "off", "ng"}:
            return False
        return bool(default)
    except Exception:
        return bool(default)


def _cfg_float(key: str, default: float, *, env_name: str | None = None) -> float:
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


def _summary_mtf_status_core(row: dict, *, source: str, interval: int) -> tuple[str, str]:
    """
    MTF判定前の状態確認。
    戻り値: (status, reason)
      status=skip  : MTFチェックを省略してよい
      status=block : テクニカル未準備としてブロック
      status=check : predict_mtfで確認
    """
    if source != "SUMMARY":
        return "check", "not_summary"

    if _env_float("SUMMARY_ENTRY_FORCE_MTF_CHECK", 0.0) > 0:
        return "check", "force_mtf_check"

    if interval == 1:
        return "skip", "summary_1min_skip_mtf"

    technical_ready = _bool_like(row.get("technical_ready"), False)
    display_ready = _bool_like(row.get("display_ready"), technical_ready)
    hist_len = _safe_float(row.get("symbol_hist_len"), 0.0)
    rsi = _safe_float(row.get("rsi"), 0.0)
    macd = _safe_float(row.get("macd"), 0.0)
    signal = _safe_float(row.get("signal"), 0.0)
    slope = _safe_float(row.get("slope"), 0.0)
    slope_atr = _safe_float(row.get("slope_atr_scaled"), 0.0)
    mtf_score = _safe_float(row.get("score_mtf") or row.get("mtf_score") or row.get("mtf"), 0.0)

    failopen_missing = _env_bool("SUMMARY_MTF_MISSING_FAIL_OPEN", False)

    if not technical_ready or not display_ready:
        return ("skip" if failopen_missing else "block"), "technical_not_ready"

    min_hist = int(_env_float("SUMMARY_MTF_MIN_HIST_LEN", 14.0))
    if hist_len and hist_len < min_hist:
        return ("skip" if failopen_missing else "block"), f"hist_short:{hist_len:.0f}<{min_hist}"

    if rsi in (0.0, 50.0) and macd == 0.0 and signal == 0.0 and slope == 0.0 and slope_atr == 0.0 and mtf_score == 0.0:
        return ("skip" if failopen_missing else "block"), "indicators_flat_or_missing"

    return "check", "mtf_available"


def _block(reason: str, confidence: float = 0.0, model_used: str = "NONE") -> dict:
    return {
        "allow": False,
        "confidence": float(confidence),
        "reason": str(reason),
        "model_used": model_used,
    }


ENTRY_SCORE_MAX = 1.50


def _allow(
    *,
    confidence: float,
    lot_multiplier: float,
    reason: str,
    model_used: str,
    ai_confidence: float | None = None,
    ai_inference: bool = False,
    confidence_source: str = "rule_entry_score",
) -> dict:
    """ENTRY 許可を返す。

    2026-09-02 の整理:
      confidence という 1 変数に
        dominant_ratio x ranking_conf x mtf_conf x immediate_p x momentum_boost
        (+ 3m/5m の x1.10 ボーナス)
      を全部掛けて clamp(1.50) していたため、AI 信頼度のつもりの値が
      1.100 のように 1.0 を超えていた。これは確率ではなく **ルール由来の
      複合エントリースコア**なので、名前と範囲を分けて持つ。

      entry_score    ルール複合スコア (0.0〜ENTRY_SCORE_MAX)
      ai_confidence  実 AI モデルが出した信頼度だけ (0.0〜1.0)。
                     モデルを使っていなければ None
      confidence     後方互換のため従来値のまま (判定は一切変えない)
    """
    conf = float(confidence)
    out = {
        "allow": True,
        "confidence": conf,
        "entry_score": conf,
        "entry_score_max": float(ENTRY_SCORE_MAX),
        "lot_multiplier": float(lot_multiplier),
        "reason": str(reason),
        "model_used": model_used,
        "ai_inference": bool(ai_inference),
        "confidence_source": str(confidence_source),
    }
    if ai_inference and ai_confidence is not None:
        # AI 信頼度は確率なので 0..1 を保証する
        out["ai_confidence"] = max(0.0, min(1.0, float(ai_confidence)))
    else:
        out["ai_confidence"] = None
    return out


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
    boost = 1 + (mtf_score * 0.05)
    entry_row["final_score"] *= boost
    entry_row["mtf_boost"] = boost
    return entry_row


def _ai_final_entry_check_core(row: dict) -> dict:
    if not isinstance(row, dict):
        return _block("invalid_row")

    symbol = str(row.get("symbol") or "")
    if not symbol:
        return _block("no_symbol")

    interval = int(row.get("interval") or 1)
    raw_source = str(row.get("source") or "SUMMARY").upper()

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

    buy_score, sell_score, total_score, raw_score = _resolve_summary_scores(row, source=source, decision=decision)
    score_total = total_score if is_ranking else max(buy_score, sell_score, total_score, abs(raw_score))

    turnover = _safe_float(row.get("turnover"))
    dominant_ratio = _safe_float(row.get("dominant_ratio"))

    summary_ai_pending = _is_summary_ai_pending(row, source)
    if summary_ai_pending and dominant_ratio <= 0:
        dominant_ratio = 1.0
        logger.info(
            "[ENTRY GATE] SUMMARY_AI dominant_ratio fail-open symbol=%s side=%s score=%.4f buy=%.4f sell=%.4f min_score=%.4f",
            symbol, decision, score_total, buy_score, sell_score, MIN_SCORE,
        )

    if score_total < MIN_SCORE:
        logger.info(
            "[ENTRY GATE] block score_low symbol=%s source=%s interval=%s side=%s score=%.4f min_score=%.4f buy=%.4f sell=%.4f total=%.4f raw=%.4f entry_type=%s",
            symbol, source, interval, decision, score_total, MIN_SCORE, buy_score, sell_score, total_score, raw_score, row.get("entry_type"),
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

    ranking_conf = 1.0
    ranking_ai_used = False
    if is_ranking:
        if _ranking_ai_model_available():
            try:
                r = predict_ranking_entry(row)
                if r.get("action") and r.get("action") != decision:
                    return _block("ranking_ai_mismatch", r.get("confidence"), "RANKING_LGBM")
                ranking_conf = _safe_float(r.get("confidence"), 1.0)
                ranking_ai_used = True
            except Exception as e:
                # 例外は握り潰さない。ただし銘柄ごとに毎回出すと
                # 30 分で 566 行になるので同一理由は抑制する。
                _log_ranking_ai_skip_once(symbol, e)
        else:
            # モデル不在。fail-open で通すが、AI が満点を出したのとは
            # 区別できるよう model_used に必ず残す。
            ranking_ai_used = False

    mtf_conf = 1.0
    mtf_status, mtf_reason = _summary_mtf_status(row, source=source, interval=interval)
    if mtf_status == "block":
        logger.info(
            "[ENTRY GATE] block SUMMARY MTF not ready symbol=%s interval=%s side=%s reason=%s score=%.4f buy=%.4f sell=%.4f technical_ready=%s hist=%s macd=%s signal=%s",
            symbol,
            interval,
            decision,
            mtf_reason,
            score_total,
            buy_score,
            sell_score,
            row.get("technical_ready"),
            row.get("symbol_hist_len"),
            row.get("macd"),
            row.get("signal"),
        )
        return _block(f"summary_mtf_not_ready:{mtf_reason}", 0.0, "MTF_READY")
    elif mtf_status == "skip":
        logger.info(
            "[ENTRY GATE] SUMMARY MTF check skipped symbol=%s interval=%s side=%s reason=%s score=%.4f buy=%.4f sell=%.4f",
            symbol, interval, decision, mtf_reason, score_total, buy_score, sell_score,
        )
    else:
        pred = predict_mtf(symbol, row.get("close_price"), interval, row.get("datetime"))
        mtf_conf = _safe_float(pred.get("prob_up" if decision == "BUY" else "prob_down"))
        if mtf_conf < MIN_MTF:
            return _block("mtf_low", mtf_conf, "MTF")

    feats = getattr(global_data, "latest_features", {}).get(symbol)
    immediate_p = _safe_float(predict_immediate_profit(feats), 1.0) if feats else 1.0

    ranking_score_final = 0.0
    if is_ranking:
        rank_feats = {
            "ranking_session_rank_ret": _safe_float(row.get("ranking_session_rank_ret")),
            "ranking_session_quality": _safe_float(row.get("ranking_session_quality"), 0.3),
            "ranking_mtf_alignment": _safe_float(row.get("ranking_mtf_alignment")),
        }
        pack = build_ranking_direct_score(rank_feats)
        ranking_score_final = _safe_float(pack.get("ranking_score_final"))
        if ranking_score_final < MIN_RANK_SCORE:
            return _block("ranking_direct_low", ranking_score_final, "RANKING_DIRECT")

    momentum_boost = _ranking_momentum_boost(row) if is_ranking else 1.0
    final_conf = dominant_ratio * ranking_conf * mtf_conf * immediate_p * momentum_boost

    # score_3m / score_5m は row.get() が欠損時に None -> 0.0 になるため、
    # 「3分足が弱い」と「3分足が attach されていない」が両方 3m=0.00 になって
    # 区別できなかった。row_adapter が付ける *_present で切り分ける。
    # 補正の適用条件 (>=4.0 / >=3.5) は一切変えていない。
    score_3m = _safe_float(row.get("score_3m"))
    score_5m = _safe_float(row.get("score_5m"))
    has_3m = bool(row.get("score_3m_present", "score_3m" in row))
    has_5m = bool(row.get("score_5m_present", "score_5m" in row))
    txt_3m = ("%.2f" % score_3m) if has_3m else "NA"
    txt_5m = ("%.2f" % score_5m) if has_5m else "NA"
    if score_3m >= 4.0:
        final_conf *= 1.10
    if score_5m >= 3.5:
        final_conf *= 1.10
    final_conf = min(final_conf, 1.50)

    if apply_confidence_bias:
        try:
            final_conf = apply_confidence_bias(symbol=symbol, confidence=final_conf)
        except Exception:
            pass

    # 2026-10-23 (ユーザー判断): 3 分足も 5 分足も付いていない候補は止める (ENTRY_MTF_DATA_REQUIRE=both_missing)。
    #   10/23 は AI_OK の 3,469 件が両方 NA (片方だけ NA は 2,303 件で通す)。any=片方でも無ければ止める / off=止めない。
    _mtf_req = str(os.getenv("ENTRY_MTF_DATA_REQUIRE", "both_missing") or "").strip().lower()
    if (_mtf_req == "any" and not (has_3m and has_5m)) or (_mtf_req == "both_missing" and not has_3m and not has_5m):
        logger.info("[ENTRY GATE] block mtf_data_not_ready symbol=%s source=%s interval=%s 3m=%s 5m=%s mode=%s",
                    symbol, source, interval, txt_3m, txt_5m, _mtf_req)
        return _block(f"mtf_data_not_ready:3m={txt_3m}|5m={txt_5m}", final_conf, "MTF_DATA")

    lot_multiplier = max(0.5, min(2.0, 0.5 + final_conf))
    if final_conf > ENTRY_SCORE_MAX + 1e-9:
        logger.error(
            "[AI ENTRY GATE] entry_score が上限を超えた symbol=%s value=%.3f max=%.2f "
            "(dominant=%.2f ranking=%.2f mtf=%.2f imm=%.2f boost=%.2f)",
            symbol, final_conf, ENTRY_SCORE_MAX,
            dominant_ratio, ranking_conf, mtf_conf, immediate_p, momentum_boost,
        )
    return _allow(
        confidence=final_conf,
        ai_confidence=(ranking_conf if ranking_ai_used else None),
        ai_inference=bool(ranking_ai_used),
        confidence_source=(
            "ai_model:ranking" if ranking_ai_used else "rule_entry_score:no_ai_model"
        ),
        lot_multiplier=lot_multiplier,
        reason=(
            f"rankScore={ranking_score_final:.3f}|"
            f"mtf={mtf_conf:.2f}|"
            f"rank={ranking_conf:.2f}({'ai' if ranking_ai_used else 'failopen_no_model'})|"
            f"imm={immediate_p:.2f}|"
            f"dom={dominant_ratio:.2f}|"
            f"boost={momentum_boost:.2f}|"
            f"3m={txt_3m}|"
            f"5m={txt_5m}|"
            f"mtf_data={'ok' if (has_3m and has_5m) else 'DATA_NOT_READY'}|"
            f"src={source}|"
            f"minScore={MIN_SCORE:.2f}"
        ),
        # モデルが動いていないのに RANKING_LGBM と報告しないこと。
        model_used=(
            ("RANKING_LGBM+MTF" if ranking_ai_used else "RANKING_AI_ABSENT+MTF")
            if is_ranking else "MTF"
        ),
    )


# ============================================================
# ai_final_entry_check の最終合成レイヤー
# ------------------------------------------------------------
# 以下は元 core/startup/ranking_entry_runtime_rescue_patch.py
# (_patch_entry_gate) と core/startup/low_volatility_entry_guard_patch.py
# (_patch_ai_entry_gate) が実運用で `AI.entry_gate.ai_final_entry_check`
# へ monkeypatch していた内容を native化したもの。
#
# 実運用での適用順序 (usercustomize.py -> core.startup.runtime_env.installer
# .install() -> _install_ranking_entry_runtime_rescue() が先、
# _install_low_volatility_entry_guard() が後、いずれも Python 起動時に
# 同期的に実行され、trading.handlers.entry_controller 等の import より前に
# 完了する) と同じく、
#   低ボラガード ( RANKING mtf_low 救済 ( _ai_final_entry_check_core ) )
# の順で適用する。ヘルパー関数名は本体の既存ヘルパー (_env_bool 等) と
# 挙動が微妙に異なる (truthy/falsy集合や数値パースの細部) ため、
# 元パッチと完全に同一の挙動を保つ目的で _low_vol_ / _rank_rescue_
# 接頭辞を付けて独立させている。
# ============================================================


def _rank_rescue_env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


_RANKING_AI_SKIP_LOGGED: set = set()


def _ranking_ai_model_available() -> bool:
    try:
        from AI.inference.ranking_entry_predictor import model_available

        return bool(model_available())
    except Exception:
        logger.exception("[RANKING AI] model_available check failed -> treat as absent")
        return False


def _log_ranking_ai_skip_once(symbol, err) -> None:
    """同じ理由の RANKING AI SKIP は 1 回だけ出す (握り潰しではない)。"""
    try:
        key = "%s:%s" % (type(err).__name__, str(err)[:200])
        if key in _RANKING_AI_SKIP_LOGGED:
            return
        _RANKING_AI_SKIP_LOGGED.add(key)
        logger.error(
            "[RANKING AI SKIP] ranking AI 推論に失敗したので fail-open で通す "
            "(以後同じ理由は抑制する) first_symbol=%s err=%s",
            symbol, err,
        )
    except Exception:
        logger.exception("[RANKING AI SKIP] log failed symbol=%s", symbol)


def _rank_rescue_env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _rank_rescue_safe_float(v, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        return float(str(v).strip().replace(",", "").replace("%", ""))
    except Exception:
        return default


def _rank_rescue_norm_text(v) -> str:
    try:
        return str(v or "").strip().upper()
    except Exception:
        return ""


def _rank_rescue_is_ranking_row(row) -> bool:
    if not isinstance(row, dict):
        return False
    source = _rank_rescue_norm_text(row.get("source") or row.get("entry_source") or row.get("candidate_source") or row.get("pipeline_source"))
    entry_type = _rank_rescue_norm_text(row.get("entry_type") or row.get("entryType"))
    if "RANKING" in source or "RANKING" in entry_type:
        return True
    ranking_keys = {
        "rank",
        "ranking_type",
        "ranking_kind",
        "rank_type",
        "source_rank",
        "change_percentage",
        "change_rate",
        "change_ratio",
        "ranking_snapshot_high",
        "ranking_snapshot_low",
    }
    return bool(set(row.keys()) & ranking_keys)


def _ranking_entry_mtf_low_failopen(row: dict, result: dict) -> dict:
    """RANKING行がmtf_lowのみで拒否された場合の救済 (元 ranking_entry_runtime_rescue_patch)。"""
    try:
        if not _rank_rescue_env_bool("RANKING_ENTRY_MTF_LOW_FAIL_OPEN", True):
            return result
        if not isinstance(result, dict) or bool(result.get("allow")):
            return result
        if not _rank_rescue_is_ranking_row(row):
            return result
        reason = str(result.get("reason") or "")
        if reason != "mtf_low" and not reason.startswith("mtf_low"):
            return result

        conf = max(
            _rank_rescue_safe_float(result.get("confidence"), 0.0),
            _rank_rescue_env_float("RANKING_ENTRY_MTF_LOW_RESCUE_CONFIDENCE", 0.72),
        )
        lot_multiplier = max(0.5, min(2.0, 0.5 + conf))
        # この confidence は AI 推論結果ではなく固定の fail-open 値。
        # 値そのものは変えない (売買条件を勝手に緩めない) が、
        # AI 出力と誤認されないよう出所を必ず残す。
        rescued = {
            "allow": True,
            "confidence": conf,
            "lot_multiplier": lot_multiplier,
            "reason": f"{reason}|ranking_mtf_low_failopen",
            "model_used": "FALLBACK_NO_AI:RANKING_MTF_RESCUE(" + str(result.get("model_used") or "MTF") + ")",
            "ai_inference": False,
            "confidence_source": "fixed_failopen:RANKING_ENTRY_MTF_LOW_RESCUE_CONFIDENCE",
            "ai_confidence": None,
        }
        logger.warning(
            "[RANKING ENTRY RUNTIME RESCUE] mtf_low fail-open symbol=%s side=%s "
            "confidence=%.3f source=FIXED_FAILOPEN (AI推論結果ではない) "
            "model=FALLBACK_NO_AI score=%s row_source=%s",
            row.get("symbol"),
            row.get("side") or row.get("entry_decision"),
            conf,
            row.get("score") or row.get("final_score") or row.get("score_total"),
            row.get("source"),
        )
        return rescued
    except Exception:
        logger.exception("[RANKING ENTRY RUNTIME RESCUE] entry gate wrapper failed; return original result")
        return result


def _low_vol_env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _low_vol_env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).strip().replace(",", ""))
    except Exception:
        return float(default)


def _low_vol_safe_float(v, default=None):
    try:
        if v is None:
            return default
        if isinstance(v, bool):
            return float(v)
        s = str(v).strip().replace(",", "").replace("%", "")
        if not s or s.lower() in {"none", "nan", "null", "na", "n/a"}:
            return default
        x = float(s)
        if math.isnan(x) or math.isinf(x):
            return default
        return x
    except Exception:
        return default


def _low_vol_norm_symbol(v) -> str:
    try:
        s = str(v or "").strip()
        if s.endswith(".0"):
            s = s[:-2]
        return s
    except Exception:
        return ""


def _low_vol_norm_side(v) -> str:
    try:
        s = str(v or "").strip().upper()
        return s if s in {"BUY", "SELL"} else ""
    except Exception:
        return ""


def _low_vol_merged_dicts(row) -> list:
    out: list = []
    if isinstance(row, dict):
        out.append(row)
        for k in ("ai_row", "source_row", "row", "summary_row", "candidate", "entry_row", "_raw", "entry_conditions"):
            v = row.get(k)
            if isinstance(v, dict):
                out.append(v)
    return out


def _low_vol_pick(row, *keys):
    for d in _low_vol_merged_dicts(row):
        for k in keys:
            if k in d:
                return d.get(k)
    return None


def _low_vol_abs_pct_value(row, *keys):
    vals: list = []
    for k in keys:
        v = _low_vol_safe_float(_low_vol_pick(row, k), None)
        if v is None:
            continue
        vals.append(abs(float(v)))
    if not vals:
        return None
    return max(vals)


def _low_vol_range_pct(row):
    explicit = _low_vol_abs_pct_value(row, "range_pct", "price_range_pct", "hl_range_pct", "high_low_range_pct", "intraday_range_pct", "display_range_pct")
    if explicit is not None:
        return explicit
    high = _low_vol_safe_float(_low_vol_pick(row, "high", "high_price", "ranking_snapshot_high", "day_high", "today_high", "intraday_high", "session_high"), None)
    low = _low_vol_safe_float(_low_vol_pick(row, "low", "low_price", "ranking_snapshot_low", "day_low", "today_low", "intraday_low", "session_low"), None)
    close = _low_vol_safe_float(_low_vol_pick(row, "close", "close_price", "price", "current_price"), None)
    if high is None or low is None or close is None or close <= 0:
        return None
    if high <= 0 or low <= 0 or high < low:
        return None
    return abs(high - low) / close * 100.0


def _low_vol_atr_pct(row):
    explicit = _low_vol_abs_pct_value(row, "atr_pct", "atr_percent", "atr_rate", "atr_ratio_pct", "display_atr_pct")
    if explicit is not None:
        return explicit
    atr = _low_vol_safe_float(_low_vol_pick(row, "atr", "ATR", "atr_1m", "atr_3m", "atr_5m"), None)
    close = _low_vol_safe_float(_low_vol_pick(row, "close", "close_price", "price", "current_price"), None)
    if atr is None or close is None or close <= 0 or atr < 0:
        return None
    return atr / close * 100.0


def _low_vol_change_pct(row):
    return _low_vol_abs_pct_value(
        row,
        "change_pct", "change_rate", "change_ratio", "change_percentage",
        "price_change_pct", "price_change_rate", "price_change_1m", "price_change_3m",
        "price_change_5m", "pct_change_1m", "pct_change_3m", "pct_change_5m",
        "return_1m", "return_3m", "return_5m", "momentum_pct",
    )


def _low_vol_slope_abs(row):
    return _low_vol_abs_pct_value(row, "slope", "display_slope", "slope_1m", "slope_3m", "slope_5m", "slope_atr_scaled", "display_slope_atr_scaled")


def _low_vol_block(row) -> tuple:
    if not _low_vol_env_bool("ENTRY_LOW_VOLATILITY_GUARD_ENABLED", True):
        return False, "", {}

    min_range_pct = _low_vol_env_float("ENTRY_MIN_RANGE_PCT", 0.25)
    min_atr_pct = _low_vol_env_float("ENTRY_MIN_ATR_PCT", 0.12)
    min_change_pct = _low_vol_env_float("ENTRY_MIN_ABS_CHANGE_PCT", 0.20)
    min_slope_abs = _low_vol_env_float("ENTRY_MIN_ABS_SLOPE_FOR_VOL", 0.0010)
    min_evidence = int(max(1.0, _low_vol_env_float("ENTRY_LOW_VOL_MIN_EVIDENCE_COUNT", 2.0)))

    rng = _low_vol_range_pct(row)
    atr = _low_vol_atr_pct(row)
    chg = _low_vol_change_pct(row)
    slp = _low_vol_slope_abs(row)

    evidence: list = []
    if rng is not None:
        evidence.append(("range_pct", rng, min_range_pct))
    if atr is not None:
        evidence.append(("atr_pct", atr, min_atr_pct))
    if chg is not None:
        evidence.append(("change_pct", chg, min_change_pct))
    if slp is not None:
        evidence.append(("slope_abs", slp, min_slope_abs))

    if _low_vol_env_bool("ENTRY_LOW_VOL_ZERO_AS_MISSING", True) and evidence:
        positive_evidence = [(name, val, th) for name, val, th in evidence if abs(float(val)) > 1e-12]
        if not positive_evidence:
            return False, "", {"reason": "zero_volatility_fields_treated_as_missing", "range_pct": rng, "atr_pct": atr, "change_pct": chg, "slope_abs": slp, "evidence_count": len(evidence)}
        evidence = positive_evidence

    if len(evidence) < min_evidence:
        return False, "", {"evidence_count": len(evidence), "range_pct": rng, "atr_pct": atr, "change_pct": chg, "slope_abs": slp}

    small = [(name, val, th) for name, val, th in evidence if val < th]
    if len(small) != len(evidence):
        return False, "", {"range_pct": rng, "atr_pct": atr, "change_pct": chg, "slope_abs": slp, "evidence_count": len(evidence)}

    detail = {
        "symbol": _low_vol_norm_symbol(_low_vol_pick(row, "symbol", "Symbol")),
        "side": _low_vol_norm_side(_low_vol_pick(row, "side", "ai_side", "entry_decision")),
        "range_pct": rng, "atr_pct": atr, "change_pct": chg, "slope_abs": slp,
        "thresholds": {"min_range_pct": min_range_pct, "min_atr_pct": min_atr_pct, "min_abs_change_pct": min_change_pct, "min_abs_slope": min_slope_abs},
        "evidence_count": len(evidence),
    }
    return True, "low_volatility", detail


def _low_vol_block_result(row, original=None) -> dict:
    blocked, reason, detail = _low_vol_block(row)
    if not blocked:
        return original or {"allow": True, "reason": "ok"}
    logger.warning("[LOW VOL ENTRY GUARD] blocked symbol=%s side=%s detail=%s", detail.get("symbol"), detail.get("side"), detail)
    return {"allow": False, "confidence": 0.0, "lot_multiplier": 0.0, "reason": reason, "model_used": "LOW_VOLATILITY_GUARD", "detail": detail}


def _low_volatility_entry_guard(row: dict, result: dict) -> dict:
    """低ボラ銘柄の最終ガード (元 low_volatility_entry_guard_patch._patch_ai_entry_gate)。"""
    try:
        if isinstance(result, dict) and not bool(result.get("allow")):
            return result
        blocked, _, _ = _low_vol_block(row)
        if blocked:
            return _low_vol_block_result(row, result if isinstance(result, dict) else None)
        return result
    except Exception:
        logger.exception("[LOW VOL ENTRY GUARD] AI.entry_gate wrapper failed; return original result")
        return result


def ai_final_entry_check(row: dict) -> dict:
    """AI最終エントリーゲート (唯一の判断場所)。

    実運用の monkeypatch 適用順序 (ranking mtf_low 救済が先、低ボラガードが
    最後に外側からラップ) と同じ合成順序を native実装として保持する。
    """
    result = _ai_final_entry_check_core(row)
    result = _ranking_entry_mtf_low_failopen(row, result)
    result = _low_volatility_entry_guard(row, result)
    # 2026-10-30: 方向 AI (tv_csv2 で学習) の予測を記録するだけ。判定は変えない。
    #   AI/mtf_shadow/runtime.py。MTF_SHADOW_ENABLED=0 で止まる。
    try:
        from AI.mtf_shadow import runtime as _mtf_shadow
        _mtf_shadow.record(row, result)
    except Exception:
        logger.debug("[MTF SHADOW] hook failed", exc_info=True)
    return result


# ============================================================
# SUMMARY MTF EARLY READY
# (旧 core/startup/summary_mtf_early_ready_patch.py を本文化)
# ------------------------------------------------------------
# SUMMARY 由来の行で MTF が "block" になったとき、MA5 が実質そろって
# いれば "skip" へ fail-open する層。ネイティブは
# _summary_mtf_status_core へ改名した。
#
# 旧パッチの他のサブ処理:
#  - controller_cache.choose_merged_cache_payload の history-first ラッパー
#    -> choose_merged_cache_payload の呼び出し元はリポジトリ全体で
#       trading/summary/summary_controller.py の 2 箇所だけで、そこは
#       from-import 束縛 (38行) を使う。旧 summary_latest_prefer_patch が
#       その束縛も上書きしていたため、history-first ラッパーは
#       誰からも呼ばれない死んだラッパーだった。よって移していない。
#  - neutral_score_guard / push_raw_fallback / order_builder_range_repair の
#    中継 install -> sitecustomize の patch リストへ直接並べた。
# ============================================================

def _emr_env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _emr_env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _emr_safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        s = str(v).strip()
        if not s or s.lower() in {"nan", "none", "nat", "<na>"}:
            return default
        return float(s.replace(",", ""))
    except Exception:
        return default


def _emr_bool_like(v: Any, default: bool = False) -> bool:
    try:
        if isinstance(v, bool):
            return v
        if v is None:
            return default
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on", "ok"}:
            return True
        if s in {"0", "false", "no", "n", "off", "ng", ""}:
            return False
        return default
    except Exception:
        return default


def _emr_side_of(row: dict[str, Any]) -> str:
    s = str(row.get("entry_decision") or row.get("side") or "").strip().upper()
    return s if s in {"BUY", "SELL"} else ""


def _emr_score_of(row: dict[str, Any], side: str) -> float:
    if side == "BUY":
        return max(
            _emr_safe_float(row.get("buy_score"), 0.0),
            _emr_safe_float(row.get("score_buy"), 0.0),
            _emr_safe_float(row.get("score"), 0.0),
            _emr_safe_float(row.get("final_score"), 0.0),
        )
    if side == "SELL":
        return max(
            _emr_safe_float(row.get("sell_score"), 0.0),
            _emr_safe_float(row.get("score_sell"), 0.0),
            abs(_emr_safe_float(row.get("score"), 0.0)),
            abs(_emr_safe_float(row.get("final_score"), 0.0)),
        )
    return abs(_emr_safe_float(row.get("score"), 0.0))


def _emr_early_ma5_ready(row: dict[str, Any], *, interval: int, reason: str) -> tuple[bool, str]:
    if not _emr_env_bool("SUMMARY_MTF_EARLY_READY_ENABLED", True):
        return False, "disabled"

    if int(interval or 1) not in {3, 5}:
        return False, "not_3m_5m"

    if "hist_short" not in str(reason or ""):
        return False, "not_hist_short"

    technical_ready = _emr_bool_like(row.get("technical_ready"), False)
    display_ready = _emr_bool_like(row.get("display_ready"), technical_ready)
    if not technical_ready or not display_ready:
        return False, "not_ready_flags"

    hist = _emr_safe_float(row.get("symbol_hist_len"), 0.0)
    min_hist = _emr_env_float("SUMMARY_MTF_EARLY_HIST_MIN", 5.0)
    if hist < min_hist:
        return False, f"hist_too_short:{hist:.0f}<{min_hist:.0f}"

    side = _emr_side_of(row)
    if not side:
        return False, "side_missing"

    close = _emr_safe_float(row.get("close_price") or row.get("close") or row.get("price"), 0.0)
    ma5 = _emr_safe_float(row.get("ma5") or row.get("ma_5"), 0.0)
    slope = _emr_safe_float(row.get("slope_atr_scaled"), _emr_safe_float(row.get("slope"), 0.0))
    score = _emr_score_of(row, side)

    min_score = _emr_env_float("SUMMARY_MTF_EARLY_MIN_SCORE", 1.0)
    min_slope = abs(_emr_env_float("SUMMARY_MTF_EARLY_MIN_SLOPE", 0.0001))

    if score < min_score:
        return False, f"score_low:{score:.3f}<{min_score:.3f}"

    if close <= 0 or ma5 <= 0:
        if side == "BUY" and slope >= min_slope:
            return True, f"early_mtf_buy_slope_only:hist={hist:.0f}:slope={slope:.5f}:score={score:.2f}"
        if side == "SELL" and slope <= -min_slope:
            return True, f"early_mtf_sell_slope_only:hist={hist:.0f}:slope={slope:.5f}:score={score:.2f}"
        return False, "ma5_or_close_missing"

    if side == "BUY":
        if close >= ma5 and slope >= -min_slope:
            return True, f"early_mtf_buy_ma5_ready:hist={hist:.0f}:close={close:.2f}:ma5={ma5:.2f}:slope={slope:.5f}:score={score:.2f}"
        return False, f"buy_ma5_direction_ng:close={close:.2f}:ma5={ma5:.2f}:slope={slope:.5f}"

    if side == "SELL":
        if close <= ma5 and slope <= min_slope:
            return True, f"early_mtf_sell_ma5_ready:hist={hist:.0f}:close={close:.2f}:ma5={ma5:.2f}:slope={slope:.5f}:score={score:.2f}"
        return False, f"sell_ma5_direction_ng:close={close:.2f}:ma5={ma5:.2f}:slope={slope:.5f}"

    return False, "side_ng"


def _summary_mtf_status(row: dict, *, source: str, interval: int) -> tuple[str, str]:
    status, reason = _summary_mtf_status_core(row, source=source, interval=interval)
    try:
        if source == "SUMMARY" and status == "block":
            ok, detail = _emr_early_ma5_ready(row, interval=int(interval), reason=str(reason))
            if ok:
                logger.warning(
                    "[SUMMARY MTF EARLY READY PATCH] fail-open SUMMARY MTF symbol=%s interval=%s side=%s original=%s detail=%s",
                    row.get("symbol"), interval, _emr_side_of(row), reason, detail,
                )
                return "skip", detail
            logger.info(
                "[SUMMARY MTF EARLY READY PATCH] keep block symbol=%s interval=%s side=%s original=%s detail=%s",
                row.get("symbol"), interval, _emr_side_of(row), reason, detail,
            )
    except Exception:
        logger.exception("[SUMMARY MTF EARLY READY PATCH] decision failed symbol=%s", row.get("symbol") if isinstance(row, dict) else None)
    return status, reason
