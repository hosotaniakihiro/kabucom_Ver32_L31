# ============================================================
# backtest16/cost_model.py
# ------------------------------------------------------------
# 売買コスト(§6.3)。手数料/スプレッド/スリッページ/貸株料/逆日歩/
# 一般信用プレミアム料/信用金利を個別に計算できる。
#
#  原則:
#   - 実口座条件が不明なものは config.COST_ASSUMPTIONS の「仮定」を使い、
#     source=ASSUMED を明示。実測が得られたら ACTUAL に差し替える。
#   - 5分保有でも貸株料を一律0とはしない(§6.3): 1日分を日割り計上する。
#   - 逆日歩は銘柄・日次変動で事前確定不可 → 既定0だが known=False を明示。
# ============================================================

from __future__ import annotations

from dataclasses import dataclass

from backtest16 import config as C


@dataclass
class CostResult:
    commission: float = 0.0        # 往復手数料(円)
    spread_cost: float = 0.0       # スプレッド負担(円)
    slippage: float = 0.0          # スリッページ(円)
    lending_fee: float = 0.0       # 貸株料(空売り, 円)
    negative_rate: float = 0.0     # 逆日歩(空売り, 円)
    short_premium: float = 0.0     # 一般信用プレミアム料(空売り, 円)
    margin_interest: float = 0.0   # 信用金利(買い, 円)
    negative_rate_known: bool = True

    @property
    def total(self) -> float:
        return (self.commission + self.spread_cost + self.slippage
                + self.lending_fee + self.negative_rate + self.short_premium
                + self.margin_interest)


def compute_costs(
    *,
    side: str,                 # "LONG" or "SHORT"
    entry_price: float,
    exit_price: float,
    qty: int,
    hold_days: int = 1,        # 日計りでも最低1日(貸株/金利の日割り)
    credit_type: str = "SEIDO",  # "SEIDO"(制度) or "GENERAL"(一般)
    cfg: dict = None,
) -> CostResult:
    cfg = cfg or C.COST_ASSUMPTIONS
    notional_entry = entry_price * qty
    notional_exit = exit_price * qty
    turnover = notional_entry + notional_exit
    r = CostResult()

    # 手数料(往復)。bps と最低額の大きい方。
    comm_bps = cfg.get("commission_bps_per_side", 0.0) / 10000.0
    c_entry = max(notional_entry * comm_bps, cfg.get("commission_min_yen", 0.0))
    c_exit = max(notional_exit * comm_bps, cfg.get("commission_min_yen", 0.0))
    r.commission = c_entry + c_exit

    # スプレッド(往復bpsをターンオーバーに対して)
    r.spread_cost = turnover * (cfg.get("assumed_spread_bps", 0.0) / 10000.0) / 2.0

    # スリッページ(片道bps × 2)
    slip_bps = cfg.get("slippage_bps_per_side", 0.0) / 10000.0
    r.slippage = turnover * slip_bps

    annual_days = max(1, cfg.get("annual_days", 365))
    hd = max(1, hold_days)

    if side == "SHORT":
        # 貸株料(年率% → 日割り)。建玉金額基準。
        lend = cfg.get("stock_lending_fee_annual_pct", 0.0) / 100.0
        r.lending_fee = notional_entry * lend * hd / annual_days
        # 一般信用プレミアム料(年率% → 日割り)。一般信用のみ。
        if credit_type == "GENERAL":
            prem = cfg.get("general_short_premium_annual_pct", 0.0) / 100.0
            r.short_premium = notional_entry * prem * hd / annual_days
        # 逆日歩: 事前確定不可。既定0・known=False。
        nr = cfg.get("negative_rate_yen_per_share_per_day", 0.0)
        r.negative_rate = nr * qty * hd
        r.negative_rate_known = bool(cfg.get("negative_rate_known", False))
    else:  # LONG(信用買い)
        mi = cfg.get("margin_buy_interest_annual_pct", 0.0) / 100.0
        r.margin_interest = notional_entry * mi * hd / annual_days

    return r


def gross_pnl(side: str, entry_price: float, exit_price: float, qty: int) -> float:
    """§2 の損益式。SHORT=(entry-exit)*qty, LONG=(exit-entry)*qty。"""
    if side == "SHORT":
        return (entry_price - exit_price) * qty
    return (exit_price - entry_price) * qty


def net_pnl(side: str, entry_price: float, exit_price: float, qty: int, **kw) -> tuple:
    g = gross_pnl(side, entry_price, exit_price, qty)
    costs = compute_costs(side=side, entry_price=entry_price, exit_price=exit_price, qty=qty, **kw)
    return (g - costs.total, g, costs)
