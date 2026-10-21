# ============================================================
# File   : kabu_api/kabu_http.py
# Version: KABU-HTTP-V1-NO-CONNECTION-CLOSE
# Function:
#   - kabu Station へ HTTP を投げるときの共通入口
#   - urllib.request を使わない理由をここ 1 箇所に集約する
# ============================================================
"""kabu Station 宛の HTTP は必ずここを通す。

urllib.request は毎回 ``Connection: close`` を送る。kabu Station は
このヘッダーが付いたリクエストに応答を返さず、接続を掴んだまま
タイムアウトさせる (2026-08-29 実測。commit dddb0a7 で token 系を修正):

    最小ヘッダー (X-API-KEY のみ) -> HTTP 401  0.01s
    + Content-Type               -> HTTP 401  0.00s
    + Accept-Encoding: identity  -> HTTP 401  0.00s
    + Connection: close          -> 応答なし  5.00s  ★これだけが原因

そのため urllib で叩いていた経路だけが必ず timeout し、
「kabu Station が実処理を伴うリクエストだけ無応答になる」ように見えていた。
実際には requests で叩いていた /positions・/ranking は同時刻でも 200 を返す。

requests は keep-alive で送るので影響を受けない。
呼び出し側の except 節をそのまま使えるよう、非 2xx は urllib 時代と同じ
``urllib.error.HTTPError`` にして送出する (``.code`` / ``.read()`` も従来どおり)。

IPv4/IPv6・プロキシ・ポート除外・API パスワードはいずれも無関係。
"""

from __future__ import annotations

import io
import json
import urllib.error
from typing import Any, Mapping, Optional

import requests

__all__ = ["request_json", "request_text", "KABU_DEFAULT_TIMEOUT_SEC"]

KABU_DEFAULT_TIMEOUT_SEC = 5.0


def _decode_json(raw: str) -> Any:
    if not raw:
        return ""
    try:
        return json.loads(raw)
    except Exception:
        return raw


def request_json(
    url: str,
    *,
    method: str = "GET",
    payload: Optional[Any] = None,
    api_key: str = "",
    timeout: float = KABU_DEFAULT_TIMEOUT_SEC,
    headers: Optional[Mapping[str, str]] = None,
    parse_json: bool = True,
) -> Any:
    """kabu Station を叩いて JSON を返す。

    - 2xx: パースした JSON (パースできなければ本文の文字列)。
      ``parse_json=False`` なら本文の文字列をそのまま返す
      (urllib 時代に ``res.read().decode()`` を返していた呼び出し用)
    - 非 2xx: ``urllib.error.HTTPError`` を送出する。``.code`` と ``.read()`` は
      urllib で叩いていたときと同じように使える
    - 接続断・タイムアウト: requests の例外がそのまま上がる
    """
    status, raw = request_text(
        url,
        method=method,
        payload=payload,
        api_key=api_key,
        timeout=timeout,
        headers=headers,
    )
    return _decode_json(raw) if parse_json else raw


def request_text(
    url: str,
    *,
    method: str = "GET",
    payload: Optional[Any] = None,
    api_key: str = "",
    timeout: float = KABU_DEFAULT_TIMEOUT_SEC,
    headers: Optional[Mapping[str, str]] = None,
) -> tuple[int, str]:
    """``(status, 本文)`` を返す。ステータスコードまで必要な呼び出し用。

    非 2xx の扱いは request_json と同じ (``urllib.error.HTTPError``)。
    """
    method = (str(method or "GET") or "GET").upper()

    sent = {"Content-Type": "application/json"}
    if api_key:
        sent["X-API-KEY"] = str(api_key)
    if headers:
        sent.update({str(k): str(v) for k, v in headers.items()})

    body = None
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    res = requests.request(method, url, data=body, headers=sent, timeout=timeout)

    raw = res.text or ""
    if 200 <= int(res.status_code) < 300:
        return int(res.status_code), raw

    raise urllib.error.HTTPError(
        url,
        int(res.status_code),
        str(res.reason or ""),
        res.headers,  # type: ignore[arg-type]
        io.BytesIO(raw.encode("utf-8", errors="replace")),
    )
