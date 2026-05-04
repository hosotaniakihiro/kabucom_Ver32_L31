# trading/summary/discord_embed.py

import logging
from datetime import datetime
from utils.alerts_util import safe_discord_post, get_webhook_url
from trading.utils_summary_format import format_reasons  # ✅ 共通関数を利用

logger = logging.getLogger(__name__)


def send_discord_signals_embed(summary_results, webhook_url: str = None):
    """
    サマリー結果から BUY/SELL シグナルが出た銘柄のみを Discord にまとめて送信する
    - summary_results: scheduler から渡される [{...}, {...}, ...] のリスト
    """
    if webhook_url is None:
        webhook_url = get_webhook_url()
    if not webhook_url:
        logger.error("❌ Webhook URL 未設定")
        return

    embed_fields = []

    for res in summary_results:
        buy_score = res.get("buy_score", 0)
        sell_score = res.get("sell_score", 0)

        # シグナルがない銘柄はスキップ
        if buy_score == 0 and sell_score == 0:
            continue

        name = res.get("name", "不明")
        symbol = res.get("symbol", "")
        close = res.get("close", "N/A")

        # BUY / SELL の理由をフォーマット
        buy_reasons = format_reasons(res.get("buy_reasons", []), side="BUY")
        sell_reasons = format_reasons(res.get("sell_reasons", []), side="SELL")

        # BUY/SELL カラー＆アイコン
        if buy_score > 0 and buy_score >= abs(sell_score):
            icon = "🟢"
            color = 0x2ecc71
            score_text = f"BUY {buy_score:.0f}"
            reasons_text = buy_reasons
        elif sell_score < 0 and abs(sell_score) > buy_score:
            icon = "🔴"
            color = 0xe74c3c
            score_text = f"SELL {sell_score:.0f}"
            reasons_text = sell_reasons
        else:
            icon = "⚪"
            color = 0x95a5a6
            score_text = "No Signal"
            reasons_text = "なし"

        # --- Embedフィールド作成 ---
        embed_fields.append({
            "name": f"{icon} {name} ({symbol})",
            "value": (
                f"終値: {close}\n"
                f"スコア: **{score_text}**\n"
                f"理由: {reasons_text}"
            ),
            "inline": False,
        })

    # シグナルが無ければ通知しない
    if not embed_fields:
        logger.info("📭 Discord通知対象のシグナル銘柄なし")
        return

    embed = {
        "title": "📊 サマリーシグナル検出 (5分足)",
        "description": f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} 時点",
        "color": 0x3498db,
        "fields": embed_fields,
    }

    payload = {"embeds": [embed]}
    safe_discord_post(webhook_url, payload)
    logger.info("📤 Discord通知送信: サマリーシグナル")
