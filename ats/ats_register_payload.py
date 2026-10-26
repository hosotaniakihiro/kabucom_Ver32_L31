# ============================================================
# File   : ats/ats_register_payload.py
# Version: Ver1.2-ATS-REGISTER-PAYLOAD-NO-CIRCULAR
# ------------------------------------------------------------
# register/unregister payload と vendor-style switch
# ✔ self import 排除
# ✔ ats_register_loop.py からの import に完全対応
# ============================================================

from __future__ import annotations

import contextlib
import logging
import time
from typing import Iterator, List

from global_state import global_data

from .ats_api import request_api_put, is_in_429_cooldown
from .ats_register_state import (
    ATS_SYMBOL_BLACKLIST,
    REGISTER_LIMIT,
    sanitize_symbols,
    unique_keep_order,
    same_symbol_set,
    set_registered_symbols,
    get_registered_symbols,
    set_last_good_batch,
    get_last_good_batch,
    get_last_good_phase,
    set_active_phase,
)
from .ats_register_logging import (
    log_registered_symbols,
    save_ats_register_log,
    _print_current_ats_registered_symbols,
)

logger = logging.getLogger(__name__)

UNREGISTER_WAIT_SEC = 5.0


@contextlib.contextmanager
def _kabu_register_sequence_lock(reason: str) -> "Iterator[None]":
    """Kabu Station の register リストをプロセス間で直列化する。

    register リストはセッション全体で 50 銘柄が上限の共有資源。
    ATS の unregister -> wait -> register のすき間に push_receiver_runner の
    rotation が 50 銘柄登録し、合計 100 になって
      {"Code":4002006,"Message":"レジスト数エラー"}
    で落ちていた。push 側が既に使っているロックをそのまま共用する。
    ロックが使えない環境では従来通り素通しする。
    """
    try:
        from trading.push.subscription_manager.register_ops import registration_sequence_lock
    except Exception:
        logger.debug("[ATS] register sequence lock unavailable -> continue without lock", exc_info=True)
        yield
        return
    try:
        with registration_sequence_lock(reason):
            yield
    except Exception:
        logger.exception("[ATS] register sequence lock failed -> continue without lock reason=%s", reason)
        yield


# ============================================================
# payload builder
# ============================================================

def build_ats_payload(symbols: List[str], limit: int = REGISTER_LIMIT):
    payload = []
    seen = set()

    flags_map = getattr(global_data, "symbol_flags", {})

    dropped_no_flags = 0
    dropped_bad_exchange = 0
    dropped_blacklist = 0

    symbols = sanitize_symbols(symbols)

    for s in symbols:
        s = str(s)

        if s in ATS_SYMBOL_BLACKLIST:
            dropped_blacklist += 1
            continue

        if s in seen:
            continue
        seen.add(s)

        flags = flags_map.get(s)
        if not flags:
            dropped_no_flags += 1
            continue

        exchange = flags.get("exchange", 1)
        try:
            exchange = int(exchange)
        except Exception:
            dropped_bad_exchange += 1
            continue

        if exchange not in (1, 2):
            dropped_bad_exchange += 1
            continue

        payload.append({"Symbol": s, "Exchange": exchange})
        if len(payload) >= limit:
            break

    if dropped_no_flags or dropped_bad_exchange or dropped_blacklist:
        logger.warning(
            "[ATS PAYLOAD] requested=%d built=%d dropped(no_flags=%d bad_exchange=%d blacklist=%d)",
            len(symbols),
            len(payload),
            dropped_no_flags,
            dropped_bad_exchange,
            dropped_blacklist,
        )

    return payload


# ============================================================
# vendor API wrappers
# ============================================================

def unregister_symbols(symbols: List[str]) -> bool:
    payload = build_ats_payload(symbols, limit=max(len(symbols), REGISTER_LIMIT))
    if not payload:
        logger.warning("[ATS UNREGISTER] payload empty")
        return False

    ok, status, body = request_api_put("/unregister", {"Symbols": payload}, timeout=8)
    if ok:
        logger.info("🧹 ATS UNREGISTER OK count=%d", len(payload))
        return True

    if status == 429:
        logger.warning("⚠ ATS UNREGISTER rate limited (429): %s", body)
    else:
        logger.error("❌ ATS UNREGISTER failed status=%s body=%s", status, body)
    return False


def unregister_all_vendor() -> bool:
    ok, status, body = request_api_put("/unregister/all", None, timeout=8)
    if ok:
        logger.info("🧹 ATS UNREGISTER ALL OK status=%s body=%s", status, body)
        set_registered_symbols([])
        set_active_phase("", [])
        return True

    if status == 429:
        logger.warning("⚠ ATS UNREGISTER ALL rate limited (429): %s", body)
    else:
        logger.error("❌ ATS UNREGISTER ALL failed status=%s body=%s", status, body)
    return False


def register_symbols_batch(symbols: List[str], phase: str) -> bool:
    payload = build_ats_payload(symbols, limit=REGISTER_LIMIT)
    if not payload:
        logger.warning("[ATS %s] register payload empty", phase)
        return False

    ok, status, body = request_api_put("/register", {"Symbols": payload}, timeout=8)
    if ok:
        logger.info("✅ ATS REGISTER success [%s] (%d symbols)", phase, len(payload))
        return True

    if status == 429:
        logger.warning(
            "⚠ ATS REGISTER rate limited [%s] body=%s symbols=%s",
            phase,
            body,
            [p["Symbol"] for p in payload],
        )
    else:
        logger.error(
            "❌ ATS REGISTER failed [%s] status=%s body=%s symbols=%s",
            phase,
            status,
            body,
            [p["Symbol"] for p in payload],
        )
    return False


def switch_batch_vendor_style(symbols: List[str], phase: str) -> bool:
    if is_in_429_cooldown():
        logger.warning("[ATS %s] switch skipped: in 429 cooldown", phase)
        return False

    # unregister から register 完了までを 1 つのクリティカルセクションにする。
    # 途中に push_receiver_runner の rotation が割り込むと合計 100 銘柄になり
    # 4002006 レジスト数エラーになる。
    with _kabu_register_sequence_lock(f"ats_switch_{phase}"):
        logger.info("[ATS %s] unregister all start", phase)
        ok_unreg = unregister_all_vendor()
        if not ok_unreg:
            logger.warning("[ATS %s] unregister all failed", phase)
            return False

        logger.info("[ATS %s] wait after unregister all %.1fs", phase, UNREGISTER_WAIT_SEC)
        time.sleep(UNREGISTER_WAIT_SEC)

        logger.info("[ATS %s] register new batch start", phase)
        return register_symbols_batch(symbols, phase)


# ============================================================
# keep / restore last good
# ============================================================

def keep_last_good_batch(reason_phase: str) -> bool:
    last_symbols = get_last_good_batch()
    last_phase = get_last_good_phase()

    if not last_symbols:
        logger.warning("[ATS %s] no last_good_batch to keep", reason_phase)
        return False

    set_registered_symbols(last_symbols)
    set_active_phase(last_phase or reason_phase, last_symbols)

    logger.warning(
        "[ATS %s] keep last good in memory phase=%s size=%d",
        reason_phase,
        last_phase,
        len(last_symbols),
    )
    _print_current_ats_registered_symbols(last_symbols, phase=f"{reason_phase}-KEEP-LAST")
    return True


def restore_last_good_batch(reason_phase: str) -> bool:
    last_symbols = get_last_good_batch()
    last_phase = get_last_good_phase()

    if not last_symbols:
        logger.warning("[ATS %s] no last_good_batch to restore", reason_phase)
        return False

    logger.warning(
        "[ATS %s] restoring last good via vendor phase=%s size=%d",
        reason_phase,
        last_phase,
        len(last_symbols),
    )

    ok = switch_batch_vendor_style(last_symbols, phase=f"{reason_phase}-RESTORE")
    if not ok:
        logger.warning("[ATS %s] restore last good failed", reason_phase)
        return False

    set_registered_symbols(last_symbols)
    set_active_phase(last_phase or reason_phase, last_symbols)
    _print_current_ats_registered_symbols(last_symbols, phase=f"{reason_phase}-RESTORED")
    return True


# ============================================================
# high level register
# ============================================================

def register_payload(symbols: List[str], phase: str, apply_all_filters) -> bool:
    symbols = sanitize_symbols(apply_all_filters(symbols))
    payload = build_ats_payload(symbols, limit=REGISTER_LIMIT)

    if not payload:
        logger.warning("[ATS %s] payload empty -> skip", phase)
        return False

    symbols_only = unique_keep_order([p["Symbol"] for p in payload])
    current_registered = get_registered_symbols()

    if same_symbol_set(symbols_only, current_registered):
        logger.info(
            "[ATS %s] same payload as current -> keep registration (%d symbols)",
            phase,
            len(symbols_only),
        )
        set_registered_symbols(current_registered)
        set_active_phase(phase, current_registered)
        _print_current_ats_registered_symbols(current_registered, phase=f"{phase}-KEEP")
        return True

    log_registered_symbols(symbols_only, phase=phase)
    save_ats_register_log(symbols_only)

    ok = switch_batch_vendor_style(symbols_only, phase)
    if ok:
        set_registered_symbols(symbols_only)
        set_last_good_batch(symbols_only, phase)
        _print_current_ats_registered_symbols(symbols_only, phase=phase)
        return True

    logger.warning("[ATS %s] 50-symbol batch failed -> restore last good batch", phase)
    if restore_last_good_batch(reason_phase=phase):
        return True

    return keep_last_good_batch(reason_phase=phase)


def register_only(symbols: List[str], phase: str, apply_all_filters) -> bool:
    """unregister を伴わない register。

    「登録 -> 保持 -> 解除 -> インターバル」ローテーション用。
    直前サイクルで unregister_all 済みで register リストが空である前提のため、
    switch_batch_vendor_style のような事前 unregister/all は行わない。

    register リストは push と共有の 50 銘柄上限資源なので、register の瞬間だけ
    sequence lock を取り、push rotation の割り込みによる
    {"Code":4002006,"レジスト数エラー"} を防ぐ。
    """
    if is_in_429_cooldown():
        logger.warning("[ATS %s] register_only skipped: in 429 cooldown", phase)
        return False

    symbols = sanitize_symbols(apply_all_filters(symbols))
    payload = build_ats_payload(symbols, limit=REGISTER_LIMIT)

    if not payload:
        logger.warning("[ATS %s] register_only payload empty -> skip", phase)
        return False

    symbols_only = unique_keep_order([p["Symbol"] for p in payload])

    log_registered_symbols(symbols_only, phase=phase)
    save_ats_register_log(symbols_only)

    with _kabu_register_sequence_lock(f"ats_register_only_{phase}"):
        ok = register_symbols_batch(symbols_only, phase)

    if ok:
        set_registered_symbols(symbols_only)
        set_last_good_batch(symbols_only, phase)
        set_active_phase(phase, symbols_only)
        _print_current_ats_registered_symbols(symbols_only, phase=phase)
        return True

    logger.warning("[ATS %s] register_only failed", phase)
    return False


def switch_batch_minimal(symbols, phase, apply_all_filters=None):
    """解除 -> 登録の「無登録時間」を最小化する切替。

    設計:
      - フィルタ / payload 生成 / 銘柄確定は、まだ旧グループが登録中のうちに
        (= 無登録の窓の外で) 済ませる。
      - クリティカルセクション内は API 2 本 (unregister/all -> register) だけ。
        ログ・DB保存・状態更新は一切やらない (呼び出し側が register 完了後に
        finalize_registration() で行う)。
      - unregister と register を 1 つの sequence lock で囲い、間に push rotation が
        割り込んで合計 100 銘柄 (4002006) になるのを防ぐ。

    戻り値: (ok: bool, registered_symbols: list[str])
    """
    # ---- 窓の外: 重い前処理はここで (旧グループはまだ登録中) ----
    if apply_all_filters is not None:
        try:
            symbols = apply_all_filters(symbols)
        except Exception:
            logger.exception("[ATS %s] minimal switch filter failed", phase)
    symbols = sanitize_symbols(symbols)

    payload = build_ats_payload(symbols, limit=REGISTER_LIMIT)
    symbols_only = unique_keep_order([p["Symbol"] for p in payload])

    if not symbols_only:
        logger.warning("[ATS %s] minimal switch payload empty -> skip", phase)
        return False, []

    if is_in_429_cooldown():
        logger.warning("[ATS %s] minimal switch skipped: in 429 cooldown", phase)
        return False, symbols_only

    # ---- ここから無登録の窓 (API 2 本のみ) ----
    with _kabu_register_sequence_lock(f"ats_switch_min_{phase}"):
        if not unregister_all_vendor():
            logger.warning("[ATS %s] minimal switch unregister/all failed", phase)
            return False, symbols_only
        ok = register_symbols_batch(symbols_only, phase)
    # ---- ここまで無登録の窓 ----

    return ok, symbols_only


def finalize_registration(symbols_only, phase) -> None:
    """register 成功後の後処理 (無登録の窓の外で呼ぶこと)。

    状態更新 + ログ出力 + ats_registered_log.db への保存。いずれも登録済みの
    状態で走るので、多少時間がかかっても PUSH は途切れない。
    """
    try:
        set_registered_symbols(symbols_only)
        set_last_good_batch(symbols_only, phase)
        set_active_phase(phase, symbols_only)
    except Exception:
        logger.exception("[ATS %s] finalize state update failed", phase)

    try:
        log_registered_symbols(symbols_only, phase=phase)
    except Exception:
        logger.exception("[ATS %s] finalize log failed", phase)

    try:
        save_ats_register_log(symbols_only)
    except Exception:
        logger.exception("[ATS %s] finalize save log failed", phase)

    try:
        _print_current_ats_registered_symbols(symbols_only, phase=phase)
    except Exception:
        logger.exception("[ATS %s] finalize print failed", phase)


__all__ = [
    "build_ats_payload",
    "unregister_symbols",
    "unregister_all_vendor",
    "register_symbols_batch",
    "switch_batch_vendor_style",
    "keep_last_good_batch",
    "restore_last_good_batch",
    "register_payload",
    "register_only",
    "switch_batch_minimal",
    "finalize_registration",
]