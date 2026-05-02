# ============================================================
# File   : trading/push/push_stream/rotation.py
# Version: PRODUCTION-STABLE-REV2.5-PUSH-STREAM-ROTATION-A-B-REGISTER-LIQUIDITY-GUARD
# ------------------------------------------------------------
# 【概要】
#   kabu Station PUSH 登録銘柄を A/B 50銘柄ずつローテーションする。
#
# 【重要方針】
#   - 100銘柄候補を A面50 / B面50 に分割
#   - A面を登録して5秒受信
#   - 全部解除
#   - 0.5秒待機
#   - B面を登録して5秒受信
#   - 全部解除
#   - 0.5秒待機
#   - これを繰り返す
#
# 【登録方式】
#   - WebSocketへ register payload は送らない
#   - subscription_manager.core.refresh_subscriptions へ委譲する
#   - 実登録は register_ops.py の HTTP API に任せる
#
# 【REV2.5 修正点】
#   - PUSH登録対象100銘柄を作成後、A/B分割前に流動性ガードを適用
#   - trading.push.subscription_manager.liquidity_guard を利用
#   - 日中出来高・売買代金が少ない銘柄をPUSH登録対象から除外
#   - ガード失敗時は runtime 継続のため元リストを維持
#   - ガードで全銘柄除外された場合は登録をskip
#
# Expected log:
#   [push_stream] rotation worker started ...
#   [push_stream] resolved register targets before_liquidity ...
#   [PUSH LIQUIDITY GUARD] source=push_stream.rotation before=100 after=...
#   [push_stream] resolved register targets after_liquidity ...
#   [push_stream] rotation cycle targets=...
#   [PUSH ROTATION REGISTER TARGETS LINE] label=A reason=rotation_A count=50 symbols=...
#   [PUSH ROTATION REGISTER TARGETS LINE] label=B reason=rotation_B count=50 symbols=...
# ============================================================

from __future__ import annotations

import concurrent.futures
import importlib
import logging
import os
import time
from typing import Any, Callable, Iterable, List, Optional, Sequence, Tuple

from .constants import (
    DEFAULT_REGISTER_CHUNK_SIZE,
    DEFAULT_REGISTER_MAX_SYMBOLS,
    DEFAULT_ROTATE_WAIT_SEC,
)
from . import state
from .runtime import _safe_get_runtime
from .transport import (
    get_ws_sender,
    _is_ws_alive,
    _call_refresh,
)
from .normalize import _normalize_symbol

logger = logging.getLogger(__name__)

VERSION = (
    "PRODUCTION-STABLE-REV2.5-PUSH-STREAM-ROTATION-A-B-"
    "REGISTER-LIQUIDITY-GUARD"
)


# ============================================================
# Optional liquidity guard import
# ------------------------------------------------------------
# 起動順や配置差異で import 失敗しても rotation 自体は止めない。
# ============================================================

try:
    from trading.push.subscription_manager.liquidity_guard import (
        filter_register_targets_by_liquidity,
    )
except Exception:
    filter_register_targets_by_liquidity = None  # type: ignore[assignment]
    logger.warning(
        "[push_stream] liquidity_guard import failed. "
        "PUSH register liquidity guard disabled.",
        exc_info=True,
    )


# ============================================================
# Settings
# ============================================================

ROTATE_HOLD_SEC = float(
    os.environ.get("PUSH_ROTATION_HOLD_SEC", str(DEFAULT_ROTATE_WAIT_SEC or 5.0))
)

UNREGISTER_TO_REGISTER_WAIT_SEC = float(
    os.environ.get("PUSH_ROTATION_UNREGISTER_WAIT_SEC", "0.1")
)

WS_WAIT_LOG_INTERVAL_SEC = float(
    os.environ.get("PUSH_ROTATION_WS_WAIT_LOG_INTERVAL_SEC", "4.9")
)

REGISTER_TIMEOUT_SEC = float(
    os.environ.get("PUSH_ROTATION_REGISTER_TIMEOUT_SEC", "3.0")
)


_RUNTIME_SYMBOL_KEYS: Tuple[str, ...] = (
    "monitor_symbols",
    "candidate_push_symbols",
    "push_candidate_symbols",
    "push_symbols_100",
    "active_symbols",
    "ats_register_targets",
    "ats_targets",
    "should_register_symbols",
    "push_symbols",
    "register_symbols",
    "subscription_symbols",
)

_GLOBAL_DATA_SYMBOL_ATTRS: Tuple[str, ...] = (
    "monitor_symbols",
    "candidate_push_symbols",
    "push_candidate_symbols",
    "push_symbols_100",
    "active_symbols",
    "ats_register_targets",
    "ats_targets",
    "push_symbols",
    "register_symbols",
    "subscription_symbols",
    "daily_watchlist_symbols",
)

_DYNAMIC_SYMBOL_PROVIDERS: Tuple[Tuple[str, str], ...] = (
    ("trading.ranking.active_symbol_manager", "get_active_symbols"),
    ("trading.ranking.active_symbol_manager", "get_monitor_symbols"),
    ("trading.ranking.active_symbol_manager", "get_push_symbols"),
    ("trading.ranking.active_symbol_manager", "get_register_symbols"),
    ("trading.ranking.active_symbol_manager", "get_current_active_symbols"),
    ("core.startup.symbol_bootstrap", "get_active_symbols"),
    ("core.startup.symbol_bootstrap", "get_monitor_symbols"),
    ("core.startup.push_bootstrap", "get_push_symbols"),
    ("core.startup.push_stream_bootstrap", "get_push_symbols"),
    ("optional.batch.daily_watchlist", "get_daily_watchlist_symbols"),
    ("optional.batch.daily_watchlist", "load_daily_watchlist_symbols"),
)


# ============================================================
# Symbol normalize
# ============================================================

def _is_filler_symbol(symbol: Any) -> bool:
    s = str(symbol).strip().upper()
    return (
        not s
        or s.startswith("FILLER")
        or s in {"NONE", "NULL", "NAN", "NA", "-", "0"}
    )


def _is_real_symbol(symbol: Any) -> bool:
    if symbol is None:
        return False

    s = str(symbol).strip().upper()

    if _is_filler_symbol(s):
        return False

    if s.endswith(".T"):
        s = s[:-2]

    if s.endswith(".0"):
        s2 = s[:-2]
        if s2.isdigit():
            s = s2

    if not s.isalnum():
        return False

    if not (3 <= len(s) <= 5):
        return False

    return True


def _normalize_real_symbol(symbol: Any) -> Optional[str]:
    try:
        s = _normalize_symbol(symbol)
    except Exception:
        s = str(symbol).strip().upper() if symbol is not None else ""

    if not s:
        return None

    s = str(s).strip().upper()

    if s.endswith(".T"):
        s = s[:-2]

    if s.endswith(".0"):
        s2 = s[:-2]
        if s2.isdigit():
            s = s2

    if not _is_real_symbol(s):
        return None

    return s


def _dedupe_keep_order(items: Iterable[str]) -> List[str]:
    out: List[str] = []
    seen: set[str] = set()

    for x in items:
        s = str(x).strip().upper()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)

    return out


def _clean_symbol_list(src: Any) -> Tuple[List[str], int, int, int]:
    if src is None:
        return [], 0, 0, 0

    try:
        if hasattr(src, "columns"):
            cols = list(getattr(src, "columns", []))
            symbol_col = None
            for c in ("symbol", "Symbol", "code", "Code", "銘柄コード"):
                if c in cols:
                    symbol_col = c
                    break
            if symbol_col:
                src = src[symbol_col].tolist()
    except Exception:
        pass

    if isinstance(src, dict):
        for key in (
            "symbols",
            "codes",
            "items",
            "monitor_symbols",
            "active_symbols",
            "candidate_push_symbols",
            "push_candidate_symbols",
            "push_symbols_100",
            "ats_targets",
            "ats_register_targets",
            "data",
        ):
            if key in src and src[key]:
                src = src[key]
                break
        else:
            src = list(src.keys())

    if isinstance(src, str):
        src = [src]

    try:
        seq = list(src)
    except Exception:
        return [], 0, 0, 0

    raw_count = len(seq)
    filler_count = 0
    invalid_count = 0
    real: List[str] = []

    for x in seq:
        if _is_filler_symbol(x):
            filler_count += 1
            continue

        s = _normalize_real_symbol(x)
        if s:
            real.append(s)
        else:
            invalid_count += 1

    real = _dedupe_keep_order(real)
    return real, raw_count, filler_count, invalid_count


# ============================================================
# Name log
# ============================================================

def _log_register_targets_with_names(
    symbols: Sequence[str],
    *,
    label: str,
    reason: str,
) -> None:
    """
    登録対象50銘柄を 銘柄コード(銘柄名) 形式で1行表示する。
    """
    try:
        cleaned, _, _, _ = _clean_symbol_list(symbols)
        cleaned = cleaned[:DEFAULT_REGISTER_CHUNK_SIZE]

        if not cleaned:
            logger.warning(
                "[PUSH ROTATION REGISTER TARGETS LINE] label=%s reason=%s count=0 symbols=",
                label,
                reason,
            )
            return

        try:
            from trading.push.subscription_manager.register_symbol_logger import (
                format_symbols_one_line,
                load_symbol_name_map,
            )

            name_map = load_symbol_name_map()
            line = format_symbols_one_line(
                cleaned,
                symbol_name_map=name_map,
            )

            logger.info(
                "[PUSH ROTATION REGISTER TARGETS LINE] label=%s reason=%s count=%d symbols=%s",
                label,
                reason,
                len(cleaned),
                line,
            )
            return

        except Exception:
            logger.debug(
                "[push_stream] register_symbol_logger name format failed -> code only fallback",
                exc_info=True,
            )

        logger.info(
            "[PUSH ROTATION REGISTER TARGETS LINE] label=%s reason=%s count=%d symbols=%s",
            label,
            reason,
            len(cleaned),
            ", ".join(cleaned),
        )

    except Exception:
        logger.exception(
            "[push_stream] failed to log register targets with names label=%s reason=%s",
            label,
            reason,
        )


# ============================================================
# Provider helpers
# ============================================================

def _log_candidate_result(
    *,
    source: str,
    raw_count: int,
    real: Sequence[str],
    filler_count: int,
    invalid_count: int,
) -> None:
    logger.info(
        "[push_stream] register target candidate source=%s raw=%d real=%d filler=%d invalid=%d head=%s",
        source,
        raw_count,
        len(real),
        filler_count,
        invalid_count,
        list(real[:10]),
    )


def _safe_call_provider(fn: Callable[..., Any]) -> Any:
    call_patterns = (
        lambda: fn(limit=DEFAULT_REGISTER_MAX_SYMBOLS),
        lambda: fn(max_symbols=DEFAULT_REGISTER_MAX_SYMBOLS),
        lambda: fn(n=DEFAULT_REGISTER_MAX_SYMBOLS),
        lambda: fn(DEFAULT_REGISTER_MAX_SYMBOLS),
        lambda: fn(),
    )

    last_err: Optional[BaseException] = None

    for caller in call_patterns:
        try:
            return caller()
        except TypeError as e:
            last_err = e
            continue
        except Exception:
            logger.debug(
                "[push_stream] symbol provider call failed fn=%s",
                fn,
                exc_info=True,
            )
            return None

    if last_err is not None:
        logger.debug(
            "[push_stream] symbol provider signature mismatch fn=%s err=%s",
            fn,
            last_err,
        )

    return None


def _refresh_result_to_ok(result: Any) -> bool:
    if result is None:
        return False

    if isinstance(result, bool):
        return result

    if isinstance(result, (int, float)):
        return result > 0

    if isinstance(result, dict):
        for key in ("ok", "success", "registered_ok"):
            if key in result:
                return bool(result.get(key))
        for key in ("registered", "count", "size", "n"):
            if key in result:
                try:
                    return int(result.get(key) or 0) > 0
                except Exception:
                    pass
        return len(result) > 0

    if isinstance(result, (list, tuple, set)):
        return len(result) > 0

    if isinstance(result, str):
        s = result.strip().lower()
        if not s:
            return False
        if s in {"ok", "true", "success", "done"}:
            return True
        if s in {"false", "ng", "error", "failed", "none"}:
            return False
        return True

    return bool(result)


def _resolve_from_runtime() -> List[str]:
    best: List[str] = []

    for key in _RUNTIME_SYMBOL_KEYS:
        try:
            src = _safe_get_runtime(key)
        except Exception:
            src = None

        if src is None:
            continue

        real, raw_count, filler_count, invalid_count = _clean_symbol_list(src)
        _log_candidate_result(
            source=f"runtime.{key}",
            raw_count=raw_count,
            real=real,
            filler_count=filler_count,
            invalid_count=invalid_count,
        )

        if real:
            best = real
            break

    return best


def _get_global_data() -> Any:
    candidates = (
        ("global_state", "global_data"),
        ("core.global_context.context", "global_data"),
    )

    for module_name, attr_name in candidates:
        try:
            mod = importlib.import_module(module_name)
            gd = getattr(mod, attr_name, None)
            if gd is not None:
                return gd
        except Exception:
            continue

    return None


def _maybe_call_or_value(obj: Any) -> Any:
    if callable(obj):
        return _safe_call_provider(obj)
    return obj


def _resolve_from_global_data() -> List[str]:
    gd = _get_global_data()
    if gd is None:
        return []

    for attr in _GLOBAL_DATA_SYMBOL_ATTRS:
        try:
            src = getattr(gd, attr, None)
        except Exception:
            src = None

        if src is None:
            continue

        src = _maybe_call_or_value(src)

        real, raw_count, filler_count, invalid_count = _clean_symbol_list(src)
        _log_candidate_result(
            source=f"global_data.{attr}",
            raw_count=raw_count,
            real=real,
            filler_count=filler_count,
            invalid_count=invalid_count,
        )

        if real:
            return real

    getter_names = (
        "get_monitor_symbols",
        "get_active_symbols",
        "get_push_symbols",
        "get_register_symbols",
        "get_ats_targets",
        "get_ats_register_targets",
    )

    for name in getter_names:
        try:
            fn = getattr(gd, name, None)
        except Exception:
            fn = None

        if not callable(fn):
            continue

        src = _safe_call_provider(fn)
        real, raw_count, filler_count, invalid_count = _clean_symbol_list(src)
        _log_candidate_result(
            source=f"global_data.{name}()",
            raw_count=raw_count,
            real=real,
            filler_count=filler_count,
            invalid_count=invalid_count,
        )

        if real:
            return real

    return []


def _resolve_from_dynamic_providers() -> List[str]:
    for module_name, func_name in _DYNAMIC_SYMBOL_PROVIDERS:
        try:
            mod = importlib.import_module(module_name)
        except Exception:
            continue

        fn = getattr(mod, func_name, None)
        if not callable(fn):
            continue

        src = _safe_call_provider(fn)
        real, raw_count, filler_count, invalid_count = _clean_symbol_list(src)

        _log_candidate_result(
            source=f"{module_name}.{func_name}()",
            raw_count=raw_count,
            real=real,
            filler_count=filler_count,
            invalid_count=invalid_count,
        )

        if real:
            return real

    return []


def _resolve_monitor_symbols() -> List[str]:
    resolvers: Tuple[Tuple[str, Callable[[], List[str]]], ...] = (
        ("runtime", _resolve_from_runtime),
        ("global_data", _resolve_from_global_data),
        ("dynamic_providers", _resolve_from_dynamic_providers),
    )

    for source_name, resolver in resolvers:
        try:
            items = resolver()
        except Exception:
            logger.exception(
                "[push_stream] resolve monitor symbols failed source=%s",
                source_name,
            )
            items = []

        items, raw_count, filler_count, invalid_count = _clean_symbol_list(items)

        if items:
            logger.info(
                "[push_stream] resolved monitor symbols source=%s total=%d real=%d filler=%d invalid=%d head=%s",
                source_name,
                raw_count,
                len(items),
                filler_count,
                invalid_count,
                items[:10],
            )
            return items

    logger.warning(
        "[push_stream] no real monitor symbols resolved. "
        "原因候補: runtime未設定 / global_data clear後に未再設定 / active_symbol_manager未公開 / 上流がFILLERのみ生成"
    )
    return []


def _apply_register_liquidity_guard(targets: Sequence[str]) -> List[str]:
    """
    PUSH登録対象に流動性ガードを適用する。

    注意:
      - liquidity_guard import 失敗時は元リストを返す
      - guard 内で ranking DB が見つからない / coverage が低い場合も pass-through になる
      - guard が全件除外した場合は空リストを返す
    """
    cleaned, raw_count, filler_count, invalid_count = _clean_symbol_list(targets)

    if not cleaned:
        logger.warning(
            "[push_stream] liquidity guard skipped: empty input raw=%d filler=%d invalid=%d",
            raw_count,
            filler_count,
            invalid_count,
        )
        return []

    if filter_register_targets_by_liquidity is None:
        logger.warning(
            "[push_stream] liquidity guard unavailable -> keep original targets size=%d head=%s",
            len(cleaned),
            cleaned[:10],
        )
        return cleaned

    try:
        filtered = filter_register_targets_by_liquidity(
            cleaned,
            source="push_stream.rotation",
        )

        filtered, f_raw, f_filler, f_invalid = _clean_symbol_list(filtered)

        logger.info(
            "[push_stream] liquidity guard applied before=%d after=%d "
            "raw=%d filler=%d invalid=%d head=%s",
            len(cleaned),
            len(filtered),
            f_raw,
            f_filler,
            f_invalid,
            filtered[:10],
        )

        return filtered

    except Exception:
        logger.exception(
            "[push_stream] liquidity guard failed -> keep original targets size=%d head=%s",
            len(cleaned),
            cleaned[:10],
        )
        return cleaned


def _resolve_register_targets() -> List[str]:
    targets = _resolve_monitor_symbols()
    targets, raw_count, filler_count, invalid_count = _clean_symbol_list(targets)

    if not targets:
        logger.warning(
            "[push_stream] no real register targets resolved raw=%d filler=%d invalid=%d",
            raw_count,
            filler_count,
            invalid_count,
        )
        return []

    before_limit = len(targets)
    targets = targets[:DEFAULT_REGISTER_MAX_SYMBOLS]

    logger.info(
        "[push_stream] resolved register targets before_liquidity total=%d limited=%d max=%d chunk=%d head=%s",
        before_limit,
        len(targets),
        DEFAULT_REGISTER_MAX_SYMBOLS,
        DEFAULT_REGISTER_CHUNK_SIZE,
        targets[:10],
    )

    targets = _apply_register_liquidity_guard(targets)

    if not targets:
        logger.warning(
            "[push_stream] no register targets after liquidity guard before_limit=%d max=%d",
            before_limit,
            DEFAULT_REGISTER_MAX_SYMBOLS,
        )
        return []

    targets = targets[:DEFAULT_REGISTER_MAX_SYMBOLS]

    logger.info(
        "[push_stream] resolved register targets after_liquidity total=%d max=%d chunk=%d head=%s",
        len(targets),
        DEFAULT_REGISTER_MAX_SYMBOLS,
        DEFAULT_REGISTER_CHUNK_SIZE,
        targets[:10],
    )

    return targets


# ============================================================
# Runtime / sleep
# ============================================================

def _ws_ready_for_rotation() -> bool:
    if state._stop_event.is_set():
        return False

    if not state._connected_event.is_set():
        return False

    if not _is_ws_alive():
        return False

    return callable(get_ws_sender())


def _sleep_or_stop(seconds: float) -> bool:
    end = time.time() + max(0.0, float(seconds))

    while time.time() < end:
        if state._stop_event.is_set():
            return True
        time.sleep(min(0.1, max(0.0, end - time.time())))

    return state._stop_event.is_set()


# ============================================================
# Register delegation
# ============================================================

def register_symbols(symbols: Iterable[str], force: bool = False, **kwargs: Any) -> bool:
    """
    互換 API。

    重要:
      - 公式ひな形に合わせ、ここでは ws.send による register はしない
      - refresh_callable(subscription_manager.core.refresh_subscriptions) に委譲する
      - refresh_callable が無い場合は False を返す
    """
    del force

    cleaned, raw_count, filler_count, invalid_count = _clean_symbol_list(symbols)
    items = cleaned[:DEFAULT_REGISTER_CHUNK_SIZE]

    if not items:
        logger.warning(
            "[push_stream] register_symbols skipped: empty_after_filter raw=%d filler=%d invalid=%d",
            raw_count,
            filler_count,
            invalid_count,
        )
        return False

    if not callable(state._refresh_callable):
        logger.warning(
            "[push_stream] register_symbols skipped: refresh callable missing size=%d head=%s",
            len(items),
            items[:10],
        )
        return False

    try:
        reason = str(kwargs.get("reason") or "rotation")
        label = str(kwargs.get("label") or reason.replace("rotation_", "").upper())

        logger.info(
            "[push_stream] refresh call reason=%s size=%d head=%s clear_first=True wait_after_clear=%.3fs",
            reason,
            len(items),
            items[:10],
            UNREGISTER_TO_REGISTER_WAIT_SEC,
        )

        result = _call_refresh(
            force=True,
            reason=reason,
            clear_first=True,
            unregister_first=True,
            wait_after_clear_sec=UNREGISTER_TO_REGISTER_WAIT_SEC,
            unregister_wait_sec=UNREGISTER_TO_REGISTER_WAIT_SEC,
            symbols=items,
            codes=items,
            items=items,
        )

        ok = _refresh_result_to_ok(result)

        logger.info(
            "[push_stream] refresh result reason=%s label=%s ok=%s result_type=%s result=%r size=%d",
            reason,
            label,
            ok,
            type(result).__name__ if result is not None else "NoneType",
            result,
            len(items),
        )
        return ok

    except Exception:
        logger.exception(
            "[push_stream] refresh call failed reason=%s size=%d",
            kwargs.get("reason") or "rotation",
            len(items),
        )
        return False


def _run_one_batch(
    *,
    label: str,
    symbols: Sequence[str],
) -> bool:
    """
    A/B batch の片側50銘柄を登録する。

    注意:
      - register_symbols() 内で clear_first=True を渡すため、
        毎回「全部解除 → 0.5秒 → 登録」になる。
    """
    cleaned, raw_count, filler_count, invalid_count = _clean_symbol_list(symbols)
    cleaned = cleaned[:DEFAULT_REGISTER_CHUNK_SIZE]

    if not cleaned:
        logger.warning(
            "[push_stream] rotation %s skipped: empty_after_filter raw=%d filler=%d invalid=%d",
            label,
            raw_count,
            filler_count,
            invalid_count,
        )
        return False

    reason = f"rotation_{label}"

    return register_symbols(
        cleaned,
        force=True,
        reason=reason,
        label=label,
    )


def _run_one_batch_with_timeout(
    *,
    label: str,
    symbols: Sequence[str],
    timeout_sec: float = REGISTER_TIMEOUT_SEC,
) -> bool:
    """
    A/B登録処理を timeout 付きで実行する。

    重要:
      - with ThreadPoolExecutor は使わない
      - timeout 後に executor.shutdown(wait=False) で即座に次へ進む
      - HTTP登録スレッドが裏で残る可能性はあるが、B面表示を止めない
    """
    cleaned, raw_count, filler_count, invalid_count = _clean_symbol_list(symbols)
    cleaned = cleaned[:DEFAULT_REGISTER_CHUNK_SIZE]

    if not cleaned:
        logger.warning(
            "[push_stream] rotation %s timeout-wrapper skipped empty raw=%d filler=%d invalid=%d",
            label,
            raw_count,
            filler_count,
            invalid_count,
        )
        return False

    logger.info(
        "[push_stream] rotation %s register dispatch timeout=%.3fs size=%d head=%s",
        label,
        timeout_sec,
        len(cleaned),
        cleaned[:10],
    )

    ex = concurrent.futures.ThreadPoolExecutor(
        max_workers=1,
        thread_name_prefix=f"push-rotation-register-{label}",
    )

    future = ex.submit(_run_one_batch, label=label, symbols=cleaned)

    try:
        ok = bool(future.result(timeout=max(0.1, float(timeout_sec))))
        logger.info(
            "[push_stream] rotation %s register returned ok=%s timeout=%.3fs size=%d",
            label,
            ok,
            timeout_sec,
            len(cleaned),
        )
        return ok

    except concurrent.futures.TimeoutError:
        logger.warning(
            "[push_stream] rotation %s register timeout %.3fs -> force continue size=%d",
            label,
            timeout_sec,
            len(cleaned),
        )
        return False

    except Exception:
        logger.exception(
            "[push_stream] rotation %s register exception -> force continue size=%d",
            label,
            len(cleaned),
        )
        return False

    finally:
        try:
            ex.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            ex.shutdown(wait=False)
        except Exception:
            logger.debug(
                "[push_stream] rotation %s executor shutdown failed",
                label,
                exc_info=True,
            )


# ============================================================
# Public APIs
# ============================================================

def enable_rotation(enabled: bool = True) -> None:
    state._rotation_enabled = bool(enabled)
    logger.info("[push_stream] rotation enabled=%s", state._rotation_enabled)


def _rotation_worker() -> None:
    """
    A/B 50銘柄を5秒ごとにローテーションする。

    Flow:
      A面表示 → A登録最大REGISTER_TIMEOUT_SEC秒待ち → 5秒待機
      B面表示 → B登録最大REGISTER_TIMEOUT_SEC秒待ち → 5秒待機
      Aへ戻る

    重要:
      - HTTP登録は WebSocket ready に依存させない
      - WebSocketが不安定でもA/Bの登録ログは出す
      - A登録に失敗/timeoutしてもBへ進む
      - B登録に失敗/timeoutしても次ループへ進む
      - 登録対象は _resolve_register_targets() 内で流動性ガード済み
    """
    logger.info(
        "[push_stream] rotation worker started version=%s hold=%.3fs unregister_wait=%.3fs register_timeout=%.3fs",
        VERSION,
        ROTATE_HOLD_SEC,
        UNREGISTER_TO_REGISTER_WAIT_SEC,
        REGISTER_TIMEOUT_SEC,
    )

    empty_count = 0
    ws_wait_count = 0
    last_ws_wait_log_ts = 0.0

    while not state._stop_event.is_set():
        try:
            if not state._rotation_enabled:
                time.sleep(1.0)
                continue

            connected = state._connected_event.is_set()
            ws_alive = _is_ws_alive()

            if not connected or not ws_alive:
                ws_wait_count += 1
                now_ts = time.time()

                if (
                    ws_wait_count == 1
                    or now_ts - last_ws_wait_log_ts >= WS_WAIT_LOG_INTERVAL_SEC
                ):
                    logger.warning(
                        "[push_stream] rotation ws_not_ready but continue HTTP refresh connected_event=%s ws_alive=%s "
                        "refresh_callable=%s sender_callable=%s wait_count=%d",
                        connected,
                        ws_alive,
                        callable(state._refresh_callable),
                        callable(get_ws_sender()),
                        ws_wait_count,
                    )
                    last_ws_wait_log_ts = now_ts
            else:
                ws_wait_count = 0

            targets = _resolve_register_targets()

            if not targets:
                empty_count += 1
                if empty_count == 1 or empty_count % 15 == 0:
                    logger.warning(
                        "[push_stream] rotation waiting: no real targets empty_count=%d "
                        "hint=check runtime/global_data/active_symbol_manager/liquidity_guard and upstream candidates",
                        empty_count,
                    )
                time.sleep(2.0)
                continue

            empty_count = 0

            first = targets[:DEFAULT_REGISTER_CHUNK_SIZE]
            second = targets[
                DEFAULT_REGISTER_CHUNK_SIZE:
                DEFAULT_REGISTER_CHUNK_SIZE * 2
            ]

            logger.info(
                "[push_stream] rotation cycle targets=%d first=%d second=%d headA=%s headB=%s refresh_callable=%s ws_ready=%s",
                len(targets),
                len(first),
                len(second),
                first[:10],
                second[:10],
                callable(state._refresh_callable),
                _ws_ready_for_rotation(),
            )

            if not callable(state._refresh_callable):
                logger.warning(
                    "[push_stream] rotation skipped: refresh callable missing. "
                    "subscription_manager refresh callable must be installed."
                )
                time.sleep(1.0)
                continue

            # A面
            if first:
                if not _ws_ready_for_rotation():
                    logger.warning(
                        "[push_stream] rotation A ws_not_ready but continue HTTP refresh connected_event=%s ws_alive=%s sender_callable=%s",
                        state._connected_event.is_set(),
                        _is_ws_alive(),
                        callable(get_ws_sender()),
                    )

                _log_register_targets_with_names(
                    first,
                    label="A",
                    reason="rotation_A",
                )

                ok_a = _run_one_batch_with_timeout(
                    label="A",
                    symbols=first,
                    timeout_sec=REGISTER_TIMEOUT_SEC,
                )

                logger.info(
                    "[push_stream] rotation A registered ok=%s size=%d hold=%.3fs",
                    ok_a,
                    len(first),
                    ROTATE_HOLD_SEC,
                )

                if _sleep_or_stop(ROTATE_HOLD_SEC):
                    break

            # B面
            if second:
                if not _ws_ready_for_rotation():
                    logger.warning(
                        "[push_stream] rotation B ws_not_ready but continue HTTP refresh connected_event=%s ws_alive=%s sender_callable=%s",
                        state._connected_event.is_set(),
                        _is_ws_alive(),
                        callable(get_ws_sender()),
                    )

                _log_register_targets_with_names(
                    second,
                    label="B",
                    reason="rotation_B",
                )

                ok_b = _run_one_batch_with_timeout(
                    label="B",
                    symbols=second,
                    timeout_sec=REGISTER_TIMEOUT_SEC,
                )

                logger.info(
                    "[push_stream] rotation B registered ok=%s size=%d hold=%.3fs",
                    ok_b,
                    len(second),
                    ROTATE_HOLD_SEC,
                )

                if _sleep_or_stop(ROTATE_HOLD_SEC):
                    break

            else:
                logger.info(
                    "[push_stream] rotation single-batch mode targets=%d hold=%.3fs",
                    len(targets),
                    ROTATE_HOLD_SEC,
                )

        except Exception:
            logger.exception("[push_stream] rotation worker loop failed")
            time.sleep(2.0)

    logger.info("[push_stream] rotation worker stopped")


__all__ = [
    "register_symbols",
    "enable_rotation",
    "_rotation_worker",
    "_resolve_register_targets",
    "_resolve_monitor_symbols",
]