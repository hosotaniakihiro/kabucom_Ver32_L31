# ============================================================
# File   : trading/exit/exit_position_runner.py
# Version: V1.3-SPLIT-POSITION-RUNNER-THREE-MIN-PROFIT-ESCAPE
# ------------------------------------------------------------
# 【概要】
#   1銘柄分のEXIT判定を担当。
#
# 【判定順序】
#   1. 価格取得
#   2. ctx / features 構築
#   3. 3分経過しても最大含み益+0.2%未達、かつ現在プラスなら返済
#   4. エントリー直後の建値撤退/早期利確/早期損切り/トレーリング損切り
#   5. collapse / inago
#   6. 殿様イナゴEXIT
#   7. collapse full exit
#   8. AI EXIT
#   9. boost guard
#   10. RL
#   11. manage_exit
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Any, Dict, Optional, Tuple

from core.global_context.context import global_context as GC
from global_state import global_data
from trading.exit.ai_exit_runner import apply_ai_exit_if_needed
from trading.exit.early_profit_guard import judge_early_profit_guard
from trading.exit.exit_features import build_exit_features_safe, inject_daily_features_safe
from trading.exit.exit_finalize import finalize_exit
from trading.exit.exit_context import ExitContext
from trading.exit.exit_price_source import get_latest_exit_price
from trading.exit.exit_state_machine import manage_exit
from trading.exit.exit_utils import safe_float
from trading.exit.tonosama_exit_runner import apply_tonosama_exit_if_needed

logger = logging.getLogger(__name__)


# ============================================================
# 3分伸びない銘柄のプラス逃げ設定
# ============================================================

# エントリーから何秒後に判定するか。3分=180秒。
THREE_MIN_PROFIT_ESCAPE_SEC = int(float(os.getenv("THREE_MIN_PROFIT_ESCAPE_SEC", "180")))

# 3分以内に最大含み益が +0.2% に到達していなければ対象。
THREE_MIN_PROFIT_ESCAPE_TARGET_PCT = float(os.getenv("THREE_MIN_PROFIT_ESCAPE_TARGET_PCT", "0.20"))

# 現在プラスの間だけ返済する。0.00より大きければ返済。
# 手数料やスリッページを考慮するなら 0.03 などへ上げられる。
THREE_MIN_PROFIT_ESCAPE_MIN_CURRENT_PCT = float(os.getenv("THREE_MIN_PROFIT_ESCAPE_MIN_CURRENT_PCT", "0.00"))

# 同じポジションで何度もログ/返済を試さないためのフラグ名。
_THREE_MIN_ESCAPE_MARK_ATTR = "three_min_profit_escape_fired"


def _pos_get(pos: Dict[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        try:
            if isinstance(pos, dict) and name in pos:
                return pos.get(name)
            if hasattr(pos, name):
                return getattr(pos, name)
        except Exception:
            continue
    return default


def _normalize_side(side: Any) -> str:
    """
    kabu Station / broker position / internal position の side 表記を BUY/SELL に統一する。

    ここで正規化できないと、早期EXITガードに入る前に
    [EXIT] skip invalid side で止まるため、数値・日本語表記も吸収する。
    """
    raw = side
    s = str(side or "").upper().strip()

    buy_values = {
        "BUY", "BUY_CREDIT", "LONG", "L",
        "2", "02", "20", "B",
        "信用買", "買", "買建", "買い", "新規買", "返済売",
    }
    sell_values = {
        "SELL", "SELL_CREDIT", "SHORT", "S",
        "1", "01", "10",
        "信用売", "売", "売建", "売り", "新規売", "返済買",
    }

    if s in buy_values:
        return "BUY"
    if s in sell_values:
        return "SELL"

    try:
        if isinstance(raw, dict):
            for key in ("side", "Side", "trade_side", "position_side", "order_side"):
                if key in raw:
                    return _normalize_side(raw.get(key))
    except Exception:
        pass

    return s


def _fallback_entry_time(pos: Dict[str, Any], now: dt.datetime) -> dt.datetime:
    raw = _pos_get(pos, "entry_time", "created_at", "timestamp", default=None)
    if isinstance(raw, dt.datetime):
        try:
            if raw.tzinfo is not None:
                return raw.replace(tzinfo=None)
        except Exception:
            pass
        return raw

    try:
        s = str(raw or "").strip()
        if not s:
            return now
        s = s.replace("T", " ").split("+", 1)[0]
        if s.endswith("Z"):
            s = s[:-1]
        return dt.datetime.fromisoformat(s)
    except Exception:
        return now


def _resolve_exit_ctx(symbol: str, pos: Dict[str, Any], *, side: str, entry_price: float, now: dt.datetime) -> Any:
    """
    EXITが発火しない最大要因を避けるためのctx解決。

    優先:
      1. GC.ai の ExitContext
      2. legacy global_state.global_data.exit_ctx
      3. open position snapshot から最小 ExitContext を生成して GC.ai に保存
    """
    ctx = None

    try:
        if hasattr(GC, "ai") and GC.ai and hasattr(GC.ai, "get_exit_ctx"):
            ctx = GC.ai.get_exit_ctx(symbol)
            if ctx is not None:
                return ctx
    except Exception:
        logger.debug("[EXIT] GC.ai ctx lookup failed symbol=%s", symbol, exc_info=True)

    try:
        legacy = getattr(global_data, "exit_ctx", None)
        if isinstance(legacy, dict):
            ctx = legacy.get(symbol)
            if ctx is not None:
                try:
                    if hasattr(GC, "ai") and GC.ai and hasattr(GC.ai, "set_exit_ctx"):
                        GC.ai.set_exit_ctx(symbol, ctx)
                except Exception:
                    pass
                return ctx
    except Exception:
        logger.debug("[EXIT] legacy ctx lookup failed symbol=%s", symbol, exc_info=True)

    atr = safe_float(_pos_get(pos, "atr_1min", "atr", default=0.0), 0.0)
    entry_time = _fallback_entry_time(pos, now)

    try:
        ctx = ExitContext(
            symbol=str(symbol),
            side=side,
            entry_price=float(entry_price),
            atr_1min=max(float(atr), 0.0),
            entry_time=entry_time,
        )
        try:
            if hasattr(GC, "ai") and GC.ai and hasattr(GC.ai, "set_exit_ctx"):
                GC.ai.set_exit_ctx(symbol, ctx)
        except Exception:
            pass
        logger.warning(
            "[EXIT] fallback ExitContext created symbol=%s side=%s entry=%.4f atr=%.4f entry_time=%s",
            symbol,
            side,
            entry_price,
            atr,
            entry_time,
        )
        return ctx
    except Exception:
        logger.exception("[EXIT] fallback ExitContext create failed symbol=%s", symbol)
        return None


def _already_three_min_escape_fired(symbol: str, ctx: Any, pos: Dict[str, Any]) -> bool:
    """
    同一ポジションで3分プラス逃げを何度も発火させないための保険。
    finalize_exit後に建玉が残っている数秒間の多重発注を避ける。
    """
    try:
        if bool(getattr(ctx, _THREE_MIN_ESCAPE_MARK_ATTR, False)):
            return True
    except Exception:
        pass

    try:
        if isinstance(pos, dict) and bool(pos.get(_THREE_MIN_ESCAPE_MARK_ATTR)):
            return True
    except Exception:
        pass

    try:
        mark = getattr(global_data, "three_min_profit_escape_fired_symbols", None)
        if isinstance(mark, set) and str(symbol) in mark:
            return True
    except Exception:
        pass

    return False


def _mark_three_min_escape_fired(symbol: str, ctx: Any, pos: Dict[str, Any]) -> None:
    try:
        setattr(ctx, _THREE_MIN_ESCAPE_MARK_ATTR, True)
    except Exception:
        pass

    try:
        if isinstance(pos, dict):
            pos[_THREE_MIN_ESCAPE_MARK_ATTR] = True
    except Exception:
        pass

    try:
        mark = getattr(global_data, "three_min_profit_escape_fired_symbols", None)
        if not isinstance(mark, set):
            mark = set()
            setattr(global_data, "three_min_profit_escape_fired_symbols", mark)
        mark.add(str(symbol))
    except Exception:
        pass


def _judge_three_min_profit_escape(
    *,
    symbol: str,
    pos: Dict[str, Any],
    side: str,
    entry_price: float,
    current_price: float,
    ctx: Any,
    now: dt.datetime,
) -> Tuple[bool, str, Dict[str, Any]]:
    """
    要望:
      エントリーしても3分経ってもプラス0.2%に行かなければ、
      プラスの間に返済する。

    判定:
      - 保有時間 >= 180秒
      - 最大含み益 MFE% < +0.2%
      - 現在損益% > 0.0%

    BUY:
      current_price > entry_price がプラス。
    SELL:
      current_price < entry_price がプラス。
    """
    try:
        if _already_three_min_escape_fired(symbol, ctx, pos):
            return False, "", {}

        if not ctx or entry_price <= 0 or current_price <= 0:
            return False, "", {}

        try:
            hold_sec = int(ctx.holding_seconds(now)) if hasattr(ctx, "holding_seconds") else 0
        except Exception:
            hold_sec = 0

        if hold_sec < THREE_MIN_PROFIT_ESCAPE_SEC:
            return False, "", {
                "hold_sec": hold_sec,
                "need_sec": THREE_MIN_PROFIT_ESCAPE_SEC,
            }

        if side == "BUY":
            current_profit_pct = (current_price - entry_price) / entry_price * 100.0
        else:
            current_profit_pct = (entry_price - current_price) / entry_price * 100.0

        try:
            mfe_pct = float(getattr(ctx, "mfe_pct", 0.0) or 0.0)
        except Exception:
            mfe_pct = 0.0

        # ctx.mfe_pct が0のままでも、現在プラスなら最低限は現在利益を反映する。
        if current_profit_pct > mfe_pct:
            mfe_pct = current_profit_pct

        detail = {
            "hold_sec": hold_sec,
            "need_sec": THREE_MIN_PROFIT_ESCAPE_SEC,
            "entry_price": entry_price,
            "current_price": current_price,
            "side": side,
            "current_profit_pct": current_profit_pct,
            "mfe_pct": mfe_pct,
            "target_pct": THREE_MIN_PROFIT_ESCAPE_TARGET_PCT,
            "min_current_pct": THREE_MIN_PROFIT_ESCAPE_MIN_CURRENT_PCT,
        }

        # 3分以内に一度でも+0.2%へ行っていれば、このルールでは返済しない。
        if mfe_pct >= THREE_MIN_PROFIT_ESCAPE_TARGET_PCT:
            return False, "", detail

        # まだプラス圏でなければ「プラスの間に返済」条件ではない。
        if current_profit_pct <= THREE_MIN_PROFIT_ESCAPE_MIN_CURRENT_PCT:
            return False, "", detail

        reason = (
            f"THREE_MIN_PROFIT_ESCAPE "
            f"hold={hold_sec}s "
            f"mfe={mfe_pct:.3f}%<target={THREE_MIN_PROFIT_ESCAPE_TARGET_PCT:.3f}% "
            f"current={current_profit_pct:.3f}%>min={THREE_MIN_PROFIT_ESCAPE_MIN_CURRENT_PCT:.3f}%"
        )
        return True, reason, detail

    except Exception:
        logger.exception("[THREE_MIN_PROFIT_ESCAPE] judge failed symbol=%s", symbol)
        return False, "", {}


def evaluate_collapse(symbol: str, regime: int, features: Dict[str, Any], side: str) -> Tuple[float, int, Optional[str]]:
    collapse_prob = 0.0
    inago_state = 0
    full_reason = None

    try:
        if not hasattr(GC, "ai") or not GC.ai:
            return collapse_prob, inago_state, full_reason

        collapse_engine = GC.ai.get_collapse_engine()

        if collapse_engine:
            result = collapse_engine.evaluate(
                symbol=symbol,
                regime=regime,
                pre_feature_dict=features,
                regime_feature_dict=features,
            )

            collapse_prob = safe_float(result.get("strength"))

            if result.get("exit_ratio", 0) >= 1.0:
                full_reason = "COLLAPSE_ENGINE_FULL"
                return collapse_prob, inago_state, full_reason

        collapse_model = GC.ai.get_collapse_model()
        if collapse_model:
            legacy_prob = collapse_model.predict_proba(
                features,
                side=side,
            )
            collapse_prob = max(collapse_prob, safe_float(legacy_prob))

        inago_model = GC.ai.get_inago_model()
        if inago_model:
            ignite_prob, exhaust_prob = inago_model.predict(features)

            ignite_prob = safe_float(ignite_prob)
            exhaust_prob = safe_float(exhaust_prob)

            if ignite_prob > 0.7:
                inago_state = 1
            elif exhaust_prob > 0.6:
                inago_state = 2

    except Exception:
        logger.exception("[COLLAPSE/INAGO_ERROR]")

    return collapse_prob, inago_state, full_reason


def evaluate_rl(symbol: str, regime: int, cluster_id: int, inago_state: int, pnl: float):
    try:
        if not hasattr(GC, "ai") or not GC.ai:
            return None, None

        rl_agent = GC.ai.get_rl_agent()
        if not rl_agent:
            return None, None

        rl_state = rl_agent.encode_state(
            regime,
            cluster_id,
            inago_state,
            pnl,
        )

        rl_action = rl_agent.select_action(rl_state)

        return rl_action, rl_state

    except Exception:
        logger.exception("[RL_ERROR]")
        return None, None


def run_exit_for_position(
    *,
    symbol: str,
    pos: Dict[str, Any],
    now: dt.datetime,
    regime: int,
    boost_active: bool,
) -> bool:
    """
    1銘柄分のEXIT処理。

    戻り値:
      True:
        何らかのEXIT処理またはDRY_RUN EXITが発生した

      False:
        EXITなし
    """

    try:
        entry_price = safe_float(_pos_get(pos, "avg_price", "entry_price", "price", "Price", "CurrentPrice"))
        side = _normalize_side(_pos_get(pos, "side", "Side", "trade_side", "position_side", "order_side"))

        if not entry_price:
            logger.debug("[EXIT] skip no entry_price symbol=%s pos=%s", symbol, pos)
            return False

        if side not in ("BUY", "SELL"):
            logger.warning("[EXIT] skip invalid side symbol=%s side=%s pos_keys=%s", symbol, side, list(pos.keys()) if isinstance(pos, dict) else type(pos))
            return False

        price, bar5s = get_latest_exit_price(symbol)
        if not price:
            logger.debug("[EXIT] skip no latest price symbol=%s", symbol)
            return False

        pnl = price - entry_price if side == "BUY" else entry_price - price
        pnl = safe_float(pnl)

        ctx = _resolve_exit_ctx(symbol, pos, side=side, entry_price=entry_price, now=now)
        if not ctx:
            logger.warning("[EXIT] skip ctx unavailable symbol=%s side=%s entry=%.4f", symbol, side, entry_price)
            return False

        try:
            if hasattr(ctx, "update_price"):
                ctx.update_price(float(price))
        except Exception:
            logger.debug("[EXIT] ctx.update_price failed symbol=%s price=%s", symbol, price, exc_info=True)

        features = build_exit_features_safe(ctx, price, pnl)
        features = inject_daily_features_safe(symbol, features)

        try:
            cluster_id = GC.positions.get_cluster(symbol) or 0
        except Exception:
            cluster_id = 0

        # ====================================================
        # 0. 3分伸びない銘柄のプラス逃げ
        #    3分経って最大含み益が +0.2% 未達、かつ現在プラスなら返済。
        #    AI / TONOSAMA / 通常EXITより前に判定する。
        # ====================================================
        three_min_exit, three_min_reason, three_min_detail = _judge_three_min_profit_escape(
            symbol=symbol,
            pos=pos,
            side=side,
            entry_price=entry_price,
            current_price=price,
            ctx=ctx,
            now=now,
        )
        if three_min_exit:
            logger.warning(
                "[THREE_MIN_PROFIT_ESCAPE] EXIT symbol=%s detail=%s reason=%s",
                symbol,
                three_min_detail,
                three_min_reason,
            )
            _mark_three_min_escape_fired(symbol, ctx, pos)
            finalize_exit(
                symbol=symbol,
                price=price,
                reason=three_min_reason,
                cluster_id=cluster_id,
                regime=regime,
                inago_state=0,
                pnl=pnl,
                collapse_prob=0.0,
                ctx=ctx,
            )
            return True

        # ====================================================
        # 1. EARLY PROFIT / BREAKEVEN / TRAILING STOP GUARD
        #    エントリー直後に一瞬プラスからマイナス化する問題を抑えるため、
        #    TONOSAMA / AI / 通常EXITより前に判定する。
        # ====================================================
        early_exit, early_reason = judge_early_profit_guard(
            symbol=symbol,
            pos=pos,
            side=side,
            entry_price=entry_price,
            current_price=price,
            ctx=ctx,
            now=now,
        )
        if early_exit:
            finalize_exit(
                symbol=symbol,
                price=price,
                reason=early_reason,
                cluster_id=cluster_id,
                regime=regime,
                inago_state=0,
                pnl=pnl,
                collapse_prob=0.0,
                ctx=ctx,
            )
            return True

        collapse_prob, inago_state, full_reason = evaluate_collapse(
            symbol,
            regime,
            features,
            side,
        )

        features["collapse_prob"] = collapse_prob

        # ====================================================
        # 2. TONOSAMA INAGO EXIT
        #    損切り・VWAP割れ・5秒足失速などをAIより前に判定
        # ====================================================
        if apply_tonosama_exit_if_needed(
            symbol=symbol,
            pos=pos,
            side=side,
            price=price,
            entry_price=entry_price,
            features=features,
            ctx=ctx,
            now=now,
            cluster_id=cluster_id,
            regime=regime,
            inago_state=inago_state,
            pnl=pnl,
            collapse_prob=collapse_prob,
            bar5s=bar5s,
        ):
            return True

        # ====================================================
        # 3. collapse full exit
        # ====================================================
        if full_reason or collapse_prob > 0.85:
            finalize_exit(
                symbol=symbol,
                price=price,
                reason=full_reason or "COLLAPSE_EXIT",
                cluster_id=cluster_id,
                regime=regime,
                inago_state=inago_state,
                pnl=pnl,
                collapse_prob=collapse_prob,
                ctx=ctx,
            )
            return True

        # ====================================================
        # 4. AI EXIT
        # ====================================================
        if apply_ai_exit_if_needed(
            symbol=symbol,
            side=side,
            price=price,
            entry_price=entry_price,
            pnl=pnl,
            features=features,
            ctx=ctx,
            now=now,
            cluster_id=cluster_id,
            regime=regime,
            inago_state=inago_state,
            collapse_prob=collapse_prob,
        ):
            return True

        # ====================================================
        # 5. boost guard
        #    boost中でATR利益が出ている場合は粘る
        # ====================================================
        if boost_active:
            atr = getattr(ctx, "atr_1min", 0.0)
            if atr and pnl > atr * 2:
                return False

        # ====================================================
        # 6. RL EXIT
        # ====================================================
        rl_action, rl_state = evaluate_rl(
            symbol,
            regime,
            cluster_id,
            inago_state,
            pnl,
        )

        if rl_action in ("EXIT", "TAKE"):
            finalize_exit(
                symbol=symbol,
                price=price,
                reason=f"RL_{rl_action}",
                cluster_id=cluster_id,
                regime=regime,
                inago_state=inago_state,
                pnl=pnl,
                collapse_prob=collapse_prob,
                ctx=ctx,
                rl_state=rl_state,
            )
            return True

        # ====================================================
        # 7. 通常 state machine EXIT
        # ====================================================
        action, reason = manage_exit(
            ctx=ctx,
            price=price,
            now=now,
        )

        if action == "EXIT":
            finalize_exit(
                symbol=symbol,
                price=price,
                reason=reason,
                cluster_id=cluster_id,
                regime=regime,
                inago_state=inago_state,
                pnl=pnl,
                collapse_prob=collapse_prob,
                ctx=ctx,
                rl_state=rl_state,
            )
            return True

        return False

    except Exception:
        logger.exception("[EXIT_SYMBOL_FATAL] symbol=%s", symbol)
        return False


__all__ = [
    "run_exit_for_position",
    "evaluate_collapse",
    "evaluate_rl",
]
