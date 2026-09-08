# ============================================================
# File   : trading/entry/summary_ai/mtf_attach.py
# Version: V1.0-SUMMARY-AI-3M-5M-ATTACH
# ------------------------------------------------------------
# 1 分足の AI 候補へ 3 分足 / 5 分足のスコアを付ける。
#
# 【なぜ必要か】(2026-09-01 12:53 のログ)
#   [SUMMARY AI GATE] AI_OK side=BUY symbol=9856 ... 3m=NA|5m=NA|mtf_data=DATA_NOT_READY
#   [SUMMARY AI WEAK FILTER] zero 3m/5m confirmation ng kind=data_missing
#       by_row_kind={'data_not_ready_attach_missing': 7}
#   AI が OK にした BUY 候補 7 件が、直後に 3m/5m 欠落で全件 NG になっていた。
#
#   調べると score_3m / score_5m を **書く処理がどこにも無かった**。
#     - AI/entry_gate.py            row.get("score_3m") を読む
#     - summary_ai/row_adapter.py   候補行から score_3m を拾おうとする
#     - しかし候補 DataFrame にその列を作る処理が存在しない
#   一方、3 分足 / 5 分足のデータ自体は同じログの中に有る:
#     [SUMMARY HISTORY GET] tf=3 source=push rows=195
#     [SUMMARY HISTORY GET] tf=5 source=push rows=206
#     [MERGED GET]          tf=3 source=push rows=47 / tf=5 rows=46
#   つまり「データが無い」のではなく「繋がっていない」状態だった。
#
# 【方針】
#   - 銘柄コード (文字列正規化) と **確定足の時刻** をキーに突き合わせる
#   - 未確定足 (今まさに作られている足) は採用しない
#   - 古すぎる足 (stale) も採用しない
#   - 本当にデータが無ければ列を作らない = 従来どおり fail-closed
#     (欠損を 0 として通すことは絶対にしない)
#   - 候補ごとに 3m_ready / 5m_ready / 採用足時刻 / データソースを出す
#
# 【銘柄コードの型不一致】
#   push 由来は "9856"、DB 由来は 9856 (int)、pandas 経由だと "9856.0"
#   になることがある。normalize_symbol() で全部 "9856" に寄せる。
#
# 【datetime】
#   summary の datetime は「その足の開始時刻」(1 分足で 12:53 時点の最新が
#   12:52:00)。確定判定は datetime + interval <= now で行う。
#   tz-aware / naive が混ざることがあるので naive(ローカル)へ寄せる。
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Any, Dict, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

VERSION = "V1.0-SUMMARY-AI-3M-5M-ATTACH"

MTF_INTERVALS: Tuple[int, ...] = (3, 5)

# side ごとに「その足のスコア」として使う列 (優先順)
_BUY_SCORE_KEYS = ("buy_score", "score_buy", "ai_disp_buy_score", "final_score", "score_total", "score")
_SELL_SCORE_KEYS = ("sell_score", "score_sell", "ai_disp_sell_score", "final_score", "score_total", "score")
_NEUTRAL_SCORE_KEYS = ("final_score", "score_total", "score")


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return bool(default)
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).strip())
    except Exception:
        return float(default)


def _max_age_sec(interval: int) -> float:
    """採用してよい足の最大齢。既定は interval の 4 本分。"""
    explicit = _env_float("SUMMARY_AI_MTF_MAX_AGE_SEC", 0.0)
    if explicit > 0:
        return explicit
    return float(interval) * 60.0 * 4.0


def normalize_symbol(v: Any) -> str:
    """9856 / '9856' / '9856.0' / ' 9856 ' をすべて '9856' にする。"""
    try:
        if v is None:
            return ""
        if isinstance(v, float) and not np.isfinite(v):
            return ""
        s = str(v).strip()
        if not s:
            return ""
        if s.endswith(".0"):
            s = s[:-2]
        return s
    except Exception:
        return ""


def _to_naive(ts: Any) -> Optional[dt.datetime]:
    try:
        t = pd.to_datetime(ts, errors="coerce")
        if t is None or pd.isna(t):
            return None
        if isinstance(t, pd.Timestamp):
            if t.tzinfo is not None:
                t = t.tz_convert(None) if t.tz is not None else t.tz_localize(None)
            return t.to_pydatetime()
        return t
    except Exception:
        return None


def _pick_score(row: Dict[str, Any], side: str) -> Optional[float]:
    keys = _BUY_SCORE_KEYS if side == "BUY" else (_SELL_SCORE_KEYS if side == "SELL" else _NEUTRAL_SCORE_KEYS)
    for k in keys:
        if k not in row:
            continue
        v = row.get(k)
        if v is None:
            continue
        try:
            if pd.isna(v):
                continue
            f = float(v)
        except Exception:
            continue
        if not np.isfinite(f):
            continue
        # SELL 側は score が負で表現されることがあるので絶対値で強さを見る。
        return abs(f) if side == "SELL" else f
    return None


# ============================================================
# tf frame の取得
# ============================================================

def _load_tf_frame(interval: int) -> Tuple[Optional[pd.DataFrame], str]:
    """3m / 5m の summary DataFrame を取る。(df, source) を返す。

    merged -> history の順に見る。どちらも無ければ (None, reason)。
    """
    try:
        from core.global_context.context import global_context as GC
    except Exception:
        logger.debug("[SUMMARY AI MTF ATTACH] global_context unavailable", exc_info=True)
        return None, "global_context_unavailable"

    for getter, label in (
        (lambda: GC.get_merged_summary(interval), "merged"),
        (lambda: GC.get_summary_history(interval, source="push"), "history_push"),
        (lambda: GC.get_summary_history(interval), "history"),
    ):
        try:
            df = getter()
        except Exception:
            logger.debug("[SUMMARY AI MTF ATTACH] %s tf=%s 取得失敗", label, interval, exc_info=True)
            continue
        if isinstance(df, pd.DataFrame) and not df.empty:
            return df, label
    return None, "empty"


def _latest_completed_rows(
    df: pd.DataFrame, interval: int, now: dt.datetime, *, require_completed: bool
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
    """銘柄ごとに「最新の確定足」を 1 本ずつ返す。

    確定判定: bar_start + interval <= now
    (summary の datetime は足の開始時刻)
    """
    diag: Dict[str, Any] = {"rows": len(df), "dropped_incomplete": 0, "dropped_stale": 0, "dropped_no_dt": 0}
    out: Dict[str, Dict[str, Any]] = {}
    if "symbol" not in df.columns:
        diag["error"] = "no symbol column"
        return out, diag

    max_age = _max_age_sec(interval)
    bar_sec = float(interval) * 60.0

    dt_col = None
    for c in ("datetime", "date_time", "bar_time", "time"):
        if c in df.columns:
            dt_col = c
            break
    if dt_col is None:
        diag["error"] = "no datetime column"
        return out, diag

    best_at: Dict[str, dt.datetime] = {}
    for rec in df.to_dict("records"):
        sym = normalize_symbol(rec.get("symbol"))
        if not sym:
            continue
        ts = _to_naive(rec.get(dt_col))
        if ts is None:
            diag["dropped_no_dt"] += 1
            continue
        age = (now - ts).total_seconds()
        if require_completed and age < bar_sec:
            # まだ確定していない足。採用しない。
            diag["dropped_incomplete"] += 1
            continue
        if age > max_age:
            diag["dropped_stale"] += 1
            continue
        prev = best_at.get(sym)
        if prev is None or ts > prev:
            best_at[sym] = ts
            out[sym] = rec

    diag["symbols"] = len(out)
    diag["max_age_sec"] = max_age
    return out, diag


# ============================================================
# attach
# ============================================================

def attach_mtf_scores(
    df: Any,
    *,
    interval: Any = 1,
    source: str = "SUMMARY",
    now: Optional[dt.datetime] = None,
) -> pd.DataFrame:
    """1 分足候補へ score_3m / score_5m を付ける。

    列を作れなかった銘柄には score_*m_present=False を立てる。
    値を 0 で埋めて「弱い」ことにはしない (欠損は欠損のまま fail-closed)。
    """
    if not _env_bool("SUMMARY_AI_MTF_ATTACH_ENABLED", True):
        return df
    if not isinstance(df, pd.DataFrame) or df.empty:
        return df
    if "symbol" not in df.columns:
        logger.warning("[SUMMARY AI MTF ATTACH] symbol 列が無いので attach しない interval=%s", interval)
        return df

    now = now or dt.datetime.now()
    require_completed = _env_bool("SUMMARY_AI_MTF_REQUIRE_COMPLETED", True)

    out = df.copy()
    sides = out.get("ai_side", out.get("side", pd.Series(["BUY"] * len(out), index=out.index)))
    sides = pd.Series(sides, index=out.index).astype(str).str.upper()
    symbols = out["symbol"].map(normalize_symbol)

    per_symbol_trace = []
    summary: Dict[str, Any] = {}

    for tf in MTF_INTERVALS:
        col = "score_%dm" % tf
        present_col = "%s_present" % col
        dt_col_out = "mtf_%dm_datetime" % tf
        src_col_out = "mtf_%dm_source" % tf

        frame, frame_src = _load_tf_frame(tf)
        if frame is None:
            out[present_col] = False
            out[src_col_out] = frame_src
            summary["%dm" % tf] = {"attached": 0, "source": frame_src, "reason": "frame_unavailable"}
            logger.warning(
                "[SUMMARY AI MTF ATTACH] tf=%s のデータが取れない source=%s "
                "-> score_%dm は付けない (欠損を 0 として通さない)",
                tf, frame_src, tf,
            )
            continue

        rows_by_symbol, diag = _latest_completed_rows(frame, tf, now, require_completed=require_completed)

        values = []
        presents = []
        bar_times = []
        for idx, sym in symbols.items():
            rec = rows_by_symbol.get(sym)
            if rec is None:
                values.append(np.nan)
                presents.append(False)
                bar_times.append(None)
                continue
            score = _pick_score(rec, str(sides.get(idx, "BUY")))
            if score is None:
                values.append(np.nan)
                presents.append(False)
                bar_times.append(None)
                continue
            values.append(float(score))
            presents.append(True)
            bar_times.append(rec.get("datetime"))

        out[col] = values
        out[present_col] = presents
        out[dt_col_out] = bar_times
        out[src_col_out] = frame_src

        attached = int(sum(1 for p in presents if p))
        summary["%dm" % tf] = {
            "attached": attached,
            "of": len(out),
            "source": frame_src,
            **{k: v for k, v in diag.items() if k in ("rows", "symbols", "dropped_incomplete", "dropped_stale", "max_age_sec")},
        }
        log = logger.warning if attached < len(out) else logger.info
        log(
            "[SUMMARY AI MTF ATTACH] tf=%s attached=%s/%s source=%s diag=%s require_completed=%s "
            "(銘柄コードと確定足時刻で突き合わせ。付かなかった行は score_%dm を作らない)",
            tf, attached, len(out), frame_src, diag, require_completed, tf,
        )

    # 候補ごとの診断 (3m_ready / 5m_ready / 採用足 / ソース)
    try:
        for idx in out.index:
            per_symbol_trace.append({
                "symbol": symbols.get(idx),
                "side": str(sides.get(idx, "BUY")),
                "3m_ready": bool(out.get("score_3m_present", pd.Series(False, index=out.index)).get(idx, False)),
                "5m_ready": bool(out.get("score_5m_present", pd.Series(False, index=out.index)).get(idx, False)),
                "score_3m": out.get("score_3m", pd.Series(np.nan, index=out.index)).get(idx),
                "score_5m": out.get("score_5m", pd.Series(np.nan, index=out.index)).get(idx),
                "bar_3m": out.get("mtf_3m_datetime", pd.Series(None, index=out.index)).get(idx),
                "bar_5m": out.get("mtf_5m_datetime", pd.Series(None, index=out.index)).get(idx),
                "src_3m": out.get("mtf_3m_source", pd.Series("", index=out.index)).get(idx),
                "src_5m": out.get("mtf_5m_source", pd.Series("", index=out.index)).get(idx),
            })
        logger.warning(
            "[SUMMARY AI MTF ATTACH][CANDIDATE TRACE] interval=%s source=%s rows=%s summary=%s detail=%s version=%s",
            interval, source, len(out), summary, per_symbol_trace[:20], VERSION,
        )
    except Exception:
        logger.debug("[SUMMARY AI MTF ATTACH] candidate trace failed", exc_info=True)

    return out


__all__ = [
    "VERSION",
    "MTF_INTERVALS",
    "attach_mtf_scores",
    "normalize_symbol",
]
