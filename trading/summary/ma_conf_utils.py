# ============================================================
# File   : trading/summary/ma_conf_utils.py
# Ver    : 1.0.1-FINAL-MA-CONF-UTILS-STABLE
# ------------------------------------------------------------
# ✔ MA5 / MA25 / MA75 の conf 共通ユーティリティ
# ✔ 時間帯別 conf 閾値（寄り直後対策）
# ✔ 銘柄タイプ別補正（大型 / 小型）
# ✔ ENTRY_GATE / summary_loader 両対応
# ✔ DB・cache には一切副作用なし（READ ONLY）
# ✔ naive / aware datetime 両対応（JST 前提）
# ============================================================

from __future__ import annotations

import datetime as dt
from typing import Literal


# ============================================================
# 型定義
# ============================================================

SymbolType = Literal["large", "normal", "small"]
MaType = Literal["ma5", "ma25", "ma75"]


# ============================================================
# デフォルト conf 閾値（時間帯外・保険）
# ============================================================

DEFAULT_MA_CONF_MIN: dict[MaType, float] = {
    "ma5":  0.30,
    "ma25": 0.35,
    "ma75": 0.40,
}


# ============================================================
# 時間帯別ルール（JST 前提）
# ============================================================

TIMEZONE_RULES: list[dict[str, float | dt.time]] = [
    # --------------------------------------------------------
    # 寄り直後（最も事故りやすい）
    # --------------------------------------------------------
    {
        "start": dt.time(9, 0),
        "end":   dt.time(9, 30),
        "ma75":  0.65,
        "ma25":  0.55,
        "ma5":   0.45,
    },
    # --------------------------------------------------------
    # 前場中盤
    # --------------------------------------------------------
    {
        "start": dt.time(9, 30),
        "end":   dt.time(11, 30),
        "ma75":  0.50,
        "ma25":  0.45,
        "ma5":   0.35,
    },
    # --------------------------------------------------------
    # 後場
    # --------------------------------------------------------
    {
        "start": dt.time(12, 30),
        "end":   dt.time(15, 30),
        "ma75":  0.35,
        "ma25":  0.30,
        "ma5":   0.25,
    },
]


# ============================================================
# 銘柄タイプ補正
# ============================================================

SYMBOL_TYPE_ADJUST: dict[SymbolType, float] = {
    "large":  -0.05,   # 大型株 → 緩める
    "normal":  0.00,
    "small":  +0.05,   # 小型株 → 厳しく
}


# ============================================================
# 内部：datetime 正規化（naive / aware 両対応）
# ============================================================

def _normalize_now(now: dt.datetime) -> dt.datetime:
    """
    tz-aware / naive を問わず time() を安全に使えるようにする
    """
    if not isinstance(now, dt.datetime):
        raise TypeError(f"now must be datetime, got {type(now)}")
    return now


# ============================================================
# 内部：時間帯ルール解決
# ============================================================

def _resolve_timezone_rule(now: dt.datetime) -> dict[str, float]:
    """
    現在時刻から該当する時間帯ルールを返す
    該当なしの場合は空 dict
    """
    now = _normalize_now(now)
    t = now.time()

    for rule in TIMEZONE_RULES:
        if rule["start"] <= t < rule["end"]:
            return rule

    return {}


# ============================================================
# 公開API：MA conf 閾値取得
# ============================================================

def get_ma_conf_threshold(
    *,
    ma: MaType,
    now: dt.datetime,
    symbol_type: SymbolType = "normal",
) -> float:
    """
    MA 種別・現在時刻・銘柄タイプから conf 閾値を返す

    Notes
    -----
    - DB / cache には一切触らない
    - 常に 0.0 ～ 1.0 に clamp
    """

    # --- base（保険） ---
    conf_min = DEFAULT_MA_CONF_MIN.get(ma, 0.4)

    # --- 時間帯 ---
    rule = _resolve_timezone_rule(now)
    if ma in rule:
        conf_min = float(rule[ma])

    # --- 銘柄タイプ補正 ---
    conf_min += SYMBOL_TYPE_ADJUST.get(symbol_type, 0.0)

    # --- safety clamp ---
    return max(0.0, min(conf_min, 1.0))


# ============================================================
# 公開API：conf 判定（ENTRY_GATE / loader 用）
# ============================================================

def is_ma_conf_sufficient(
    *,
    ma: MaType,
    conf: float | None,
    now: dt.datetime,
    symbol_type: SymbolType = "normal",
) -> tuple[bool, float]:
    """
    MA conf が十分かどうかを判定する

    Returns
    -------
    (ok, threshold)
        ok        : conf >= threshold
        threshold : 使用された閾値
    """

    threshold = get_ma_conf_threshold(
        ma=ma,
        now=now,
        symbol_type=symbol_type,
    )

    if conf is None:
        return False, threshold

    try:
        conf_val = float(conf)
    except Exception:
        return False, threshold

    return conf_val >= threshold, threshold
