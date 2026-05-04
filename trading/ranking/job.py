# ============================================================
# pj/trading/ranking/job.py
# Ver30.6-RANKING-JOB-TONOSAMA-CLUSTER-AI-MARKETSAFE-DIFF-GUARDED
# Updated: 2026-01-28
# ------------------------------------------------------------
# ✔ 銘柄クラスタリング対応
# ✔ クラスタ別閾値制御（Optuna / KPI 連動）
# ✔ 銘柄別 AI 閾値（symbol_ai_threshold.csv）
# ✔ market_crash_ai による地合い崩壊時の完全停止
# ✔ HOLD 秒数 AI を ENTRY 前に確定
# ✔ PUSH 非依存・裁量ゼロ・完全自動
# ✔ ★ cluster_params=None でも絶対に落ちない
# ✔ ★ ranking_diff_update を job 層で適用
# ✔ ★ open_positions / entry_inflight Guarded 対応
# ============================================================

import logging
import datetime as dt
from pathlib import Path
import pandas as pd

from global_state import global_data

# ------------------------------------------------------------
# 🚨 地合いクラッシュ判定（最優先）
# ------------------------------------------------------------
from trading.market.market_crash_ai import is_market_danger

# ------------------------------------------------------------
# 🔥 初動火力
# ------------------------------------------------------------
from trading.entry.ignition.five_sec import calc_fast_ret

# ------------------------------------------------------------
# 🧠 殿様イナゴ AI
# ------------------------------------------------------------
from trading.entry.ignition.ai_boost import infer_tonosama_entry
from trading.entry.tonosama_ai import tonosama_judge

# ------------------------------------------------------------
# ⏱ HOLD 秒数 AI
# ------------------------------------------------------------
from trading.entry.ignition.holdtime_ai import predict_hold_seconds

# ------------------------------------------------------------
# 📌 ENTRY 登録
# ------------------------------------------------------------
from trading.ranking.ranking_trigger import trigger_ranking_entry
from trading.entry.tonosama_watch_manager import (
    update_watch,
    cleanup_watch,
)

# ------------------------------------------------------------
# 📊 ランキング差分
# ------------------------------------------------------------
from trading.ranking.diff_updater import ranking_diff_update

logger = logging.getLogger(__name__)

# ============================================================
# クラスタ別デフォルト閾値（安全フォールバック）
# ============================================================
DEFAULT_CLUSTER_PARAMS = {
    0: {"volume_speed": 4000, "fast_ret": 0.20, "ai_confidence": 0.78},
    1: {"volume_speed": 8000, "fast_ret": 0.18, "ai_confidence": 0.70},
    2: {"volume_speed": 5000, "fast_ret": 0.15, "ai_confidence": 0.65},
    3: {"volume_speed": 6000, "fast_ret": 0.18, "ai_confidence": 0.72},
    4: {"disable": True},
    5: {"volume_speed": 3000, "fast_ret": 0.25, "ai_confidence": 0.75},
}

COOLDOWN_SEC = 180
_last_entry_time = {}

# ============================================================
# 銘柄別 AI 閾値（起動時ロード）
# ============================================================
SYMBOL_TH_PATH = Path("AI/config/symbol_ai_threshold.csv")
_symbol_ai_threshold = {}

if SYMBOL_TH_PATH.exists():
    try:
        df_th = pd.read_csv(SYMBOL_TH_PATH)
        _symbol_ai_threshold = {
            str(r.symbol): float(r.ai_threshold)
            for r in df_th.itertuples()
        }
        logger.info(
            "[AI TH] symbol threshold loaded count=%d",
            len(_symbol_ai_threshold),
        )
    except Exception as e:
        logger.warning("[AI TH] load failed: %s", e)


# ============================================================
# クラスタ別パラメータ取得（None-SAFE）
# ============================================================
def _get_cluster_params(cluster_id):
    cp = getattr(global_data, "cluster_params", None)
    if isinstance(cp, dict):
        params = cp.get(cluster_id)
        if isinstance(params, dict):
            return params
    return DEFAULT_CLUSTER_PARAMS.get(cluster_id, {})


# ============================================================
# メイン：ランキング → TONOSAMA ENTRY
# ============================================================
def run_ranking_job(global_data):

    # --------------------------------------------------------
    # 🚨 地合い崩壊時は完全停止
    # --------------------------------------------------------
    if is_market_danger(global_data):
        logger.warning("🚨 MARKET DANGER → TONOSAMA ENTRY STOP")
        return

    latest = getattr(global_data, "latest_ranking", None)
    prev = getattr(global_data, "prev_ranking", None)

    if not isinstance(latest, dict) or not latest:
        return

    now = dt.datetime.now()
    market_open = now.replace(hour=9, minute=0, second=0, microsecond=0)

    # --------------------------------------------------------
    # ★ Guarded 参照（重要）
    # --------------------------------------------------------
    open_positions = global_data.open_positions
    if not isinstance(open_positions, dict):
        logger.error("[RANK JOB] open_positions is not dict → abort")
        return

    entry_inflight = global_data.entry_inflight
    if not isinstance(entry_inflight, set):
        logger.error("[RANK JOB] entry_inflight is not set → abort")
        return

    added = 0

    # ========================================================
    # ★ ランキング差分付与（job 層の責務）
    # ========================================================
    latest_with_diff = {}

    for key, df_now in latest.items():
        if df_now is None or df_now.empty:
            continue

        df_prev = prev.get(key) if isinstance(prev, dict) else None
        df_now = ranking_diff_update(df_now, df_prev)
        latest_with_diff[key] = df_now

    # 次回用に保存
    global_data.prev_ranking = latest
    global_data.latest_ranking = latest_with_diff

    # ========================================================
    # ENTRY ロジック（差分は参考情報）
    # ========================================================
    for key, df in latest_with_diff.items():
        if df is None or df.empty:
            continue

        for _, r in df.iterrows():

            symbol = str(r.get("symbol"))
            symbolname = r.get("symbolname", "")
            volume_speed = float(r.get("volume_speed") or 0.0)
            price = float(r.get("current_price") or 0.0)

            # クールダウン
            last = _last_entry_time.get(symbol)
            if last and (now - last).total_seconds() < COOLDOWN_SEC:
                continue

            # 保有・発注中は除外
            if symbol in open_positions or symbol in entry_inflight:
                continue

            # クラスタ
            cluster_id = getattr(global_data, "symbol_cluster", {}).get(symbol, -1)
            params = _get_cluster_params(cluster_id)
            if params.get("disable"):
                continue

            VOL_TH = float(params.get("volume_speed", 5000))
            FAST_RET_TH = float(params.get("fast_ret", 0.20))
            AI_TH = float(params.get("ai_confidence", 0.72))

            if volume_speed < VOL_TH:
                continue

            fast_ret = calc_fast_ret(symbol, price)
            if fast_ret < FAST_RET_TH:
                continue

            ai_result = infer_tonosama_entry(
                symbol=symbol,
                fast_ret=fast_ret,
                volume_speed=volume_speed,
            )
            if not isinstance(ai_result, dict) or not ai_result.get("ok"):
                continue

            ai_conf = float(ai_result.get("ai_confidence", 0.0))
            symbol_th = _symbol_ai_threshold.get(symbol, AI_TH)
            if ai_conf < symbol_th:
                continue

            features = {
                "rank_strength": r.get("rank_strength", 1),
                "volume_speed": volume_speed,
                "price_change_1m": r.get("price_change_1m", 0.0),
                "spread": r.get("spread", 0.0),
                "atr_ratio": r.get("atr_ratio", 0.0),
                "time_from_open": int((now - market_open).total_seconds()),
                "is_limit_near": r.get("is_limit_near", 0),
                "cluster_id": cluster_id,
            }

            ai_prob = float(tonosama_judge(features))
            if ai_prob < AI_TH:
                continue

            hold_sec = predict_hold_seconds({
                "volume_speed": volume_speed,
                "fast_ret": fast_ret,
                "rank_position": features["rank_strength"],
                "price": price,
                "spread": features["spread"],
                "entry_second": now.second,
            })

            trigger_ranking_entry(
                symbol=symbol,
                symbolname=symbolname,
                type_name=key,
                ranking_strength=features["rank_strength"],
                volume_speed=volume_speed,
                market=r.get("market", "ALL"),
                reason=(
                    f"TONOSAMA C{cluster_id} "
                    f"ai={ai_conf:.2f}/{symbol_th:.2f} "
                    f"ret={fast_ret:.2f}% "
                    f"hold={hold_sec}s"
                ),
                extra={
                    "cluster_id": cluster_id,
                    "hold_limit_sec": hold_sec,
                    "ai_confidence": ai_conf,
                    "rank_diff": r.get("rank_diff"),
                    "is_new_rank": r.get("is_new"),
                },
            )

            _last_entry_time[symbol] = now
            added += 1

            logger.info(
                "👑 TONOSAMA %s C%d AI=%.2f/%.2f HOLD=%ss",
                symbol,
                cluster_id,
                ai_conf,
                symbol_th,
                hold_sec,
            )

    if added:
        logger.info("[RANK JOB] tonosama_added=%d", added)
