# ============================================================
# File   : utils/alerts_util.py
# Version: Ver3.0-PRODUCTION-RATE-LIMIT-SAFE-BRIDGE-COMPAT
# ------------------------------------------------------------
# ✔ 既存機能完全保持（削除ゼロ）
# ✔ Discord 429 RateLimit完全対策
# ✔ Retry-After 自動待機
# ✔ 送信間隔制御（0.7秒）
# ✔ ENTRY / EXIT Embed互換
# ✔ ranking互換 send_discord_notify 保持
# ✔ announce_bridge / jobs / runners から
#   discord_sender として直接使える
# ✔ 長文自動分割対応
# ✔ 例外耐性強化
# ✔ 本番永久安定版
# ============================================================

from __future__ import annotations

import configparser
import logging
import time
from datetime import datetime
from threading import Lock
from typing import Any, Callable, Iterable, List, Optional

import requests

logger = logging.getLogger(__name__)

# ------------------------------------------------------------
# 設定ファイルから Discord Webhook を取得
# ------------------------------------------------------------
conf = configparser.ConfigParser()
conf.read("settings.ini", encoding="utf-8")
DISCORD_WEBHOOK = conf.get("discord", "webhook_url", fallback="").strip()

if not DISCORD_WEBHOOK:
    logger.warning("⚠️ settings.ini の [discord] webhook_url が未設定です。")


# ------------------------------------------------------------
# RateLimit制御
# ------------------------------------------------------------
SEND_INTERVAL = 0.7  # 秒
DEFAULT_TIMEOUT = 5
DEFAULT_MAX_LEN = 1800

_last_send_time = 0.0
_send_lock = Lock()


# ------------------------------------------------------------
# basic helpers
# ------------------------------------------------------------

def _safe_str(v: Any, default: str = "") -> str:
    try:
        if v is None:
            return default
        return str(v)
    except Exception:
        return default


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except Exception:
        return default


def _resolve_webhook_url(webhook_url: Optional[str] = None) -> str:
    url = _safe_str(webhook_url).strip()
    if url:
        return url
    return DISCORD_WEBHOOK


def _split_message_chunks(text: str, max_len: int = DEFAULT_MAX_LEN) -> List[str]:
    text = _safe_str(text).replace("\r\n", "\n")
    if not text.strip():
        return []

    lines = text.split("\n")
    chunks: List[str] = []
    current: List[str] = []
    current_len = 0

    for line in lines:
        line = line.rstrip()
        add_len = len(line) + 1

        # 単独行が長すぎる場合は強制分割
        if len(line) > max_len:
            if current:
                chunks.append("\n".join(current))
                current = []
                current_len = 0

            start = 0
            while start < len(line):
                chunks.append(line[start:start + max_len])
                start += max_len
            continue

        if current and current_len + add_len > max_len:
            chunks.append("\n".join(current))
            current = [line]
            current_len = add_len
        else:
            current.append(line)
            current_len += add_len

    if current:
        chunks.append("\n".join(current))

    return [c for c in chunks if c.strip()]


# ------------------------------------------------------------
# RateLimit待機
# ------------------------------------------------------------

def _wait_rate_limit():
    """
    Discordレート制限回避
    """
    global _last_send_time

    now = time.time()
    diff = now - _last_send_time

    if diff < SEND_INTERVAL:
        time.sleep(SEND_INTERVAL - diff)

    _last_send_time = time.time()


# ------------------------------------------------------------
# 内部送信関数
# ------------------------------------------------------------

def _post_discord(payload, *, webhook_url: Optional[str] = None, timeout: int = DEFAULT_TIMEOUT) -> bool:
    resolved_webhook = _resolve_webhook_url(webhook_url)

    if not resolved_webhook:
        logger.warning("⚠️ Discord送信スキップ: webhook_url 未設定")
        return False

    if not payload:
        logger.warning("⚠️ Discord送信スキップ: payload empty")
        return False

    with _send_lock:
        _wait_rate_limit()

        try:
            r = requests.post(
                resolved_webhook,
                json=payload,
                timeout=timeout,
            )

            if r.status_code in (200, 204):
                logger.info("✅ Discord送信成功 status=%s", r.status_code)
                return True

            if r.status_code == 429:
                retry_after = _safe_float(r.headers.get("Retry-After", 1), 1.0)

                logger.warning(
                    "Discord rate limit hit. sleeping %ss",
                    retry_after,
                )

                time.sleep(retry_after)

                r = requests.post(
                    resolved_webhook,
                    json=payload,
                    timeout=timeout,
                )

                if r.status_code in (200, 204):
                    logger.info("✅ Discord再送信成功 status=%s", r.status_code)
                    return True

                logger.warning(
                    "⚠️ Discord retry response status=%s body=%s",
                    r.status_code,
                    getattr(r, "text", "")[:300],
                )
                return False

            logger.warning(
                "⚠️ Discord response status=%s body=%s",
                r.status_code,
                getattr(r, "text", "")[:300],
            )
            return False

        except Exception as e:
            logger.error("❌ Discord送信エラー: %s", e, exc_info=True)
            return False


# ------------------------------------------------------------
# 共通: Discord POST 送信（画像なし）
# ------------------------------------------------------------

def send_discord_message(
    content: Optional[str] = None,
    embeds: Optional[list] = None,
    *,
    webhook_url: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> bool:
    resolved_webhook = _resolve_webhook_url(webhook_url)

    if not resolved_webhook:
        logger.warning("⚠️ Discord Webhook URL 未設定")
        return False

    payload = {}

    if content:
        payload["content"] = content

    if embeds:
        payload["embeds"] = embeds

    if not payload:
        logger.warning("⚠️ Discord送信スキップ: content/embeds empty")
        return False

    return _post_discord(payload, webhook_url=resolved_webhook, timeout=timeout)


# ------------------------------------------------------------
# 長文 / 行配列送信
# ------------------------------------------------------------

def send_discord_text(
    msg: str,
    *,
    webhook_url: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT,
    max_len: int = DEFAULT_MAX_LEN,
) -> bool:
    """
    announce_bridge / jobs / runners からそのまま
    discord_sender として使えるテキスト送信関数。
    長文は自動分割する。
    """
    if not msg:
        return False

    resolved_webhook = _resolve_webhook_url(webhook_url)
    if not resolved_webhook:
        logger.warning("⚠️ Discord Webhook URL 未設定")
        return False

    chunks = _split_message_chunks(msg, max_len=max_len)
    if not chunks:
        return False

    sent = 0
    for chunk in chunks:
        ok = send_discord_message(
            content=chunk,
            webhook_url=resolved_webhook,
            timeout=timeout,
        )
        if not ok:
            logger.warning("⚠️ Discordチャンク送信失敗 sent=%d/%d", sent, len(chunks))
            return False
        sent += 1

    return sent == len(chunks)


def send_discord_lines(
    lines: Iterable[str],
    *,
    webhook_url: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT,
    max_len: int = DEFAULT_MAX_LEN,
) -> bool:
    text = "\n".join([_safe_str(x) for x in lines if _safe_str(x)])
    return send_discord_text(
        text,
        webhook_url=webhook_url,
        timeout=timeout,
        max_len=max_len,
    )


def build_discord_sender(
    *,
    webhook_url: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT,
    max_len: int = DEFAULT_MAX_LEN,
) -> Callable[[str], bool]:
    """
    runner / job に渡せる discord_sender を返す。

    例:
        sender = build_discord_sender()
        job_push_summary_1m(
            return_details=True,
            announce_bridge=True,
            discord_sender=sender,
        )
    """
    resolved_webhook = _resolve_webhook_url(webhook_url)

    def _sender(text: str) -> bool:
        return send_discord_text(
            text,
            webhook_url=resolved_webhook,
            timeout=timeout,
            max_len=max_len,
        )

    return _sender


# ------------------------------------------------------------
# ENTRY 通知（軽量版）
# ------------------------------------------------------------

def send_discord_notify_embed_entry(symbol, symbolname, side, price, qty, reasons):

    embed = {
        "title": f"🚀 ENTRY: {symbolname} ({symbol})",
        "description": "\n".join(reasons) if isinstance(reasons, (list, tuple)) else _safe_str(reasons),
        "color": 3066993,
        "fields": [
            {"name": "方向", "value": _safe_str(side), "inline": True},
            {"name": "価格", "value": _safe_str(price), "inline": True},
            {"name": "数量", "value": _safe_str(qty), "inline": True},
        ],
        "footer": {
            "text": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
    }

    return send_discord_message(embeds=[embed])


# ------------------------------------------------------------
# EXIT 通知（軽量版）
# ------------------------------------------------------------

def send_discord_notify_embed_exit(
        symbol,
        symbolname,
        side,
        exit_price,
        qty,
        pnl,
        reason
):

    try:
        pnl_text = f"{float(pnl):.2f}"
    except Exception:
        pnl_text = str(pnl)

    embed = {
        "title": f"💸 EXIT: {symbolname} ({symbol})",
        "color": 15158332,
        "fields": [
            {"name": "方向", "value": _safe_str(side), "inline": True},
            {"name": "決済価格", "value": _safe_str(exit_price), "inline": True},
            {"name": "数量", "value": _safe_str(qty), "inline": True},
            {"name": "損益", "value": pnl_text, "inline": True},
            {"name": "理由", "value": _safe_str(reason), "inline": False},
        ],
        "footer": {
            "text": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
    }

    return send_discord_message(embeds=[embed])


# ------------------------------------------------------------
# 汎用テキスト通知（軽量）
# ------------------------------------------------------------

def send_discord_notify(msg: str):
    """
    ランキングENTRY / 出来高急増などが呼び出す互換テキスト通知関数。
    send_discord_text のラッパー。
    """
    return send_discord_text(msg)