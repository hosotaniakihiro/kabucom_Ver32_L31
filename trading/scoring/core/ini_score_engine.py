# ============================================================
# ini_score_engine.py
# ------------------------------------------------------------
# ・ini 定義をそのままスコア化
# ・条件列が True / 非0 の場合に加算
# ============================================================

def apply_ini_scores(row, score, reasons, score_table):
    """
    Parameters
    ----------
    row : pd.Series
    score : int
    reasons : dict
    score_table : dict (from ini)

    Returns
    -------
    score, reasons
    """

    for key, value in score_table.items():
        if key not in row:
            continue

        try:
            v = row.get(key)
            if v is None:
                continue

            # bool / 数値 / 0,1 対応
            active = False
            if isinstance(v, bool):
                active = v
            elif isinstance(v, (int, float)):
                active = v != 0
            else:
                active = bool(v)

            if active and value != 0:
                score += int(value)
                reasons[key.upper()] = reasons.get(key.upper(), 0) + int(value)

        except Exception:
            continue

    return score, reasons
