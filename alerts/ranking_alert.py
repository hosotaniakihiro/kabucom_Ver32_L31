# ============================================================
# File   : alerts/ranking_alert.py
# Version: Ver1.0-RANKING-DISCORD-ALERT
# ------------------------------------------------------------
# ✔ ranking alert
# ✔ 日本語項目
# ✔ 銘柄名表示
# ✔ ranking種別表示
# ✔ Discord embed
# ✔ production safe
# ============================================================

import logging

from utils.alerts_util import send_discord_message

logger = logging.getLogger(__name__)


def send_ranking_alert(row, alert_title):

    try:

        symbol = str(row.get("symbol", ""))
        symbolname = str(row.get("symbolname", ""))

        name = f"{symbol} {symbolname}"

        price = row.get("price")
        score = row.get("ranking_score")

        ranking_type = row.get("ranking_type", "不明")
        rank = row.get("rank")

        volume_ratio = row.get("volume_ratio")
        velocity = row.get("velocity_score")

        embed = {
            "title": alert_title,
            "color": 15158332,
            "fields": [
                {
                    "name": "銘柄",
                    "value": name,
                    "inline": False
                },
                {
                    "name": "価格",
                    "value": str(price),
                    "inline": True
                },
                {
                    "name": "スコア",
                    "value": str(round(score,2)) if score else "-",
                    "inline": True
                },
                {
                    "name": "ランキング",
                    "value": str(ranking_type),
                    "inline": True
                },
                {
                    "name": "順位",
                    "value": f"{rank}位" if rank else "-",
                    "inline": True
                },
                {
                    "name": "出来高倍率",
                    "value": str(round(volume_ratio,2)) if volume_ratio else "-",
                    "inline": True
                },
                {
                    "name": "速度",
                    "value": str(velocity) if velocity else "-",
                    "inline": True
                },
            ],
        }

        send_discord_message(embeds=[embed])

    except Exception:

        logger.exception("[ranking_alert] failed")