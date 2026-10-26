# ============================================================
# File   : ats/ats_register_loop.py
# Version: Ver1.0-ATS-REGISTER-LOOP
# ------------------------------------------------------------
# ATS cycle / loop orchestration
# ============================================================

from __future__ import annotations

import logging
import time
from typing import List

from global_state import global_data
from ats.ats_rotation_manager import ATSRotationManager
from ats.ats_filters import apply_all_filters

from .ats_api import is_in_429_cooldown
from .ats_ranking_source import resolve_ranking_only_targets
from .ats_register_state import (
    ATS_BATCH_SIZE,
    sanitize_symbols,
    split_batches_50,
    get_last_good_batch,
    get_last_good_phase,
    today_str,
)
from .ats_register_payload import (
    build_ats_payload,
    keep_last_good_batch,
    restore_last_good_batch,
    switch_batch_minimal,
    finalize_registration,
)
from .ats_register_logging import _print_current_ats_registered_symbols

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SEC = 20

# ローテーション時間設計(壁時計合わせ):
#   5 秒スロットで A/B を交互登録し、毎分の秒を 5 で割った境界にスナップする。
#     A = :00,:10,:20,:30,:40,:50   (スロット番号が偶数)
#     B = :05,:15,:25,:35,:45,:55   (スロット番号が奇数)
#   各境界で unregister/all -> 対象グループ register を連続実行して切替。
#   銘柄プール(A/B の中身)は毎分先頭(:00 スロット)で 1 回だけ再構築する。
SLOT_SEC = 5.0            # 1 スロットの長さ(= 切替間隔)


def build_cycle_targets(rotator: ATSRotationManager) -> List[str]:
    try:
        ranking_targets = resolve_ranking_only_targets(today_str(), limit=ATS_BATCH_SIZE)
        if ranking_targets:
            logger.info(
                "[ATS CYCLE] ranking-only targets=%d head20=%s",
                len(ranking_targets),
                ranking_targets[:20],
            )
            return ranking_targets
    except Exception:
        logger.exception("[ATS CYCLE] ranking-only resolve failed")

    try:
        result = rotator.build_candidates()
        if isinstance(result, tuple):
            ats_targets = result[0] if len(result) >= 1 else []
        else:
            ats_targets = result

        ats_targets = sanitize_symbols(ats_targets)

        logger.warning(
            "[ATS CYCLE] fallback rotator targets=%d head20=%s",
            len(ats_targets),
            ats_targets[:20],
        )
        return ats_targets
    except Exception:
        logger.exception("[ATS CYCLE] fallback rotator failed")
        return []


def build_registerable_symbol_pool(
    rotator: ATSRotationManager,
    target_size: int = ATS_BATCH_SIZE,
    max_attempts: int = 10,
) -> List[str]:
    ranking_targets = resolve_ranking_only_targets(today_str(), limit=target_size)
    ranking_targets = sanitize_symbols(apply_all_filters(ranking_targets))
    payload = build_ats_payload(ranking_targets, limit=target_size)
    registerable = list(dict.fromkeys([p["Symbol"] for p in payload]))

    logger.info(
        "[ATS POOL] ranking-only filtered=%d registerable=%d target=%d flags=%d",
        len(ranking_targets),
        len(registerable),
        target_size,
        len(getattr(global_data, "symbol_flags", {})),
    )

    if registerable:
        return registerable[:target_size]

    collected = []
    seen = set()

    for attempt in range(max_attempts):
        raw_targets = build_cycle_targets(rotator)

        if not raw_targets:
            logger.warning("[ATS POOL] empty raw targets attempt=%s", attempt + 1)
            try:
                rotator.rotate()
            except Exception:
                logger.exception("[ATS POOL] rotator.rotate failed after empty raw")
            continue

        for s in raw_targets:
            s = str(s).strip()
            if not s or s in seen:
                continue
            seen.add(s)
            collected.append(s)

        collected = sanitize_symbols(collected)
        filtered_collected = sanitize_symbols(apply_all_filters(collected))
        payload = build_ats_payload(filtered_collected, limit=target_size)
        registerable = list(dict.fromkeys([p["Symbol"] for p in payload]))

        logger.info(
            "[ATS POOL] fallback attempt=%d raw=%d collected=%d filtered=%d registerable=%d target=%d flags=%d",
            attempt + 1,
            len(raw_targets),
            len(collected),
            len(filtered_collected),
            len(registerable),
            target_size,
            len(getattr(global_data, "symbol_flags", {})),
        )

        if len(registerable) >= target_size:
            return registerable[:target_size]

        try:
            rotator.rotate()
        except Exception:
            logger.exception("[ATS POOL] rotator.rotate failed")

    filtered_collected = sanitize_symbols(apply_all_filters(collected))
    payload = build_ats_payload(filtered_collected, limit=target_size)
    registerable = list(dict.fromkeys([p["Symbol"] for p in payload]))

    logger.warning(
        "[ATS POOL] insufficient registerable symbols -> %d / %d",
        len(registerable),
        target_size,
    )
    return registerable[:target_size]


def _ats_registration_allowed() -> bool:
    """PUSH WebSocket を所有していないプロセスでは ATS 登録ループを回さない。

    ATS も unregister/all -> register を撃つので、
    push_receiver_runner.py が持っている購読を消してしまう。
    """
    import os as _os

    if str(_os.getenv("ATS_REGISTER_REQUIRE_WS_OWNER", "1")).strip().lower() in {"0", "false", "no", "off"}:
        return True
    try:
        from trading.push.push_stream.ws_owner import registration_allowed

        allowed, holder = registration_allowed("ats_register_loop")
    except Exception:
        logger.debug("[ATS] ws owner check failed -> allow", exc_info=True)
        return True
    if not allowed:
        logger.error(
            "[ATS][MULTI_WS_DETECTED] ats_register_loop skipped: "
            "このプロセスは PUSH WebSocket の所有者ではない pid=%s holder=%s",
            _os.getpid(), holder,
        )
    return bool(allowed)


def ats_register_loop(*args, **kwargs):
    if not _ats_registration_allowed():
        return

    if getattr(global_data, "_ats_register_running", False):
        logger.warning("ATS register loop already running -> skip")
        return

    global_data._ats_register_running = True

    interval_sec = DEFAULT_INTERVAL_SEC

    for a in args:
        if isinstance(a, (int, float)):
            interval_sec = int(a)

    if isinstance(kwargs.get("interval_sec"), (int, float)):
        interval_sec = int(kwargs["interval_sec"])

    logger.info(
        "🚀 ats_register_loop START (clock-aligned %.1fs slots / A=:00,:10.. B=:05,:15.. "
        "/ batch50 unregister-all->register / ranking-only targets)",
        SLOT_SEC,
    )

    rotator = ATSRotationManager(batch_size=100, shift=50)

    registerable_targets = []
    batch_a = []
    batch_b = []
    last_pool_minute = None

    def _sleep_until_next_slot():
        """次の SLOT_SEC 境界(:00,:05,:10..)までスリープし、壁時計に再整列する。"""
        now = time.time()
        next_boundary = (int(now // SLOT_SEC) + 1) * SLOT_SEC
        wait = next_boundary - now
        if wait < 0.0:
            wait = 0.0
        time.sleep(wait)

    while True:
        try:
            now = time.time()
            slot = int(now // SLOT_SEC)
            minute = int(now // 60)
            # スロット偶数 -> A(:00,:10,:20,:30,:40,:50) / 奇数 -> B(:05,:15,..)
            phase_name = "A" if slot % 2 == 0 else "B"

            if is_in_429_cooldown():
                logger.warning(
                    "[ATS LOOP] in 429 cooldown -> keep last good, skip %s slot, realign",
                    phase_name,
                )
                keep_last_good_batch(reason_phase="COOLDOWN")
                _sleep_until_next_slot()
                continue

            # 銘柄プール(A/B の中身)は毎分先頭で 1 回だけ再構築する
            if not registerable_targets or minute != last_pool_minute:
                registerable_targets = build_registerable_symbol_pool(
                    rotator=rotator,
                    target_size=ATS_BATCH_SIZE,
                    max_attempts=10,
                )

                batch_a, batch_b = split_batches_50(registerable_targets)
                last_pool_minute = minute

                logger.info(
                    "[ATS CYCLE] pool rebuilt batchA=%d batchB=%d total=%d",
                    len(batch_a),
                    len(batch_b),
                    len(registerable_targets),
                )

                try:
                    global_data.ats_register_targets = registerable_targets
                    global_data.ats_targets = registerable_targets
                    global_data.should_register_symbols = registerable_targets
                    global_data.push_symbols = registerable_targets
                except Exception:
                    logger.debug("[ATS LOOP] global_data reflect failed", exc_info=True)

            current_symbols = batch_a if phase_name == "A" else batch_b

            if not current_symbols:
                if phase_name == "B" and batch_a:
                    logger.warning(
                        "[ATS B] batchB empty but batchA exists -> reuse batchA this slot",
                    )
                    current_symbols = batch_a
                    phase_name = "A-reuse"
                else:
                    logger.warning(
                        "[ATS %s] batch empty -> keep last good, force rebuild next minute",
                        phase_name,
                    )
                    keep_last_good_batch(reason_phase=f"{phase_name}-EMPTY")
                    registerable_targets = []
                    last_pool_minute = None
                    _sleep_until_next_slot()
                    continue

            # 境界で切替: 50 銘柄上限のため A/B 同時不可 -> unregister/all してから register。
            # 無登録時間を最小化するため、フィルタ/payload は旧グループ登録中(窓の外)で
            # 済ませ、クリティカルセクションは解除API+登録APIの 2 本だけにする。
            ok, registered = switch_batch_minimal(
                current_symbols,
                phase=phase_name,
                apply_all_filters=apply_all_filters,
            )

            if ok:
                # ログ/DB保存は登録済み状態(窓の外)で実施 -> PUSH は途切れない
                finalize_registration(registered, phase_name)
                logger.info(
                    "[ATS %s] switched size=%d @slot%+d (sec_in_min=%.1f)",
                    phase_name,
                    len(registered),
                    slot % 12,
                    now % 60.0,
                )
            else:
                logger.warning(
                    "[ATS %s] switch failed -> last_good_phase=%s size=%d (keep)",
                    phase_name,
                    get_last_good_phase(),
                    len(get_last_good_batch()),
                )
                keep_last_good_batch(reason_phase=f"{phase_name}-REGFAIL")

            # 次の 5 秒境界まで待って壁時計に再整列
            _sleep_until_next_slot()

        except Exception:
            logger.exception("❌ ats_register_loop error")

            try:
                restore_last_good_batch(reason_phase="LOOP-ERROR")
            except Exception:
                try:
                    keep_last_good_batch(reason_phase="LOOP-ERROR")
                except Exception:
                    pass

            registerable_targets = []
            batch_a = []
            batch_b = []
            last_pool_minute = None
            _sleep_until_next_slot()