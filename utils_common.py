# ============================================================
# utils_common.py
# Ver23.1-FINAL-LTS-REV10-FORMATFLOAT-FIXED
# ------------------------------------------------------------
# ✔ normalize_symbol
# ✔ safe_float / to_float（NaN 完全耐性）
# ✔ format_float（表示用・NEW）
# ✔ resolve_entry_price（ENTRY価格決定を一本化）
# ✔ get_latest_bid_ask（PUSH限定・API不使用）
# ✔ calculate_shares（500,000円基準・最低1単元）
# ✔ get_trading_unit（100株固定）
# ✔ realtime_liquidity_filter
# ✔ calculate_qty_by_budget
# ✔ get_tick_size
# ✔ range_volatility_filter（段階フォールバック）
# ------------------------------------------------------------
# ⚠ 設計方針
# - 「取得・計算・判定」まで
# - ENTRY / EXIT の意思決定は持たない
# - format_float はログ・表示専用（判断非関与）
# ============================================================

import logging
import math
import pandas as pd

from global_state import global_data

logger = logging.getLogger(__name__)

# ============================================================
# 🔵 symbol 正規化
# ============================================================

def normalize_symbol(sym):
    if sym is None:
        return ""
    s = str(sym).strip()
    if s.isdigit():
        return str(int(s))
    return s


# ============================================================
# 🔵 数値変換（NaN / None 完全耐性）
# ============================================================

def to_float(x, default=None):
    try:
        v = float(x)
        return v if not pd.isna(v) else default
    except Exception:
        return default


def safe_float(x, default=None):
    try:
        if x is None:
            return default
        v = float(x)
        if math.isnan(v):
            return default
        return v
    except Exception:
        return default


# ============================================================
# 🔵 表示専用 float フォーマット（★ 追加）
# ============================================================

def format_float(
    value,
    *,
    ndigits: int = 2,
    none_str: str = "-"
):
    """
    float を安全に文字列化（ログ・表示専用）
    - None / NaN → none_str
    - 数値 → 小数 ndigits 桁
    - 文字列 → そのまま返す
    """
    try:
        if value is None:
            return none_str

        if isinstance(value, str):
            return value

        v = float(value)
        if math.isnan(v):
            return none_str

        return f"{v:.{ndigits}f}"
    except Exception:
        return none_str


# ============================================================
# 🔵 ENTRY用 価格決定（一本化）
# ============================================================

def resolve_entry_price(
    *,
    symbol: str,
    source: str | None = None,
    summary_row: dict | pd.Series | None = None,
):
    symbol = normalize_symbol(symbol)

    # --- ① SUMMARY ---
    if summary_row is not None:
        for k in ("close_price", "close", "price"):
            try:
                v = safe_float(summary_row.get(k))
                if v and v > 0:
                    return v, f"summary:{k}"
            except Exception:
                pass

    # --- ② PUSH 板 ---
    ba = get_latest_bid_ask(symbol)
    if ba:
        v = safe_float(ba.get("ask_price"))
        if v and v > 0:
            return v, "push:ask"

    # --- ③ 最新 tick ---
    try:
        tick = global_data.get_latest_tick(symbol)
        if tick:
            v = safe_float(tick.get("price"))
            if v and v > 0:
                return v, "tick:price"
    except Exception:
        pass

    logger.error(
        "[ENTRY_PRICE] NOT_FOUND symbol=%s source=%s",
        symbol,
        source,
    )
    return None, "price_not_found"


# ============================================================
# 🔵 PUSH限定・板情報取得
# ============================================================

def _get_latest_bid_ask_core(symbol: str):

    symbol = normalize_symbol(symbol)

    try:
        dfp = global_data.get_push_df()
        if dfp is None or dfp.empty:
            return None

        rename_map = {
            "BidPrice": "bid_price",
            "AskPrice": "ask_price",
            "BidQty": "bid_qty",
            "AskQty": "ask_qty",
            "CurrentPriceTime": "datetime",
            "time": "datetime",
        }

        df = dfp.copy()
        for old, new in rename_map.items():
            if old in df.columns and new not in df.columns:
                df[new] = df[old]

        if "symbol" not in df.columns:
            return None

        df_sym = df[df["symbol"] == symbol]
        if df_sym.empty:
            return None

        if "datetime" in df_sym.columns:
            df_sym = df_sym.copy()
            df_sym["datetime"] = pd.to_datetime(df_sym["datetime"], errors="coerce")
            df_sym = df_sym.dropna(subset=["datetime"])
            if df_sym.empty:
                return None
            row = df_sym.sort_values("datetime").iloc[-1]
        else:
            row = df_sym.iloc[-1]

        ask = safe_float(row.get("ask_price"))
        bid = safe_float(row.get("bid_price"))

        if ask is None or bid is None or ask <= 0 or bid <= 0:
            return None

        ask_qty = int(safe_float(row.get("ask_qty"), 0))
        bid_qty = int(safe_float(row.get("bid_qty"), 0))
        # 2026-10-01: kabu の BidPrice は売気配・AskPrice は買気配 (名前が逆)。
        #   push_df は BidPrice -> bid_price と素直に写しているので、ここで向きを直す。
        #   以前は spread_pct が常に負で、スプレッドガードが一度も効いていなかった。
        if bid > ask:
            bid, ask = ask, bid
            bid_qty, ask_qty = ask_qty, bid_qty

        return {
            "symbol": symbol,
            "ask_price": ask,
            "bid_price": bid,
            "ask_qty": ask_qty,
            "bid_qty": bid_qty,
            "source": "push_df",
        }

    except Exception:
        logger.error("❌ get_latest_bid_ask error", exc_info=True)
        return None


# ------------------------------------------------------------
# BOARD RETRY (旧 core/startup/board_retry_patch.py の本文化)
# ------------------------------------------------------------
# 旧 install() が
#   utils_common.get_latest_bid_ask = _wrap_get_latest_bid_ask(native)
# としていた 1 枚を、ここで native に固定した。
# リトライ本体・REST フォールバック・cooldown は kabu_api/board_retry.py。
#
# board_retry を import できない環境 (単体利用など) では素の PUSH 実装に
# フォールバックする。旧構成でも install 失敗時は native のままだった。
# ------------------------------------------------------------

def get_latest_bid_ask(symbol: str, *args, **kwargs):
    try:
        from kabu_api.board_retry import retry_fetch_board
    except Exception:
        return _get_latest_bid_ask_core(symbol)
    return retry_fetch_board(_get_latest_bid_ask_core, symbol, *args, **kwargs)


# ============================================================
# 🔵 株数計算（500,000円基準・最低1単元保証）
# ============================================================

def calculate_shares(
    price: float,
    lot_yen: int = 500_000,
    unit: int = 100,
    *,
    symbol: str | None = None,
    source: str | None = None,
):
    price = safe_float(price)
    if price is None or price <= 0:
        logger.error(
            "[QTY_CALC] INVALID_PRICE symbol=%s price=%s source=%s",
            symbol,
            price,
            source,
        )
        return 0

    if unit <= 0:
        logger.error(
            "[QTY_CALC] INVALID_UNIT symbol=%s unit=%s",
            symbol,
            unit,
        )
        return 0

    try:
        qty = int(lot_yen // (price * unit)) * unit
    except Exception:
        logger.exception(
            "[QTY_CALC] EXCEPTION symbol=%s price=%s unit=%s",
            symbol,
            price,
            unit,
        )
        return 0

    if qty < unit:
        logger.warning(
            "[QTY_CALC] MIN_UNIT_FALLBACK symbol=%s price=%.1f lot_yen=%s source=%s",
            symbol,
            price,
            lot_yen,
            source,
        )
        return unit

    return qty


# ============================================================
# 🔵 単元株数（API OFF 固定）
# ============================================================

def get_trading_unit(symbol: str):
    return 100


# ============================================================
# 🔵 リアルタイム流動性フィルター
# ============================================================

def realtime_liquidity_filter(symbol, df_push, df_1m, df_5m):

    d = df_push[df_push["symbol"] == symbol]
    if d.empty:
        return True, "PUSHなし"

    last = d.iloc[-1]

    bid = safe_float(last.get("bid_price"), 0)
    ask = safe_float(last.get("ask_price"), 0)
    if bid <= 0 or ask <= 0:
        return True, "板なし"

    spread = ask - bid
    spread_pct = spread / bid * 100
    if spread_pct > 0.4 or spread >= 3:
        return True, f"スプレッド過大({spread_pct:.2f}%)"

    if safe_float(last.get("bid_qty"), 0) < 200 or safe_float(last.get("ask_qty"), 0) < 200:
        return True, "板薄い"

    return False, ""


# ============================================================
# 🔵 資金固定型 株数計算
# ============================================================

def calculate_qty_by_budget(
    price: float,
    budget: int = 500_000,
    lot: int = 100,
) -> int:
    price = safe_float(price)
    if price is None or price <= 0:
        return 0
    raw_qty = budget // price
    return int((raw_qty // lot) * lot)


# ============================================================
# 🔵 Tick Size
# ============================================================

def get_order_tick_size(price) -> float:
    """発注価格を丸めるための東証の呼値 (TOPIX100 以外の通常銘柄の表)。

    2026-10-01: get_tick_size は 1,000〜3,000 円を 5 円刻みにしている (旧制度)。
    これで売り指値を切り上げると 5706 は 気配 2371 -> 2375 になり、
    買い気配より 4 円上の指値で約定せず 1 秒で取消されていた (売り新規 14 回中 0 約定)。
    TOPIX100 銘柄はもっと細かいが、この表の刻みは TOPIX100 でも有効な値段になる。
    """
    try:
        p = float(price)
    except Exception:
        return 1.0
    if p <= 0:
        return 1.0
    for upper, tick in (
        (3000, 1.0), (5000, 5.0), (30000, 10.0), (50000, 50.0),
        (300000, 100.0), (500000, 500.0), (3000000, 1000.0),
        (5000000, 5000.0), (30000000, 10000.0), (50000000, 50000.0),
    ):
        if p <= upper:
            return tick
    return 100000.0


def get_tick_size(price_or_symbol):
    try:
        if isinstance(price_or_symbol, str):
            latest = global_data.get_latest_tick(price_or_symbol)
            price = safe_float(latest.get("price")) if latest else None
        else:
            price = safe_float(price_or_symbol)

        if price is None or price <= 0:
            return 1

        if price < 1000:
            return 1
        elif price < 5000:
            return 5
        elif price < 30000:
            return 10
        elif price < 50000:
            return 50
        else:
            return 100
    except Exception:
        return 1


# ============================================================
# 🔥 RANGE ボラティリティ判定（段階フォールバック）
# ============================================================

def range_volatility_filter(symbol, df_1m=None, df_5m=None):
    """
    ボラティリティ段階判定
    - 5m → 最優先
    - 1m → フォールバック
    - 未生成 → NG
    """

    symbol = normalize_symbol(symbol)

    if isinstance(df_5m, pd.DataFrame) and not df_5m.empty:
        d5 = df_5m[df_5m["symbol"] == symbol]
        if not d5.empty:
            high = safe_float(d5.iloc[-1].get("high_price"), 0)
            low = safe_float(d5.iloc[-1].get("low_price"), 0)
            rng = high - low
            if rng < max(5, high * 0.003):
                return False, {
                    "timeframe": "5m",
                    "detail": "5mボラ不足",
                    "range": rng,
                }
            return True, {
                "timeframe": "5m",
                "detail": "5m OK",
                "range": rng,
            }

    if isinstance(df_1m, pd.DataFrame) and not df_1m.empty:
        d1 = df_1m[df_1m["symbol"] == symbol]
        if not d1.empty:
            high = safe_float(d1.iloc[-1].get("high_price"), 0)
            low = safe_float(d1.iloc[-1].get("low_price"), 0)
            rng = high - low
            if rng < max(2, high * 0.0015):
                return False, {
                    "timeframe": "1m",
                    "detail": "1mボラ不足",
                    "range": rng,
                }
            return True, {
                "timeframe": "1m",
                "detail": "1m OK (fallback)",
                "range": rng,
            }

    return False, {
        "timeframe": "NONE",
        "detail": "1m/5m未生成",
    }