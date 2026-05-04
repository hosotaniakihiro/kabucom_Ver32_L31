# ============================================================
# scoring/core/decision.py
# Ver10.1-FINAL-INSTANT-ENTRY-INIFLAG
# ------------------------------------------------------------
# ・score_total のみで ENTRY 判定（即発火モード）
# ・BUY / SELL / NONE の最終責務
# ・scoring_core / realtime / AI学習 共通
# ・AI は一切使用しない
# ・score_config.ini 駆動（既存互換）
# ============================================================

from configparser import ConfigParser
import math
from typing import Tuple, Dict


# ============================================================
# 🔧 score_config.ini 読み込み（安全）
# ============================================================

_conf = ConfigParser()
_conf.read("score_config.ini", encoding="utf-8")

# 既存互換：INI が無くても動く
BUY_THRESHOLD = _conf.getfloat("trade", "threshold", fallback=5)
SELL_THRESHOLD = _conf.getfloat("trade", "sell_threshold", fallback=-5)

# 即発火モード（INI で制御可能）
INSTANT_ENTRY_ENABLED = _conf.getboolean(
    "trade",
    "instant_entry_enabled",
    fallback=True,
)


# ============================================================
# 🔧 理由正規化（既存思想を壊さない）
# ============================================================
def _normalize_reasons(reasons) -> Dict[str, int]:
    """
    reasons:
        None / dict / str / list[str] / tuple[str]
    return:
        dict[str, int]
    """
    if not reasons:
        return {}

    if isinstance(reasons, dict):
        return dict(reasons)

    if isinstance(reasons, str):
        return {reasons.strip(): 1}

    if isinstance(reasons, (list, tuple)):
        out: Dict[str, int] = {}
        for r in reasons:
            if not r:
                continue
            k = str(r).strip()
            out[k] = out.get(k, 0) + 1
        return out

    return {str(reasons): 1}


# ============================================================
# 🔥 ENTRY 判定（唯一の正解・即発火）
# ============================================================
def decide_entry_from_score(
    *,
    score_total: float,
    volume: float | None = None,
    price: float | None = None,
    reasons: dict | None = None,
) -> Tuple[str, Dict[str, int]]:
    """
    即発火 ENTRY 判定（AI 不使用）

    Returns
    -------
    decision : str
        "BUY" / "SELL" / "NONE"
    reasons : dict[str, int]
        ENTRY に使われた理由（正規化済）
    """

    # --------------------------------------------------------
    # 流動性・価格の最低限ガード（既存思想）
    # --------------------------------------------------------
    try:
        if volume is None or price is None:
            return "NONE", {}

        if math.isnan(volume) or math.isnan(price):
            return "NONE", {}

        if volume <= 0 or price <= 0:
            return "NONE", {}
    except Exception:
        return "NONE", {}

    reasons = _normalize_reasons(reasons)

    # --------------------------------------------------------
    # 即発火 OFF（将来拡張・既存互換）
    # --------------------------------------------------------
    if not INSTANT_ENTRY_ENABLED:
        return "NONE", {}

    # --------------------------------------------------------
    # 🔥 即発火ロジック（核心）
    # --------------------------------------------------------
    if score_total >= BUY_THRESHOLD:
        out = reasons.copy()
        out["instant_score_buy"] = int(score_total)
        return "BUY", out

    if score_total <= SELL_THRESHOLD:
        out = reasons.copy()
        out["instant_score_sell"] = int(score_total)
        return "SELL", out

    return "NONE", {}


# ============================================================
# 方向バイアス（既存互換・未使用でも保持）
# ============================================================
def get_direction_bias(row):
    buy = 0
    sell = 0
    reasons = {"buy": None, "sell": None}

    def _b(v):
        return bool(v) if v is not None else False

    if _b(row.get("dir_up")):
        buy += 2
        reasons["buy"] = "MA上向き"

    if _b(row.get("dir_down")):
        sell -= 2
        reasons["sell"] = "MA下向き"

    return {"buy": buy, "sell": sell}, reasons
