# ============================================================
# AI/tools/model_health_checker.py
# ------------------------------------------------------------
# ✔ ENTRY 履歴からモデル健全性を評価
# ✔ model_used 別 勝率 / confidence / dominant_ratio 集計
# ✔ 成績不良モデルを自動で無効化
# ✔ predict_mtf と完全連携
# ✔ 夜間バッチ実行前提
# ============================================================

import json
import sqlite3
from pathlib import Path
from typing import Dict
import logging

import pandas as pd

logger = logging.getLogger(__name__)

# ============================================================
# 設定
# ============================================================

DB_FILE = Path("AI/data/ai_entry_events.db")
DISABLED_MODEL_FILE = Path("AI/models/model_disabled.json")

# 判定基準（運用で調整）
MIN_TRADES = 30           # 最低サンプル数
MIN_WIN_RATE = 0.45       # 勝率下限
MIN_CONFIDENCE = 0.01     # confidence 下限
MIN_DOM_RATIO = 0.55      # dominant_ratio 下限


# ============================================================
# メイン
# ============================================================

def check_model_health(dry_run: bool = False) -> Dict[str, dict]:
    """
    モデル健全性チェック

    Returns:
        {
          model_path: {
            trades: int,
            win_rate: float,
            avg_confidence: float,
            avg_dom_ratio: float,
            disabled: bool,
          }
        }
    """

    if not DB_FILE.exists():
        logger.warning("entry_events DB not found")
        return {}

    df = _load_entry_events()
    if df.empty:
        return {}

    stats = _build_model_stats(df)
    disabled_models = _load_disabled_models()

    for model, s in stats.items():
        disable = _should_disable(s)

        if disable and model not in disabled_models:
            logger.warning(
                "[MODEL DISABLE] %s trades=%d win=%.2f conf=%.4f dom=%.2f",
                model,
                s["trades"],
                s["win_rate"],
                s["avg_confidence"],
                s["avg_dom_ratio"],
            )
            if not dry_run:
                disabled_models.add(model)

    if not dry_run:
        _save_disabled_models(disabled_models)

    return stats


# ============================================================
# 内部処理
# ============================================================

def _load_entry_events() -> pd.DataFrame:
    """
    ENTRYイベントを DataFrame 化
    """
    conn = sqlite3.connect(DB_FILE)
    df = pd.read_sql(
        """
        SELECT
            side,
            features_json
        FROM entry_events
        WHERE features_json IS NOT NULL
        """,
        conn,
    )
    conn.close()

    if df.empty:
        return df

    feats = df["features_json"].apply(json.loads)
    feat_df = pd.json_normalize(feats)

    df = pd.concat([df.drop(columns=["features_json"]), feat_df], axis=1)

    return df


def _build_model_stats(df: pd.DataFrame) -> Dict[str, dict]:
    """
    model_used 別に統計生成
    """

    if "model_used" not in df.columns:
        return {}

    df = df.dropna(subset=["model_used"])

    results: Dict[str, dict] = {}

    for model, g in df.groupby("model_used"):
        trades = len(g)
        if trades == 0:
            continue

        # 勝敗判定（将来 HOLDTIME 等に差し替え可）
        wins = g["confidence"].fillna(0) > 0
        win_rate = wins.mean()

        results[model] = {
            "trades": trades,
            "win_rate": float(win_rate),
            "avg_confidence": float(g.get("confidence", 0).mean()),
            "avg_dom_ratio": float(g.get("dominant_ratio", 0).mean()),
        }

    return results


def _should_disable(stat: dict) -> bool:
    """
    無効化判定
    """
    if stat["trades"] < MIN_TRADES:
        return False  # データ不足は様子見

    if stat["win_rate"] < MIN_WIN_RATE:
        return True

    if stat["avg_confidence"] < MIN_CONFIDENCE:
        return True

    if stat["avg_dom_ratio"] < MIN_DOM_RATIO:
        return True

    return False


def _load_disabled_models() -> set[str]:
    """
    無効モデル一覧ロード
    """
    if not DISABLED_MODEL_FILE.exists():
        return set()

    try:
        with open(DISABLED_MODEL_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return set(data.get("disabled_models", []))
    except Exception:
        return set()


def _save_disabled_models(models: set[str]):
    """
    無効モデル保存
    """
    DISABLED_MODEL_FILE.parent.mkdir(parents=True, exist_ok=True)

    with open(DISABLED_MODEL_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {"disabled_models": sorted(models)},
            f,
            indent=2,
            ensure_ascii=False,
        )


# ============================================================
# CLI 実行用
# ============================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    stats = check_model_health(dry_run=False)

    for model, s in stats.items():
        logger.info(
            "[MODEL] %s trades=%d win=%.2f conf=%.4f dom=%.2f",
            model,
            s["trades"],
            s["win_rate"],
            s["avg_confidence"],
            s["avg_dom_ratio"],
        )
