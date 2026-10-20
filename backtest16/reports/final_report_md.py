# ============================================================
# backtest16/reports/final_report_md.py
# ------------------------------------------------------------
# §19 最終報告(日本語, final_report.md)を summary.json から生成。
# §20 判定基準に基づく GO / CONDITIONAL GO / NO-GO / INSUFFICIENT DATA。
# 根拠のないGOは出さない(§9⑨)。空売りは売建可否履歴が無いため最大でCONDITIONAL。
# ============================================================

from __future__ import annotations

import os
from typing import Optional

MIN_SAMPLES = 30


def judge(row: dict, detail: dict) -> tuple:
    """(判定, 理由)。"""
    closed = row.get("closed") or 0
    exp = row.get("expectancy")
    pf = row.get("profit_factor")
    side = row.get("side")
    wf = row.get("wf_consistency")
    if closed < MIN_SAMPLES:
        return ("INSUFFICIENT DATA", f"決済{closed}件(<{MIN_SAMPLES})でサンプル不足")
    if not isinstance(exp, (int, float)) or exp <= 0:
        return ("NO-GO", f"ネット期待値が非正({exp})。コスト差引後に優位性なし")
    pf_ok = isinstance(pf, (int, float)) and pf >= 1.2
    conc = (detail.get("C_OHLC", {}) or {}).get("concentration_warning")
    if side == "SHORT":
        # 売建可否(区分A)が履歴から確認できない=実行可能性未確認 → 最大でCONDITIONAL
        base = "空売りは当時の売建可否・在庫・価格規制が履歴から確認できず実行可能性未確認"
        if exp > 0 and pf_ok and wf == "robust" and not conc:
            return ("CONDITIONAL GO", base + "。統計は良好だが要実売建確認")
        return ("CONDITIONAL GO" if exp > 0 else "NO-GO", base)
    # LONG
    if exp > 0 and pf_ok and wf == "robust" and not conc:
        return ("GO", f"期待値+{exp:.0f}/PF{pf}/3期間一貫。バックテスト上の採用候補")
    reasons = []
    if not pf_ok: reasons.append(f"PF={pf}(<1.2)")
    if wf != "robust": reasons.append(f"3期間一貫性={wf}")
    if conc: reasons.append(conc)
    return ("CONDITIONAL GO", "期待値は正だが " + " / ".join(reasons) if reasons else "期待値は正")


def _bests(rows):
    def best(lst, key, rev=True):
        lst2 = [r for r in lst if isinstance(r.get(key), (int, float)) and (r.get("closed") or 0) >= MIN_SAMPLES]
        return sorted(lst2, key=lambda r: r[key], reverse=rev)[0] if lst2 else None
    longs = [r for r in rows if r["side"] == "LONG"]
    shorts = [r for r in rows if r["side"] == "SHORT"]
    return {
        "long_wr": best(longs, "win_rate"), "short_wr": best(shorts, "win_rate"),
        "exp": best(rows, "expectancy"), "pf": best(rows, "profit_factor"),
    }


def _gap_tbl(ls: dict) -> str:
    gb = ls.get("by_gap_bucket", {})
    out = "| ギャップ | 買い(勝率/期待値/件数) | 空売り(勝率/期待値/件数) |\n|---|---|---|\n"
    for k in ["GU>=7", "GU5-7", "GU3-5", "FLAT(-3,3)", "GD3-5", "GD5-7", "GD<=-7"]:
        if k not in gb:
            continue
        L, S = gb[k].get("LONG", {}), gb[k].get("SHORT", {})
        def c(x):
            wr = x.get("win_rate")
            return f'{wr*100:.1f}%/{x.get("expectancy")}円/{x.get("n")}' if wr is not None else "-"
        out += f"| {k} | {c(L)} | {c(S)} |\n"
    return out


def build(summary: dict, out_dir: str, meta: Optional[dict] = None) -> str:
    meta = meta or {}
    rows = summary.get("strategy_summary", [])
    detail = summary.get("strategies", {})
    dq = summary.get("data_quality", {})
    port = summary.get("portfolio", {})
    ls = summary.get("long_vs_short", {})
    f5 = summary.get("first5_analysis", {})
    cfg = summary.get("config", {})
    b = _bests(rows)

    def line(r):
        wr = f'{r["win_rate"]*100:.1f}%' if isinstance(r.get("win_rate"), (int, float)) else "-"
        return f'| {r["id"]} | {r["side"]} | {r["name"]} | {wr} | {r.get("expectancy")} | {r.get("profit_factor")} | {r.get("closed") or 0} | {r.get("net_profit")} |'

    # 判定
    judge_rows = []
    counts = {"GO": 0, "CONDITIONAL GO": 0, "NO-GO": 0, "INSUFFICIENT DATA": 0}
    for r in rows:
        v, why = judge(r, detail.get(r["id"], {}))
        counts[v] += 1
        judge_rows.append(f'| {r["id"]} | {r["side"]} | **{v}** | {why} |')

    def bstr(x, metric="win_rate"):
        if not x:
            return "該当なし(サンプル不足)"
        val = x.get(metric)
        val = f'{val*100:.1f}%' if metric == "win_rate" and isinstance(val, (int, float)) else val
        return f'{x["id"]} ({x["name"]}) {val}'

    # 時間帯
    ew = ls.get("by_entry_window", {})
    def ewbest():
        best = None
        for k, v in ew.items():
            for side in ("LONG", "SHORT"):
                e = v.get(side, {}).get("expectancy")
                if isinstance(e, (int, float)) and (best is None or e > best[2]):
                    best = (k, side, e)
        return best

    md = f"""# 日本株 寄り付き 16戦略 バックテスト 最終レポート

- 検証期間: {cfg.get('period',['',''])[0]} 〜 {cfg.get('period',['',''])[1]}（基準日 2026-10-20）
- 設定: 利確 +{cfg.get('take_profit_pct')}% / 損切 -{cfg.get('stop_loss_pct')}% / 最大保有 {cfg.get('max_hold_seconds')}秒 / 1銘柄 {cfg.get('per_symbol_notional'):,}円 / 最大同時 {cfg.get('max_concurrent')}建玉
- 売買コスト: {cfg.get('cost',{}).get('source')}（仮定値。逆日歩は銘柄・日次変動で不明）
- 生成: {summary.get('generated_at')} / commit {meta.get('end_commit','(未commit)')} / branch {meta.get('branch','')}
- データ: {dq.get('days_processed')}営業日処理（{dq.get('date_min')}〜{dq.get('date_max')}）、総シグナル {dq.get('total_signals'):,}

> ⚠️ 本結果はバックテスト（過去データ・モデル約定）であり、実資金の自動発注を許可するものではない。
> 約定モードA/B/Cは混ぜず主たる執行可能モードは **C_OHLC（確定1分足）**。空売りは当時の売建可否履歴が無いため **仮想**（区分A=0件）。

---

## ① 総合結果
- 買いで最高勝率: {bstr(b['long_wr'])}
- 空売りで最高勝率: {bstr(b['short_wr'])}
- 全16戦略で最高期待値: {bstr(b['exp'],'expectancy')}
- 最高PF: {bstr(b['pf'],'profit_factor')}
- 最も安定: {next((r['id'] for r in rows if r.get('wf_consistency')=='robust' and isinstance(r.get('expectancy'),(int,float)) and r['expectancy']>0), '該当なし（全戦略で3期間一貫の正期待値に未達）')}
- ポートフォリオ純益（仮想）: 買いのみ {port.get('long',{}).get('net_profit')} / 空売りのみ {port.get('short',{}).get('net_profit')} / 両方 {port.get('both',{}).get('net_profit')}

## ② 全16戦略 一覧（C_OHLC・標準条件）
| ID | 方向 | 戦略 | 勝率 | 期待値(円) | PF | 件数 | 純益(円) |
|---|---|---|---|---|---|---|---|
{chr(10).join(line(r) for r in sorted(rows, key=lambda x:(x['side'],x['id'])))}

## ③ GU3%以上の詳細分析
### ギャップ別 買い/空売り
{_gap_tbl(ls)}
### 初動5分足の方向性（§13, 全{f5.get('n',0)}銘柄日）
主要グループ（先に+0.3%到達率 / 先に-0.3%到達率 / 平均最大上昇% / 平均最大下落%）:

{chr(10).join(f"- **{k}** (n={v['n']}): +0.3%先行 {('%.1f%%'%(v['up_0.3_first_rate']*100)) if v.get('up_0.3_first_rate') is not None else '-'} / -0.3%先行 {('%.1f%%'%(v['down_0.3_first_rate']*100)) if v.get('down_0.3_first_rate') is not None else '-'} / 最大上昇 {v.get('avg_max_up_pct')}% / 最大下落 {v.get('avg_max_down_pct')}%" for k,v in sorted(f5.get('groups',{}).items()) if ('GU' in k))}

## ④ 最適なエントリー時間帯
{('最良: 時間帯 %s / %s / 期待値 %.0f円' % ewbest()) if ewbest() else '該当データなし'}

| 時間帯 | 買い(勝率/期待値/件数) | 空売り(勝率/期待値/件数) |
|---|---|---|
{chr(10).join(f"| {k} | {_fmt_side(v.get('LONG',{}))} | {_fmt_side(v.get('SHORT',{}))} |" for k,v in ew.items())}

## ⑤ 利確・損切り評価（+0.3% / -0.3% / 5分）
標準条件の出口内訳（代表戦略）と、対称TP/SLでの全体傾向を示す。多くの戦略で勝率が50%を下回る/
期待値が負であれば、0.3%対称＋コストでは優位性が出にくいことを意味する。詳細はダッシュボードの
出口理由(TP/SL/TIME)と strategy_summary.csv を参照。パラメータ感度は parameter_sensitivity（別途サンプル日で算出）。

## ⑥ 実取引可能性（空売り）
- 信用売建可否 区分（§6.1）: {summary.get('borrow_class_counts',{})}
- **区分A（当時の売建可否・数量を確認済）は履歴データが存在せず 0 件** → 空売りの実損益は仮想。
- 価格規制（§6.2）は当時の基準値段・発動フラグの履歴が無く、シグナル単位での確定は不可（当日安値-10%到達で「不確定」）。
- 一律禁止はしていない。規制不確定・売建可否不明の件数は short_sale_constraints.csv / excluded_signals.csv に記録。

## ⑦ データ品質
- 処理営業日 {dq.get('days_processed')}（{dq.get('date_min')}〜{dq.get('date_max')}）、平均ユニバース {dq.get('avg_universe')}、平均バー有り銘柄 {dq.get('avg_symbols_with_bars')}。
- push板ティック（A/B）はシグナル銘柄の一部のみ（1日平均 {dq.get('avg_tick_symbols_per_day')} 銘柄）→ **A/B は限定的、C_OHLC が主**。
- 初動5分足レコード {dq.get('first5_records'):,} 件。
- 既存監視ユニバースは当時の購読履歴（push_subscription_symbols_history）を使用。現在のリストを過去へ無条件適用していない。
- 注記: {' / '.join(dq.get('notes',[]))}

## ⑧ 本番システムへの導入候補（設計案のみ・本番コード変更なし）
- GO/CONDITIONAL GO かつ 期待値が正の戦略のみを候補とする。既存の Ranking / Tonosama / Summary-AI の
  「エントリー候補生成」に、本バックテストの条件（例: ギャップ×初動5分足方向のフィルタ）を**追加フィルタ**として
  組み込む設計が考えられる（trading/entry/gapup_breakout.py は既に LONG_01 相当を実装済み）。
- 空売りは売建可否のリアルタイム確認（kabu `/regulations`・`/marginpremium`）を発注前提にする必要があり、
  まず日次スナップショットの保存層を追加してからバックテストの実行可能性を A 区分化するのが先決。

## ⑨ 判定（§20: 正データ→未来情報なし→実行可能性→ネット期待値→頑健性→DD→再現性→勝率）
内訳: GO {counts['GO']} / CONDITIONAL GO {counts['CONDITIONAL GO']} / NO-GO {counts['NO-GO']} / INSUFFICIENT DATA {counts['INSUFFICIENT DATA']}

| ID | 方向 | 判定 | 根拠 |
|---|---|---|---|
{chr(10).join(judge_rows)}

> GO はバックテスト上の採用候補であり、実資金の自動発注許可ではない。実導入には別途ペーパートレード・
> 前向き検証・リスク制限・注文可否確認を要する。

## ⑩ Git とテスト
- ブランチ: {meta.get('branch','')}
- 開始commit: {meta.get('start_commit','')}
- 終了commit: {meta.get('end_commit','(このレポート生成後にcommit)')}
- 変更ファイル: backtest16/ 配下の新規追加のみ（本番コード・既存DB・設定は不変）
- テスト成功/失敗: {meta.get('tests_passed','?')} / {meta.get('tests_failed','0')}
- push・merge・deploy: **未実施**（§13 遵守）
- 未完了作業: {meta.get('todo','パラメータ感度の全戦略×全グリッド網羅、A/B約定の広域カバレッジ（push疎のため限定）')}
"""
    path = os.path.join(out_dir, "final_report.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(md)
    return path


def _fmt_side(x: dict) -> str:
    wr = x.get("win_rate")
    if wr is None:
        return "-"
    return f'{wr*100:.1f}%/{x.get("expectancy")}円/{x.get("n")}'
