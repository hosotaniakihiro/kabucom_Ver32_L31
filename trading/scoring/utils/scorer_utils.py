# ============================================================
# scoring/scorer_utils.py
# ------------------------------------------------------------
# ・スコア理由・ラベル系ユーティリティ
# ・本番耐性 / 後方互換 最優先
# ============================================================

from typing import Iterable, List


# ------------------------------------------------------------
# 🔧 unique_list（順序保持・重複除去）
# ------------------------------------------------------------
def unique_list(seq: Iterable) -> List:
    if not seq:
        return []
    seen = []
    for x in seq:
        if x not in seen:
            seen.append(x)
    return seen


# ------------------------------------------------------------
# 🔧 正規化（複数 → 表示用文字列）
# ------------------------------------------------------------
def normalize_reasons(reasons):
    """
    reasons を表示用の文字列に正規化する

    Parameters
    ----------
    reasons : list[str] | str | None

    Returns
    -------
    str
    """
    if not reasons:
        return "シグナルなし"

    if isinstance(reasons, str):
        return reasons

    try:
        return " / ".join(str(r) for r in reasons)
    except Exception:
        return str(reasons)


# ------------------------------------------------------------
# 🔧 後方互換（単数名）
# ------------------------------------------------------------
def normalize_reason(reasons):
    """
    ⚠ 後方互換用エイリアス
    score_attach.py など既存コード対応
    """
    return normalize_reasons(reasons)
