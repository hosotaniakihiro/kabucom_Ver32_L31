# ==========================================================
# File   : core/startup/summary_controller_concat_duplicate_columns_patch.py
# Version: Ver1.0-SUMMARY-CONTROLLER-CONCAT-DUPLICATE-COLUMNS-GUARD
# ----------------------------------------------------------
# 【目的】
#   trading.summary.controller_cache.concat_frames() で
#   pandas.errors.InvalidIndexError:
#     Reindexing only valid with uniquely valued Index objects
#   が出る問題を runtime patch で防止する。
#
# 【原因】
#   normalize_fn / MTF attach / daily MA attach などの合成過程で、
#   close / close_price / score / score_mtf / mtf / display_ready などが
#   同名カラムとして重複する場合がある。
#   pd.concat(axis=0) は行方向結合でも列Indexを揃えるため、
#   columns が一意でない DataFrame が混ざると InvalidIndexError になる。
#
# 【方針】
#   - concat 前後、attach_display_ready 前に columns を必ず一意化する
#   - 重複カラムは「左側優先、ただし左が欠損なら右側で補完」する
#   - 元の controller_cache.py の設計は変えず、起動時 patch として適用する
# ==========================================================

from __future__ import annotations

import logging
from typing import Callable

import pandas as pd

logger = logging.getLogger(__name__)

_INSTALLED = False
_ORIG_ATTACH_DISPLAY_READY = None
_ORIG_CONCAT_FRAMES = None


def _dedupe_duplicate_columns(df: pd.DataFrame, *, context: str = "") -> pd.DataFrame:
    """
    DataFrame.columns を一意にする。

    同名カラムが複数ある場合:
      - 先頭列を優先
      - 先頭列が NaN/NA の行だけ、後続の同名列で補完
    """
    if not isinstance(df, pd.DataFrame) or df.empty:
        return df

    try:
        if df.columns.is_unique:
            return df
    except Exception:
        return df

    try:
        out = df.copy()
        cols = list(out.columns)
        seen: dict[str, list[int]] = {}
        order: list[str] = []

        for i, c in enumerate(cols):
            name = str(c)
            if name not in seen:
                seen[name] = []
                order.append(name)
            seen[name].append(i)

        dup_names = [name for name, idxs in seen.items() if len(idxs) > 1]

        if dup_names:
            logger.warning(
                "[SUMMARY CONTROLLER DUPCOL PATCH] duplicate columns detected context=%s dup_count=%s dup_names=%s cols_before=%s",
                context,
                len(dup_names),
                dup_names[:30],
                len(cols),
            )

        series_list = []
        new_cols = []

        for name in order:
            idxs = seen[name]
            if len(idxs) == 1:
                s = out.iloc[:, idxs[0]]
            else:
                block = out.iloc[:, idxs]
                try:
                    s = block.bfill(axis=1).iloc[:, 0]
                except Exception:
                    s = block.iloc[:, 0]
            series_list.append(pd.Series(s).reset_index(drop=True))
            new_cols.append(name)

        fixed = pd.concat(series_list, axis=1, ignore_index=True)
        fixed.columns = new_cols
        fixed.index = out.index

        if dup_names:
            logger.warning(
                "[SUMMARY CONTROLLER DUPCOL PATCH] duplicate columns fixed context=%s cols_after=%s rows=%s",
                context,
                len(fixed.columns),
                len(fixed),
            )

        return fixed

    except Exception:
        logger.exception(
            "[SUMMARY CONTROLLER DUPCOL PATCH] failed to dedupe duplicate columns context=%s",
            context,
        )
        try:
            return df.loc[:, ~df.columns.duplicated()].copy()
        except Exception:
            return df


def install() -> bool:
    global _INSTALLED, _ORIG_ATTACH_DISPLAY_READY, _ORIG_CONCAT_FRAMES

    if _INSTALLED:
        return True

    try:
        import trading.summary.controller_cache as cc

        _ORIG_ATTACH_DISPLAY_READY = getattr(cc, "attach_display_ready", None)
        _ORIG_CONCAT_FRAMES = getattr(cc, "concat_frames", None)

        if not callable(_ORIG_ATTACH_DISPLAY_READY) or not callable(_ORIG_CONCAT_FRAMES):
            logger.warning(
                "[SUMMARY CONTROLLER DUPCOL PATCH] install skipped: original functions unavailable attach=%s concat=%s",
                callable(_ORIG_ATTACH_DISPLAY_READY),
                callable(_ORIG_CONCAT_FRAMES),
            )
            return False

        def patched_attach_display_ready(df: pd.DataFrame) -> pd.DataFrame:
            df = _dedupe_duplicate_columns(df, context="attach_display_ready:before")
            out = _ORIG_ATTACH_DISPLAY_READY(df)
            out = _dedupe_duplicate_columns(out, context="attach_display_ready:after")
            return out

        def patched_concat_frames(
            frames: list[pd.DataFrame],
            normalize_fn: Callable[[pd.DataFrame], pd.DataFrame],
        ) -> pd.DataFrame:
            xs: list[pd.DataFrame] = []

            for i, x in enumerate(frames or []):
                try:
                    df = normalize_fn(x)
                    df = _dedupe_duplicate_columns(df, context=f"concat:normalized:{i}")
                    if isinstance(df, pd.DataFrame) and not df.empty:
                        df = patched_attach_display_ready(df)
                        df = _dedupe_duplicate_columns(df, context=f"concat:ready:{i}")
                        xs.append(df)
                except Exception:
                    logger.exception(
                        "[SUMMARY CONTROLLER DUPCOL PATCH] normalize/ready failed frame_index=%s",
                        i,
                    )

            if not xs:
                return pd.DataFrame()

            try:
                xs = [
                    _dedupe_duplicate_columns(x, context=f"concat:pre:{i}")
                    for i, x in enumerate(xs)
                    if isinstance(x, pd.DataFrame) and not x.empty
                ]
                out = pd.concat(xs, axis=0, ignore_index=True, sort=False)
                out = _dedupe_duplicate_columns(out, context="concat:post_concat")
                out = normalize_fn(out)
                out = _dedupe_duplicate_columns(out, context="concat:post_normalize")
                out = patched_attach_display_ready(out)
                out = _dedupe_duplicate_columns(out, context="concat:final")
                return out
            except Exception:
                logger.exception("[SUMMARY CONTROLLER DUPCOL PATCH] concat frames failed even after dupcol guard")
                return pd.DataFrame()

        cc.attach_display_ready = patched_attach_display_ready
        cc.concat_frames = patched_concat_frames
        setattr(cc, "_dedupe_duplicate_columns", _dedupe_duplicate_columns)

        _INSTALLED = True
        logger.warning("[SUMMARY CONTROLLER DUPCOL PATCH] installed")
        return True

    except Exception:
        logger.exception("[SUMMARY CONTROLLER DUPCOL PATCH] install failed")
        return False


__all__ = ["install"]
