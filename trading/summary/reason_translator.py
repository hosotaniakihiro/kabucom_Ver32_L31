# ============================================================
# reason_translator.py（Ver23-FULL-FIXED-ROW-SCORE）
# ------------------------------------------------------------
# ・score_config.ini を参照せず、row 内の理由スコア辞書を使用
# ・format_reasons_with_score(reasons_str, score_dict) に統一
# ・summary_printer から呼ぶ際に score_dict を渡せば点数が反映
# ============================================================

from trading.scoring.utils.label_translations import LABEL_JA


# ------------------------------------------------------------
# 🔍 単一 reason → 日本語（＋点数）に変換
# ------------------------------------------------------------
def _translate_one(token: str, score_dict: dict) -> str:
    token = token.strip()
    if not token:
        return ""

    # 日本語ラベル（無い場合は英語）
    jp = LABEL_JA.get(token, token)

    # row 内のスコア辞書から点数を取得
    score = 0
    if score_dict and token in score_dict:
        score = score_dict[token]

    # スコアの表記
    if score > 0:
        return f"{jp}(+{score})"
    elif score < 0:
        return f"{jp}({score})"
    else:
        return jp


# ------------------------------------------------------------
# 🔥 英語理由列 → 日本語理由（＋点数）へ
# ------------------------------------------------------------
# ============================================================
# trading/summary/reason_translator.py
# ------------------------------------------------------------
# ✔ score_reasons(dict[str,int]) をそのまま表示
# ✔ score_labels 非依存
# ✔ 未定義理由も必ず表示
# ============================================================

def format_reasons_with_score(reason_text, reason_scores):
    """
    reason_scores: dict[str, int]
    """

    if not isinstance(reason_scores, dict) or not reason_scores:
        return ""

    parts = []
    for k, v in reason_scores.items():
        try:
            v = int(v)
        except Exception:
            continue
        sign = "+" if v > 0 else ""
        parts.append(f"{k}({sign}{v})")

    return " ".join(parts)
