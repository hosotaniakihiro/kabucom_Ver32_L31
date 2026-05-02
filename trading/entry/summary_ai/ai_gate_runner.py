# ============================================================
# File   : trading/entry/summary_ai/ai_gate_runner.py
# Version: PRODUCTION-STABLE-REV2.1-AI-GATE-TO-ENTRY-DAILY-CACHE
# ------------------------------------------------------------
# 【概要】
#   summary候補 DataFrame を AI gate に通し、
#   AI_OK 銘柄だけ既存 entry_handler.place_entry_buy() へ渡す。
#
# 【流れ】
#   summary_df
#     -> 起動時キャッシュ済み日足情報を付与
#     -> run_ai_gate_for_candidates()
#     -> AI_OK 抽出
#     -> place_entry_buy()
#     -> kabu_api.buy_sell_entry
#
# 【重要】
#   - 既存の trading.handlers.entry_handler を使用
#   - 判断・AI判定はこのファイル
#   - 実発注は entry_handler 側
#   - dry_run=True の場合は発注しない
#   - 日足DBはここで直接読まない
#   - 日足情報は trading.daily.daily_signal_cache のメモリキャッシュを参照する
#
# 【REV2.1 変更点】
#   ✔ 起動時キャッシュ済み日足情報をAIgateへ組み込み
#   ✔ daily_score / daily_buy_score / daily_sell_score をAI rowへ注入
#   ✔ daily_ok_buy / daily_exit_warn をログ・結果へ追加
#   ✔ 初期状態では日足で候補を除外しない
#   ✔ daily_filter_buy=True の場合のみ AI前に日足NGを除外可能
#   ✔ daily_hard_block_exit_warn=True の場合のみ exit警戒を強制NG可能
#   ✔ daily_min_score 指定時のみ日足スコア下限で強制NG可能
#   ✔ 既存 alias / entry 実行処理は維持
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

import pandas as pd

from .row_adapter import convert_summary_row_to_ai_gate_row
from .utils import get_ai_final_entry_check, safe_df, safe_float, safe_str

logger = logging.getLogger(__name__)

DEFAULT_MIN_AI_CONFIDENCE = 0.65


# ============================================================
# env helpers
# ============================================================

def _env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return bool(default)
    return str(v).strip().lower() in {"1", "true", "yes", "on", "y"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.environ.get(name, default)))
    except Exception:
        return int(default)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except Exception:
        return float(default)


def _env_str(name: str, default: str) -> str:
    v = os.environ.get(name)
    if v is None or str(v).strip() == "":
        return default
    return str(v).strip()


def _safe_int(v: Any, default: int = 100) -> int:
    try:
        q = int(float(v))
        return q if q > 0 else int(default)
    except Exception:
        return int(default)


def _safe_bool(v: Any, default: bool = False) -> bool:
    try:
        if v is None:
            return bool(default)
        if isinstance(v, bool):
            return v
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "on", "y", "ok"}:
            return True
        if s in {"0", "false", "no", "off", "n", "ng", ""}:
            return False
        return bool(default)
    except Exception:
        return bool(default)


def _safe_optional_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        s = str(v).strip()
        if s == "":
            return None
        return float(s)
    except Exception:
        return None


def _append_reason(base: str, extra: str) -> str:
    base = safe_str(base, "")
    extra = safe_str(extra, "")
    if not extra:
        return base
    if not base:
        return extra
    return f"{base}|{extra}"


# ============================================================
# low layer resolver
# ============================================================

def _get_place_entry_buy():
    try:
        from trading.handlers.entry_handler import place_entry_buy
        return place_entry_buy
    except Exception:
        logger.exception("[SUMMARY AI ENTRY] failed to import place_entry_buy")
        return None


# ============================================================
# daily cache helpers
# ============================================================

def _find_symbol_col(df: pd.DataFrame) -> str:
    for c in ("symbol", "stock_code", "code", "銘柄コード"):
        if c in df.columns:
            return c
    return "symbol"


def _attach_daily_cache_safe(
    df: pd.DataFrame,
    *,
    enabled: bool = True,
    filter_buy: bool = False,
) -> pd.DataFrame:
    """
    起動時キャッシュ済みの日足情報を候補DFへ付与する。

    重要:
      - ここでは stock_analysis.db を直接読まない
      - daily_signal_cache のメモリキャッシュを参照する
      - cache未作成時のみ fallback_load_if_empty=True で1回だけwarmup
      - 失敗してもAIgate処理は止めない
    """
    if df is None or df.empty:
        return df

    if not enabled:
        logger.info("[SUMMARY AI GATE] daily cache disabled")
        return df

    try:
        from trading.daily.daily_signal_cache import (
            attach_daily_decision_from_cache,
            get_daily_cache_size,
            is_daily_cache_ready,
        )

        symbol_col = _find_symbol_col(df)
        before = len(df)

        out = attach_daily_decision_from_cache(
            df,
            symbol_col=symbol_col,
            fallback_load_if_empty=True,
            filter_buy=filter_buy,
        )

        out = safe_df(out)
        after = len(out)

        hit = 0
        if not out.empty and "daily_date" in out.columns:
            try:
                hit = int(out["daily_date"].astype(str).str.len().gt(0).sum())
            except Exception:
                hit = 0

        logger.info(
            "[SUMMARY AI GATE] daily cache attached before=%s after=%s hit=%s "
            "cache_ready=%s cache_size=%s filter_buy=%s",
            before,
            after,
            hit,
            is_daily_cache_ready(),
            get_daily_cache_size(),
            filter_buy,
        )

        return out

    except Exception as e:
        logger.exception("[SUMMARY AI GATE] daily cache attach failed err=%s", e)
        return df


def _inject_daily_fields_to_ai_row(ai_row: Dict[str, Any], row: pd.Series) -> Dict[str, Any]:
    """
    convert_summary_row_to_ai_gate_row() が daily_* を拾わない場合でも、
    ai_final_entry_check に日足情報を確実に渡す。
    """
    daily_fields = [
        "daily_score",
        "daily_buy_score",
        "daily_sell_score",
        "daily_ok_buy",
        "daily_ok_sell",
        "daily_exit_warn",
        "daily_reason",
        "daily_date",
    ]

    for c in daily_fields:
        if c not in row.index:
            continue

        try:
            v = row.get(c)

            if c in {"daily_ok_buy", "daily_ok_sell", "daily_exit_warn"}:
                ai_row[c] = _safe_bool(v, False)
            elif c in {"daily_score", "daily_buy_score", "daily_sell_score"}:
                ai_row[c] = safe_float(v, 0.0)
            else:
                ai_row[c] = safe_str(v, "")

        except Exception:
            pass

    # AI側で読みやすい別名
    ai_row["daily_trend_score"] = safe_float(ai_row.get("daily_score"), 0.0)
    ai_row["daily_trend_ok"] = _safe_bool(ai_row.get("daily_ok_buy"), False)
    ai_row["daily_exit_risk"] = _safe_bool(ai_row.get("daily_exit_warn"), False)

    return ai_row


# ============================================================
# AI gate only
# ============================================================

def run_ai_gate_for_candidates(
    candidates_df: pd.DataFrame,
    *,
    interval: int | str = 1,
    source: str = "SUMMARY",
    min_ai_confidence: float = DEFAULT_MIN_AI_CONFIDENCE,
    default_dominant_ratio: float = 1.0,

    # --------------------------------------------------------
    # daily cache options
    # --------------------------------------------------------
    use_daily_cache: bool = True,

    # 最初は False 推奨。
    # Trueにすると daily_ok_buy=False の候補はAIに渡す前に除外される。
    daily_filter_buy: bool = False,

    # 最初は False 推奨。
    # Trueにすると daily_exit_warn=True は AI_OK でも強制NGにする。
    daily_hard_block_exit_warn: bool = False,

    # None 推奨。
    # 数値を入れると daily_score がこの値未満なら強制NG。
    daily_min_score: Optional[float] = None,
) -> List[Dict[str, Any]]:
    df = safe_df(candidates_df)
    if df.empty:
        return []

    # --------------------------------------------------------
    # 1. 日足キャッシュ付与
    # --------------------------------------------------------
    df = _attach_daily_cache_safe(
        df,
        enabled=use_daily_cache,
        filter_buy=daily_filter_buy,
    )

    df = safe_df(df)
    if df.empty:
        logger.info(
            "[SUMMARY AI GATE] no candidates after daily filter "
            "interval=%s source=%s daily_filter_buy=%s",
            interval,
            source,
            daily_filter_buy,
        )
        return []

    ai_check = get_ai_final_entry_check()
    if ai_check is None:
        logger.error("[SUMMARY AI GATE] ai_final_entry_check not found")
        return []

    results: List[Dict[str, Any]] = []

    logger.info(
        "[SUMMARY AI GATE] start rows=%s interval=%s source=%s min_conf=%.2f "
        "daily_cache=%s daily_filter_buy=%s daily_hard_block_exit_warn=%s daily_min_score=%s",
        len(df),
        interval,
        source,
        min_ai_confidence,
        use_daily_cache,
        daily_filter_buy,
        daily_hard_block_exit_warn,
        daily_min_score,
    )

    # --------------------------------------------------------
    # 2. AI gate
    # --------------------------------------------------------
    for _, row in df.iterrows():
        ai_row = convert_summary_row_to_ai_gate_row(
            row,
            interval=interval,
            source=source,
            default_dominant_ratio=default_dominant_ratio,
            side="BUY",
        )

        ai_row = _inject_daily_fields_to_ai_row(ai_row, row)

        symbol = safe_str(ai_row.get("symbol"), "")
        symbolname = safe_str(ai_row.get("symbolname"), "")

        daily_score = safe_float(ai_row.get("daily_score"), 0.0)
        daily_buy_score = safe_float(ai_row.get("daily_buy_score"), 0.0)
        daily_sell_score = safe_float(ai_row.get("daily_sell_score"), 0.0)
        daily_ok_buy = _safe_bool(ai_row.get("daily_ok_buy"), False)
        daily_ok_sell = _safe_bool(ai_row.get("daily_ok_sell"), False)
        daily_exit_warn = _safe_bool(ai_row.get("daily_exit_warn"), False)
        daily_reason = safe_str(ai_row.get("daily_reason"), "")
        daily_date = safe_str(ai_row.get("daily_date"), "")

        try:
            gate_result = ai_check(ai_row)
            if not isinstance(gate_result, dict):
                gate_result = {
                    "allow": False,
                    "confidence": 0.0,
                    "reason": "invalid_ai_result",
                    "model_used": "UNKNOWN",
                }

        except Exception:
            logger.exception("[SUMMARY AI GATE] AI gate failed symbol=%s", symbol)
            gate_result = {
                "allow": False,
                "confidence": 0.0,
                "reason": "ai_gate_exception",
                "model_used": "ERROR",
            }

        allow = bool(gate_result.get("allow", False))
        conf = safe_float(gate_result.get("confidence"), 0.0)
        reason = safe_str(gate_result.get("reason"), "")
        model_used = safe_str(gate_result.get("model_used"), "")

        # ----------------------------------------------------
        # 3. confidence gate
        # ----------------------------------------------------
        if allow and conf < float(min_ai_confidence):
            allow = False
            reason = _append_reason(
                reason,
                f"confidence_low:{conf:.3f}<{float(min_ai_confidence):.3f}",
            )

        # ----------------------------------------------------
        # 4. daily optional hard block
        # ----------------------------------------------------
        if allow and daily_hard_block_exit_warn and daily_exit_warn:
            allow = False
            reason = _append_reason(
                reason,
                f"daily_exit_warn score={daily_score:.2f} sell={daily_sell_score:.2f}",
            )
            model_used = model_used or "DAILY_BLOCK"

        if allow and daily_min_score is not None:
            try:
                min_score = float(daily_min_score)
            except Exception:
                min_score = None

            if min_score is not None and daily_score < min_score:
                allow = False
                reason = _append_reason(
                    reason,
                    f"daily_score_low:{daily_score:.2f}<{min_score:.2f}",
                )
                model_used = model_used or "DAILY_BLOCK"

        item = {
            "allow": allow,
            "confidence": conf,
            "reason": reason,
            "model_used": model_used,
            "lot_multiplier": safe_float(gate_result.get("lot_multiplier"), 1.0),

            "ai_row": ai_row,
            "source_row": dict(row),

            "symbol": symbol,
            "symbolname": symbolname,

            "buy_score": ai_row.get("buy_score"),
            "sell_score": ai_row.get("sell_score"),
            "score_total": ai_row.get("score_total"),
            "final_score": ai_row.get("final_score"),
            "close_price": ai_row.get("close_price"),
            "turnover": ai_row.get("turnover"),

            # daily fields
            "daily_score": daily_score,
            "daily_buy_score": daily_buy_score,
            "daily_sell_score": daily_sell_score,
            "daily_ok_buy": daily_ok_buy,
            "daily_ok_sell": daily_ok_sell,
            "daily_exit_warn": daily_exit_warn,
            "daily_reason": daily_reason,
            "daily_date": daily_date,
        }

        results.append(item)

        if allow:
            logger.info(
                "[SUMMARY AI GATE] AI_OK symbol=%s name=%s conf=%.3f lot=%.2f "
                "buy=%.2f sell=%.2f total=%.2f close=%.1f "
                "daily=%.2f daily_buy=%.2f daily_sell=%.2f daily_ok=%s exit_warn=%s "
                "model=%s reason=%s daily_reason=%s",
                symbol,
                symbolname,
                conf,
                item["lot_multiplier"],
                safe_float(ai_row.get("buy_score")),
                safe_float(ai_row.get("sell_score")),
                safe_float(ai_row.get("score_total")),
                safe_float(ai_row.get("close_price")),
                daily_score,
                daily_buy_score,
                daily_sell_score,
                daily_ok_buy,
                daily_exit_warn,
                model_used,
                reason,
                daily_reason,
            )
        else:
            logger.info(
                "[SUMMARY AI GATE] AI_NG symbol=%s name=%s conf=%.3f "
                "buy=%.2f sell=%.2f total=%.2f "
                "daily=%.2f daily_buy=%.2f daily_sell=%.2f daily_ok=%s exit_warn=%s "
                "reason=%s model=%s daily_reason=%s",
                symbol,
                symbolname,
                conf,
                safe_float(ai_row.get("buy_score")),
                safe_float(ai_row.get("sell_score")),
                safe_float(ai_row.get("score_total")),
                daily_score,
                daily_buy_score,
                daily_sell_score,
                daily_ok_buy,
                daily_exit_warn,
                reason,
                model_used,
                daily_reason,
            )

    return results


# ============================================================
# AI gate -> entry
# ============================================================

def _extract_entry_values(r: Dict[str, Any]) -> Dict[str, Any]:
    ai_row = r.get("ai_row") or {}
    source_row = r.get("source_row") or {}

    symbol = (
        ai_row.get("symbol")
        or r.get("symbol")
        or source_row.get("symbol")
        or ""
    )

    symbolname = (
        ai_row.get("symbolname")
        or ai_row.get("name")
        or r.get("symbolname")
        or source_row.get("symbolname")
        or source_row.get("name")
        or ""
    )

    price = (
        ai_row.get("close_price")
        or ai_row.get("close")
        or r.get("close_price")
        or source_row.get("close_price")
        or source_row.get("close")
    )

    reason = r.get("reason") or "AI_OK"

    daily_score = safe_float(r.get("daily_score"), 0.0)
    daily_reason = safe_str(r.get("daily_reason"), "")

    if daily_reason:
        reason = f"{reason}|daily_score={daily_score:.2f}|{daily_reason}"
    else:
        reason = f"{reason}|daily_score={daily_score:.2f}"

    return {
        "symbol": str(symbol).strip(),
        "symbolname": str(symbolname),
        "price": safe_float(price, 0.0),
        "reason": str(reason),
    }


def run_push_summary_ai_entry(
    summary_df: Optional[pd.DataFrame] = None,
    *,
    df: Optional[pd.DataFrame] = None,
    interval: int | str = 1,
    interval_label: Optional[str] = None,
    source: str = "SUMMARY",
    top_n: int = 10,
    max_entries: int = 1,
    min_ai_confidence: float = DEFAULT_MIN_AI_CONFIDENCE,
    min_confidence: Optional[float] = None,
    min_conf: Optional[float] = None,
    dry_run: bool = False,
    require_market_open: bool = True,
    default_dominant_ratio: float = 1.0,
    order_type: Optional[str] = None,
    test_qty: Optional[int] = None,

    # --------------------------------------------------------
    # daily cache options
    # --------------------------------------------------------
    use_daily_cache: Optional[bool] = None,
    daily_filter_buy: Optional[bool] = None,
    daily_hard_block_exit_warn: Optional[bool] = None,
    daily_min_score: Optional[float] = None,

    **kwargs,
) -> Dict[str, Any]:
    """
    summary AI gate 通過銘柄を実エントリーへ流す本体。

    hook側から呼ばれる想定:
      run_push_summary_ai_entry(summary_df=df, interval=1, source="SUMMARY", dry_run=False)

    戻り値:
      {
        "ai_results": [...],
        "ai_ok": [...],
        "approved_rows": [...],
        "execution": {...}
      }
    """

    base_df = summary_df if isinstance(summary_df, pd.DataFrame) else df
    base_df = safe_df(base_df)

    if min_confidence is not None:
        min_ai_confidence = float(min_confidence)
    if min_conf is not None:
        min_ai_confidence = float(min_conf)

    max_entries = _safe_int(max_entries, 1)
    top_n = _safe_int(top_n, 10)

    order_type = str(order_type or _env_str("SUMMARY_AI_ENTRY_ORDER_TYPE", "LIMIT")).upper()
    qty = _safe_int(
        test_qty if test_qty is not None else _env_int("SUMMARY_AI_ENTRY_TEST_QTY", 100),
        100,
    )

    cancel_after_send = _env_bool("SUMMARY_AI_ENTRY_CANCEL_AFTER_SEND", False)

    # --------------------------------------------------------
    # daily option defaults
    # --------------------------------------------------------
    if use_daily_cache is None:
        use_daily_cache = _env_bool("SUMMARY_AI_USE_DAILY_CACHE", True)

    if daily_filter_buy is None:
        daily_filter_buy = _env_bool("SUMMARY_AI_DAILY_FILTER_BUY", False)

    if daily_hard_block_exit_warn is None:
        daily_hard_block_exit_warn = _env_bool("SUMMARY_AI_DAILY_HARD_BLOCK_EXIT_WARN", False)

    if daily_min_score is None:
        env_daily_min = os.environ.get("SUMMARY_AI_DAILY_MIN_SCORE")
        daily_min_score = _safe_optional_float(env_daily_min)

    logger.info(
        "[SUMMARY AI ENTRY] received rows=%s interval=%s interval_label=%s source=%s "
        "top_n=%s max_entries=%s min_conf=%.2f dry_run=%s require_market_open=%s "
        "order_type=%s qty=%s cancel_after_send=%s "
        "daily_cache=%s daily_filter_buy=%s daily_hard_block_exit_warn=%s daily_min_score=%s",
        len(base_df),
        interval,
        interval_label,
        source,
        top_n,
        max_entries,
        float(min_ai_confidence),
        dry_run,
        require_market_open,
        order_type,
        qty,
        cancel_after_send,
        use_daily_cache,
        daily_filter_buy,
        daily_hard_block_exit_warn,
        daily_min_score,
    )

    if base_df.empty:
        return {
            "candidates": [],
            "ai_results": [],
            "ai_ok": [],
            "approved_rows": [],
            "execution": {
                "executed": False,
                "orders": [],
                "skip_reason": "empty_df",
            },
        }

    candidates_df = base_df.head(top_n).copy()

    ai_results = run_ai_gate_for_candidates(
        candidates_df,
        interval=interval,
        source=source,
        min_ai_confidence=float(min_ai_confidence),
        default_dominant_ratio=default_dominant_ratio,
        use_daily_cache=bool(use_daily_cache),
        daily_filter_buy=bool(daily_filter_buy),
        daily_hard_block_exit_warn=bool(daily_hard_block_exit_warn),
        daily_min_score=daily_min_score,
    )

    ai_ok = [r for r in ai_results if bool(r.get("allow"))]

    logger.info(
        "[SUMMARY AI ENTRY] start ai_results=%s ai_ok=%s dry_run=%s max_entries=%s",
        len(ai_results),
        len(ai_ok),
        dry_run,
        max_entries,
    )

    if not ai_ok:
        return {
            "candidates": candidates_df,
            "ai_results": ai_results,
            "ai_ok": [],
            "approved_rows": [],
            "execution": {
                "executed": False,
                "orders": [],
                "skip_reason": "no_ai_ok",
            },
        }

    place_entry_buy = _get_place_entry_buy()
    if place_entry_buy is None:
        return {
            "candidates": candidates_df,
            "ai_results": ai_results,
            "ai_ok": ai_ok,
            "approved_rows": ai_ok,
            "execution": {
                "executed": False,
                "orders": [],
                "skip_reason": "place_entry_buy_import_failed",
            },
        }

    orders: List[Dict[str, Any]] = []

    for r in ai_ok[:max_entries]:
        v = _extract_entry_values(r)

        symbol = v["symbol"]
        symbolname = v["symbolname"]
        price = v["price"]
        reason = v["reason"]

        daily_score = safe_float(r.get("daily_score"), 0.0)
        daily_ok_buy = _safe_bool(r.get("daily_ok_buy"), False)
        daily_exit_warn = _safe_bool(r.get("daily_exit_warn"), False)

        if not symbol:
            logger.warning("[SUMMARY AI ENTRY] skipped empty symbol item=%s", r)
            orders.append({
                "symbol": "",
                "ok": False,
                "order_id": None,
                "reason": "empty_symbol",
            })
            continue

        if dry_run:
            logger.warning(
                "[SUMMARY AI ENTRY DRY_RUN] would entry BUY symbol=%s name=%s "
                "price=%.1f qty=%s order_type=%s daily=%.2f daily_ok=%s exit_warn=%s reason=%s",
                symbol,
                symbolname,
                price,
                qty,
                order_type,
                daily_score,
                daily_ok_buy,
                daily_exit_warn,
                reason,
            )
            orders.append({
                "symbol": symbol,
                "symbolname": symbolname,
                "ok": True,
                "dry_run": True,
                "order_id": None,
                "qty": qty,
                "order_type": order_type,
                "daily_score": daily_score,
                "daily_ok_buy": daily_ok_buy,
                "daily_exit_warn": daily_exit_warn,
                "reason": reason,
            })
            continue

        logger.warning(
            "[SUMMARY AI ENTRY SEND] BUY symbol=%s name=%s price=%.1f qty=%s "
            "order_type=%s daily=%.2f daily_ok=%s exit_warn=%s reason=%s",
            symbol,
            symbolname,
            price,
            qty,
            order_type,
            daily_score,
            daily_ok_buy,
            daily_exit_warn,
            reason,
        )

        order_id = None
        try:
            order_id = place_entry_buy(
                symbol,
                symbolname,
                price,
                reason,
                order_type=order_type,
                qty=qty,
            )
        except Exception:
            logger.exception("[SUMMARY AI ENTRY SEND FAILED] symbol=%s", symbol)

        ok = bool(order_id)

        logger.warning(
            "[SUMMARY AI ENTRY SENT] symbol=%s name=%s qty=%s order_id=%s ok=%s "
            "daily=%.2f reason=%s",
            symbol,
            symbolname,
            qty,
            order_id,
            ok,
            daily_score,
            reason,
        )

        orders.append({
            "symbol": symbol,
            "symbolname": symbolname,
            "ok": ok,
            "dry_run": False,
            "order_id": order_id,
            "qty": qty,
            "order_type": order_type,
            "daily_score": daily_score,
            "daily_ok_buy": daily_ok_buy,
            "daily_exit_warn": daily_exit_warn,
            "reason": reason,
        })

    executed = any(bool(x.get("ok")) for x in orders)

    return {
        "candidates": candidates_df,
        "ai_results": ai_results,
        "ai_ok": ai_ok,
        "approved_rows": ai_ok,
        "execution": {
            "executed": executed,
            "orders": orders,
            "skip_reason": None if executed else "entry_send_failed",
        },
    }


# ============================================================
# compatibility aliases
# ============================================================

def run_summary_ai_entry_from_df(*args, **kwargs):
    return run_push_summary_ai_entry(*args, **kwargs)


def run_summary_ai_gate(*args, **kwargs):
    return run_push_summary_ai_entry(*args, **kwargs)


def run_ai_gate_once(*args, **kwargs):
    return run_push_summary_ai_entry(*args, **kwargs)


def run(*args, **kwargs):
    return run_push_summary_ai_entry(*args, **kwargs)


def start(*args, **kwargs):
    return run_push_summary_ai_entry(*args, **kwargs)


__all__ = [
    "DEFAULT_MIN_AI_CONFIDENCE",
    "run_ai_gate_for_candidates",
    "run_push_summary_ai_entry",
    "run_summary_ai_entry_from_df",
    "run_summary_ai_gate",
    "run_ai_gate_once",
    "run",
    "start",
]