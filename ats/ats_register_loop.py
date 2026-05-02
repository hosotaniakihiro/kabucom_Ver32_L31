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
    register_payload,
)
from .ats_register_logging import _print_current_ats_registered_symbols

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SEC = 20


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


def ats_register_loop(*args, **kwargs):
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
        "🚀 ats_register_loop START (interval=%s sec / batch50 unregister-all vendor-style / ranking-only targets)",
        interval_sec,
    )

    rotator = ATSRotationManager(batch_size=100, shift=50)

    phase = 0
    registerable_targets = []
    batch_a = []
    batch_b = []

    while True:
        try:
            if is_in_429_cooldown():
                logger.warning(
                    "[ATS LOOP] in 429 cooldown -> keep last good and sleep %ss",
                    interval_sec,
                )
                keep_last_good_batch(reason_phase="COOLDOWN")
                time.sleep(interval_sec)
                continue

            if not registerable_targets or phase == 0:
                registerable_targets = build_registerable_symbol_pool(
                    rotator=rotator,
                    target_size=ATS_BATCH_SIZE,
                    max_attempts=10,
                )

                batch_a, batch_b = split_batches_50(registerable_targets)

                logger.info(
                    "[ATS CYCLE] registerable batchA=%d batchB=%d total=%d",
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

            if phase == 0:
                current_symbols = batch_a
                phase_name = "A"
            else:
                current_symbols = batch_b
                phase_name = "B"

            if not current_symbols:
                if phase == 1 and batch_a:
                    logger.warning(
                        "[ATS %s] current batch empty but batchA exists -> keep/reuse batchA",
                        phase_name,
                    )
                    current_symbols = batch_a
                    phase_name = "A-reuse"
                else:
                    logger.warning(
                        "[ATS %s] current batch empty -> keep last good and reset cycle",
                        phase_name,
                    )
                    keep_last_good_batch(reason_phase=f"{phase_name}-EMPTY")
                    registerable_targets = []
                    batch_a = []
                    batch_b = []
                    phase = 0
                    time.sleep(interval_sec)
                    continue

            ok = register_payload(current_symbols, phase=phase_name, apply_all_filters=apply_all_filters)

            if ok:
                logger.info(
                    "[ATS %s] active batch applied size=%d",
                    phase_name,
                    len(sanitize_symbols(current_symbols)),
                )
            else:
                logger.warning(
                    "[ATS %s] apply failed -> last_good_phase=%s size=%d",
                    phase_name,
                    get_last_good_phase(),
                    len(get_last_good_batch()),
                )

            time.sleep(interval_sec)
            phase = 1 - phase

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
            phase = 0
            time.sleep(interval_sec)