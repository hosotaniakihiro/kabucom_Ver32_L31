# ============================================================
# File   : trading/ai/alpha_evolution_ai.py
# Version: Ver1.0-ALPHA-EVOLUTION-AI-PRODUCTION
# ------------------------------------------------------------
# ✔ アルファ生成AI
# ✔ flag combination discovery
# ✔ profit expectancy analysis
# ✔ ranking boost
# ✔ production safe
# ✔ HFT軽量設計
# ============================================================

from __future__ import annotations

import logging
import pandas as pd
import numpy as np
import datetime as dt
import itertools
import random

from sqlalchemy import text

from database.session import get_position_engine

logger = logging.getLogger(__name__)


# ============================================================
# SETTINGS
# ============================================================

LOOKBACK_DAYS = 45
MIN_SAMPLE = 8
MAX_FLAGS_PER_ALPHA = 3
MAX_COMBINATIONS = 2000


# ============================================================
# CACHE
# ============================================================

_alpha_models = {}
_last_update = None


# ============================================================
# LOAD TRADE HISTORY
# ============================================================

def _load_history():

    engine = get_position_engine()

    since = dt.date.today() - dt.timedelta(days=LOOKBACK_DAYS)

    sql = text("""
    SELECT
        pnl,
        entry_reason,
        entry_time
    FROM trade_history
    WHERE entry_time >= :since
    """)

    df = pd.read_sql(sql, engine, params={"since": since})

    return df


# ============================================================
# PARSE FLAGS
# ============================================================

def _parse_flags(df):

    rows = []

    for _, r in df.iterrows():

        flags = str(r.entry_reason).split(",")

        flags = [f.strip() for f in flags if f.strip()]

        rows.append({

            "flags": flags,
            "pnl": r.pnl
        })

    return rows


# ============================================================
# FLAG UNIVERSE
# ============================================================

def _collect_flag_universe(rows):

    s = set()

    for r in rows:

        for f in r["flags"]:

            s.add(f)

    return list(s)


# ============================================================
# TEST ALPHA
# ============================================================

def _evaluate_alpha(rows, combo):

    pnls = []

    for r in rows:

        if all(f in r["flags"] for f in combo):

            pnls.append(r["pnl"])

    n = len(pnls)

    if n < MIN_SAMPLE:

        return None

    pnls = np.array(pnls)

    win = (pnls > 0).sum()

    win_rate = win / n

    avg = pnls.mean()

    expectancy = avg * win_rate

    return {

        "trades": n,
        "win_rate": win_rate,
        "expectancy": expectancy
    }


# ============================================================
# GENERATE ALPHAS
# ============================================================

def _discover_alphas(rows):

    flags = _collect_flag_universe(rows)

    if not flags:

        return {}

    results = {}

    tested = 0

    while tested < MAX_COMBINATIONS:

        k = random.randint(1, MAX_FLAGS_PER_ALPHA)

        combo = tuple(sorted(random.sample(flags, k)))

        if combo in results:

            continue

        stat = _evaluate_alpha(rows, combo)

        tested += 1

        if not stat:

            continue

        results[combo] = stat

    return results


# ============================================================
# UPDATE MODEL
# ============================================================

def update_alpha_model(force=False):

    global _alpha_models
    global _last_update

    now = dt.datetime.now()

    if not force:

        if _last_update and (now - _last_update).seconds < 900:

            return

    try:

        df = _load_history()

        if df.empty:

            return

        rows = _parse_flags(df)

        models = _discover_alphas(rows)

        _alpha_models = models

        _last_update = now

        logger.info(
            "[ALPHA AI] discovered=%s",
            len(models)
        )

    except Exception:

        logger.exception("[ALPHA AI] update failed")


# ============================================================
# APPLY ALPHA BOOST
# ============================================================

def alpha_boost(row):

    flags = []

    for c in row.index:

        if c.startswith("flag_") and row[c] == 1:

            flags.append(c)

    if not flags:

        return 0

    boost = 0

    flag_set = set(flags)

    for combo, stat in _alpha_models.items():

        if all(f in flag_set for f in combo):

            boost += stat["expectancy"]

    return boost


# ============================================================
# ENTRY FILTER
# ============================================================

def alpha_filter(row):

    flags = []

    for c in row.index:

        if c.startswith("flag_") and row[c] == 1:

            flags.append(c)

    if not flags:

        return True

    flag_set = set(flags)

    bad = 0

    for combo, stat in _alpha_models.items():

        if all(f in flag_set for f in combo):

            if stat["expectancy"] < 0:

                bad += 1

    if bad >= 2:

        return False

    return True


# ============================================================
# DEBUG
# ============================================================

def dump_alpha_models():

    items = sorted(
        _alpha_models.items(),
        key=lambda x: -x[1]["expectancy"]
    )

    for combo, stat in items[:30]:

        logger.info(
            "[ALPHA] %s trades=%s exp=%.1f",
            ",".join(combo),
            stat["trades"],
            stat["expectancy"]
        )