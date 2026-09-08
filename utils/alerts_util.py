# ============================================================
# File   : utils/alerts_util.py
# Version: Ver3.5-DISCORD-SECRET-MASK-AND-TRANSPORT-BREAKER
# ------------------------------------------------------------
# Discord通知ユーティリティ
# - settings.ini の [discord] webhook_url を優先
# - YOUR_DISCORD_WEBHOOK_URL 等のプレースホルダー/不正URLは送信前にスキップ
# - Discord 429 RateLimit 対応
# - 長文分割対応
# - SUMMARY/AI PASSED 系の横長1行通知を送信直前に3行化
# - DataFrame repr が混入したSUMMARY見出しを送信直前に修復
#
# Ver3.5 (2026-09-01 の障害対応):
# - 例外ログに Webhook URL 全体 (ID + トークン) がそのまま出ていた。
#   ログもトレースバックも必ずマスクしてから出す。
# - TLS 検証失敗 (CERTIFICATE_VERIFY_FAILED) が 385 件連続し、
#   そのたびにフルトレースバックを吐いてログと負荷を膨らませていた。
#   通信レイヤの失敗には指数バックオフのサーキットブレーカーを入れ、
#   開いている間はネットワークに触らず即座に諦める。
# - verify=False は使わない。CA は
#   settings.ini [discord]/[alerts] ca_bundle -> REQUESTS_CA_BUNDLE /
#   SSL_CERT_FILE -> certifi の順で解決する。
#   それでも解決できない場合は環境要因 (TLS 傍受プロキシ等) なので、
#   検証を無効化せず診断ログを出して運用側へ回す。
# ============================================================

from __future__ import annotations

import configparser
import logging
import os
import re
import time
import traceback
from datetime import datetime
from threading import Lock
from typing import Any, Callable, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)

# ------------------------------------------------------------
# 設定ファイルから Discord Webhook を取得
# ------------------------------------------------------------
conf = configparser.ConfigParser()
conf.read("settings.ini", encoding="utf-8")
DISCORD_WEBHOOK = conf.get("discord", "webhook_url", fallback="").strip()

# ------------------------------------------------------------
# RateLimit制御
# ------------------------------------------------------------
SEND_INTERVAL = 0.7  # 秒
DEFAULT_TIMEOUT = 5
DEFAULT_MAX_LEN = 1800

_last_send_time = 0.0
_send_lock = Lock()

# ------------------------------------------------------------
# 通信失敗のサーキットブレーカー
# ------------------------------------------------------------
# TLS 検証失敗のように「同じ条件では必ず失敗する」障害で連打しないため。
# HTTP 応答が返った失敗 (4xx/5xx) は対象外。応答が返っている＝通信は生きている。
_TRANSPORT_BACKOFF_SEC = (30.0, 60.0, 300.0, 900.0)
_breaker_lock = Lock()
_breaker_open_until = 0.0
_breaker_streak = 0
_breaker_last_error = ""
_breaker_skips = 0
_breaker_last_skip_log = 0.0


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).strip())
    except Exception:
        return float(default)

_PLACEHOLDER_WEBHOOK_VALUES = {
    "",
    "YOUR_DISCORD_WEBHOOK_URL",
    "DISCORD_WEBHOOK_URL",
    "WEBHOOK_URL",
    "YOUR_WEBHOOK_URL",
    "none",
    "null",
    "dummy",
    "sample",
    "example",
}


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


# ------------------------------------------------------------
# 秘密値マスク
# ------------------------------------------------------------
# Webhook URL は「ID + トークン」でそのまま投稿権限になる。
# 例外メッセージ / トレースバックには URL がそのまま入るので、
# ログへ出す前に必ずここを通す。
_WEBHOOK_URL_RE = re.compile(
    r"(https?://[^\s'\"]*?/api/webhooks/)(\d+)/([A-Za-z0-9._\-]+)",
    re.IGNORECASE,
)
_WEBHOOK_PATH_RE = re.compile(
    r"(/api/webhooks/)(\d+)/([A-Za-z0-9._\-]+)",
    re.IGNORECASE,
)


def _mask_webhook_url(url: Optional[str]) -> str:
    """ログ出力用にマスクした Webhook URL を返す。

    ホスト名だけは診断に要るので残し、ID とトークンは伏せる。
    """
    s = _safe_str(url).strip()
    if not s:
        return "(unset)"
    masked = _WEBHOOK_URL_RE.sub(r"\1***/***", s)
    if masked != s:
        return masked
    try:
        parsed = urlparse(s)
        if parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}/***"
    except Exception:
        pass
    return "***"


def _mask_secrets(text: Any) -> str:
    """任意のテキスト (例外文字列 / トレースバック) から秘密値を落とす。"""
    s = _safe_str(text)
    if not s:
        return s
    s = _WEBHOOK_URL_RE.sub(r"\1***/***", s)
    s = _WEBHOOK_PATH_RE.sub(r"\1***/***", s)
    # 設定済みの実 URL が別形式で混ざっていても必ず落とす。
    try:
        real = _safe_str(DISCORD_WEBHOOK).strip()
        if real and real in s:
            s = s.replace(real, _mask_webhook_url(real))
    except Exception:
        pass
    return s


# ------------------------------------------------------------
# CA バンドル解決 (verify=False は使わない)
# ------------------------------------------------------------
_ca_bundle_cache: Optional[Any] = None


def _resolve_ca_bundle() -> Any:
    """requests に渡す verify 値を返す。

    優先順位:
      1. settings.ini [discord]/[alerts] ca_bundle
      2. 環境変数 REQUESTS_CA_BUNDLE / SSL_CERT_FILE
      3. certifi.where()
      4. True (requests 既定)

    verify=False は返さない。TLS 検証は落とさない。
    """
    global _ca_bundle_cache
    if _ca_bundle_cache is not None:
        return _ca_bundle_cache

    for section in ("discord", "alerts", "Alerts"):
        try:
            if conf.has_section(section):
                v = _safe_str(conf.get(section, "ca_bundle", fallback="")).strip()
                if v and os.path.exists(v):
                    logger.info("[DISCORD TLS] CA bundle from settings.ini [%s] ca_bundle", section)
                    _ca_bundle_cache = v
                    return _ca_bundle_cache
        except Exception:
            continue

    for env_name in ("REQUESTS_CA_BUNDLE", "SSL_CERT_FILE"):
        v = _safe_str(os.getenv(env_name)).strip()
        if v and os.path.exists(v):
            logger.info("[DISCORD TLS] CA bundle from env %s", env_name)
            _ca_bundle_cache = v
            return _ca_bundle_cache

    # certifi だけでは足りない環境がある (2026-09-01 実測)。
    # この PC はセキュリティソフトが TLS を傍受しており、差し替え後の
    # 証明書の発行元は Windows 証明書ストアにしか無いため、
    # certifi 単体だと CERTIFICATE_VERIFY_FAILED になる。
    #   certifi のみ                -> discord.com 検証失敗
    #   certifi + Windows ROOT 合成 -> discord.com status 200
    # verify=False にはせず、この PC が実際に信頼している CA を足す。
    try:
        from utils.ca_bundle import build_bundle

        merged = build_bundle()
        if merged and os.path.exists(merged):
            logger.warning(
                "[DISCORD TLS] CA bundle = certifi + Windows 証明書ストア合成 path=%s "
                "(TLS 検証は維持)",
                merged,
            )
            _ca_bundle_cache = merged
            return _ca_bundle_cache
    except Exception:
        logger.debug("[DISCORD TLS] merged CA bundle unavailable", exc_info=True)

    try:
        import certifi

        path = certifi.where()
        if path and os.path.exists(path):
            _ca_bundle_cache = path
            return _ca_bundle_cache
    except Exception:
        logger.debug("[DISCORD TLS] certifi unavailable", exc_info=True)

    _ca_bundle_cache = True
    return _ca_bundle_cache


# ------------------------------------------------------------
# 通信失敗ブレーカー
# ------------------------------------------------------------

def _is_transport_failure(exc: BaseException) -> bool:
    """HTTP 応答が返らなかった種類の失敗か。"""
    return isinstance(
        exc,
        (
            requests.exceptions.SSLError,
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            requests.exceptions.ProxyError,
        ),
    )


def _breaker_blocked() -> Tuple[bool, float]:
    global _breaker_skips, _breaker_last_skip_log
    with _breaker_lock:
        now = time.monotonic()
        if _breaker_open_until <= now:
            return False, 0.0
        wait = _breaker_open_until - now
        _breaker_skips += 1
        should_log = (now - _breaker_last_skip_log) >= 60.0
        if should_log:
            _breaker_last_skip_log = now
    if should_log:
        logger.warning(
            "⚠️ Discord送信スキップ: 通信ブレーカーが開いています retry_in=%.0fs streak=%d skips=%d err=%s",
            wait, _breaker_streak, _breaker_skips, _mask_secrets(_breaker_last_error)[:160],
        )
    return True, wait


def _breaker_note_success() -> None:
    global _breaker_open_until, _breaker_streak, _breaker_last_error, _breaker_skips
    with _breaker_lock:
        recovered = _breaker_streak > 0
        _breaker_open_until = 0.0
        _breaker_streak = 0
        _breaker_last_error = ""
        skips = _breaker_skips
        _breaker_skips = 0
    if recovered:
        logger.warning("✅ Discord通信ブレーカーを閉じました (復旧) skipped_while_open=%d", skips)


def _breaker_note_transport_failure(exc: BaseException) -> float:
    global _breaker_open_until, _breaker_streak, _breaker_last_error
    scale = max(0.0, _env_float("DISCORD_TRANSPORT_BACKOFF_SCALE", 1.0))
    with _breaker_lock:
        idx = min(_breaker_streak, len(_TRANSPORT_BACKOFF_SEC) - 1)
        wait = _TRANSPORT_BACKOFF_SEC[idx] * scale
        _breaker_streak += 1
        _breaker_last_error = f"{type(exc).__name__}: {exc}"
        _breaker_open_until = time.monotonic() + wait
        streak = _breaker_streak
    logger.error(
        "❌ Discord通信失敗 (%s) -> %.0fs バックオフ streak=%d detail=%s "
        "（verify=False にはしません。CA/プロキシ設定を確認してください）",
        type(exc).__name__, wait, streak, _mask_secrets(exc)[:300],
    )
    if streak == 1:
        # 初回だけ、マスク済みトレースバックを DEBUG に残す。
        logger.debug("[DISCORD TRANSPORT] %s", _mask_secrets(traceback.format_exc()))
    return wait


def discord_transport_status() -> dict:
    with _breaker_lock:
        now = time.monotonic()
        return {
            "open": _breaker_open_until > now,
            "retry_in_sec": max(0.0, _breaker_open_until - now),
            "streak": _breaker_streak,
            "skips": _breaker_skips,
            "last_error": _mask_secrets(_breaker_last_error)[:200],
        }


def reset_discord_transport_breaker() -> None:
    """テスト / 手動復旧用。"""
    global _breaker_open_until, _breaker_streak, _breaker_last_error, _breaker_skips, _breaker_last_skip_log
    global _ca_bundle_cache
    with _breaker_lock:
        _breaker_open_until = 0.0
        _breaker_streak = 0
        _breaker_last_error = ""
        _breaker_skips = 0
        _breaker_last_skip_log = 0.0
    _ca_bundle_cache = None


def _is_valid_discord_webhook_url(url: Optional[str]) -> bool:
    """Discord webhook URLとして実送信してよい文字列かを検証する。"""
    try:
        s = _safe_str(url).strip()
        if not s:
            return False
        if s in _PLACEHOLDER_WEBHOOK_VALUES:
            return False
        if s.upper() in _PLACEHOLDER_WEBHOOK_VALUES:
            return False
        if "YOUR_" in s.upper() or "PLACEHOLDER" in s.upper():
            return False

        parsed = urlparse(s)
        if parsed.scheme not in {"http", "https"}:
            return False
        if not parsed.netloc:
            return False

        # Discord webhook 以外を完全禁止にはしないが、最低限 http(s) URL だけ許可する。
        return True
    except Exception:
        return False


def _resolve_webhook_url(webhook_url: Optional[str] = None) -> str:
    url = _safe_str(webhook_url).strip()
    if url:
        return url
    return _safe_str(DISCORD_WEBHOOK).strip()


def _resolve_valid_webhook_url(webhook_url: Optional[str] = None) -> str:
    resolved = _resolve_webhook_url(webhook_url)
    if not _is_valid_discord_webhook_url(resolved):
        return ""
    return resolved


if not _is_valid_discord_webhook_url(DISCORD_WEBHOOK):
    logger.warning("⚠️ settings.ini の [discord] webhook_url が未設定または不正です。Discord送信はスキップします。")


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

_DF_TITLE_RE = re.compile(
    r"(?P<head>=+\s*(?:📊\s*)?(?:🤖\s*)?(?:SUMMARY TOP10|PUSH SUMMARY TOP10|RANKING SUMMARY TOP10|AI PASSED BUY CANDIDATES|AI PASSED SELL CANDIDATES|AI PASSED EXIT CANDIDATES)\s*)"
    r"\(.*?\[\s*\d+\s+rows\s+x\s+\d+\s+columns\s*\]\s*\)\s*(?P<tail>=+)",
    re.IGNORECASE | re.DOTALL,
)
_DF_REPR_LINE_RE = re.compile(r"^\s*(?:\.\.\.|\d+)\s+\S+\s+.*\.\.\.\s+.*$")
_ROWS_COLS_LINE_RE = re.compile(r"^\s*\[\s*\d+\s+rows\s+x\s+\d+\s+columns\s*\]\)?\s*=*\s*$", re.IGNORECASE)


def _contains_summary_title(line: str) -> bool:
    u = _safe_str(line).upper()
    return any(w in u for w in _TITLE_WORDS)


def _looks_like_df_header_start(line: str) -> bool:
    s = _safe_str(line)
    return _contains_summary_title(s) and "symbol" in s.lower() and ("..." in s or " id" in s.lower() or "ai_confidence" in s.lower())


def _clean_title_from_df_start(line: str) -> str:
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
    """DataFrame repr が見出しに混入したブロックを丸ごと削除/修復する。"""
    try:
        s = _safe_str(text)
        if "rows x" not in s and "columns" not in s and "symbol" not in s.lower():
            return s

        lines = s.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        out: list[str] = []
        skipping_df = False

        for line in lines:
            raw = line.rstrip()

            if skipping_df:
                if _ROWS_COLS_LINE_RE.match(raw) or "rows x" in raw:
                    skipping_df = False
                continue

            if _looks_like_df_header_start(raw):
                clean = _clean_title_from_df_start(raw)
                if not out or out[-1] != clean:
                    out.append(clean)
                skipping_df = True
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

        if raw and raw not in {"-", "flag_score"}:
            if "売りスコア優勢" not in raw and "買いスコア優勢" not in raw:
                parts.append(raw)
        elif raw == "flag_score":
            parts.append("スコア条件で抽出")

        return " / ".join(parts) if parts else (raw or "理由データ不足")
    except Exception:
        return _safe_str(reason, "理由生成失敗")


def _normalize_summary_one_line(line: str) -> str:
    """横長SUMMARY候補行を3行に直す最終防衛。"""
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
    resolved_webhook = _resolve_valid_webhook_url(webhook_url)

    if not resolved_webhook:
        logger.warning("⚠️ Discord送信スキップ: webhook_url 未設定または不正")
        return False

    if not payload:
        logger.warning("⚠️ Discord送信スキップ: payload empty")
        return False

    # 通信ブレーカーが開いている間はネットワークに触らない。
    blocked, _wait = _breaker_blocked()
    if blocked:
        return False

    verify = _resolve_ca_bundle()

    with _send_lock:
        _wait_rate_limit()
        try:
            r = requests.post(resolved_webhook, json=payload, timeout=timeout, verify=verify)
            # HTTP 応答が返った時点で通信レイヤは生きている。
            _breaker_note_success()

            if r.status_code in (200, 204):
                logger.info("✅ Discord送信成功 status=%s", r.status_code)
                return True

            if r.status_code == 429:
                retry_after = _safe_float(r.headers.get("Retry-After", 1), 1.0)
                logger.warning("Discord rate limit hit. sleeping %ss", retry_after)
                time.sleep(retry_after)
                r = requests.post(resolved_webhook, json=payload, timeout=timeout, verify=verify)
                if r.status_code in (200, 204):
                    logger.info("✅ Discord再送信成功 status=%s", r.status_code)
                    return True
                logger.warning(
                    "⚠️ Discord retry response status=%s body=%s",
                    r.status_code, _mask_secrets(getattr(r, "text", ""))[:300],
                )
                return False

            logger.warning(
                "⚠️ Discord response status=%s body=%s",
                r.status_code, _mask_secrets(getattr(r, "text", ""))[:300],
            )
            return False
        except (
            requests.exceptions.MissingSchema,
            requests.exceptions.InvalidSchema,
            requests.exceptions.InvalidURL,
        ) as e:
            logger.warning(
                "⚠️ Discord送信スキップ: webhook_url 不正 url=%s err=%s",
                _mask_webhook_url(resolved_webhook), _mask_secrets(e)[:200],
            )
            return False
        except Exception as e:
            # 例外文字列にもトレースバックにも Webhook URL が入る。必ずマスクする。
            if _is_transport_failure(e):
                _breaker_note_transport_failure(e)
            else:
                logger.error(
                    "❌ Discord送信エラー url=%s type=%s detail=%s",
                    _mask_webhook_url(resolved_webhook), type(e).__name__, _mask_secrets(e)[:300],
                )
                logger.debug("[DISCORD SEND] %s", _mask_secrets(traceback.format_exc()))
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
    resolved_webhook = _resolve_valid_webhook_url(webhook_url)

    if not resolved_webhook:
        logger.warning("⚠️ Discord Webhook URL 未設定または不正のため送信スキップ")
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

    resolved_webhook = _resolve_valid_webhook_url(webhook_url)
    if not resolved_webhook:
        logger.warning("⚠️ Discord Webhook URL 未設定または不正のため送信スキップ")
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
    resolved_webhook = _resolve_valid_webhook_url(webhook_url)

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
