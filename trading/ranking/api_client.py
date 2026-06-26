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
import urllib.error
import urllib.parse
import urllib.request
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


def _request_once(params: dict[str, Any], token: str | None, timeout_sec: float) -> dict[str, Any]:
    query = urllib.parse.urlencode(params)
    url = f"{RANKING_API_URL}?{query}"

    req = urllib.request.Request(url, method="GET")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-API-KEY", str(token))

    with urllib.request.urlopen(req, timeout=timeout_sec) as res:
        raw = res.read()

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
            return payload

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

    logger.error(
        "[RANKING API CLIENT] request failed after retries params=%s last_exc=%r",
        params,
        last_exc,
    )
    return None
