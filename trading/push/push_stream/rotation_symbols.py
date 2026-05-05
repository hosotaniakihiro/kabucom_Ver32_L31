# ============================================================
# File   : trading/push/push_stream/rotation_symbols.py
# Version: PRODUCTION-STABLE-REV2-PUSH-ROTATION-SYMBOLS-INDEPENDENT
# ------------------------------------------------------------
# PUSH A/Bローテーション用の銘柄解決・正規化API。
#
# Responsibilities:
#   - runtime / global_data / dynamic providers から登録候補を解決
#   - FILLER / 無効銘柄を除外
#   - 銘柄コードを正規化
#   - 100銘柄上限へ制限
#   - 流動性ガードを適用
#
# Notes:
#   - 旧 rotation.py へ依存しない独立実装。
# ============================================================

from __future__ import annotations

import importlib
import logging
from typing import Any, Callable, Iterable, List, Optional, Sequence, Tuple

from .normalize import _normalize_symbol
from .runtime import _safe_get_runtime
from .rotation_settings import (
    DEFAULT_REGISTER_CHUNK_SIZE,
    DEFAULT_REGISTER_MAX_SYMBOLS,
)

logger = logging.getLogger(__name__)

VERSION = "PRODUCTION-STABLE-REV2-PUSH-ROTATION-SYMBOLS-INDEPENDENT"


try:
    from trading.push.subscription_manager.liquidity_guard import (
        filter_register_targets_by_liquidity,
    )
except Exception:
    filter_register_targets_by_liquidity = None  # type: ignore[assignment]
    logger.warning(
        "[push_stream] liquidity_guard import failed. PUSH register liquidity guard disabled.",
        exc_info=True,
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


def is_filler_symbol(symbol: Any) -> bool:
    s = str(symbol).strip().upper()
    return (
        not s
        or s.startswith("FILLER")
        or s in {"NONE", "NULL", "NAN", "NA", "-", "0"}
    )


def is_real_symbol(symbol: Any) -> bool:
    if symbol is None:
        return False

    s = str(symbol).strip().upper()

    if is_filler_symbol(s):
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


def normalize_real_symbol(symbol: Any) -> Optional[str]:
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

    if not is_real_symbol(s):
        return None

    return s


def dedupe_keep_order(items: Iterable[str]) -> List[str]:
    out: List[str] = []
    seen: set[str] = set()

    for x in items:
        s = str(x).strip().upper()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)

    return out


def clean_symbol_list(src: Any) -> Tuple[List[str], int, int, int]:
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
        if is_filler_symbol(x):
            filler_count += 1
            continue

        s = normalize_real_symbol(x)
        if s:
            real.append(s)
        else:
            invalid_count += 1

    real = dedupe_keep_order(real)
    return real, raw_count, filler_count, invalid_count


def log_candidate_result(
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


def safe_call_provider(fn: Callable[..., Any]) -> Any:
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
            logger.debug("[push_stream] symbol provider call failed fn=%s", fn, exc_info=True)
            return None

    if last_err is not None:
        logger.debug("[push_stream] symbol provider signature mismatch fn=%s err=%s", fn, last_err)

    return None


def _resolve_from_runtime() -> List[str]:
    best: List[str] = []

    for key in _RUNTIME_SYMBOL_KEYS:
        try:
            src = _safe_get_runtime(key)
        except Exception:
            src = None

        if src is None:
            continue

        real, raw_count, filler_count, invalid_count = clean_symbol_list(src)
        log_candidate_result(
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
        return safe_call_provider(obj)
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
        real, raw_count, filler_count, invalid_count = clean_symbol_list(src)
        log_candidate_result(
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

        src = safe_call_provider(fn)
        real, raw_count, filler_count, invalid_count = clean_symbol_list(src)
        log_candidate_result(
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

        src = safe_call_provider(fn)
        real, raw_count, filler_count, invalid_count = clean_symbol_list(src)
        log_candidate_result(
            source=f"{module_name}.{func_name}()",
            raw_count=raw_count,
            real=real,
            filler_count=filler_count,
            invalid_count=invalid_count,
        )

        if real:
            return real

    return []


def resolve_monitor_symbols() -> List[str]:
    resolvers: Tuple[Tuple[str, Callable[[], List[str]]], ...] = (
        ("runtime", _resolve_from_runtime),
        ("global_data", _resolve_from_global_data),
        ("dynamic_providers", _resolve_from_dynamic_providers),
    )

    for source_name, resolver in resolvers:
        try:
            items = resolver()
        except Exception:
            logger.exception("[push_stream] resolve monitor symbols failed source=%s", source_name)
            items = []

        items, raw_count, filler_count, invalid_count = clean_symbol_list(items)

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


def apply_register_liquidity_guard(targets: Sequence[str]) -> List[str]:
    cleaned, raw_count, filler_count, invalid_count = clean_symbol_list(targets)

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
        filtered, f_raw, f_filler, f_invalid = clean_symbol_list(filtered)

        logger.info(
            "[push_stream] liquidity guard applied before=%d after=%d raw=%d filler=%d invalid=%d head=%s",
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


def resolve_register_targets() -> List[str]:
    targets = resolve_monitor_symbols()
    targets, raw_count, filler_count, invalid_count = clean_symbol_list(targets)

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

    targets = apply_register_liquidity_guard(targets)

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


__all__ = [
    "VERSION",
    "is_filler_symbol",
    "is_real_symbol",
    "normalize_real_symbol",
    "dedupe_keep_order",
    "clean_symbol_list",
    "log_candidate_result",
    "safe_call_provider",
    "resolve_monitor_symbols",
    "apply_register_liquidity_guard",
    "resolve_register_targets",
]
