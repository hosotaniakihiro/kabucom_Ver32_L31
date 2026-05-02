# ============================================================
# File: AI/feature/ranking_session_features.py
# Version: Ver1.2-FINAL-RANKING-SESSION-FEATURES-CANONICAL
# ------------------------------------------------------------
# ranking_session_1min → AI feature 変換
#
# ✔ 最新セッションを1件取得（時系列安全）
# ✔ quality を安全に数値化（未知値耐性）
# ✔ 欠損・未生成セッション完全耐性
# ✔ ENTRY / EXIT / 学習 共通で使用可能
# ✔ READ ONLY（副作用ゼロ）
# ✔ DB / migrate / ranking_session_1min と完全整合
# ============================================================

from typing import Dict, Optional
from sqlalchemy import text

from database.session import ranking_engine

# ============================================================
# quality → 数値スコア変換（設計FIX）
# ============================================================

QUALITY_MAP = {
    "excellent": 1.0,
    "good": 0.7,
    "neutral": 0.4,
    "bad": 0.1,
}

# quality が未定義・未知の場合の保険値
DEFAULT_QUALITY_SCORE = 0.3


# ============================================================
# 最新 ranking_session を取得
# ============================================================

def fetch_latest_ranking_session(symbol: str) -> Optional[Dict]:
    """
    ranking_session_1min から
    指定 symbol の最新セッションを1件取得する

    - DB 未生成 / データ欠損時は None
    - 時系列は end_dt 正
    """
    sql = text("""
        SELECT *
        FROM ranking_session_1min
        WHERE symbol = :symbol
        ORDER BY end_dt DESC
        LIMIT 1
    """)

    with ranking_engine.begin() as conn:
        row = conn.execute(
            sql,
            {"symbol": symbol},
        ).fetchone()

    if not row:
        return None

    # Row → dict（SQLAlchemy Row 安全変換）
    return dict(row)


# ============================================================
# AI 用特徴量ビルド（正本）
# ============================================================

def build_ranking_session_features(symbol: str) -> Dict[str, float]:
    """
    ranking_session_1min を AI 用の数値特徴量に変換する

    - ENTRY / EXIT / 学習 共通
    - 欠損時は空 dict を返す（AI 側で自然無視）
    """
    row = fetch_latest_ranking_session(symbol)
    if not row:
        return {}

    # --------------------------------------------------------
    # quality を安全に数値化
    # --------------------------------------------------------
    quality_raw = (row.get("quality") or "").lower()
    quality_score = QUALITY_MAP.get(
        quality_raw,
        DEFAULT_QUALITY_SCORE,
    )

    # --------------------------------------------------------
    # AI feature（設計確定）
    # --------------------------------------------------------
    return {
        # --------------------
        # セッション構造
        # --------------------
        "ranking_session_minutes": row.get("minutes", 0),
        "ranking_session_rank_ret": row.get("rank_ret", 0.0),
        "ranking_session_rank_range": row.get("rank_range", 0.0),
        "ranking_session_rank_slope": row.get("rank_slope", 0.0),
        "ranking_session_rank_improve": row.get("rank_improve", 0),

        # --------------------
        # 価格・MA 関係
        # --------------------
        "ranking_session_d_ma25": row.get("d_ma25", 0.0),
        "ranking_session_d_ma75": row.get("d_ma75", 0.0),
        "ranking_session_d_vwap": row.get("d_vwap", 0.0),
        "ranking_session_d_close": row.get("d_close", 0.0),

        # --------------------
        # 品質（最重要）
        # --------------------
        "ranking_session_quality": quality_score,
    }


# ============================================================
# debug / standalone check（任意）
# ============================================================

if __name__ == "__main__":
    # 単体実行テスト（副作用なし）
    test_symbol = "7203"
    feats = build_ranking_session_features(test_symbol)
    print(f"[DEBUG] {test_symbol} ranking_session_features:")
    for k, v in feats.items():
        print(f"  {k}: {v}")