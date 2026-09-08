# ============================================================
# File   : trading/ranking/api_client.py
# Version: Ver2.3-RANKING-API-CLIENT-SETTINGS-TOKEN-FIRST
# ------------------------------------------------------------
# ✔ kabu Station ranking API client
# ✔ timeout 対応
# ✔ retry 対応
# ✔ HTTPError / URLError / socket timeout / JSON decode safe
# ✔ 4001009(APIキー不一致) / 401 時に runtime refresh しない
# ✔ ranking_collector は token_manager のメモリキャッシュを優先しない
# ✔ settings.ini に保存された最新 token を毎回直接読む
# ✔ logger 統一
# ✔ elapsed ログ
# ============================================================

from __future__ import annotations

import json
import logging
import os
import socket
import time
import threading
import urllib.error
import urllib.parse
import urllib.request

import requests
from configparser import ConfigParser
from pathlib import Path
from typing import Any

try:
    from token_manager import get_valid_token
except Exception:  # pragma: no cover - startup import safety
    get_valid_token = None  # type: ignore

logger = logging.getLogger(__name__)

RANKING_API_URL = "http://localhost:18080/kabusapi/ranking"
API_TIMEOUT_SEC = 5.0
API_RETRY_MAX = 2
API_RETRY_SLEEP_SEC = 0.35
API_KEY_MISMATCH_CODE = 4001009

# ------------------------------------------------------------
# 失敗種別 (2026-09-01)
# ------------------------------------------------------------
# 以前は timeout も 429 も HTTP エラーも JSON 崩れも、すべて None を
# 返していた。呼び出し側 (breaker) からは
#     "probe invalid response data_type=NoneType"
# としか見えず、
#   ・レート制限で一時的に弾かれただけなのか
#   ・kabu Station が本当に落ちているのか
# を区別できなかった。実測 (13:47) では
#     /ranking は 0.02s / 50 行で正常応答
#     連打すると 429 が返る
#     urllib 経由 (Connection: close) だと応答が返らず 5s タイムアウト
# であり、breaker は「レート制限」を「障害」と誤認して
# backoff を 120 -> 300 -> 600s と伸ばし、復旧できなくなっていた。
OUTCOME_OK = "ok"
OUTCOME_TIMEOUT = "timeout"
OUTCOME_RATE_LIMITED = "rate_limited"
OUTCOME_HTTP_ERROR = "http_error"
OUTCOME_AUTH = "auth_rejected"
OUTCOME_BAD_JSON = "bad_json"
OUTCOME_TRANSPORT = "transport_error"

RATE_LIMIT_STATUS = 429

_LAST_OUTCOME = OUTCOME_OK
_LAST_ERROR = ""
_LAST_STATUS = None
_OUTCOME_LOCK = threading.RLock()

_SESSION: "requests.Session | None" = None
_SESSION_LOCK = threading.RLock()


def _session() -> "requests.Session":
    """keep-alive の Session を使い回す。

    urllib.request は毎回 "Connection: close" を送り、kabu Station は
    そのリクエストに応答を返さないままタイムアウトさせる (2026-08-29/09-01 実測)。
    token 側は既に requests へ移行済みで、ranking だけ取り残されていた。
    """
    global _SESSION
    with _SESSION_LOCK:
        if _SESSION is None:
            _SESSION = requests.Session()
        return _SESSION


def _rate_limit_backoff_sec(attempt: int) -> float:
    try:
        base = float(os.getenv("RANKING_API_RATE_LIMIT_BACKOFF_SEC", "0.6") or 0.6)
    except Exception:
        base = 0.6
    return max(0.0, base * max(1, int(attempt)))


def _rate_limit_max_wait_sec() -> float:
    try:
        return max(0.0, float(os.getenv("RANKING_API_RATE_LIMIT_MAX_WAIT_SEC", "3.0") or 3.0))
    except Exception:
        return 3.0


def _set_outcome(outcome: str, error: str = "", status=None) -> None:
    global _LAST_OUTCOME, _LAST_ERROR, _LAST_STATUS
    with _OUTCOME_LOCK:
        _LAST_OUTCOME = outcome
        _LAST_ERROR = str(error or "")[:200]
        _LAST_STATUS = status


def last_outcome() -> tuple:
    """(outcome, error, status)。breaker が失敗種別で判断するために使う。"""
    with _OUTCOME_LOCK:
        return _LAST_OUTCOME, _LAST_ERROR, _LAST_STATUS


def last_call_was_rate_limited() -> bool:
    with _OUTCOME_LOCK:
        return _LAST_OUTCOME == OUTCOME_RATE_LIMITED

_LAST_TOKEN_DIAG_LOG_TS = 0.0
_LAST_TOKEN_DIAG_KEY = ""


def _sanitize_params(params: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(params, dict):
        return {}
    out: dict[str, Any] = {}
    for k, v in params.items():
        if v is None:
            continue
        out[str(k)] = v
    return out


def _is_api_key_mismatch(status: Any, body_json: Any, body_text: str = "") -> bool:
    """kabu Station の APIキー不一致を判定する。"""
    try:
        if int(status or 0) == 401:
            return True
    except Exception:
        pass

    if isinstance(body_json, dict):
        try:
            if int(body_json.get("Code") or 0) == API_KEY_MISMATCH_CODE:
                return True
        except Exception:
            pass
        try:
            if "APIキー不一致" in str(body_json.get("Message") or ""):
                return True
        except Exception:
            pass

    if body_text and ("4001009" in body_text or "APIキー不一致" in body_text):
        return True

    return False


def _project_root_candidates() -> list[Path]:
    """settings.ini 探索候補。子プロセスの cwd 差異に強くする。"""
    out: list[Path] = []

    for env_name in ("KABU_PROJECT_ROOT", "PROJECT_ROOT", "APP_ROOT"):
        v = os.getenv(env_name)
        if v:
            out.append(Path(v))

    try:
        # trading/ranking/api_client.py -> repo root
        out.append(Path(__file__).resolve().parents[2])
    except Exception:
        pass

    try:
        out.append(Path.cwd())
    except Exception:
        pass

    uniq: list[Path] = []
    seen: set[str] = set()
    for p in out:
        try:
            key = str(p.resolve())
        except Exception:
            key = str(p)
        if key not in seen:
            seen.add(key)
            uniq.append(p)
    return uniq


def _settings_candidates() -> list[Path]:
    out: list[Path] = []

    for env_name in ("SETTINGS_INI_PATH", "KABU_SETTINGS_INI"):
        v = os.getenv(env_name)
        if v:
            out.append(Path(v))

    for root in _project_root_candidates():
        out.append(root / "settings.ini")
        out.append(root / "config" / "settings.ini")

    uniq: list[Path] = []
    seen: set[str] = set()
    for p in out:
        try:
            key = str(p.resolve())
        except Exception:
            key = str(p)
        if key not in seen:
            seen.add(key)
            uniq.append(p)
    return uniq


def _read_settings_token() -> tuple[str, str]:
    """settings.ini から token を直接読む。

    重要:
    token_manager.get_valid_token() はプロセス内 API_TOKEN をキャッシュする。
    ranking_collector 子プロセスが古い token をキャッシュしたままだと、親が
    settings.ini を更新してもランキングAPIだけ 401/APIキー不一致 が続く。
    そのため ranking API client では settings.ini を毎回直接読む。
    """
    tried: list[str] = []

    for path in _settings_candidates():
        try:
            tried.append(str(path))
            if not path.exists():
                continue

            conf = ConfigParser()
            conf.read(path, encoding="utf-8-sig")

            for sec in ("aukabu", "kabusapi"):
                if not conf.has_section(sec):
                    continue
                token = str(conf.get(sec, "token", fallback="") or "").strip()
                if token:
                    return token, f"{path} [{sec}]"
        except Exception:
            logger.debug("[RANKING API CLIENT] settings token read failed path=%s", path, exc_info=True)
            continue

    return "", "tried=" + " | ".join(tried)


def _read_env_token() -> tuple[str, str]:
    for name in (
        "KABU_API_TOKEN",
        "KABUSAPI_TOKEN",
        "AUKABU_TOKEN",
        "API_TOKEN",
        "TOKEN",
        "KABU_API_KEY",
        "KABUSAPI_API_KEY",
        "X_API_KEY",
        "KABU_TOKEN",
    ):
        v = os.getenv(name)
        if v and str(v).strip():
            return str(v).strip(), f"env:{name}"
    return "", "env:none"


def _log_token_diag_once(token: str, source: str) -> None:
    global _LAST_TOKEN_DIAG_LOG_TS, _LAST_TOKEN_DIAG_KEY

    now = time.time()
    key = f"{source}|{len(str(token or ''))}"
    if key == _LAST_TOKEN_DIAG_KEY and (now - _LAST_TOKEN_DIAG_LOG_TS) < 60.0:
        return

    _LAST_TOKEN_DIAG_KEY = key
    _LAST_TOKEN_DIAG_LOG_TS = now
    logger.warning(
        "[RANKING API CLIENT] token source=%s token_len=%d settings_first=True",
        source,
        len(str(token or "")),
    )


def _get_startup_token() -> str:
    """Read the settings.ini token without calling refresh_token()."""
    token, source = _read_settings_token()
    if token:
        _log_token_diag_once(token, source)
        return token

    token, source = _read_env_token()
    if token:
        _log_token_diag_once(token, source)
        return token

    if callable(get_valid_token):
        try:
            token = str(get_valid_token() or "").strip()
            if token:
                _log_token_diag_once(token, "token_manager:get_valid_token:fallback")
                return token
        except Exception:
            logger.warning("[RANKING API CLIENT] get_valid_token failed; request will proceed without token", exc_info=True)

    _log_token_diag_once("", source)
    return ""


class RankingApiRateLimited(Exception):
    """kabu Station が 429 を返した。障害ではなく流量制御。"""

    def __init__(self, retry_after: float = 0.0, body: str = ""):
        super().__init__("ranking API rate limited (429) retry_after=%.2fs" % float(retry_after or 0.0))
        self.retry_after = float(retry_after or 0.0)
        self.body = str(body or "")[:200]


class RankingApiHttpError(Exception):
    def __init__(self, status: int, body: str = ""):
        super().__init__("ranking API HTTP %s" % status)
        self.status = int(status)
        self.body = str(body or "")[:200]


def _retry_after_sec(res) -> float:
    try:
        v = res.headers.get("Retry-After")
        if v is None:
            return 0.0
        return max(0.0, float(str(v).strip()))
    except Exception:
        return 0.0


def _request_once(params: dict[str, Any], token: str | None, timeout_sec: float) -> dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-API-KEY"] = str(token)

    res = _session().get(
        RANKING_API_URL, params=params, headers=headers, timeout=timeout_sec
    )
    status = int(res.status_code)
    raw = res.text or ""

    if status == RATE_LIMIT_STATUS:
        raise RankingApiRateLimited(_retry_after_sec(res), raw)
    if status >= 400:
        raise RankingApiHttpError(status, raw)

    return json.loads(raw)


def get_data_from_api(
    params: dict[str, Any],
    *,
    timeout_sec: float = API_TIMEOUT_SEC,
    retry_max: int = API_RETRY_MAX,
) -> dict[str, Any] | None:
    params = _sanitize_params(params)

    last_exc: Exception | None = None

    for attempt in range(1, retry_max + 1):
        t0 = time.perf_counter()
        token = ""
        try:
            token = _get_startup_token()

            logger.info(
                "[RANKING API CLIENT] request start attempt=%s/%s params=%s timeout=%.1fs token_len=%s",
                attempt,
                retry_max,
                params,
                timeout_sec,
                len(str(token or "")),
            )

            payload = _request_once(params, token, timeout_sec)
            elapsed = time.perf_counter() - t0

            rows = payload.get("Ranking") if isinstance(payload, dict) else None
            row_count = len(rows) if isinstance(rows, list) else 0

            logger.info(
                "[RANKING API CLIENT] request ok attempt=%s/%s params=%s rows=%s elapsed=%.3fs",
                attempt,
                retry_max,
                params,
                row_count,
                elapsed,
            )
            _set_outcome(OUTCOME_OK, "", 200)
            return payload

        except RankingApiRateLimited as e:
            # 429 は「落ちている」ではなく「速すぎる」。
            # breaker を開かせず、Retry-After 分だけ待って次の attempt へ。
            elapsed = time.perf_counter() - t0
            wait = e.retry_after if e.retry_after > 0 else _rate_limit_backoff_sec(attempt)
            logger.warning(
                "[RANKING API CLIENT] rate limited (429) attempt=%s/%s params=%s elapsed=%.3fs "
                "retry_after=%.2fs body=%s",
                attempt, retry_max, params, elapsed, wait, e.body,
            )
            _set_outcome(OUTCOME_RATE_LIMITED, e.body or "429", 429)
            last_exc = e
            if attempt < retry_max and wait > 0:
                time.sleep(min(wait, _rate_limit_max_wait_sec()))
            continue

        except RankingApiHttpError as e:
            elapsed = time.perf_counter() - t0
            logger.error(
                "[RANKING API CLIENT] HTTP %s attempt=%s/%s params=%s elapsed=%.3fs body=%s",
                e.status, attempt, retry_max, params, elapsed, e.body,
            )
            _set_outcome(
                OUTCOME_AUTH if e.status in (401, 403) else OUTCOME_HTTP_ERROR,
                e.body, e.status,
            )
            last_exc = e

        except requests.exceptions.Timeout as e:
            elapsed = time.perf_counter() - t0
            logger.warning(
                "[RANKING API CLIENT] read timeout attempt=%s/%s params=%s elapsed=%.3fs timeout=%.1fs",
                attempt, retry_max, params, elapsed, timeout_sec,
            )
            _set_outcome(OUTCOME_TIMEOUT, str(e), None)
            last_exc = e

        except requests.exceptions.RequestException as e:
            elapsed = time.perf_counter() - t0
            logger.warning(
                "[RANKING API CLIENT] transport error attempt=%s/%s params=%s elapsed=%.3fs err=%s: %s",
                attempt, retry_max, params, elapsed, type(e).__name__, e,
            )
            _set_outcome(OUTCOME_TRANSPORT, "%s: %s" % (type(e).__name__, e), None)
            last_exc = e

        except urllib.error.HTTPError as e:
            elapsed = time.perf_counter() - t0
            body_text = ""
            body_json = None
            try:
                raw_err = e.read()
                body_text = raw_err.decode("utf-8", errors="ignore")
                try:
                    body_json = json.loads(body_text)
                except Exception:
                    body_json = None
            except Exception:
                pass

            logger.warning(
                "[RANKING API CLIENT] HTTPError status=%s reason=%s attempt=%s/%s params=%s elapsed=%.3fs body=%s",
                getattr(e, "code", None),
                getattr(e, "reason", None),
                attempt,
                retry_max,
                params,
                elapsed,
                body_json if body_json is not None else body_text[:300],
            )
            last_exc = e

            if _is_api_key_mismatch(getattr(e, "code", None), body_json, body_text):
                logger.error(
                    "[RANKING API CLIENT] API key mismatch; runtime refresh disabled by startup-once policy params=%s token_len=%s. "
                    "Check settings.ini token source log immediately above; restart children after parent refresh if needed.",
                    params,
                    len(str(token or "")),
                )
                # APIキー不一致は同じsettings.ini tokenで再試行しても改善しないため、
                # live loopを詰まらせないよう即時に失敗を返す。
                break

        except urllib.error.URLError as e:
            elapsed = time.perf_counter() - t0
            logger.warning(
                "[RANKING API CLIENT] URLError reason=%s attempt=%s/%s params=%s elapsed=%.3fs",
                getattr(e, "reason", None),
                attempt,
                retry_max,
                params,
                elapsed,
            )
            last_exc = e

        except socket.timeout as e:
            elapsed = time.perf_counter() - t0
            logger.warning(
                "[RANKING API CLIENT] socket timeout attempt=%s/%s params=%s elapsed=%.3fs timeout=%.1fs",
                attempt,
                retry_max,
                params,
                elapsed,
                timeout_sec,
            )
            last_exc = e

        except json.JSONDecodeError as e:
            elapsed = time.perf_counter() - t0
            logger.exception(
                "[RANKING API CLIENT] json decode failed attempt=%s/%s params=%s elapsed=%.3fs",
                attempt,
                retry_max,
                params,
                elapsed,
            )
            last_exc = e

        except Exception as e:
            elapsed = time.perf_counter() - t0
            logger.exception(
                "[RANKING API CLIENT] unexpected error attempt=%s/%s params=%s elapsed=%.3fs",
                attempt,
                retry_max,
                params,
                elapsed,
            )
            last_exc = e

        if attempt < retry_max:
            sleep_sec = API_RETRY_SLEEP_SEC * attempt
            logger.info(
                "[RANKING API CLIENT] retry sleep=%.2fs next_attempt=%s params=%s",
                sleep_sec,
                attempt + 1,
                params,
            )
            time.sleep(sleep_sec)

    outcome, err, status = last_outcome()
    logger.error(
        "[RANKING API CLIENT] request failed after retries params=%s outcome=%s status=%s "
        "last_exc=%r detail=%s",
        params, outcome, status, last_exc, err,
    )
    return None
