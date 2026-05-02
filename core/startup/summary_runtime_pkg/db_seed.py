# ============================================================
# File   : core/startup/summary_runtime_pkg/db_seed.py
# Version: REV4.1-SUMMARY-RUNTIME-DB-SEED-ORCHESTRATOR
#          -MODULARIZED
#          -BOOT-HISTORY-BARS-FORCE
#          -STORE-HISTORY-CACHE-AND-LATEST-CACHE
#          -POST-SEED-MTF-REBUILD
#          -INCLUDE-PREVIOUS-BUSINESS-DAY
#          -MULTI-DAY-SUMMARY-DB-DIRECT-TAIL
#          -DISPLAY-SEED-TECHNICAL-FFILL
# ------------------------------------------------------------
# 【概要】
#   起動時 summary DB 履歴 seed の薄い orchestrator
#
# 【主な機能】
#   ✔ summary DB から履歴を復元
#   ✔ 当日DB + 前営業日DB の multi-day seed に対応
#   ✔ summary history cache へ全行保存
#   ✔ 表示用 merged summary cache へ保存
#   ✔ MTF / scoring rebuild
#   ✔ 外部互換 API を維持
#
# 【REV4.1 修正】
#   ✔ history cache は full history のまま保持
#   ✔ merged summary に渡す表示用DFだけ technical columns を forward-fill
#   ✔ latest 1行/銘柄へ圧縮されたときに rsi/macd/slope/mtf が消える問題を軽減
#   ✔ 0 が未計算値として入る列は表示用のみ NaN 扱いにして ffill
#
# 【分割先】
#   db_seed_policy.py
#   db_seed_diagnostics.py
#   db_seed_cache.py
#   db_seed_anchor.py
#   db_seed_loaders.py
#   db_seed_multiday_sqlite.py
#   db_seed_rebuild.py
# ============================================================

from __future__ import annotations

import logging

import pandas as pd

from . import state
from .state import (
    SUMMARY_TFS,
    SUMMARY_DB_SEED_MIN_ROWS,
)
from .dataframe_utils import (
    log_summary_profile,
    merge_existing_and_seed,
)
from .db_seed_policy import (
    BOOT_HISTORY_BARS_BY_TF,
    BOOT_HISTORY_REQUIRED_HINT_BY_TF,
    get_seed_bars,
    latest_dt,
    nonzero_count,
)
from .db_seed_diagnostics import (
    log_history_quality,
    log_indicator_profile,
    safe_symbols_count,
)
from .db_seed_cache import (
    get_push_merged_summary_safe,
    set_push_merged_summary_safe,
    get_summary_history_safe,
    set_summary_history_safe,
)
from .db_seed_anchor import (
    resolve_anchor_for_seed,
)
from .db_seed_loaders import (
    normalize_seed_df,
    call_loader_with_supported_kwargs,
    load_summary_seed_by_latest_snapshot,
    load_summary_seed_by_recent_tail_loader,
    load_summary_seed_from_db,
)
from .db_seed_multiday_sqlite import (
    load_summary_seed_by_multiday_sqlite_direct,
)
from .db_seed_rebuild import (
    maybe_rebuild_seed_indicators,
    post_seed_mtf_rebuild,
)

logger = logging.getLogger(__name__)


def _get_existing_history_or_latest(tf: int) -> pd.DataFrame:
    """
    DB seed merge 用の既存データ取得。

    優先:
      1. summary history cache
      2. push merged summary
    """
    try:
        existing = get_summary_history_safe(tf)
        if isinstance(existing, pd.DataFrame) and not existing.empty:
            logger.info(
                "[summary_runtime] existing history cache found tf=%s rows=%d symbols=%d",
                tf,
                len(existing),
                safe_symbols_count(existing),
            )
            return existing
    except Exception:
        logger.debug(
            "[summary_runtime] existing history cache get failed tf=%s",
            tf,
            exc_info=True,
        )

    try:
        existing = get_push_merged_summary_safe(tf)
        if isinstance(existing, pd.DataFrame) and not existing.empty:
            logger.info(
                "[summary_runtime] existing push merged cache fallback tf=%s rows=%d symbols=%d",
                tf,
                len(existing),
                safe_symbols_count(existing),
            )
            return existing
    except Exception:
        logger.debug(
            "[summary_runtime] existing push merged cache get failed tf=%s",
            tf,
            exc_info=True,
        )

    return pd.DataFrame()


def _prepare_display_seed_df(df: pd.DataFrame, *, tf: int) -> pd.DataFrame:
    """
    表示用 merged summary に渡す前の補正。

    summary_history は full history を保持する。
    しかし merged summary は latest 1行/銘柄へ圧縮されるため、
    最新行に technical 指標が NaN/0 の場合、表示で rsi/macd/slope/mtf が消える。

    そこで、表示用だけ symbol ごとに technical columns を forward-fill してから渡す。

    注意:
      - DB保存用ではない
      - history cache 用でもない
      - あくまで起動直後の表示 cache 用
    """
    if not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame()

    out = df.copy()

    if "symbol" not in out.columns or "datetime" not in out.columns:
        logger.warning(
            "[summary_runtime] display seed prepare skipped tf=%s reason=missing symbol/datetime cols=%s",
            tf,
            list(out.columns),
        )
        return out

    try:
        out["symbol"] = out["symbol"].astype(str).str.strip()
        out = out[out["symbol"].ne("")].copy()

        out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
        try:
            if getattr(out["datetime"].dt, "tz", None) is not None:
                out["datetime"] = out["datetime"].dt.tz_localize(None)
        except Exception:
            pass

        out = out.dropna(subset=["datetime"]).copy()
        if out.empty:
            return out

        out = out.sort_values(["symbol", "datetime"], kind="stable").reset_index(drop=True)

    except Exception:
        logger.debug(
            "[summary_runtime] display seed base normalize failed tf=%s",
            tf,
            exc_info=True,
        )
        return df

    # latest 1行化で消えやすい technical columns
    technical_cols = [
        "rsi",
        "macd",
        "signal",
        "hist",
        "slope",
        "slope_raw",
        "slope_atr_scaled",
        "score_slope",
        "mtf",
        "score_mtf",
        "mtf_score",
        "mtf_alignment",
        "ma5",
        "ma25",
        "ma75",
        "ma75_slope",
        "ema12",
        "ema26",
        "atr",
        "vwap",
        "vwap_slope",
        "volume_slope",
    ]

    existing = [c for c in technical_cols if c in out.columns]

    for c in existing:
        try:
            out[c] = pd.to_numeric(out[c], errors="coerce")
        except Exception:
            pass

    # 0 が「未計算値」として入りやすい列。
    # 表示用だけ NaN 扱いにして、過去の有効値を ffill させる。
    # score自体はここでは触らない。
    zero_as_missing_cols = [
        "slope",
        "slope_raw",
        "slope_atr_scaled",
        "score_slope",
        "mtf",
        "score_mtf",
        "mtf_score",
        "mtf_alignment",
    ]

    for c in zero_as_missing_cols:
        if c in out.columns:
            try:
                s = pd.to_numeric(out[c], errors="coerce")
                out.loc[s.eq(0), c] = pd.NA
            except Exception:
                logger.debug(
                    "[summary_runtime] display seed zero->NA failed tf=%s col=%s",
                    tf,
                    c,
                    exc_info=True,
                )

    try:
        if existing:
            out[existing] = out.groupby("symbol", group_keys=False)[existing].ffill()
    except Exception:
        logger.debug(
            "[summary_runtime] display seed ffill failed tf=%s",
            tf,
            exc_info=True,
        )

    # 最新行の表示で使われる score_mtf / mtf_score の alias 補完
    try:
        if "score_mtf" in out.columns and "mtf_score" in out.columns:
            sm = pd.to_numeric(out["score_mtf"], errors="coerce")
            ms = pd.to_numeric(out["mtf_score"], errors="coerce")
            out["score_mtf"] = sm.combine_first(ms)
            out["mtf_score"] = ms.combine_first(sm)
        elif "score_mtf" in out.columns and "mtf_score" not in out.columns:
            out["mtf_score"] = out["score_mtf"]
        elif "mtf_score" in out.columns and "score_mtf" not in out.columns:
            out["score_mtf"] = out["mtf_score"]
    except Exception:
        logger.debug(
            "[summary_runtime] display seed mtf alias fill failed tf=%s",
            tf,
            exc_info=True,
        )

    try:
        logger.info(
            "[summary_runtime] display seed prepared tf=%s rows=%d symbols=%d "
            "rsi_nonnull=%d macd_nonnull=%d signal_nonnull=%d "
            "slope_nonnull=%d score_slope_nonnull=%d mtf_nonnull=%d "
            "score_mtf_nonnull=%d mtf_score_nonnull=%d latest_dt=%s",
            tf,
            len(out),
            safe_symbols_count(out),
            int(pd.to_numeric(out["rsi"], errors="coerce").notna().sum()) if "rsi" in out.columns else 0,
            int(pd.to_numeric(out["macd"], errors="coerce").notna().sum()) if "macd" in out.columns else 0,
            int(pd.to_numeric(out["signal"], errors="coerce").notna().sum()) if "signal" in out.columns else 0,
            int(pd.to_numeric(out["slope"], errors="coerce").notna().sum()) if "slope" in out.columns else 0,
            int(pd.to_numeric(out["score_slope"], errors="coerce").notna().sum()) if "score_slope" in out.columns else 0,
            int(pd.to_numeric(out["mtf"], errors="coerce").notna().sum()) if "mtf" in out.columns else 0,
            int(pd.to_numeric(out["score_mtf"], errors="coerce").notna().sum()) if "score_mtf" in out.columns else 0,
            int(pd.to_numeric(out["mtf_score"], errors="coerce").notna().sum()) if "mtf_score" in out.columns else 0,
            latest_dt(out),
        )
    except Exception:
        logger.debug(
            "[summary_runtime] display seed prepared log failed tf=%s",
            tf,
            exc_info=True,
        )

    return out


def seed_runtime_summary_cache_from_db(
    *,
    force: bool = False,
    stage: str = "startup",
    rebuild_missing_scores: bool = False,
) -> dict[int, int]:
    """
    起動時に summary DB の履歴を runtime cache へ復元する。

    保存先:
      - summary_history_cache:
          計算用。履歴を全行保持。
      - merged_summary:
          表示用。GlobalContext 側で最新1行/銘柄へ圧縮。

    目的:
      - global_data.clear_all() 後でも履歴を失わない
      - 起動直後の PUSH差分だけでテクニカル計算しない
      - RSI / MACD / slope / MTF の不足を減らす
    """
    if state.RUNTIME_DB_SEED_RUNNING:
        logger.info("[summary_runtime] DB seed already running -> skip stage=%s", stage)
        return {}

    if state.RUNTIME_DB_SEED_DONE and not force:
        logger.info("[summary_runtime] DB seed already done -> skip stage=%s", stage)
        return {}

    state.set_runtime_db_seed_flags(running=True, failed=False)

    loaded: dict[int, int] = {}
    rebuilt_mtf: dict[int, int] = {}

    try:
        logger.info(
            "[summary_runtime] DB seed start stage=%s force=%s rebuild_missing_scores=%s",
            stage,
            force,
            rebuild_missing_scores,
        )

        for tf in SUMMARY_TFS:
            tf = int(tf)
            bars = get_seed_bars(tf)

            existing = _get_existing_history_or_latest(tf)
            log_summary_profile(f"db-seed-existing-before-{stage}", tf, existing)

            seed_df = load_summary_seed_from_db(tf)
            seed_df = normalize_seed_df(seed_df, tf, bars=bars)

            log_history_quality(
                seed_df,
                tf=tf,
                bars=bars,
                label=f"db-seed-loaded-{stage}",
            )
            log_indicator_profile(
                seed_df,
                tf=tf,
                label=f"db-seed-loaded-{stage}",
            )

            if rebuild_missing_scores:
                seed_df = maybe_rebuild_seed_indicators(seed_df, tf)

            if not isinstance(seed_df, pd.DataFrame) or seed_df.empty:
                loaded[tf] = 0
                rebuilt_mtf[tf] = 0
                logger.warning("[summary_runtime] DB seed empty tf=%s stage=%s", tf, stage)
                continue

            if len(seed_df) < SUMMARY_DB_SEED_MIN_ROWS.get(tf, 1):
                loaded[tf] = 0
                rebuilt_mtf[tf] = 0
                logger.warning(
                    "[summary_runtime] DB seed too small -> skip tf=%s stage=%s rows=%d",
                    tf,
                    stage,
                    len(seed_df),
                )
                continue

            merged = merge_existing_and_seed(
                existing,
                seed_df,
                tf,
                label=f"db-seed-{stage}",
            )
            merged = normalize_seed_df(merged, tf, bars=bars)

            if not isinstance(merged, pd.DataFrame) or merged.empty:
                loaded[tf] = 0
                rebuilt_mtf[tf] = 0
                logger.warning("[summary_runtime] DB seed merged empty tf=%s stage=%s", tf, stage)
                continue

            before_mtf = (
                nonzero_count(merged, "mtf")
                + nonzero_count(merged, "score_mtf")
                + nonzero_count(merged, "mtf_score")
            )

            merged = post_seed_mtf_rebuild(merged, tf)
            merged = normalize_seed_df(merged, tf, bars=bars)

            after_mtf = (
                nonzero_count(merged, "mtf")
                + nonzero_count(merged, "score_mtf")
                + nonzero_count(merged, "mtf_score")
            )

            rebuilt_mtf[tf] = len(merged) if after_mtf > before_mtf else 0

            log_indicator_profile(
                merged,
                tf=tf,
                label=f"post-seed-mtf-rebuilt-{stage}",
            )

            # ====================================================
            # 1) 計算用履歴 cache
            # ----------------------------------------------------
            # ここには full history を保存する。
            # indicator / scoring / later incremental calculation 用。
            # ====================================================
            set_summary_history_safe(tf, merged)
            log_summary_profile(f"db-seed-stored-history-{stage}", tf, merged)

            # ====================================================
            # 2) 表示用 latest cache
            # ----------------------------------------------------
            # GlobalContext 側で latest 1行/銘柄へ圧縮される。
            # latest 行だけ technical が NaN/0 の場合に表示から消えるため、
            # 表示用だけ technical columns を ffill して渡す。
            # ====================================================
            display_seed = _prepare_display_seed_df(merged, tf=tf)
            log_summary_profile(f"db-seed-display-before-merged-set-{stage}", tf, display_seed)

            set_push_merged_summary_safe(tf, display_seed)

            loaded[tf] = len(merged)

            logger.info(
                "[summary_runtime] DB seed stored tf=%s stage=%s history_rows=%d display_rows=%d symbols=%d latest_dt=%s",
                tf,
                stage,
                len(merged),
                len(display_seed) if isinstance(display_seed, pd.DataFrame) else 0,
                safe_symbols_count(merged),
                latest_dt(merged),
            )

        ok = any(v > 0 for v in loaded.values())
        state.set_runtime_db_seed_flags(done=ok, failed=not ok)

        logger.info(
            "[summary_runtime] DB seed done stage=%s loaded=%s rebuilt_mtf=%s ok=%s",
            stage,
            loaded,
            rebuilt_mtf,
            ok,
        )
        return loaded

    except Exception:
        state.set_runtime_db_seed_flags(failed=True)
        logger.exception("[summary_runtime] DB seed failed stage=%s", stage)
        return loaded

    finally:
        state.set_runtime_db_seed_flags(running=False)


__all__ = [
    "BOOT_HISTORY_BARS_BY_TF",
    "BOOT_HISTORY_REQUIRED_HINT_BY_TF",
    "get_summary_history_safe",
    "set_summary_history_safe",
    "resolve_anchor_for_seed",
    "call_loader_with_supported_kwargs",
    "load_summary_seed_by_latest_snapshot",
    "load_summary_seed_by_recent_tail_loader",
    "load_summary_seed_by_multiday_sqlite_direct",
    "load_summary_seed_from_db",
    "maybe_rebuild_seed_indicators",
    "seed_runtime_summary_cache_from_db",
]