# ============================================================
# File   : alerts/discord_alert_engine.py
# Version: Ver2.0-PRO-TRADING-ALERT-ENGINE
# ------------------------------------------------------------
# ✔ TONOSAMA / ORDERFLOW / IGNITION / SMARTMONEY / BIGPLAYER
# ✔ 日本語表示
# ✔ ranking情報
# ✔ embed通知
# ✔ duplicate alert 防止
# ✔ production safe
# ============================================================

import logging
import time

from utils.alerts_util import send_discord_message

logger = logging.getLogger(__name__)


# ============================================================
# alert cache（重複防止）
# ============================================================

_alert_cache = {}

ALERT_COOLDOWN = 60


# ============================================================
# duplicate guard
# ============================================================

def _allow_alert(key: str):

    now = time.time()

    last = _alert_cache.get(key)

    if last and now - last < ALERT_COOLDOWN:
        return False

    _alert_cache[key] = now

    return True


# ============================================================
# embed builder
# ============================================================

def _build_embed(title, color, fields):

    return {
        "title": title,
        "color": color,
        "fields": fields
    }


# ============================================================
# alert sender
# ============================================================

def send_symbol_alert(
    alert_type: str,
    symbol: str,
    symbolname: str = "",
    price=None,
    score=None,
    ranking_type=None,
    rank=None,
    volume_ratio=None,
    velocity=None,
    spread=None,
):

    try:

        if not symbol:
            return

        key = f"{alert_type}_{symbol}"

        if not _allow_alert(key):
            return

        name = f"{symbol} {symbolname}" if symbolname else symbol

        fields = [
            {
                "name": "銘柄",
                "value": name,
                "inline": False
            }
        ]

        if price is not None:
            fields.append(
                {"name": "価格", "value": str(price), "inline": True}
            )

        if ranking_type:
            if rank:
                ranking_text = f"{ranking_type} ({rank}位)"
            else:
                ranking_text = str(ranking_type)

            fields.append(
                {"name": "ランキング", "value": ranking_text, "inline": True}
            )

        if score is not None:
            fields.append(
                {"name": "スコア", "value": str(round(score,2)), "inline": True}
            )

        if volume_ratio is not None:
            fields.append(
                {"name": "出来高倍率", "value": str(round(volume_ratio,2)), "inline": True}
            )

        if velocity is not None:
            fields.append(
                {"name": "速度", "value": str(velocity), "inline": True}
            )

        if spread is not None:
            fields.append(
                {"name": "スプレッド", "value": str(round(spread,3)), "inline": True}
            )

        # ------------------------------------------------
        # alert type
        # ------------------------------------------------

        title_map = {
            "TONOSAMA": "👑 殿様イナゴ検出",
            "ORDERFLOW": "🚨 オーダーフロー異常",
            "IGNITION": "🔥 点火シグナル",
            "SMARTMONEY": "💰 スマートマネー流入",
            "BIGPLAYER": "🐋 大口参加",
        }

        color_map = {
            "TONOSAMA": 15844367,
            "ORDERFLOW": 15158332,
            "IGNITION": 15105570,
            "SMARTMONEY": 3066993,
            "BIGPLAYER": 3447003,
        }

        embed = _build_embed(
            title_map.get(alert_type, alert_type),
            color_map.get(alert_type, 15158332),
            fields
        )

        send_discord_message(embeds=[embed])

    except Exception:

        logger.exception("[discord_alert_engine] send failed")