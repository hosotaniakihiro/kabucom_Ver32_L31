# -*- coding: utf-8 -*-
# ============================================================
# File   : core/startup/intraday_load_guard.py
# Version: V2-INTRADAY-LOAD-GUARD-YAHOO-FAILCACHE-PUSH-HARD100
# ------------------------------------------------------------
# 旧 core/startup/intraday_load_guard_patch.py の残り 2 つ
# (パッチではなく通常の設定/サードパーティ用モジュール)。
#
#   1) env 既定値の setdefault
#   2) yfinance.download の fail-cache ラップ
#
# 関数ラップのうち本体側へ畳めたものは移設済み:
#   core/yahoo_tasks._start_yahoo_worker                     (スロットル)
#   trading/push/push_stream/rotation_symbols.resolve_*      (100件クランプ)
#   trading/push/push_stream/rotation_stability_patch._d     (中間ログのクランプ)
#
# 移していないもの (旧パッチ内で完全な no-op だったため):
#   _patch_yahoo_fail_cache() のモジュール総当たりスイープ。
#   候補 7 モジュール x 候補 14 関数名を調べたが、実在する組み合わせは 0 件。
#   (trading/yahoo/pipeline/download.py など 4 モジュールはそもそも存在せず、
#    実在する compute.py / complement_scheduler.py / core/yahoo_tasks.py には
#    download_1m 等の名前が一つも無い)。
#   したがって _wrap_download_function も一度も使われていなかった。
#   実効があったのは下の yfinance.download ラップだけ。
#
# yfinance はサードパーティなので本体へ畳めない。ここに残す。
# ============================================================
from __future__ import annotations

import logging
import os
import sys
import time
from typing import Any

logger = logging.getLogger(__name__)

VERSION = "V2-INTRADAY-LOAD-GUARD-YAHOO-FAILCACHE-PUSH-HARD100"
_INSTALLED = False
_TRUE = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
_YFINANCE_FAIL_CACHE: dict[str, float] = {}


def _env_bool(name: str, default: bool = False) -> bool:
    try:
        raw = str(os.getenv(name, "")).strip().lower()
        if raw in _TRUE:
            return True
        if raw in {"0", "false", "no", "n", "off", "disable", "disabled"}:
            return False
    except Exception:
        pass
    return bool(default)


def _env_int(name: str, default: int, *, min_value: int | None = None, max_value: int | None = None) -> int:
    try:
        v = int(float(str(os.getenv(name, str(default))).replace(",", "")))
    except Exception:
        v = int(default)
    if min_value is not None:
        v = max(v, min_value)
    if max_value is not None:
        v = min(v, max_value)
    return int(v)


def _norm_symbol(symbol: Any) -> str:
    s = str(symbol or "").strip().upper()
    if s.endswith(".T"):
        s = s[:-2]
    return s


def _symbol_from_yfinance_args(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    ticker = kwargs.get("tickers") or kwargs.get("ticker") or kwargs.get("symbol")
    if ticker is None and args:
        ticker = args[0]
    if isinstance(ticker, (list, tuple, set)):
        ticker = next(iter(ticker), "")
    text = str(ticker or "").strip()
    # yfinance often receives "5074.T". Keep the same cache key normalized.
    return _norm_symbol(text)


def _set_default_envs() -> None:
    """Set safer defaults before yahoo_tasks / rotation modules read env."""
    # 日中Yahoo補完はランキング/PUSHを主軸にし、1分ごとの重複workerを避ける。
    os.environ.setdefault("YAHOO_COMPLEMENT_EVERY_SECONDS", "300")
    os.environ.setdefault("YAHOO_COMPLEMENT_TIMEOUT_SEC", "180")
    os.environ.setdefault("YAHOO_COMPLEMENT_STUCK_GRACE_SEC", "15")
    os.environ.setdefault("YAHOO_COMPLEMENT_STUCK_COOLDOWN_SEC", "180")
    os.environ.setdefault("YAHOO_COMPLEMENT_MIN_START_GAP_SEC", "240")
    os.environ.setdefault("YAHOO_COMPLEMENT_SKIP_LOG_EVERY", "5")

    # Yahooで当日失敗した銘柄は短時間で再試行しない。
    os.environ.setdefault("YAHOO_SYMBOL_FAIL_BACKOFF_SEC", "1800")
    os.environ.setdefault("YAHOO_SYMBOL_FAIL_CACHE_MAX", "1000")

    # PUSH候補は各段階で100固定に寄せる。
    os.environ.setdefault("PUSH_ROTATION_TARGET_MIN_KEEP", "100")
    os.environ.setdefault("PUSH_ROTATION_TARGET_MAX_KEEP", "100")
    os.environ.setdefault("PUSH_REGISTER_MIN_KEEP", "100")
    os.environ.setdefault("PUSH_REGISTER_MAX_KEEP", "100")


def _apply_yfinance_download_wrap() -> bool:
    """yfinance.download を fail-cache でラップする (yfinance が import 済みの前提)。"""
    try:
        import yfinance as yf  # type: ignore
    except Exception:
        logger.debug("[INTRADAY LOAD GUARD] yfinance not importable", exc_info=True)
        return False

    old_download = getattr(yf, "download", None)
    if not callable(old_download):
        return False
    if getattr(old_download, "_intraday_load_guard_yf_wrapped", False):
        return True

    def _download_wrapped(*args: Any, **kwargs: Any) -> Any:
        key = _symbol_from_yfinance_args(args, kwargs)
        backoff = _env_int("YAHOO_SYMBOL_FAIL_BACKOFF_SEC", 1800, min_value=0, max_value=86400)
        now_ts = time.time()
        if key and backoff > 0:
            until = _YFINANCE_FAIL_CACHE.get(key, 0.0)
            if until > now_ts:
                logger.info(
                    "[INTRADAY LOAD GUARD] yfinance.download skipped by fail-cache symbol=%s remain=%.0fs",
                    key,
                    until - now_ts,
                )
                try:
                    import pandas as pd  # type: ignore
                    return pd.DataFrame()
                except Exception:
                    return None
        try:
            ret = old_download(*args, **kwargs)
            empty = ret is None
            try:
                empty = empty or bool(getattr(ret, "empty", False))
            except Exception:
                pass
            if key and empty and backoff > 0:
                _YFINANCE_FAIL_CACHE[key] = now_ts + backoff
            return ret
        except Exception:
            if key and backoff > 0:
                _YFINANCE_FAIL_CACHE[key] = now_ts + backoff
            raise

    _download_wrapped._intraday_load_guard_yf_wrapped = True  # type: ignore[attr-defined]
    _download_wrapped._original = old_download  # type: ignore[attr-defined]
    try:
        yf.download = _download_wrapped  # type: ignore[assignment]
        logger.warning("[INTRADAY LOAD GUARD] yfinance.download fail-cache patched backoff=%s", os.getenv("YAHOO_SYMBOL_FAIL_BACKOFF_SEC"))
        return True
    except Exception:
        logger.exception("[INTRADAY LOAD GUARD] yfinance.download fail-cache patch failed")
        return False


# ============================================================
# yfinance の遅延ラップ (2026-09-03)
# ------------------------------------------------------------
# 旧実装は install() から無条件に `import yfinance` していた。
# yfinance は pandas / numpy / requests / bs4 / curl_cffi / protobuf を
# 芋づるで引き込み、NAS(SMB) 上の実測で **単独 43.9 秒** かかっていた
# (push_receiver context の probe: 全 48.05 秒中 43.88 秒)。
#
# yfinance.download を実際に呼ぶのは Yahoo 補完系だけで、
# push_receiver / db_prepare / ranking_collector / summary_database は
# 一度も呼ばない。それらのプロセスでは丸ごと無駄だった。
#
# ここでは import 済みなら即ラップし、未 import なら meta_path に
# post-import hook を置く。誰かが yfinance を import した瞬間に
# 同じラップが必ず適用されるので、ガードが外れる経路は無い。
#
# INTRADAY_LOAD_GUARD_YFINANCE_EAGER=1 で従来の即時 import に戻せる。
# ============================================================
_YF_HOOK_INSTALLED = False


class _YFinanceLazyPatchFinder:
    """yfinance の import 完了直後に download ラップを当てる meta_path finder。"""

    def find_spec(self, fullname, path=None, target=None):
        if fullname != "yfinance":
            return None
        # 自分を外してから本来の spec を取り直す (無限再帰防止)。
        try:
            sys.meta_path.remove(self)
        except ValueError:
            pass
        try:
            import importlib.util

            spec = importlib.util.find_spec(fullname)
        except Exception:
            logger.debug("[INTRADAY LOAD GUARD] yfinance spec lookup failed", exc_info=True)
            return None
        if spec is None or spec.loader is None:
            return None

        loader = spec.loader
        original_exec_module = getattr(loader, "exec_module", None)
        if not callable(original_exec_module):
            return spec

        def _exec_module(module):
            original_exec_module(module)
            try:
                ok = _apply_yfinance_download_wrap()
                logger.warning(
                    "[INTRADAY LOAD GUARD] yfinance imported later -> download fail-cache applied ok=%s", ok
                )
            except Exception:
                logger.exception("[INTRADAY LOAD GUARD] deferred yfinance wrap failed")

        loader.exec_module = _exec_module  # type: ignore[assignment]
        return spec


def _patch_yfinance_download_fail_cache() -> Any:
    """yfinance.download のラップを「今」か「import された時」に必ず適用する。

    戻り値: True (適用済み) / "deferred" (hook 設置) / False (失敗)
    """
    global _YF_HOOK_INSTALLED

    if "yfinance" in sys.modules:
        return _apply_yfinance_download_wrap()

    if _env_bool("INTRADAY_LOAD_GUARD_YFINANCE_EAGER", False):
        return _apply_yfinance_download_wrap()

    if _YF_HOOK_INSTALLED:
        return "deferred"

    try:
        sys.meta_path.insert(0, _YFinanceLazyPatchFinder())
        _YF_HOOK_INSTALLED = True
        logger.warning(
            "[INTRADAY LOAD GUARD] yfinance not imported yet -> deferred download wrap via meta_path hook "
            "(yfinance の即時 import は NAS 上で 43.9 秒かかるため。"
            "INTRADAY_LOAD_GUARD_YFINANCE_EAGER=1 で従来動作)"
        )
        return "deferred"
    except Exception:
        logger.exception("[INTRADAY LOAD GUARD] yfinance lazy hook install failed")
        return _apply_yfinance_download_wrap()


def install() -> bool:
    global _INSTALLED
    if _env_bool("DISABLE_INTRADAY_LOAD_GUARD_PATCH", False):
        logger.warning("[INTRADAY LOAD GUARD] disabled by env")
        return False

    _set_default_envs()
    yf_ok = _patch_yfinance_download_fail_cache()

    first_install = not _INSTALLED
    _INSTALLED = True
    logger.warning(
        "[INTRADAY LOAD GUARD] installed version=%s first=%s yahoo_fail_cache=%s yahoo_every=%s yahoo_timeout=%s",
        VERSION,
        first_install,
        yf_ok,
        os.getenv("YAHOO_COMPLEMENT_EVERY_SECONDS"),
        os.getenv("YAHOO_COMPLEMENT_TIMEOUT_SEC"),
    )
    return True


__all__ = ["VERSION", "install"]
