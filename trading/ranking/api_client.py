# ============================================================
# File   : trading/ranking/api_client.py
# Version: Ver2.1-RANKING-API-CLIENT-TOKEN-REFRESH-ON-401
# ------------------------------------------------------------
# ✔ kabu Station ranking API client
# ✔ timeout 対応
# ✔ retry 対応
# ✔ HTTPError / URLError / socket timeout / JSON decode safe
# ✔ 4001009(APIキー不一致) / 401 時に refresh_token() して即時再試行
# ✔ logger 統一
# ✔ elapsed ログ
# ============================================================

from __future__ import annotations

import json
import logging
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

try:
    from token_manager import get_valid_token, refresh_token
except Exception:  # pragma: no cover - startup import safety
    from token_manager import get_valid_token  # type: ignore
    refresh_token = None  # type: ignore

logger = logging.getLogger(__name__)

RANKING_API_URL = "http://localhost:18080/kabusapi/ranking"
API_TIMEOUT_SEC = 5.0
API_RETRY_MAX = 2
API_RETRY_SLEEP_SEC = 0.35
API_KEY_MISMATCH_CODE = 4001009


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


def _refresh_token_after_mismatch(params: dict[str, Any], attempt: int) -> str | None:
    """401/4001009 の直後に token を再取得する。失敗しても呼び出し元で通常retryに戻す。"""
    if refresh_token is None:
        logger.warning(
            "[RANKING API CLIENT] token refresh unavailable after API key mismatch attempt=%s params=%s",
            attempt,
            params,
        )
        return None

    try:
        token = refresh_token()
        if token:
            logger.warning(
                "[RANKING API CLIENT] refreshed token after API key mismatch attempt=%s token_len=%s params=%s",
                attempt,
                len(str(token)),
                params,
            )
            return str(token)
    except Exception:
        logger.exception(
            "[RANKING API CLIENT] token refresh failed after API key mismatch attempt=%s params=%s",
            attempt,
            params,
        )
    return None


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
    refreshed_once = False

    for attempt in range(1, retry_max + 1):
        t0 = time.perf_counter()
        try:
            token = get_valid_token()

            logger.info(
                "[RANKING API CLIENT] request start attempt=%s/%s params=%s timeout=%.1fs",
                attempt,
                retry_max,
                params,
                timeout_sec,
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

            if (not refreshed_once) and _is_api_key_mismatch(getattr(e, "code", None), body_json, body_text):
                refreshed_once = True
                new_token = _refresh_token_after_mismatch(params, attempt)
                if new_token:
                    retry_t0 = time.perf_counter()
                    try:
                        payload = _request_once(params, new_token, timeout_sec)
                        retry_elapsed = time.perf_counter() - retry_t0
                        rows = payload.get("Ranking") if isinstance(payload, dict) else None
                        row_count = len(rows) if isinstance(rows, list) else 0
                        logger.info(
                            "[RANKING API CLIENT] retry after token refresh ok attempt=%s/%s params=%s rows=%s elapsed=%.3fs",
                            attempt,
                            retry_max,
                            params,
                            row_count,
                            retry_elapsed,
                        )
                        return payload
                    except Exception as retry_exc:
                        last_exc = retry_exc if isinstance(retry_exc, Exception) else e
                        logger.warning(
                            "[RANKING API CLIENT] retry after token refresh failed attempt=%s/%s params=%s err=%r",
                            attempt,
                            retry_max,
                            params,
                            retry_exc,
                        )

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
