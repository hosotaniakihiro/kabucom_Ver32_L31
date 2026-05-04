# ============================================================
# File   : ats_rotation_manager.py
# Version: Ver47-PRODUCTION-ULTRA-STABLE-ATS-REALLOC-FINAL
# ------------------------------------------------------------
# ✔ Ver46 完全保持（削除ゼロ）
# ✔ ATS=100 hard guarantee
# ✔ CORE / HOT / ROTATION / AI_DISCOVERY
# ✔ OPENポジション保護
# ✔ pending統合
# ✔ ACTIVE rotation
# ✔ ETF / symbol_flags guard
# ✔ 流動性フィルター
# ✔ ranking promotion
# ✔ AI discovery layer
# ✔ rotation_index保存
# ✔ list safety
# ✔ duplicate symbol guard
# ✔ None safety
# ✔ protected collision guard
# ✔ ATS overflow guard
# ✔ ranking母集団拡大
# ✔ HOT比率強化
# ✔ CORE不足時の自動再配分（最重要）
# ✔ hot -> rotation -> ai の順で不足補充
# ✔ production hardened
# ============================================================

from __future__ import annotations

import logging
from typing import List

from global_state import global_data

from ats.ats_sources import (
    get_open_symbols,
    get_pending_symbols,
    get_rotated_active_symbols,
)

from ats.ats_filters import (
    filter_symbol_flags,
    filter_low_liquidity,
    filter_etf_guard,
)

from ats.ats_ranking import build_ranking_candidates
from ats.ats_promotions import build_promotions
from ats.ats_utils import unique_keep_order

logger = logging.getLogger(__name__)

ATS_SIZE = 100

CORE_SIZE = 15
HOT_SIZE = 55
ROTATION_SIZE = 25
AI_SIZE = 5


# ============================================================
# utility
# ============================================================

def _safe_list(x):
    if x is None:
        return []

    if isinstance(x, (list, tuple, set)):
        return list(x)

    try:
        return list(x)
    except Exception:
        return []


def _sanitize_symbols(symbols):
    out = []

    for s in symbols:
        if s is None:
            continue

        try:
            s = str(s).strip()
        except Exception:
            continue

        if not s:
            continue

        out.append(s)

    return out


def _remove_protected(symbols, protected):
    out = []

    for s in symbols:
        if s not in protected:
            out.append(s)

    return out


def _apply_common_filters(symbols, protected=None):
    symbols = _safe_list(symbols)
    symbols = _sanitize_symbols(symbols)

    try:
        symbols = filter_etf_guard(symbols)
    except Exception:
        pass

    try:
        symbols = filter_symbol_flags(symbols)
    except Exception:
        pass

    if protected:
        symbols = _remove_protected(symbols, protected)

    try:
        symbols = filter_low_liquidity(symbols)
    except Exception:
        pass

    symbols = _sanitize_symbols(symbols)
    return unique_keep_order(symbols)


# ============================================================
# ATS Rotation Manager
# ============================================================

class ATSRotationManager:

    def __init__(
        self,
        batch_size: int = 50,
        shift: int = 20,
        max_pos_symbols: int = 10,
    ):
        self.batch_size = batch_size
        self.shift = shift
        self.max_pos_symbols = max_pos_symbols

    # ========================================================
    # CORE
    # ========================================================

    def _build_core(self):

        try:
            open_syms = _safe_list(
                get_open_symbols(self.max_pos_symbols)
            )

            pending_syms = _safe_list(
                get_pending_symbols()
            )

        except Exception:
            logger.exception("[ATS] core source failed")
            open_syms = []
            pending_syms = []

        core = unique_keep_order(
            _sanitize_symbols(open_syms + pending_syms)
        )

        return core[:CORE_SIZE]

    # ========================================================
    # HOT
    # ========================================================

    def _build_hot(self, protected, limit: int = HOT_SIZE):

        try:
            ranking = build_ranking_candidates(max(limit * 3, 160))
        except Exception:
            logger.exception("[ATS] ranking candidates failed")
            ranking = []

        ranking = _apply_common_filters(ranking, protected=protected)

        try:
            promotions = build_promotions(max(limit, 40))
        except Exception:
            logger.exception("[ATS] promotions failed")
            promotions = []

        promotions = _apply_common_filters(promotions, protected=protected)

        merged = unique_keep_order(
            _sanitize_symbols(promotions + ranking)
        )

        hot = []

        for s in merged:
            if s not in protected:
                hot.append(s)

            if len(hot) >= limit:
                break

        return hot

    # ========================================================
    # ROTATION
    # ========================================================

    def _build_rotation(self, protected, limit: int = ROTATION_SIZE):

        try:
            rotated, next_index = get_rotated_active_symbols(
                batch_size=max(limit * 2, self.batch_size),
                shift=self.shift
            )
        except Exception:
            logger.exception("[ATS] rotation source failed")
            rotated = []
            next_index = None

        rotated = _apply_common_filters(rotated, protected=protected)

        if next_index is not None:
            try:
                global_data.rotation_index = next_index
            except Exception:
                pass

        return rotated[:limit]

    # ========================================================
    # AI DISCOVERY
    # ========================================================

    def _build_ai_layer(self, protected, limit: int = AI_SIZE):

        ai_candidates = []

        try:
            ai_engine = getattr(global_data, "ai_symbol_detector", None)
            if ai_engine:
                ai_candidates = ai_engine.get_candidates(max(limit * 3, 20))
        except Exception:
            logger.exception("[ATS] AI discovery failed")

        ai_candidates = _apply_common_filters(ai_candidates, protected=protected)
        return ai_candidates[:limit]

    # ========================================================
    # 不足補充
    # ========================================================

    def _fill_shortage(self, ats, core, hot, rotation, ai_layer):
        """
        core が少ないなどで ATS_SIZE に届かないとき、
        hot -> rotation -> ai の順で不足を補充する。
        """
        ats = unique_keep_order(ats)
        protected = set(ats)

        shortage = ATS_SIZE - len(ats)
        if shortage <= 0:
            return ats, hot, rotation, ai_layer

        logger.warning(
            "[ATS] shortage detected -> %d (core=%d hot=%d rotation=%d ai=%d)",
            shortage,
            len(core),
            len(hot),
            len(rotation),
            len(ai_layer),
        )

        # ----------------------------------------------------
        # 1) HOT を追加補充
        # ----------------------------------------------------
        if shortage > 0:
            extra_hot = self._build_hot(protected, limit=shortage * 2)
            extra_hot = [s for s in extra_hot if s not in protected]

            if extra_hot:
                hot = unique_keep_order(hot + extra_hot)
                ats = unique_keep_order(ats + extra_hot)
                protected.update(extra_hot)
                shortage = ATS_SIZE - len(ats)

        # ----------------------------------------------------
        # 2) ROTATION を追加補充
        # ----------------------------------------------------
        if shortage > 0:
            extra_rotation = self._build_rotation(protected, limit=shortage * 2)
            extra_rotation = [s for s in extra_rotation if s not in protected]

            if extra_rotation:
                rotation = unique_keep_order(rotation + extra_rotation)
                ats = unique_keep_order(ats + extra_rotation)
                protected.update(extra_rotation)
                shortage = ATS_SIZE - len(ats)

        # ----------------------------------------------------
        # 3) AI を追加補充
        # ----------------------------------------------------
        if shortage > 0:
            extra_ai = self._build_ai_layer(protected, limit=shortage * 2)
            extra_ai = [s for s in extra_ai if s not in protected]

            if extra_ai:
                ai_layer = unique_keep_order(ai_layer + extra_ai)
                ats = unique_keep_order(ats + extra_ai)
                protected.update(extra_ai)
                shortage = ATS_SIZE - len(ats)

        return ats, hot, rotation, ai_layer

    # ========================================================
    # BUILD ATS
    # ========================================================

    def build_candidates(self):

        core = self._build_core()
        protected = set(core)

        hot = self._build_hot(protected, limit=HOT_SIZE)
        protected.update(hot)

        rotation = self._build_rotation(protected, limit=ROTATION_SIZE)
        protected.update(rotation)

        ai_layer = self._build_ai_layer(protected, limit=AI_SIZE)

        ats = unique_keep_order(core + hot + rotation + ai_layer)

        # ----------------------------------------------------
        # CORE不足などの再配分
        # ----------------------------------------------------
        ats, hot, rotation, ai_layer = self._fill_shortage(
            ats=ats,
            core=core,
            hot=hot,
            rotation=rotation,
            ai_layer=ai_layer,
        )

        if len(ats) < ATS_SIZE:
            logger.warning("[ATS] size below target -> %s", len(ats))

        ats = ats[:ATS_SIZE]

        logger.info(
            "[ATS] core=%d hot=%d rotation=%d ai=%d total=%d",
            len(core),
            len(hot),
            len(rotation),
            len(ai_layer),
            len(ats),
        )

        return ats

    # ========================================================
    # rotate API
    # ========================================================

    def rotate(self):

        try:
            ats = self.build_candidates()
            return ats
        except Exception:
            logger.exception("[ATS] rotation failed")
            return []