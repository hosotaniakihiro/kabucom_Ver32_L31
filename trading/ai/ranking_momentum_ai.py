# ============================================================
# File   : trading/ai/ranking_momentum_ai.py
# Version: RANKING-MOMENTUM-AI-PRODUCTION-STABLE
# ------------------------------------------------------------
# ✔ ランキングモメンタムAI
# ✔ 資金流入検出
# ✔ ランキング急上昇
# ✔ 出来高モメンタム
# ✔ 持続性評価
# ✔ 価格変化率
# ✔ liquidity inflow
# ✔ NaN / None / inf 安全
# ✔ DataFrame API
# ✔ production safe
# ============================================================

from __future__ import annotations

import logging
import math
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# safe float
# ============================================================

def _safe(v):

    try:

        f = float(v)

        if not math.isfinite(f):
            return None

        return f

    except Exception:

        return None


# ============================================================
# ranking jump
# ============================================================

def _ranking_jump(row):

    rank = row.get("rank_position")
    prev = row.get("rank_prev")

    try:

        rank = int(rank)
        prev = int(prev)

    except Exception:

        return 0

    jump = prev - rank

    if jump >= 30:
        return 4

    if jump >= 20:
        return 3

    if jump >= 10:
        return 2

    if jump >= 5:
        return 1

    return 0


# ============================================================
# ranking persistence
# ============================================================

def _ranking_persistence(row):

    persistence = row.get("rank_persistence")

    try:

        p = int(persistence)

    except Exception:

        return 0

    if p >= 30:
        return 3

    if p >= 15:
        return 2

    if p >= 5:
        return 1

    return 0


# ============================================================
# volume momentum
# ============================================================

def _volume_momentum(row):

    vol = _safe(row.get("volume"))
    vol_avg = _safe(row.get("volume_avg"))

    if vol is None or vol_avg is None or vol_avg == 0:
        return 0

    ratio = vol / vol_avg

    if ratio >= 6:
        return 4

    if ratio >= 4:
        return 3

    if ratio >= 2:
        return 2

    if ratio >= 1.5:
        return 1

    return 0


# ============================================================
# price momentum
# ============================================================

def _price_momentum(row):

    change = _safe(row.get("change_rate"))

    if change is None:
        return 0

    if change >= 8:
        return 3

    if change >= 5:
        return 2

    if change >= 2:
        return 1

    return 0


# ============================================================
# volume speed
# ============================================================

def _volume_speed(row):

    speed = _safe(row.get("volume_speed"))

    if speed is None:
        return 0

    if speed >= 5:
        return 3

    if speed >= 3:
        return 2

    if speed >= 1.5:
        return 1

    return 0


# ============================================================
# liquidity inflow
# ============================================================

def _liquidity_inflow(row):

    vol = _safe(row.get("volume"))
    price = _safe(row.get("close_price"))

    if vol is None or price is None:
        return 0

    turnover = vol * price

    if turnover > 5e9:
        return 3

    if turnover > 2e9:
        return 2

    if turnover > 5e8:
        return 1

    return 0


# ============================================================
# compute ranking momentum score
# ============================================================

def compute_ranking_momentum_score(row):

    score = 0
    reasons = {}

    funcs = {

        "ranking_jump": _ranking_jump,
        "ranking_persistence": _ranking_persistence,
        "volume_momentum": _volume_momentum,
        "price_momentum": _price_momentum,
        "volume_speed": _volume_speed,
        "liquidity_inflow": _liquidity_inflow,

    }

    for name, func in funcs.items():

        try:

            s = func(row)

            if s:
                score += s
                reasons[name] = s

        except Exception:

            logger.exception(f"[RANKING AI] {name} failed")

    return score, reasons


# ============================================================
# signal classification
# ============================================================

def classify_ranking_signal(row):

    score, reasons = compute_ranking_momentum_score(row)

    if score >= 10:

        return "STRONG_MONEY_INFLOW", score, reasons

    if score >= 6:

        return "MONEY_INFLOW", score, reasons

    if score >= 3:

        return "WEAK_MOMENTUM", score, reasons

    return "NO_FLOW", score, reasons


# ============================================================
# DataFrame API
# ============================================================

def apply_ranking_momentum_ai(df: pd.DataFrame):

    if df is None or df.empty:
        return df

    try:

        df = df.copy()

        scores = []
        signals = []

        for row in df.to_dict("records"):

            signal, score, _ = classify_ranking_signal(row)

            scores.append(score)
            signals.append(signal)

        df["ranking_momentum_score"] = scores
        df["ranking_signal"] = signals

        logger.info(
            f"[RANKING AI] processed rows={len(df)}"
        )

        return df

    except Exception:

        logger.exception("[RANKING AI] failed")

        return df


# ============================================================
# compatibility API
# ============================================================

def calc_ranking_momentum_score(row: pd.Series) -> int:

    score = 0

    rank_gap = row.get("rank_gap_ratio", 0)

    try:

        rank_gap = float(rank_gap)

    except Exception:

        rank_gap = 0

    if rank_gap < -0.3:
        score += 3

    if rank_gap < -0.5:
        score += 5

    if rank_gap < -0.7:
        score += 7

    return score