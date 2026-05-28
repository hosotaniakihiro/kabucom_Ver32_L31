# ============================================================
# File   : utils/alerts_util.py
# Version: Ver3.3-PRODUCTION-DISCORD-FINAL-DF-BLOCK-GUARD
# ------------------------------------------------------------
# ✔ 既存機能完全保持（削除ゼロ）
# ✔ Discord 429 RateLimit完全対策
# ✔ Retry-After 自動待機
# ✔ 送信間隔制御（0.7秒）
# ✔ ENTRY / EXIT Embed互換
# ✔ ranking互換 send_discord_notify 保持
# ✔ announce_bridge / jobs / runners から discord_sender として直接使える
# ✔ 長文自動分割対応
# ✔ 例外耐性強化
# ✔ 最終防衛: SUMMARY/AI PASSED 系の横長1行通知を送信直前に3行化
# ✔ 最終防衛: interval_label に DataFrame が入った見出しを送信直前に修復
# ✔ 最終防衛: 見出し直後に DataFrame repr 本体が混入したブロックを丸ごと削除
# ============================================================

from __future__ import annotations

import configparser
import logging
import re
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


# ------------------------------------------------------------
# Discord summary final formatter
# ------------------------------------------------------------

_TITLE_WORDS = (
    "SUMMARY TOP10",
    "PUSH SUMMARY TOP10",
    "RANKING SUMMARY TOP10",
    "AI PASSED BUY CANDIDATES",
    "AI PASSED SELL CANDIDATES",
    "AI PASSED EXIT CANDIDATES",
)

_SUMMARY_ONE_LINE_RE = re.compile(
    r"^(?P<prefix>\s*(?:[🟦🟥🔴🔵🟩🟨⬛⬜🟧🟪]\s*)?\d+\.\s+)"
    r"(?P<head>.*?)\s+"
    r"(?:株価|価|Price|price|close)=(?P<price>[-+0-9.,]+)\s+"
    r"(?:score|Score)=(?P<score>[-+0-9.,]+)\s+"
    r"(?:buy|Buy)=(?P<buy>[-+0-9.,]+)\s+"
    r"(?:sell|Sell)=(?P<sell>[-+0-9.,]+)\s+"
    r"(?:total=[-+0-9.,]+\s+)?"
    r"(?:final=[-+0-9.,]+\s+)?"
    r"(?:close=[-+0-9.,]+\s+)?"
    r"(?:slope|Slope)=(?P<slope>[-+0-9.,]+)\s+"
    r"(?:mtf|MTF)=(?P<mtf>[-+0-9.,]+)\s+"
    r"(?:rsi|RSI)=(?P<rsi>[-+0-9.,]+)\s+"
    r"(?:macd|MACD)=(?P<macd>[-+0-9.,]+)\s+"
    r"(?:理由|REASON|Reason)=(?P<reason>.*)$",
    re.IGNORECASE,
)

# AI/SUMMARY見出しの interval_label に DataFrame repr が入る事故を送信直前に直す。
_DF_TITLE_RE = re.compile(
    r"(?P<head>=+\s*(?:📊\s*)?(?:🤖\s*)?(?:SUMMARY TOP10|PUSH SUMMARY TOP10|RANKING SUMMARY TOP10|AI PASSED BUY CANDIDATES|AI PASSED SELL CANDIDATES|AI PASSED EXIT CANDIDATES)\s*)"
    r"\(.*?\[\s*\d+\s+rows\s+x\s+\d+\s+columns\s*\]\s*\)\s*(?P<tail>=+)",
    re.IGNORECASE | re.DOTALL,
)

_DF_REPR_LINE_RE = re.compile(r"^\s*(?:\.\.\.|\d+)\s+\S+\s+.*\.\.\.\s+.*$")
_ROWS_COLS_LINE_RE = re.compile(r"^\s*\[\s*\d+\s+rows\s+x\s+\d+\s+columns\s*\]\)?\s*=*\s*$", re.IGNORECASE)
_HEADER_WITH_DF_START_RE = re.compile(r"^(?P<head>.*?(?:SUMMARY TOP10|AI PASSED BUY CANDIDATES|AI PASSED SELL CANDIDATES|AI PASSED EXIT CANDIDATES).*?)(?:\(|\s+)\s*symbol\s+", re.IGNORECASE)


def _contains_summary_title(line: str) -> bool:
    u = _safe_str(line).upper()
    return any(w in u for w in _TITLE_WORDS)


def _looks_like_df_header_start(line: str) -> bool:
    s = _safe_str(line)
    return _contains_summary_title(s) and "symbol" in s.lower() and ("..." in s or " id" in s.lower() or "ai_confidence" in s.lower())


def _clean_title_from_df_start(line: str) -> str:
    """DataFrame repr が付いた見出し行を安全な見出しへ戻す。"""
    s = _safe_str(line).rstrip()
    upper = s.upper()
    title = None
    for w in _TITLE_WORDS:
        if w in upper:
            title = w
            break
    if not title:
        return "📊 SUMMARY TOP10 1min"

    emoji = "🤖 " if "AI PASSED" in title else "📊 "
    if s.lstrip().startswith("="):
        return f"========== {emoji}{title} (1min) =========="
    return f"{emoji}{title} 1min"


def _strip_dataframe_repr_blocks(text: str) -> str:
    """
    DataFrame repr が見出しに混入したブロックを丸ごと削除/修復する。

    対象例:
      📊 SUMMARY TOP10      symbol id ...
      0 1301 ...
      ...
      [4109 rows x 173 columns]

      ========== 🤖 AI PASSED SELL CANDIDATES (     symbol id ...
      ...
      [4109 rows x 173 columns]) ==========
    """
    try:
        s = _safe_str(text)
        if "rows x" not in s and "columns" not in s and "symbol" not in s.lower():
            return s

        lines = s.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        out: list[str] = []
        skipping_df = False
        skip_replaced = False

        for line in lines:
            raw = line.rstrip()

            if skipping_df:
                if _ROWS_COLS_LINE_RE.match(raw) or "rows x" in raw:
                    skipping_df = False
                    skip_replaced = False
                continue

            if _looks_like_df_header_start(raw):
                clean = _clean_title_from_df_start(raw)
                if not out or out[-1] != clean:
                    out.append(clean)
                skipping_df = True
                skip_replaced = True
                continue

            if _DF_REPR_LINE_RE.match(raw):
                continue
            if _ROWS_COLS_LINE_RE.match(raw):
                continue
            if "..." in raw and "NaN" in raw and len(raw) > 40:
                continue
            if raw.strip().startswith("...") and "..." in raw:
                continue

            out.append(raw)

        return "\n".join(out)
    except Exception:
        logger.debug("[DISCORD FINAL FORMATTER] strip dataframe blocks failed", exc_info=True)
        return text


def _summary_reason_to_ja(reason: str, *, buy: str, sell: str, slope: str, mtf: str, rsi: str, macd: str) -> str:
    """英語コードや既存理由を、短めの日本語理由へ寄せる。"""
    try:
        raw = _safe_str(reason).strip()
        parts: list[str] = []
        buy_f = _safe_float(buy, 0.0)
        sell_f = _safe_float(sell, 0.0)
        slope_f = _safe_float(slope, 0.0)
        mtf_f = _safe_float(mtf, 0.0)
        rsi_f = _safe_float(rsi, 50.0)
        macd_f = _safe_float(macd, 0.0)

        if sell_f > buy_f and sell_f > 0:
            parts.append(f"売りスコア優勢 sell={sell_f:.2f}")
            if slope_f < 0:
                parts.append(f"下向き傾き slope={slope_f:.4f}")
            else:
                parts.append(f"下落傾きは弱い slope={slope_f:.4f}")
        elif buy_f > 0:
            parts.append(f"買いスコア優勢 buy={buy_f:.2f}")
            if slope_f > 0:
                parts.append(f"上向き傾き slope={slope_f:.4f}")
            else:
                parts.append(f"傾きは弱い slope={slope_f:.4f}")

        if abs(mtf_f) > 0:
            parts.append(f"複数時間足={mtf_f:.2f}")
        if rsi_f != 50.0:
            parts.append(f"RSI={rsi_f:.1f}")
        if abs(macd_f) > 0:
            parts.append(f"MACD={macd_f:.3f}")

        # 既に日本語理由が入っている場合は、重複が少なければ追加。
        if raw and raw not in {"-", "flag_score"}:
            if "売りスコア優勢" not in raw and "買いスコア優勢" not in raw:
                parts.append(raw)
        elif raw == "flag_score":
            parts.append("スコア条件で抽出")

        return " / ".join(parts) if parts else (raw or "理由データ不足")
    except Exception:
        return _safe_str(reason, "理由生成失敗")


def _normalize_summary_one_line(line: str) -> str:
    """
    どの通知ルートから来ても、横長SUMMARY候補行を3行に直す最終防衛。
    """
    try:
        s = _safe_str(line).rstrip()
        if not s or "score=" not in s.lower() or "理由=" not in s:
            return line
        if "\n" in s:
            return line

        m = _SUMMARY_ONE_LINE_RE.match(s)
        if not m:
            return line

        gd = m.groupdict()
        reason = _summary_reason_to_ja(
            gd.get("reason", ""),
            buy=gd.get("buy", "0"),
            sell=gd.get("sell", "0"),
            slope=gd.get("slope", "0"),
            mtf=gd.get("mtf", "0"),
            rsi=gd.get("rsi", "50"),
            macd=gd.get("macd", "0"),
        )
        return (
            f"{gd['prefix']}{gd['head']} Price={gd['price']} Score={gd['score']} Buy={gd['buy']} Sell={gd['sell']}\n"
            f"   Slope={gd['slope']} MTF={gd['mtf']} RSI={gd['rsi']} MACD={gd['macd']}\n"
            f"   理由={reason}"
        )
    except Exception:
        logger.debug("[DISCORD FINAL FORMATTER] normalize one line failed", exc_info=True)
        return line


def _normalize_bad_dataframe_titles(text: str) -> str:
    """見出しの括弧内・直後に DataFrame repr が入った場合、(1min) に修復する。"""
    try:
        s = _safe_str(text)
        if "rows x" not in s and "columns" not in s and "symbol" not in s.lower():
            return s

        s = _DF_TITLE_RE.sub(lambda m: f"{m.group('head')}(1min) {m.group('tail')}", s)
        s = _strip_dataframe_repr_blocks(s)
        return s
    except Exception:
        logger.debug("[DISCORD FINAL FORMATTER] dataframe title normalize failed", exc_info=True)
        return text


def _normalize_discord_summary_text(text: str) -> str:
    try:
        if not text:
            return text
        s = _safe_str(text)
        s = _normalize_bad_dataframe_titles(s)

        lines = s.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        out = [_normalize_summary_one_line(line) for line in lines]
        return "\n".join(out)
    except Exception:
        logger.debug("[DISCORD FINAL FORMATTER] normalize text failed", exc_info=True)
        return text


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
    """Discordレート制限回避"""
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
            r = requests.post(resolved_webhook, json=payload, timeout=timeout)
            if r.status_code in (200, 204):
                logger.info("✅ Discord送信成功 status=%s", r.status_code)
                return True

            if r.status_code == 429:
                retry_after = _safe_float(r.headers.get("Retry-After", 1), 1.0)
                logger.warning("Discord rate limit hit. sleeping %ss", retry_after)
                time.sleep(retry_after)
                r = requests.post(resolved_webhook, json=payload, timeout=timeout)
                if r.status_code in (200, 204):
                    logger.info("✅ Discord再送信成功 status=%s", r.status_code)
                    return True
                logger.warning("⚠️ Discord retry response status=%s body=%s", r.status_code, getattr(r, "text", "")[:300])
                return False

            logger.warning("⚠️ Discord response status=%s body=%s", r.status_code, getattr(r, "text", "")[:300])
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
        payload["content"] = _normalize_discord_summary_text(content)

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
    """announce_bridge / jobs / runners からそのまま discord_sender として使えるテキスト送信関数。"""
    if not msg:
        return False

    resolved_webhook = _resolve_webhook_url(webhook_url)
    if not resolved_webhook:
        logger.warning("⚠️ Discord Webhook URL 未設定")
        return False

    msg = _normalize_discord_summary_text(msg)
    chunks = _split_message_chunks(msg, max_len=max_len)
    if not chunks:
        return False

    sent = 0
    for chunk in chunks:
        ok = send_discord_message(content=chunk, webhook_url=resolved_webhook, timeout=timeout)
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
    return send_discord_text(text, webhook_url=webhook_url, timeout=timeout, max_len=max_len)


def build_discord_sender(
    *,
    webhook_url: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT,
    max_len: int = DEFAULT_MAX_LEN,
) -> Callable[[str], bool]:
    """runner / job に渡せる discord_sender を返す。"""
    resolved_webhook = _resolve_webhook_url(webhook_url)

    def _sender(text: str) -> bool:
        return send_discord_text(text, webhook_url=resolved_webhook, timeout=timeout, max_len=max_len)

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
        "footer": {"text": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
    }
    return send_discord_message(embeds=[embed])


# ------------------------------------------------------------
# EXIT 通知（軽量版）
# ------------------------------------------------------------

def send_discord_notify_embed_exit(symbol, symbolname, side, exit_price, qty, pnl, reason):
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
        "footer": {"text": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
    }
    return send_discord_message(embeds=[embed])


# ------------------------------------------------------------
# 汎用テキスト通知（軽量）
# ------------------------------------------------------------

def send_discord_notify(msg: str):
    """ランキングENTRY / 出来高急増などが呼び出す互換テキスト通知関数。"""
    return send_discord_text(msg)
