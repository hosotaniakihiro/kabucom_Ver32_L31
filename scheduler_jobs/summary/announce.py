# ============================================================
# File   : trading/summary/announce.py
# Version: Ver1.0-PRODUCTION-SUMMARY-ANNOUNCE-JA
# ------------------------------------------------------------
# Function:
#   - TOP候補の表示文生成
#   - Discord送信用メッセージ整形
#   - 日本語 setup / 日本語 reasons を優先表示
#   - PUSH / RANKING 両対応
#   - DataFrame / List[dict] 両対応
# ------------------------------------------------------------
# Main APIs:
#   ✔ build_top_candidates_message()
#   ✔ build_top_candidates_lines()
#   ✔ build_candidate_line()
#   ✔ build_interval_header()
#   ✔ announce_top_candidates_to_discord()
# ============================================================

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Sequence

import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# safe helpers
# ============================================================

def _safe_str(v: Any, default: str = "") -> str:
    try:
        if v is None:
            return default
        if pd.isna(v):
            return default
        return str(v)
    except Exception:
        return default


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        if isinstance(v, (int, float)):
            if pd.isna(v):
                return default
            return float(v)
        s = str(v).strip()
        if not s:
            return default
        return float(s)
    except Exception:
        return default


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        return int(_safe_float(v, float(default)))
    except Exception:
        return default


def _to_records(data: Any) -> List[Dict[str, Any]]:
    """
    DataFrame / list[dict] / list[pd.Series] を list[dict] に寄せる
    """
    if data is None:
        return []

    if isinstance(data, pd.DataFrame):
        if data.empty:
            return []
        return data.to_dict(orient="records")

    if isinstance(data, list):
        out: List[Dict[str, Any]] = []
        for x in data:
            if isinstance(x, dict):
                out.append(dict(x))
            elif isinstance(x, pd.Series):
                out.append(x.to_dict())
        return out

    if isinstance(data, tuple):
        out: List[Dict[str, Any]] = []
        for x in data:
            if isinstance(x, dict):
                out.append(dict(x))
            elif isinstance(x, pd.Series):
                out.append(x.to_dict())
            elif isinstance(x, pd.DataFrame):
                out.extend(x.to_dict(orient="records"))
        return out

    return []


# ============================================================
# labels
# ============================================================

SIDE_LABELS_JA = {
    "BUY": "買い",
    "SELL": "売り",
}

SOURCE_LABELS_JA = {
    "push": "PUSH",
    "ranking": "RANKING",
    "merged": "統合",
    "PUSH": "PUSH",
    "RANKING": "RANKING",
}


def _side_label_ja(side: str) -> str:
    s = _safe_str(side).upper()
    return SIDE_LABELS_JA.get(s, s)


def _source_label_ja(source: str) -> str:
    s = _safe_str(source)
    return SOURCE_LABELS_JA.get(s, s)


# ============================================================
# line builders
# ============================================================

def build_interval_header(
    *,
    side: str = "",
    interval: Any = "",
    source: str = "",
    title: str = "",
) -> str:
    side_ja = _side_label_ja(side)
    source_ja = _source_label_ja(source)
    interval_str = _safe_str(interval)
    title_str = _safe_str(title)

    parts: List[str] = []
    if title_str:
        parts.append(title_str)
    if source_ja:
        parts.append(source_ja)
    if interval_str:
        parts.append(f"{interval_str}分")
    if side_ja:
        parts.append(side_ja)

    if not parts:
        return "■ 候補"

    return "■ " + " / ".join(parts)


def build_candidate_line(
    candidate: Dict[str, Any],
    *,
    index: int | None = None,
    include_reason: bool = True,
    include_reason_ja: bool = True,
    include_setup: bool = True,
    include_price: bool = True,
) -> str:
    symbol = _safe_str(candidate.get("symbol"))
    symbolname = _safe_str(candidate.get("symbolname") or candidate.get("name"))
    side = _side_label_ja(candidate.get("side", ""))
    source = _source_label_ja(candidate.get("source", ""))
    interval = _safe_str(candidate.get("interval", ""))

    entry_score_v4 = _safe_float(candidate.get("entry_score_v4"))
    entry_score = _safe_float(candidate.get("entry_score"))
    setup_score = _safe_float(candidate.get("setup_score"))
    final_score = _safe_float(candidate.get("final_score"))
    score_buy = _safe_float(candidate.get("score_buy"))
    score_sell = _safe_float(candidate.get("score_sell"))
    score_mtf = _safe_float(candidate.get("score_mtf"))
    score_slope = _safe_float(candidate.get("score_slope"))
    close = _safe_float(candidate.get("close") or candidate.get("current_price"))

    setup_label = _safe_str(candidate.get("entry_setup_label_ja"))
    if not setup_label:
        setup_label = _safe_str(candidate.get("entry_setup_type"))

    subtype_label = _safe_str(candidate.get("pullback_subtype_label_ja"))
    if not subtype_label:
        subtype_label = _safe_str(candidate.get("pullback_subtype"))

    reasons = ""
    if include_reason:
        if include_reason_ja:
            reasons = _safe_str(candidate.get("score_reason_summary_ja"))
        if not reasons:
            reasons = _safe_str(candidate.get("score_reason_summary"))

    prefix = f"{index:02d}. " if index is not None else ""

    main_parts: List[str] = []
    main_parts.append(f"{prefix}{symbol}")
    if symbolname:
        main_parts.append(symbolname)

    meta_parts: List[str] = []
    if source:
        meta_parts.append(source)
    if interval:
        meta_parts.append(f"{interval}分")
    if side:
        meta_parts.append(side)
    if include_setup and setup_label:
        meta_parts.append(setup_label)
    if subtype_label and subtype_label not in ("", "generic", "一般押し目"):
        meta_parts.append(subtype_label)

    score_parts: List[str] = []
    if entry_score_v4 != 0:
        score_parts.append(f"entry={entry_score_v4:.2f}")
    elif entry_score != 0:
        score_parts.append(f"entry={entry_score:.2f}")

    if setup_score != 0:
        score_parts.append(f"setup={setup_score:.2f}")
    score_parts.append(f"final={final_score:.2f}")
    score_parts.append(f"buy={score_buy:.2f}")
    score_parts.append(f"sell={score_sell:.2f}")
    score_parts.append(f"mtf={score_mtf:.2f}")
    score_parts.append(f"slope={score_slope:.2f}")

    if include_price and close != 0:
        score_parts.append(f"price={close:.1f}")

    line1 = " | ".join(
        [x for x in [" ".join(main_parts), " / ".join(meta_parts), " ".join(score_parts)] if x]
    )

    if reasons:
        line2 = f"   理由: {reasons}"
        return line1 + "\n" + line2

    return line1


def build_top_candidates_lines(
    candidates: Any,
    *,
    title: str = "",
    max_rows: int = 10,
    include_header: bool = True,
    include_reason: bool = True,
    include_reason_ja: bool = True,
) -> List[str]:
    rows = _to_records(candidates)
    if not rows:
        if include_header:
            return [build_interval_header(title=title), "候補なし"]
        return ["候補なし"]

    rows = rows[: max(0, int(max_rows))]

    first = rows[0]
    header = build_interval_header(
        side=first.get("side", ""),
        interval=first.get("interval", ""),
        source=first.get("source", ""),
        title=title,
    )

    lines: List[str] = []
    if include_header:
        lines.append(header)

    for i, row in enumerate(rows, start=1):
        lines.append(
            build_candidate_line(
                row,
                index=i,
                include_reason=include_reason,
                include_reason_ja=include_reason_ja,
                include_setup=True,
                include_price=True,
            )
        )

    return lines


def build_top_candidates_message(
    candidates: Any,
    *,
    title: str = "",
    max_rows: int = 10,
    include_reason: bool = True,
    include_reason_ja: bool = True,
) -> str:
    lines = build_top_candidates_lines(
        candidates,
        title=title,
        max_rows=max_rows,
        include_header=True,
        include_reason=include_reason,
        include_reason_ja=include_reason_ja,
    )
    return "\n".join(lines)


# ============================================================
# Discord
# ============================================================

def _split_message_chunks(text: str, max_len: int = 1800) -> List[str]:
    if not text:
        return []

    lines = text.splitlines()
    chunks: List[str] = []
    current: List[str] = []
    current_len = 0

    for line in lines:
        add_len = len(line) + 1
        if current and current_len + add_len > max_len:
            chunks.append("\n".join(current))
            current = [line]
            current_len = add_len
        else:
            current.append(line)
            current_len += add_len

    if current:
        chunks.append("\n".join(current))

    return chunks


def announce_top_candidates_to_discord(
    discord_sender,
    candidates: Any,
    *,
    title: str = "",
    max_rows: int = 10,
    include_reason: bool = True,
    include_reason_ja: bool = True,
) -> bool:
    """
    discord_sender(text: str) を受け取り、候補一覧を送信する。
    """
    if discord_sender is None:
        logger.warning("[announce] discord_sender is None")
        return False

    message = build_top_candidates_message(
        candidates,
        title=title,
        max_rows=max_rows,
        include_reason=include_reason,
        include_reason_ja=include_reason_ja,
    )

    chunks = _split_message_chunks(message, max_len=1800)
    if not chunks:
        return False

    sent = 0
    for chunk in chunks:
        try:
            discord_sender(chunk)
            sent += 1
        except Exception:
            logger.exception("[announce] discord send failed")
            return False

    logger.info("[announce] discord sent chunks=%d", sent)
    return True