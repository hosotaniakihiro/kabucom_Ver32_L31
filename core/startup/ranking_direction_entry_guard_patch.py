# ============================================================
# File   : core/startup/ranking_direction_entry_guard_patch.py
# Version: Ver01-RANKING-DIRECTION-ENTRY-GUARD
# ------------------------------------------------------------
# ランキング方向に逆らうエントリーを禁止する。
#
# 仕様:
#   - 下落率ランキング/下落優勢銘柄への BUY を禁止
#   - 上昇率ランキング/上昇優勢銘柄への SELL を禁止
#   - ranking_type が無い場合も、score_buy / score_sell / score_total / final_score / change_rate
#     から方向を推定して止める
#
# 目的:
#   下落率rankingに入っている銘柄をBUYで逆張りして負ける、
#   上昇率rankingに入っている銘柄をSELLで逆張りして負ける問題を防ぐ。
# ============================================================

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)
_INSTALLED = False
_ORIGINAL = None


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return default
        return float(v)
    except Exception:
        return default


def _row_to_dict(row: Any) -> dict:
    try:
        if row is None:
            return {}
        if isinstance(row, dict):
            return dict(row)
        if hasattr(row, "to_dict"):
            v = row.to_dict()
            if isinstance(v, dict):
                return dict(v)
        return {}
    except Exception:
        return {}


def _norm_side(v: Any) -> str:
    s = str(v or "").strip().upper()
    if s in {"BUY", "B", "LONG", "買", "買い", "信用買"}:
        return "BUY"
    if s in {"SELL", "S", "SHORT", "売", "売り", "信用売"}:
        return "SELL"
    return s


def _norm_symbol(v: Any) -> str:
    try:
        s = str(v or "").strip()
        if s.endswith(".0") and s[:-2].isdigit():
            return s[:-2]
        return s
    except Exception:
        return ""


def _contains_any(text: str, words: tuple[str, ...]) -> bool:
    t = str(text or "").lower()
    return any(w.lower() in t for w in words)


def _infer_direction(row: dict) -> tuple[str, str]:
    """
    Returns:
      (UP|DOWN|UNKNOWN, reason)
    """
    # ranking_type/name/source などの文字列からまず判定
    text_keys = (
        "ranking_type", "rank_type", "ranking_name", "source", "entry_source",
        "market", "reason", "category", "ranking_category",
    )
    text = " ".join(str(row.get(k, "")) for k in text_keys if row.get(k) is not None)

    down_words = (
        "下落", "値下", "値下がり", "decline", "decliner", "down", "fall", "drop", "minus", "negative",
        "loser", "sell", "short",
    )
    up_words = (
        "上昇", "値上", "値上がり", "rise", "riser", "up", "gain", "gainer", "plus", "positive",
        "buy", "long",
    )

    if _contains_any(text, down_words):
        return "DOWN", f"text_down:{text}"
    if _contains_any(text, up_words):
        return "UP", f"text_up:{text}"

    # 値上がり率/騰落率などから判定
    for k in ("change_rate", "change_pct", "rate", "騰落率", "price_change_rate", "ranking_change_rate"):
        if k in row:
            x = _safe_float(row.get(k), 0.0)
            if x <= -0.1:
                return "DOWN", f"{k}={x}"
            if x >= 0.1:
                return "UP", f"{k}={x}"

    score_buy = _safe_float(row.get("score_buy"), 0.0)
    score_sell = _safe_float(row.get("score_sell"), 0.0)
    score_total = _safe_float(row.get("score_total", row.get("final_score", row.get("score", 0.0))), 0.0)

    # score_sellが明確に優勢ならDOWN、score_buyが明確に優勢ならUP
    if score_sell >= max(1.0, score_buy + 0.5):
        return "DOWN", f"score_sell_dominant sell={score_sell:.2f} buy={score_buy:.2f} total={score_total:.2f}"
    if score_buy >= max(1.0, score_sell + 0.5):
        return "UP", f"score_buy_dominant buy={score_buy:.2f} sell={score_sell:.2f} total={score_total:.2f}"

    # totalが明確にマイナス/プラスの場合
    if score_total <= -1.0:
        return "DOWN", f"score_total_negative={score_total:.2f}"
    if score_total >= 1.0:
        return "UP", f"score_total_positive={score_total:.2f}"

    return "UNKNOWN", "no_direction_signal"


def _guard_row(row: Any, side: Any) -> tuple[bool, dict]:
    d = _row_to_dict(row)
    side_s = _norm_side(side or d.get("side") or d.get("Side"))
    symbol = _norm_symbol(d.get("symbol") or d.get("code") or d.get("stock_code"))
    direction, reason = _infer_direction(d)

    detail = {"symbol": symbol, "side": side_s, "direction": direction, "reason": reason}

    if direction == "DOWN" and side_s == "BUY":
        detail["block_reason"] = "BUY_AGAINST_DOWN_RANKING"
        logger.warning("[RANKING DIRECTION GUARD] NG %s", detail)
        return False, detail

    if direction == "UP" and side_s == "SELL":
        detail["block_reason"] = "SELL_AGAINST_UP_RANKING"
        logger.warning("[RANKING DIRECTION GUARD] NG %s", detail)
        return False, detail

    logger.info("[RANKING DIRECTION GUARD] OK %s", detail)
    return True, detail


def install() -> bool:
    global _INSTALLED, _ORIGINAL
    if _INSTALLED:
        return True
    try:
        import trading.handlers.entry_controller as ec

        # 既存の _build_scored_candidates に入る前後どちらでも拾えるよう、
        # まずは candidate filter 関数が存在すれば差し替える。
        target_names = [
            "_passes_side_filter",
            "_passes_entry_side_filter",
            "_allow_candidate_side",
            "_side_filter_ok",
        ]
        for name in target_names:
            old = getattr(ec, name, None)
            if callable(old):
                def _patched(row, side=None, *args, __old=old, **kwargs):
                    try:
                        ok, detail = _guard_row(row, side or kwargs.get("side"))
                        if not ok:
                            return False
                    except Exception:
                        logger.exception("[RANKING DIRECTION GUARD] guard failed in %s", name)
                    return __old(row, side, *args, **kwargs)
                setattr(ec, name, _patched)
                logger.warning("[RANKING DIRECTION GUARD] patched %s", name)
                _INSTALLED = True
                return True

        # 明示的なfilter関数がない環境では、entry_controller.run_entry_pipeline の直前で entries を削る。
        old_run = getattr(ec, "run_entry_pipeline", None)
        if callable(old_run):
            _ORIGINAL = old_run
            def _patched_run_entry_pipeline(*args, **kwargs):
                try:
                    entries = kwargs.get("entries")
                    if entries is None and args:
                        # 引数名が不明でも、listの最初をentriesとして扱う
                        for a in args:
                            if isinstance(a, list):
                                entries = a
                                break
                    if isinstance(entries, list):
                        kept = []
                        for e in entries:
                            side = None
                            try:
                                side = e.get("side") if isinstance(e, dict) else getattr(e, "side", None)
                            except Exception:
                                pass
                            ok, detail = _guard_row(e, side)
                            if ok:
                                kept.append(e)
                            else:
                                logger.warning("[RANKING DIRECTION GUARD] entry removed detail=%s", detail)
                        if entries is kwargs.get("entries"):
                            kwargs["entries"] = kept
                        elif args:
                            new_args = []
                            replaced = False
                            for a in args:
                                if not replaced and a is entries:
                                    new_args.append(kept)
                                    replaced = True
                                else:
                                    new_args.append(a)
                            args = tuple(new_args)
                except Exception:
                    logger.exception("[RANKING DIRECTION GUARD] run_entry_pipeline prefilter failed")
                return old_run(*args, **kwargs)
            _patched_run_entry_pipeline._ranking_direction_guard_v1 = True  # type: ignore[attr-defined]
            ec.run_entry_pipeline = _patched_run_entry_pipeline
            _INSTALLED = True
            logger.warning("[RANKING DIRECTION GUARD] installed run_entry_pipeline prefilter")
            return True

        logger.warning("[RANKING DIRECTION GUARD] install skipped no target")
        return False
    except Exception:
        logger.exception("[RANKING DIRECTION GUARD] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[RANKING DIRECTION GUARD] auto install failed")

__all__ = ["install"]
