# ============================================================
# trading/symbols/active_symbol_manager.py
# Ver2.1-ABSOLUTE-FINAL-STATEFUL-ACTIVE-AI-AWARE-PRODUCTION
# ------------------------------------------------------------
# ✔ MONITOR / ACTIVE / COOLDOWN 状態管理
# ✔ ranking ノイズ耐性（decay）
# ✔ summary / liquidity / AI 連動
# ✔ ACTIVE 昇格に学習AI（STEP8）
# ✔ 時間帯別 threshold（STEP9）
# ✔ ATS 余力ブレーキ対応
# ✔ 状態遷移ログ DB 保存（STEP6）
# ✔ 副作用ゼロ設計
# ✔ scheduler停止防止
# ✔ exitロジック混入完全除去（重要修正）
# ============================================================

from __future__ import annotations

import math
import logging
import datetime as dt
from dataclasses import dataclass
from typing import Dict, List, Optional

from config.dynamic_symbol_config import DYNAMIC_SYMBOL_CONFIG as CFG
from config.active_ai_time_threshold import get_active_ai_threshold

from AI.inference.active_state_predictor import ActiveStatePredictor
from trading.symbols.state_logger import save_symbol_state_log
from trading.symbols.active_ai_logger import log_active_ai

logger = logging.getLogger(__name__)

# ============================================================
# 状態定数
# ============================================================

STATE_OUT = "OUT"
STATE_MONITOR = "MONITOR"
STATE_ACTIVE = "ACTIVE"
STATE_COOLDOWN = "COOLDOWN"


# ============================================================
# 内部状態
# ============================================================

@dataclass
class SymbolState:
    symbol: str
    status: str = STATE_OUT

    # ranking
    last_ranking_seen: Optional[dt.datetime] = None
    ranking_hits: int = 0
    ranking_score: float = 0.0

    # summary / liquidity
    last_summary_time: Optional[dt.datetime] = None
    summary_count: int = 0
    turnover: float = 0.0

    # AI
    ai_score: Optional[float] = None

    # 状態管理
    status_since: Optional[dt.datetime] = None


# ============================================================
# マネージャ本体
# ============================================================

class DynamicSymbolManager:
    """
    動的銘柄状態マネージャ
    - MONITOR / ACTIVE / COOLDOWN を厳密管理
    - ACTIVE 昇格はルール + 学習AI
    - 副作用ゼロ設計
    """

    def __init__(self):

        self.symbols: Dict[str, SymbolState] = {}

        # ranking decay
        self.decay_tau_min: int = CFG["RANKING"]["DECAY_TAU_MIN"]

        # ACTIVE 昇格 AI
        self.active_ai = ActiveStatePredictor(
            model_path="AI/models/active_state_lgbm.txt",
            threshold=0.6,
        )

    # --------------------------------------------------------
    # ranking イベント
    # --------------------------------------------------------
    def on_ranking_update(self, symbols: List[str], now: dt.datetime):

        try:
            for sym in symbols:

                st = self.symbols.setdefault(
                    sym,
                    SymbolState(symbol=sym),
                )

                st.last_ranking_seen = now
                st.ranking_hits += 1
                st.ranking_score = max(st.ranking_score, 1.0)

                # OUT → MONITOR
                if (
                    st.status == STATE_OUT
                    and st.ranking_hits >= CFG["RANKING"]["MIN_HITS_TO_MONITOR"]
                ):
                    self._transition(st, STATE_MONITOR, now)

        except Exception:
            logger.exception("active_symbol_manager: ranking update failed")

    # --------------------------------------------------------
    # summary 更新
    # --------------------------------------------------------
    def on_summary_update(self, symbol: str, *, turnover: float, ai_score: float, now: dt.datetime):

        try:
            st = self.symbols.setdefault(
                symbol,
                SymbolState(symbol=symbol),
            )

            st.last_summary_time = now
            st.summary_count += 1
            st.turnover = float(turnover)
            st.ai_score = float(ai_score)

        except Exception:
            logger.exception("dynamic_symbol_manager: summary update failed")

    # --------------------------------------------------------
    # 定期評価
    # --------------------------------------------------------
    def evaluate(self, now: dt.datetime, *, ats_free_slots: Optional[int] = None):

        try:
            # 時間帯別 threshold
            self.active_ai.threshold = get_active_ai_threshold(now)

            # ATSブレーキ
            if ats_free_slots is not None:
                active_limit = ats_free_slots * 1.2
                if len(self.get_active_symbols()) >= active_limit:
                    self.active_ai.threshold += 0.05

            for st in self.symbols.values():
                self._apply_ranking_decay(st, now)
                self._evaluate_transition(st, now)

        except Exception:
            logger.exception("active_symbol_manager: evaluate failed")

    # --------------------------------------------------------
    # ranking 減衰
    # --------------------------------------------------------
    def _apply_ranking_decay(self, st: SymbolState, now: dt.datetime):

        if not st.last_ranking_seen:
            return

        minutes = (now - st.last_ranking_seen).total_seconds() / 60.0
        decay = math.exp(-minutes / self.decay_tau_min)
        st.ranking_score *= decay

    # --------------------------------------------------------
    # 状態遷移評価
    # --------------------------------------------------------
    def _evaluate_transition(self, st: SymbolState, now: dt.datetime):

        # MONITOR → ACTIVE
        if st.status == STATE_MONITOR:
            if self._can_promote_to_active(st, now):
                self._transition(st, STATE_ACTIVE, now)

        # ACTIVE → COOLDOWN
        elif st.status == STATE_ACTIVE:
            if (
                st.ai_score is not None
                and st.ai_score < CFG["AI"]["MIN_AI_SCORE_ACTIVE"]
            ):
                self._transition(st, STATE_COOLDOWN, now)

        # COOLDOWN → ACTIVE
        elif st.status == STATE_COOLDOWN:
            if (
                st.ai_score is not None
                and st.ai_score >= CFG["AI"]["RECOVER_AI_SCORE"]
            ):
                self._transition(st, STATE_ACTIVE, now)

        # 完全除外
        if self._should_drop(st, now):
            self._transition(st, STATE_OUT, now)

    # --------------------------------------------------------
    # ACTIVE 昇格判定（AI）
    # --------------------------------------------------------
    def _can_promote_to_active(self, st: SymbolState, now: dt.datetime) -> bool:

        # ルールベース安全網
        if st.summary_count < CFG["SUMMARY"]["MIN_SUMMARY_COUNT_ACTIVE"]:
            return False

        if st.turnover < CFG["SUMMARY"]["MIN_TURNOVER_ACTIVE"]:
            return False

        if st.ai_score is None:
            return False

        # AI判定
        features = {
            "ranking_score": st.ranking_score,
            "summary_count": st.summary_count,
            "turnover": st.turnover,
            "ai_score": st.ai_score,
            "prev_state": 1,
            "new_state": 2,
            "side": 0,
        }

        prob = self.active_ai.predict_proba(features)
        allow = prob >= self.active_ai.threshold

        log_active_ai(
            symbol=st.symbol,
            prob=prob,
            allow=allow,
            ranking_score=st.ranking_score,
            summary_count=st.summary_count,
            turnover=st.turnover,
            ai_score=st.ai_score,
        )

        logger.info(
            f"[ACTIVE_AI] {st.symbol} prob={prob:.3f} "
            f"th={self.active_ai.threshold:.2f} "
            f"→ {'ALLOW' if allow else 'BLOCK'}"
        )

        return allow

    # --------------------------------------------------------
    # 完全除外判定
    # --------------------------------------------------------
    def _should_drop(self, st: SymbolState, now: dt.datetime) -> bool:

        if st.status == STATE_OUT:
            return False

        stale_summary = (
            st.last_summary_time is None
            or now - st.last_summary_time > CFG["SUMMARY"]["SUMMARY_STALE_LIMIT"]
        )

        ranking_dead = (
            st.ranking_score < CFG["RANKING"]["MIN_RANKING_SCORE_ALIVE"]
        )

        return stale_summary and ranking_dead

    # --------------------------------------------------------
    # 状態遷移
    # --------------------------------------------------------
    def _transition(self, st: SymbolState, new_status: str, now: dt.datetime):

        if st.status == new_status:
            return

        prev = st.status

        save_symbol_state_log(
            symbol=st.symbol,
            prev_state=prev,
            new_state=new_status,
            ranking_score=st.ranking_score,
            summary_count=st.summary_count,
            turnover=st.turnover,
            ai_score=st.ai_score,
        )

        logger.info(
            f"[SYMBOL_STATE] {st.symbol} {prev} → {new_status}"
        )

        st.status = new_status
        st.status_since = now

    # --------------------------------------------------------
    # 外部公開API
    # --------------------------------------------------------
    def get_monitor_symbols(self) -> List[str]:
        return [
            s.symbol
            for s in self.symbols.values()
            if s.status in (STATE_MONITOR, STATE_ACTIVE, STATE_COOLDOWN)
        ]

    def get_active_symbols(self) -> List[str]:
        return [
            s.symbol
            for s in self.symbols.values()
            if s.status == STATE_ACTIVE
        ]

    def dump_states(self) -> Dict[str, str]:
        return {
            s.symbol: s.status
            for s in self.symbols.values()
        }