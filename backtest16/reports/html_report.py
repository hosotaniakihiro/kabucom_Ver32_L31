# ============================================================
# backtest16/reports/html_report.py
# ------------------------------------------------------------
# 自己完結HTMLダッシュボード(§12)。外部クラウド/CDN不要・ローカル閲覧専用。
#   - グラフは matplotlib(Aggバックエンド)→PNGをbase64埋め込み。
#   - 16戦略一覧表は素のJSで方向/検索フィルタ。
#   - すべて実測/モデル算出値。推測値は明記。
# ============================================================

from __future__ import annotations

import base64
import io
import json
import os
from typing import List

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

from backtest16 import config as C

_CSS = """
:root{--bg:#ffffff;--fg:#1b1f24;--mut:#667;--card:#f6f8fa;--bd:#d9dee4;--accent:#2563eb;--pos:#128a4c;--neg:#c0392b}
@media (prefers-color-scheme:dark){:root{--bg:#0f1317;--fg:#e6edf3;--mut:#9aa7b3;--card:#171c22;--bd:#2b333b;--accent:#5b9bff;--pos:#3fb950;--neg:#f85149}}
*{box-sizing:border-box}body{margin:0;font-family:-apple-system,"Segoe UI",Meiryo,sans-serif;background:var(--bg);color:var(--fg);line-height:1.5}
.wrap{max-width:1200px;margin:0 auto;padding:20px}
h1{font-size:22px;margin:8px 0}h2{font-size:17px;margin:22px 0 8px;border-bottom:2px solid var(--bd);padding-bottom:4px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin:14px 0}
.kpi{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:12px}
.kpi .l{font-size:12px;color:var(--mut)}.kpi .v{font-size:18px;font-weight:700;margin-top:4px}
.pos{color:var(--pos)}.neg{color:var(--neg)}
table{border-collapse:collapse;width:100%;font-size:13px}th,td{border:1px solid var(--bd);padding:6px 8px;text-align:right}
th{background:var(--card);cursor:pointer;position:sticky;top:0}td.l,th.l{text-align:left}
.scroll{overflow-x:auto;border:1px solid var(--bd);border-radius:8px}
.note{background:var(--card);border-left:4px solid var(--accent);padding:10px 12px;border-radius:6px;margin:10px 0;font-size:13px;color:var(--mut)}
.controls{margin:10px 0}button,input{font:inherit;padding:5px 10px;border:1px solid var(--bd);border-radius:6px;background:var(--card);color:var(--fg)}
img{max-width:100%;height:auto;background:#fff;border:1px solid var(--bd);border-radius:8px}
small{color:var(--mut)}
"""


def _b64(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight")
    plt.close(fig)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _barh(labels, values, title, color="#2563eb"):
    fig, ax = plt.subplots(figsize=(7, max(2.5, 0.32 * len(labels))))
    ax.barh(labels, values, color=color)
    ax.set_title(title, fontsize=11)
    ax.axvline(0, color="#888", lw=0.6)
    ax.tick_params(labelsize=8)
    fig.tight_layout()
    return _b64(fig)


def _cum_chart(trades_csv, title):
    try:
        df = pd.read_csv(trades_csv)
    except Exception:
        return None
    df = df[(df["exec_mode"] == "C_OHLC") & (df["filled"] == True) & df["exit_price"].notna()]
    if df.empty:
        return None
    fig, ax = plt.subplots(figsize=(8, 3))
    for side, col in (("LONG", "#2563eb"), ("SHORT", "#c0392b")):
        g = df[df["side"] == side].copy()
        if g.empty:
            continue
        g["date"] = g["date"].astype(str)
        daily = g.groupby("date")["net_pnl"].sum().sort_index().cumsum()
        ax.plot(range(len(daily)), daily.values, label=side, color=col)
    both = df.copy(); both["date"] = both["date"].astype(str)
    d2 = both.groupby("date")["net_pnl"].sum().sort_index().cumsum()
    ax.plot(range(len(d2)), d2.values, label="ALL", color="#128a4c", lw=2)
    ax.axhline(0, color="#888", lw=0.6); ax.legend(fontsize=8); ax.set_title(title, fontsize=11)
    ax.set_xlabel("trading days (ordered)"); ax.tick_params(labelsize=8); fig.tight_layout()
    return _b64(fig)


def _kpi(label, value, cls=""):
    return f'<div class="kpi"><div class="l">{label}</div><div class="v {cls}">{value}</div></div>'


def _fmt(v):
    if isinstance(v, (int, float)):
        return f"{v:,.0f}" if abs(v) >= 100 else f"{v}"
    return str(v)


def _page(title, body, favicon="📊"):
    return f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>{_CSS}</style></head><body><div class="wrap">{body}</div></body></html>"""


def build_dashboard(summary: dict, out_dir: str):
    rows = summary.get("strategy_summary", [])
    dq = summary.get("data_quality", {})
    port = summary.get("portfolio", {})

    longs = [r for r in rows if r["side"] == "LONG" and r.get("closed")]
    shorts = [r for r in rows if r["side"] == "SHORT" and r.get("closed")]
    def best(lst, key, reverse=True):
        lst2 = [r for r in lst if isinstance(r.get(key), (int, float))]
        return sorted(lst2, key=lambda r: r[key], reverse=reverse)[0] if lst2 else None
    bl = best(longs, "win_rate"); bs = best(shorts, "win_rate")
    be = best(rows, "expectancy")
    bpf = best([r for r in rows if isinstance(r.get("profit_factor"), (int, float))], "profit_factor")
    stable = [r for r in rows if r.get("wf_consistency") == "robust" and isinstance(r.get("expectancy"), (int, float)) and r["expectancy"] > 0]
    stable = sorted(stable, key=lambda r: r["expectancy"], reverse=True)
    most_stable = stable[0] if stable else None

    def pnl_cls(v): return "pos" if (isinstance(v, (int, float)) and v > 0) else ("neg" if isinstance(v, (int, float)) and v < 0 else "")
    kpis = "".join([
        _kpi("買い 最高勝率", f'{bl["id"]} {bl["win_rate"]*100:.1f}%' if bl else "該当なし"),
        _kpi("空売り 最高勝率", f'{bs["id"]} {bs["win_rate"]*100:.1f}%' if bs else "該当なし"),
        _kpi("最高期待値(円/取引)", f'{be["id"]} {be["expectancy"]:,.0f}' if be else "該当なし", pnl_cls(be["expectancy"] if be else 0)),
        _kpi("最高PF", f'{bpf["id"]} {bpf["profit_factor"]}' if bpf else "該当なし"),
        _kpi("最も安定(3期間+)", most_stable["id"] if most_stable else "該当なし(全戦略で未達)"),
        _kpi("買いのみ 純益", _fmt(port.get("long", {}).get("net_profit", 0)), pnl_cls(port.get("long", {}).get("net_profit", 0))),
        _kpi("空売りのみ 純益(仮想)", _fmt(port.get("short", {}).get("net_profit", 0)), pnl_cls(port.get("short", {}).get("net_profit", 0))),
        _kpi("買い+空売り 純益(仮想)", _fmt(port.get("both", {}).get("net_profit", 0)), pnl_cls(port.get("both", {}).get("net_profit", 0))),
    ])

    # 16戦略表(JSフィルタ)
    trows = ""
    for r in sorted(rows, key=lambda x: (x["side"], x["id"])):
        wr = f'{r["win_rate"]*100:.1f}%' if isinstance(r.get("win_rate"), (int, float)) else "-"
        trows += (f'<tr data-side="{r["side"]}"><td class="l">{r["id"]}</td><td>{r["side"]}</td>'
                  f'<td class="l">{r["name"]}</td><td>{wr}</td><td>{_fmt(r.get("expectancy"))}</td>'
                  f'<td>{r.get("profit_factor")}</td><td>{r.get("closed") or 0}</td>'
                  f'<td class="{pnl_cls(r.get("net_profit"))}">{_fmt(r.get("net_profit"))}</td>'
                  f'<td>{r.get("wf_consistency")}</td></tr>')

    # charts
    ids = [r["id"] for r in rows]
    wr_png = _barh(ids, [(r.get("win_rate") or 0) * 100 for r in rows], "Win rate %% (C_OHLC)".replace("%%","%"))
    ex_png = _barh(ids, [r.get("expectancy") or 0 for r in rows], "Expectancy (yen/trade)",
                   color="#128a4c")
    cum_png = _cum_chart(os.path.join(out_dir, "trades.csv"), "Cumulative net PnL (C_OHLC, yen)")

    notes = "".join(f"<li>{n}</li>" for n in dq.get("notes", []))
    body = f"""
<h1>📊 日本株 寄り付き 16戦略 バックテスト (買い6 / 空売り10)</h1>
<small>検証期間 {summary['config']['period'][0]}〜{summary['config']['period'][1]} ／ 利確+{C.TAKE_PROFIT_PCT}% 損切-{C.STOP_LOSS_PCT}% 最大保有{C.MAX_HOLD_SECONDS}s ／ 生成 {summary.get('generated_at')}</small>
<div class="note">約定モードA/B/Cは混ぜず、主たる執行可能モードは <b>C_OHLC(確定1分足)</b>。空売りは当時の売建可否履歴が無いため<b>仮想</b>(区分B/D)。<ul>{notes}</ul></div>
<div class="kpis">{kpis}</div>
<h2>16戦略 一覧</h2>
<div class="controls">
 <button onclick="flt('ALL')">全て</button><button onclick="flt('LONG')">買い</button><button onclick="flt('SHORT')">空売り</button>
 <input id="q" placeholder="検索(ID/名前)" oninput="flt(window._f||'ALL')">
 <a href="strategy_summary.csv" download>CSV</a>
</div>
<div class="scroll"><table id="t">
<thead><tr><th class="l">ID</th><th>方向</th><th class="l">戦略</th><th>勝率</th><th>期待値</th><th>PF</th><th>件数</th><th>純益</th><th>3期間一貫性</th></tr></thead>
<tbody>{trows}</tbody></table></div>
<h2>勝率 / 期待値</h2><img src="{wr_png}"><img src="{ex_png}">
<h2>累積純益</h2>{('<img src="'+cum_png+'">') if cum_png else '<p>データ不足</p>'}
<h2>ポートフォリオ(最大3建玉・1銘柄70万)</h2>
<div class="scroll"><table><thead><tr><th>構成</th><th>採用</th><th>見送り</th><th>勝率</th><th>純益</th></tr></thead><tbody>
{''.join(f'<tr><td class="l">{m}</td><td>{port[m]["taken"]}</td><td>{port[m]["missed"]}</td><td>{port[m]["win_rate"]*100:.1f}%</td><td class="{pnl_cls(port[m]["net_profit"])}">{_fmt(port[m]["net_profit"])}</td></tr>' for m in ("long","short","both") if m in port)}
</tbody></table></div>
<p><small>関連: <a href="data_quality.html">データ品質</a> ・ <a href="long_vs_short.html">買いVS空売り</a> ・ <a href="first_5min_analysis.html">初動5分足分析</a> ・ <a href="final_report.md">最終レポート(md)</a></small></p>
<script>
function flt(side){{window._f=side;var q=(document.getElementById('q').value||'').toLowerCase();
document.querySelectorAll('#t tbody tr').forEach(function(tr){{
var okS=(side==='ALL'||tr.dataset.side===side);
var okQ=(!q||tr.textContent.toLowerCase().indexOf(q)>=0);tr.style.display=(okS&&okQ)?'':'none';}});}}
</script>
"""
    with open(os.path.join(out_dir, "backtest_16strategies.html"), "w", encoding="utf-8") as f:
        f.write(_page("16戦略バックテスト", body))


def build_first5(summary: dict, out_dir: str):
    f5 = summary.get("first5_analysis", {})
    groups = f5.get("groups", {})
    rows = ""
    for k, v in sorted(groups.items()):
        def pct(x): return f"{x*100:.1f}%" if isinstance(x, (int, float)) else "-"
        rows += (f'<tr><td class="l">{k}</td><td>{v["n"]}</td><td>{pct(v.get("high_first_rate"))}</td>'
                 f'<td>{pct(v.get("low_first_rate"))}</td><td>{pct(v.get("up_0.3_first_rate"))}</td>'
                 f'<td>{pct(v.get("down_0.3_first_rate"))}</td><td>{v.get("avg_max_up_pct")}</td>'
                 f'<td>{v.get("avg_max_down_pct")}</td><td>{pct(v.get("tick_source_rate"))}</td></tr>')
    body = f"""<h1>🔎 初動5分足の方向性分析 (§13)</h1>
<small>全{f5.get('n',0)}銘柄日。寄り付きギャップ×最初の5分足(陽線/陰線)で、その後の値動き確率がどう変わるか。</small>
<div class="note">ティックが無い区間は1分足で近似し、同一足で高安を両方跨ぐ先後関係は「不明」として除外。tick_source列は先後判定にティックを使えた割合。</div>
<div class="scroll"><table><thead><tr><th class="l">ギャップ / 初動5分足</th><th>件数</th><th>先に高値更新</th><th>先に安値更新</th><th>先に+0.3%</th><th>先に-0.3%</th><th>平均最大上昇%</th><th>平均最大下落%</th><th>tick判定率</th></tr></thead>
<tbody>{rows}</tbody></table></div>
<p><small><a href="backtest_16strategies.html">← ダッシュボード</a></small></p>"""
    with open(os.path.join(out_dir, "first_5min_analysis.html"), "w", encoding="utf-8") as f:
        f.write(_page("初動5分足分析", body, "🔎"))


def build_long_short(summary: dict, out_dir: str):
    ls = summary.get("long_vs_short", {})
    def tbl(d, label):
        if not isinstance(d, dict) or not d:
            return "<p>データなし</p>"
        r = ""
        for k, v in d.items():
            L, S = v.get("LONG", {}), v.get("SHORT", {})
            def cell(x):
                wr = x.get("win_rate");
                return f'{(wr*100):.1f}% / {x.get("expectancy")}円 (n={x.get("n")})' if wr is not None else "-"
            r += f'<tr><td class="l">{k}</td><td>{cell(L)}</td><td>{cell(S)}</td></tr>'
        return f'<div class="scroll"><table><thead><tr><th class="l">{label}</th><th>買い(勝率/期待値)</th><th>空売り(勝率/期待値)</th></tr></thead><tbody>{r}</tbody></table></div>'
    body = f"""<h1>⚖️ 買い VS 空売り (§9C)</h1>
<small>C_OHLC・標準条件。空売りは仮想(売建可否履歴なし)。</small>
<h2>ギャップ別</h2>{tbl(ls.get('by_gap_bucket',{}),'ギャップ')}
<h2>初動5分足の陽線/陰線別</h2>{tbl(ls.get('by_first5_dir',{}),'初動5分足')}
<h2>エントリー時間帯別</h2>{tbl(ls.get('by_entry_window',{}),'時間帯')}
<h2>VWAP上/下別</h2>{tbl(ls.get('by_vwap_side',{}),'VWAP位置')}
<p><small><a href="backtest_16strategies.html">← ダッシュボード</a></small></p>"""
    with open(os.path.join(out_dir, "long_vs_short.html"), "w", encoding="utf-8") as f:
        f.write(_page("買いVS空売り", body, "⚖️"))


def build_data_quality(summary: dict, out_dir: str):
    dq = summary.get("data_quality", {})
    items = "".join(f"<tr><td class='l'>{k}</td><td>{_fmt(v) if not isinstance(v,(list,dict)) else ''}</td></tr>"
                    for k, v in dq.items() if k != "notes")
    notes = "".join(f"<li>{n}</li>" for n in dq.get("notes", []))
    bc = summary.get("borrow_class_counts", {})
    body = f"""<h1>🧪 データ品質と約定再現性 (§3/§7)</h1>
<div class="scroll"><table><tbody>{items}</tbody></table></div>
<h2>信用売建可否 区分(§6.1)</h2>
<div class="note">区分A(当時の売建可否・数量を確認済)は履歴データが無いため<b>0件</b>。空売り実損益は仮想扱い。</div>
<div class="scroll"><table><thead><tr><th>区分</th><th>件数</th></tr></thead><tbody>
{''.join(f'<tr><td class="l">{k}: {C.BORROW_CLASS.get(k,"")}</td><td>{v}</td></tr>' for k,v in bc.items())}</tbody></table></div>
<h2>注記</h2><ul>{notes}</ul>
<p><small><a href="backtest_16strategies.html">← ダッシュボード</a></small></p>"""
    with open(os.path.join(out_dir, "data_quality.html"), "w", encoding="utf-8") as f:
        f.write(_page("データ品質", body, "🧪"))


def build_all(summary: dict, out_dir: str = None):
    out_dir = out_dir or C.REPORTS_DIR
    os.makedirs(out_dir, exist_ok=True)
    build_dashboard(summary, out_dir)
    build_first5(summary, out_dir)
    build_long_short(summary, out_dir)
    build_data_quality(summary, out_dir)
    return out_dir
