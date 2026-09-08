# ============================================================
# File   : kabu_api/rest_limits.py
# Version: V1.0-KABU-REST-READ-POLICY-SINGLE-OWNER
# ------------------------------------------------------------
# kabu Station REST の「読み取り API をどれくらいの間隔で叩いてよいか」を
# 決める数値の唯一の持ち主。
#
# 背景 (2026-09-01 のログ):
#   同じ /orders・/positions を叩く経路が複数あり、それぞれが
#   自前のタイムアウト・リトライ・間隔を持っていた。
#
#     force_cancel_loop        timeout 5s  / 2 回 / 独自 min_interval
#     pending_monitor          timeout 5s  / 独自
#     kabu_api.positions       timeout 10s / 3 回リトライ (= 1 呼び出し 31 秒)
#     kabu_position_reader     timeout 3s  / 独自
#     trading.ranking.api_client timeout 5s / 2 回
#
#   localhost:18080 が無応答になったとき、これらが同時に走って
#   「読めない相手へ読みに行く」ことそのものが kabu Station の
#   同時接続を占有し、障害を増幅していた (本ログだけで /orders 失敗 1000 件超)。
#
#   数値が各モジュールに散っていると、間隔を見直すたびに全部を
#   直さなければならず、実際 1 箇所だけ古い値が残っていた。
#
# 責務:
#   - API 種別ごとの ReadPolicy (TTL / 最短間隔 / timeout / backoff) を返す
#   - 解決順序を 1 本にする: ENV -> settings.ini [API_RATE_LIMIT] -> 既定
#   - 起動時に実効値を 1 回だけログへ出す
#
# 安全規則:
#   - ここは「読み取り」専用。発注 / 取消 / 返済 (POST/PUT/DELETE) の
#     タイムアウトはここでは決めない。注文送信をこの間隔で絞ってはならない。
# ============================================================

from __future__ import annotations

import configparser
import logging
import os
import threading
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

SETTINGS_SECTION = "API_RATE_LIMIT"


@dataclass(frozen=True)
class ReadPolicy:
    """1 つの読み取り API に対する共有ポリシー。"""

    name: str
    cache_ttl_sec: float
    min_interval_sec: float
    timeout_sec: float
    backoff_sec: Tuple[float, ...]

    def as_log_dict(self) -> dict:
        return {
            "cache_ttl_sec": self.cache_ttl_sec,
            "min_interval_sec": self.min_interval_sec,
            "timeout_sec": self.timeout_sec,
            "backoff_sec": list(self.backoff_sec),
        }


# ------------------------------------------------------------
# 既定値
#   cache_ttl_sec    : この間は実リクエストを出さず共有キャッシュを返す
#   min_interval_sec : 成功していても実リクエストはこの間隔より詰めない
#   timeout_sec      : 1 リクエストの読み取りタイムアウト
#   backoff_sec      : 連続失敗時の待ち (最後の値で頭打ち)
# ------------------------------------------------------------
_DEFAULTS: Dict[str, ReadPolicy] = {
    "orders": ReadPolicy(
        name="orders",
        cache_ttl_sec=2.0,
        min_interval_sec=1.0,
        timeout_sec=5.0,
        backoff_sec=(1.0, 3.0, 5.0, 10.0, 30.0),
    ),
    "positions": ReadPolicy(
        name="positions",
        cache_ttl_sec=1.0,
        min_interval_sec=1.0,
        timeout_sec=3.0,
        backoff_sec=(1.0, 2.0, 5.0, 10.0, 30.0),
    ),
    "ranking": ReadPolicy(
        name="ranking",
        cache_ttl_sec=0.0,          # ranking は毎分の収集そのものが実リクエスト
        min_interval_sec=0.5,       # 28 呼び出しの spacing
        timeout_sec=5.0,
        backoff_sec=(30.0, 60.0, 120.0, 300.0),
    ),
}

# 旧実装が使っていた ENV 名。互換のため最優先で見る。
_LEGACY_ENV: Dict[Tuple[str, str], Tuple[str, ...]] = {
    ("orders", "cache_ttl_sec"): ("KABU_ORDERS_CACHE_TTL_SEC",),
    ("orders", "backoff_sec"): ("KABU_ORDERS_FAIL_BACKOFF_SEC",),
    ("orders", "min_interval_sec"): ("KABU_GET_ORDERS_MIN_INTERVAL_SEC",),
    ("positions", "cache_ttl_sec"): ("POSITION_STATE_CACHE_TTL_SEC",),
    ("positions", "backoff_sec"): ("POSITION_STATE_BROKER_BACKOFF_SEC",),
    ("positions", "timeout_sec"): ("KABU_POSITIONS_TIMEOUT_SEC",),
}

_LOCK = threading.RLock()
_CACHE: Dict[str, ReadPolicy] = {}
_SETTINGS: Optional[Dict[str, str]] = None
_LOGGED = False


def _settings_map() -> Dict[str, str]:
    """settings.ini [API_RATE_LIMIT] を小文字キーで読む (無ければ空)。"""
    global _SETTINGS
    if _SETTINGS is not None:
        return _SETTINGS

    out: Dict[str, str] = {}
    try:
        path = os.getenv("AUTOSTOCK_SETTINGS_INI", "").strip()
        if not path:
            here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            path = os.path.join(here, "settings.ini")
        if os.path.exists(path):
            cp = configparser.ConfigParser()
            # settings.ini は環境によって BOM 付き UTF-8。
            cp.read(path, encoding="utf-8-sig")
            for section in cp.sections():
                if section.strip().lower() != SETTINGS_SECTION.lower():
                    continue
                for k, v in cp.items(section):
                    out[str(k).strip().lower()] = str(v).strip()
    except Exception:
        logger.debug("[REST LIMITS] settings.ini 読み取りに失敗 (既定値を使う)", exc_info=True)

    _SETTINGS = out
    return out


def _raw_value(api: str, field: str) -> Optional[str]:
    """ENV -> settings.ini の順で生の設定値を探す。"""
    for name in _LEGACY_ENV.get((api, field), ()):  # 旧 ENV 名
        v = os.getenv(name)
        if v is not None and str(v).strip() != "":
            return str(v).strip()

    env_name = "KABU_READ_%s_%s" % (api.upper(), field.upper())
    v = os.getenv(env_name)
    if v is not None and str(v).strip() != "":
        return str(v).strip()

    smap = _settings_map()
    for key in ("%s_%s" % (api, field), "%s.%s" % (api, field)):
        if key in smap and smap[key] != "":
            return smap[key]
    return None


def _as_float(raw: Optional[str], default: float, *, floor: float = 0.0) -> float:
    if raw is None:
        return float(default)
    try:
        return max(float(floor), float(raw))
    except Exception:
        logger.warning("[REST LIMITS] 数値として読めない値なので既定を使う raw=%r default=%s", raw, default)
        return float(default)


def _as_backoff(raw: Optional[str], default: Tuple[float, ...]) -> Tuple[float, ...]:
    if raw is None:
        return tuple(default)
    out = []
    for part in str(raw).split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(max(0.5, float(part)))
        except Exception:
            continue
    return tuple(out) if out else tuple(default)


def policy(api: str) -> ReadPolicy:
    """API 種別の読み取りポリシーを返す。未知の名前は positions 相当を返す。"""
    key = str(api or "").strip().lower()
    with _LOCK:
        cached = _CACHE.get(key)
        if cached is not None:
            return cached

        base = _DEFAULTS.get(key)
        if base is None:
            logger.warning("[REST LIMITS] 未知の API 名 api=%s -> positions の既定を使う", api)
            base = _DEFAULTS["positions"]

        resolved = ReadPolicy(
            name=key or base.name,
            cache_ttl_sec=_as_float(_raw_value(key, "cache_ttl_sec"), base.cache_ttl_sec),
            min_interval_sec=_as_float(_raw_value(key, "min_interval_sec"), base.min_interval_sec),
            timeout_sec=_as_float(_raw_value(key, "timeout_sec"), base.timeout_sec, floor=0.5),
            backoff_sec=_as_backoff(_raw_value(key, "backoff_sec"), base.backoff_sec),
        )
        _CACHE[key] = resolved
        return resolved


def log_effective_policies_once() -> dict:
    """実効値を 1 回だけ WARNING で出す (起動時の証跡)。"""
    global _LOGGED
    snapshot = {name: policy(name).as_log_dict() for name in sorted(_DEFAULTS)}
    with _LOCK:
        if _LOGGED:
            return snapshot
        _LOGGED = True
    logger.warning(
        "[REST LIMITS] kabu Station 読み取り API の実効ポリシー %s "
        "(解決順序: ENV -> settings.ini [%s] -> 既定。数値の持ち主はこのモジュールだけ)",
        snapshot, SETTINGS_SECTION,
    )
    return snapshot


def reset_for_test() -> None:
    global _SETTINGS, _LOGGED
    with _LOCK:
        _CACHE.clear()
        _SETTINGS = None
        _LOGGED = False


__all__ = [
    "ReadPolicy",
    "policy",
    "log_effective_policies_once",
    "reset_for_test",
    "SETTINGS_SECTION",
]
