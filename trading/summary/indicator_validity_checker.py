# ============================================================
# indicator_validity_checker.py
# FINAL-INDICATOR-VALIDITY-GUARD
# ------------------------------------------------------------
# ✔ MA25 / MA75 / VWAP の成立を保証
# ✔ symbol 単位で検証
# ✔ 未成立理由を明示ログ
# ✔ ENTRY / EXIT / DISPLAY 前に使用
# ============================================================

import logging
import pandas as pd

logger = logging.getLogger(__name__)

MA25 = 25
MA75 = 75


# ============================================================
def check_indicator_validity(
    df: pd.DataFrame,
    *,
    require_ma25: bool = True,
    require_ma75: bool = True,
    require_vwap: bool = True,
) -> dict:
    """
    指標成立チェック

    Returns
    -------
    dict:
        {
          "ok": bool,
          "invalid_symbols": dict[symbol, list[str]],
          "summary": dict[str, int],
        }
    """

    if df is None or df.empty:
        return {
            "ok": False,
            "invalid_symbols": {},
            "summary": {"empty_df": 1},
        }

    invalid: dict[str, list[str]] = {}

    for symbol, g in df.groupby("symbol"):
        reasons = []

        # ---------------- MA25
        if require_ma25:
            if "ma25" not in g.columns:
                reasons.append("ma25_missing")
            else:
                valid = g["ma25"].dropna()
                if len(valid) < MA25:
                    reasons.append(f"ma25_insufficient({len(valid)})")

        # ---------------- MA75
        if require_ma75:
            if "ma75" not in g.columns:
                reasons.append("ma75_missing")
            else:
                valid = g["ma75"].dropna()
                if len(valid) < MA75:
                    reasons.append(f"ma75_insufficient({len(valid)})")

        # ---------------- VWAP
        if require_vwap:
            if "vwap" not in g.columns:
                reasons.append("vwap_missing")
            else:
                if g["vwap"].dropna().empty:
                    reasons.append("vwap_empty")

        if reasons:
            invalid[str(symbol)] = reasons

    ok = len(invalid) == 0

    if not ok:
        for sym, rs in invalid.items():
            logger.warning(
                "[INDICATOR INVALID] %s → %s",
                sym,
                ",".join(rs),
            )

    return {
        "ok": ok,
        "invalid_symbols": invalid,
        "summary": {
            "symbols_total": df["symbol"].nunique(),
            "symbols_invalid": len(invalid),
        },
    }
