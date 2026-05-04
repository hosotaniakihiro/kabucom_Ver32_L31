# ============================================================
# pj/trading/entry/ignition/five_sec.py
# Ver6.3-FINAL
# ------------------------------------------------------------
# 5秒足の短期火力指標（TONOSAMA完全対応）
# ・PUSH 5秒足があれば最優先
# ・なければ summary 1min から疑似 fast_ret を生成
# ・それも無ければ ranking snapshot から生成
# ・単位はすべて [%]
# ============================================================

from global_state import global_data


# ============================================================
# 疑似 5秒リターン（summary 1min fallback）
# ============================================================
def fast_ret_from_summary_1m(symbol: str) -> float:
    """
    summary 1min から疑似 fast_ret を生成
    ・前足 close → 現在 close の変化率
    ・必ず float を返す
    ・単位 [%]
    """

    df = getattr(global_data, "summary_cache", {}).get("1min")
    if df is None or df.empty:
        return 0.0

    d = df[df["symbol"] == symbol]
    if len(d) < 2:
        return 0.0

    prev = d.iloc[-2]
    last = d.iloc[-1]

    ref = prev.get("close_price")
    now = last.get("close_price")

    if not ref or ref <= 0 or not now:
        return 0.0

    return (now - ref) / ref * 100.0


# ============================================================
# ranking snapshot 由来 fast_ret（最終 fallback）
# ============================================================
_last_rank_price = {}


def fast_ret_from_ranking(symbol: str, price_now: float) -> float:
    """
    ranking snapshot から疑似 fast_ret を生成
    ・前回ランキング価格 → 今回価格
    ・単位 [%]
    """

    if not price_now or price_now <= 0:
        return 0.0

    prev = _last_rank_price.get(symbol)
    _last_rank_price[symbol] = price_now

    if prev is None or prev <= 0:
        return 0.0

    return (price_now - prev) / prev * 100.0


# ============================================================
# fast_ret 統合計算（ENTRY / EXIT 共通）
# ============================================================
def calc_fast_ret(symbol: str, price_now: float | None) -> float:
    """
    fast_ret を統合算出する

    優先順位：
      1) summary 1min（有効な動きがある場合）
      2) ranking snapshot
    """

    # --- summary 1min ---
    r = fast_ret_from_summary_1m(symbol)

    # ★ ノイズ除去（0.01% 未満は「動いていない」扱い）
    if abs(r) >= 0.01:
        return r

    # --- ranking snapshot ---
    return fast_ret_from_ranking(symbol, price_now)


# ============================================================
# 5秒足 火力解析（PUSH がある場合）
# ============================================================
def analyze_five_sec(symbol: str):
    """
    5秒足の瞬間的な強さを評価する。

    return:
        {
          "fast_ret": 上昇率 [%],
          "break"   : 高値ブレイク判定,
          "source"  : push_5s / summary_1m / ranking
        }
    """

    # --------------------------------------------------------
    # PUSH 由来 5秒足（最優先）
    # --------------------------------------------------------
    bar = getattr(global_data, "latest_5s_bar", {}).get(symbol)

    if bar:
        o = bar.get("open")
        c = bar.get("close")

        if not o or not c:
            fast_ret = 0.0
        else:
            fast_ret = (c - o) / o * 100.0

        # --- 高値ブレイク判定（直近6本） ---
        hist = getattr(global_data, "latest_5s_history", {}).get(symbol, [])
        if len(hist) >= 6:
            try:
                past_high = max(b.get("high", 0) for b in hist[-6:])
                is_break = c > past_high
            except Exception:
                is_break = False
        else:
            is_break = False

        return {
            "fast_ret": fast_ret,
            "break": is_break,
            "source": "push_5s",
        }

    # --------------------------------------------------------
    # fallback：summary / ranking
    # --------------------------------------------------------
    price_now = float(
        getattr(global_data, "latest_price", {}).get(symbol, 0.0) or 0.0
    )

    fast_ret = calc_fast_ret(symbol, price_now)

    return {
        "fast_ret": fast_ret,
        "break": False,
        "source": "summary_or_ranking",
    }
