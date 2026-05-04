# ============================================================
# AI/master_ai.py
# (Ver26-FINAL-MASTER-AI)
# ------------------------------------------------------------
# ✔ BUY / SELL / NO-TRADE を統合
# ✔ indicator_ready を最上位ゲートに
# ✔ ENTRY / EXIT / AI 学習 共通判断
# ============================================================

class MasterAI:
    def __init__(self, model):
        """
        model:
          predict_proba(X) -> [p_no, p_buy, p_sell]
        """
        self.model = model

    def decide(self, X, indicator_ready: bool) -> str:
        """
        return: BUY / SELL / NO-TRADE
        """

        # ----------------------------------------------------
        # 🔒 最上位ゲート：指標未完成
        # ----------------------------------------------------
        if not indicator_ready:
            return "NO-TRADE"

        # ----------------------------------------------------
        # AI 確率判定
        # ----------------------------------------------------
        p_no, p_buy, p_sell = self.model.predict_proba(X)[0]

        # NO-TRADE 優先
        if p_no >= 0.5:
            return "NO-TRADE"

        return "BUY" if p_buy > p_sell else "SELL"
