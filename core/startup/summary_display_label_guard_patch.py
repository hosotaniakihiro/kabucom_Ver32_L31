# ============================================================
# File   : core/startup/summary_display_label_guard_patch.py
# Version: V1.1-DISCORD-SUMMARY-SOURCE-AND-TIME
# ------------------------------------------------------------
# 【目的】
#   SUMMARY表示で interval_label に DataFrame が誤って渡り、
#   "AI PASSED BUY CANDIDATES (   symbol ... [47 rows x 127 columns])"
#   のように画面へDataFrame本体が表示される問題を防ぐ。
#
# 【追加目的 V1.1】
#   Discordへ BUY TOP10 / SELL TOP10 を送るとき、
#   PUSH由来サマリーかランキング由来サマリーかを明示する。
#   さらに、そのTOP10がどの時刻のサマリー結果かを明示する。
#
# 【方針】
#   - scheduler_jobs.summary.display の公開表示関数を薄くwrapする。
#   - interval_label が DataFrame / Series / dict / list / tuple の場合は表示名として使わない。
#   - kwargs の interval があれば "3min" のように復元する。
#   - それも無ければ "-" にする。
#   - 表示だけの修正で、エントリー判定・AI判定・DB保存には触らない。
#   - Discord専用TOP10生成関数もwrapし、BUY/SELL見出しに由来と結果時刻を出す。
# ============================================================

from __future__ import annotations

import logging
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)
_PATCHED = False


def _looks_bad_label(v: Any) -> bool:
    try:
        if v is None:
            return False
        if isinstance(v, (pd.DataFrame, pd.Series, dict, list, tuple, set)):
            return True
        s = str(v)
        # pandas DataFrame の文字列表現がタイトルに混ざる場合の保険
        if " rows x " in s and "columns" in s:
            return True
        if "   symbol" in s and "datetime" in s:
            return True
        if len(s) > 80:
            return True
        return False
    except Exception:
        return True


def _label_from_kwargs(kwargs: dict[str, Any], default: str = "-") -> str:
    try:
        lbl = kwargs.get("interval_label")
        if lbl is not None and not _looks_bad_label(lbl):
            return str(lbl)

        interval = kwargs.get("interval")
        if interval is not None and not _looks_bad_label(interval):
            try:
                return f"{int(float(interval))}min"
            except Exception:
                return str(interval)
    except Exception:
        pass
    return default


def _normalize_args_kwargs(args: tuple[Any, ...], kwargs: dict[str, Any]) -> tuple[tuple[Any, ...], dict[str, Any]]:
    args_l = list(args)
    kw = dict(kwargs)

    # interval_label がkeywordで壊れている場合
    if "interval_label" in kw and _looks_bad_label(kw.get("interval_label")):
        old_type = type(kw.get("interval_label")).__name__
        kw["interval_label"] = _label_from_kwargs(kw, default="-")
        logger.warning(
            "[SUMMARY DISPLAY LABEL GUARD] fixed bad keyword interval_label old_type=%s new=%s",
            old_type,
            kw.get("interval_label"),
        )

    # 第2 positional が interval_label の関数用。ここにDataFrameが入る事故を補正。
    if len(args_l) >= 2 and _looks_bad_label(args_l[1]):
        old_type = type(args_l[1]).__name__
        args_l[1] = _label_from_kwargs(kw, default="-")
        logger.warning(
            "[SUMMARY DISPLAY LABEL GUARD] fixed bad positional interval_label old_type=%s new=%s",
            old_type,
            args_l[1],
        )

    return tuple(args_l), kw


def _wrap(fn: Any, name: str):
    if not callable(fn):
        return fn
    if getattr(fn, "_summary_display_label_guard_v1", False):
        return fn

    def _wrapped(*args: Any, **kwargs: Any):
        a, kw = _normalize_args_kwargs(args, kwargs)
        return fn(*a, **kw)

    _wrapped._summary_display_label_guard_v1 = True  # type: ignore[attr-defined]
    _wrapped._original = fn  # type: ignore[attr-defined]
    _wrapped.__name__ = getattr(fn, "__name__", name)
    _wrapped.__qualname__ = getattr(fn, "__qualname__", name)
    _wrapped.__doc__ = getattr(fn, "__doc__", None)
    return _wrapped


def _summary_source_label_for_discord(ranking: bool) -> str:
    return "ランキング由来サマリー" if bool(ranking) else "PUSH由来サマリー"


def _latest_summary_time_for_discord(df: pd.DataFrame) -> str:
    """
    Discord表示用に、DataFrame内の最新サマリー時刻を抽出する。
    DBや経路によって列名が揺れるため、候補列を順に見る。
    """
    try:
        if df is None or not isinstance(df, pd.DataFrame) or df.empty:
            return "-"

        candidates = [
            "summary_datetime",
            "summary_dt",
            "bar_datetime",
            "datetime",
            "dt",
            "timestamp",
            "time",
            "saved_at",
            "updated_at",
            "created_at",
        ]

        for col in candidates:
            if col not in df.columns:
                continue
            try:
                s = pd.to_datetime(df[col], errors="coerce")
                if s.notna().any():
                    mx = s.max()
                    if pd.notna(mx):
                        return mx.strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                continue

    except Exception:
        logger.debug("[SUMMARY DISPLAY LABEL GUARD] latest summary time detect failed", exc_info=True)

    return "-"


def _patched_collect_discord_top10_sections(
    df: pd.DataFrame,
    interval_label: str,
    *,
    ranking: bool = False,
) -> list[str]:
    """
    scheduler_jobs.summary.display._collect_discord_top10_sections の置換版。
    BUY TOP10 / SELL TOP10 見出しに、由来とサマリー結果時刻を表示する。
    """
    lines: list[str] = []

    try:
        import scheduler_jobs.summary.display as disp

        source_label = _summary_source_label_for_discord(ranking)
        summary_time = _latest_summary_time_for_discord(df)
        title_prefix = "RANKING SUMMARY" if ranking else "PUSH SUMMARY"
        meta = f"{source_label} / 結果時刻={summary_time}"

        lines.append(f"========== 📊 {title_prefix} TOP10 ({interval_label}) / {meta} ==========")

        lines.append(f"🔵 BUY TOP10【{meta}】")
        buy_df = disp.prepare_buy_df(df)
        if buy_df.empty:
            lines.append(" (no buy candidates)")
        else:
            for i, (_, row) in enumerate(buy_df.head(10).iterrows(), start=1):
                lines.append(disp._build_discord_candidate_2lines(i, row, side="BUY"))

        lines.append(f"🔴 SELL TOP10【{meta}】")
        sell_df = disp.prepare_sell_df(df)
        if sell_df.empty:
            lines.append(" (no sell candidates)")
        else:
            for i, (_, row) in enumerate(sell_df.head(10).iterrows(), start=1):
                lines.append(disp._build_discord_candidate_2lines(i, row, side="SELL"))

    except Exception:
        logger.exception("[SUMMARY DISPLAY LABEL GUARD] patched discord top10 sections failed")

    return lines


def _patch_discord_top10_sections(disp: Any) -> bool:
    try:
        old = getattr(disp, "_collect_discord_top10_sections", None)
        if getattr(old, "_summary_display_source_time_v1", False):
            return True

        _patched_collect_discord_top10_sections._summary_display_source_time_v1 = True  # type: ignore[attr-defined]
        _patched_collect_discord_top10_sections._original = old  # type: ignore[attr-defined]
        setattr(disp, "_collect_discord_top10_sections", _patched_collect_discord_top10_sections)
        logger.warning("[SUMMARY DISPLAY LABEL GUARD] discord top10 source/time patch installed")
        return True
    except Exception:
        logger.exception("[SUMMARY DISPLAY LABEL GUARD] discord top10 source/time patch failed")
        return False


def install() -> bool:
    global _PATCHED
    if _PATCHED:
        return True

    try:
        import scheduler_jobs.summary.display as disp
    except Exception:
        logger.exception("[SUMMARY DISPLAY LABEL GUARD] import display failed")
        return False

    names = [
        "print_summary_top10",
        "print_ranking_summary_top10",
        "print_push_summary",
        "print_ranking_summary",
        "display_ai_passed_summary",
        "display_summary",
        "display_push_summary",
        "display_ranking_summary",
    ]

    patched = []
    for name in names:
        try:
            old = getattr(disp, name, None)
            if callable(old):
                setattr(disp, name, _wrap(old, name))
                patched.append(name)
        except Exception:
            logger.debug("[SUMMARY DISPLAY LABEL GUARD] patch failed name=%s", name, exc_info=True)

    discord_patched = _patch_discord_top10_sections(disp)

    _PATCHED = bool(patched) or bool(discord_patched)
    logger.warning(
        "[SUMMARY DISPLAY LABEL GUARD] installed=%s patched=%s discord_source_time=%s",
        _PATCHED,
        patched,
        discord_patched,
    )
    return _PATCHED


try:
    install()
except Exception:
    logger.exception("[SUMMARY DISPLAY LABEL GUARD] auto install failed")

__all__ = ["install"]
