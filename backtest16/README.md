# backtest16 — 買い6戦略＋空売り10戦略 比較バックテスト基盤

日本株の寄り付き後の値動きについて、買い6戦略・空売り10戦略の計16戦略を統一条件で比較し、
**実取引で再現可能な、最も期待値の高い売買条件**を元データまで追跡可能な形で検証する。

> 本番の売買エンジン(main.py / main_database.py 等)・既存DB・設定・認証情報は一切変更しない。
> 実際の発注APIは呼ばない。既存SQLite DBは読み取り専用(過去日は `immutable=1`)。commitのみ(push/merge/deployなし)。

## 実行
```bash
# ソースコード/ から（.venv 必須）
.venv/Scripts/python.exe -m backtest16.run --start 2026-04-20 --end 2026-10-20 --resume   # 全窓(日次チェックポイント)
.venv/Scripts/python.exe -m backtest16.run --days 2026-06-16                               # 単日
.venv/Scripts/python.exe -m backtest16.aggregate                                           # 集計→CSV/summary.json
.venv/Scripts/python.exe -m backtest16.param_sensitivity --days 8                          # §11 感度(学習+検証のみ)
.venv/Scripts/python.exe -m pytest backtest16/tests -q                                     # 自動テスト
```
レポート生成は `aggregate.build()` → `reports.html_report.build_all()` → `reports.final_report_md.build()`
（`generate_reports.py` が一括実行）。出力は `backtest16/reports/`。

## データ（読み取り専用）
- `X:/raw_data/kabu_station/{push,summary,ranking}/<kind>YYYYMMDD.db`（NAS複製, 各~0.5-1GB/日）
- push.stream_data: 現値+板10本値+`previousclose`(※実データは全NULL)+`opening_price`。PUSHスナップショットであり取引所全約定ではない。
- summary.stock_summary_{1,3,5}min: 確定足OHLCV+vwap+ma5/25/75（点in-time正）。
- 前日終値 = **前営業日 summary_5min の最終close**（push.previousclose がNULLのため）。
- 当時の監視ユニバース = `push_subscription_symbols_history.db`（現在のリストを過去へ適用しない）。

## 重要な前提・制約（結果を読む際に必須）
1. **約定モードA/B/Cは混ぜない**（§8）。主たる執行可能モードは **C_OHLC（確定1分足・次足始値エントリー・同足両到達は損切り優先）**。
   板ティック由来の A_BOARD / B_PUSH は push が疎なためシグナル銘柄の一部のみ（補助）。
2. **空売りの売建可否・価格規制・在庫の履歴データが存在しない**（本番はkabu APIのリアルタイム判定のみ）。
   → 区分A（当時の可否・数量を確認済）は0件。**空売りの実損益は仮想**（区分B/D）。価格規制はシグナル単位で確定不可。
3. 未来情報防止（§7）: 確定足は `ts+interval` で確定時刻を判定、09:05前に初動5分足を使わない、評価足を出来高平均に含めない、
   エントリーは必ずシグナル時刻より後、同一イベントでのシグナル成立＆理想約定を禁止。単体テスト `test_no_lookahead.py` で担保。
4. コスト（§6.3）は仮定値（手数料0/スプレッド/スリッページ/貸株料/一般信用プレミアム/信用金利）。逆日歩は不明扱い。5分保有でも貸株料0にしない。
5. 分割・併合の調整済み前日高値（SHORT_10）は Yahoo日足が理想だが、本実装は前営業日のセッション高値を代理に使用（分割は要注記）。

## 構成（§15 推奨構成からの調整）
推奨の `strategies/<id>.py` 個別ファイルは、保守性のため `strategies/longs.py`・`strategies/shorts.py` に集約
（各戦略クラスは明確にID付与。`strategies/base.py` に共通ヘルパ＋REGISTRY）。
`engine/position_manager.py`・`risk_manager.py`・`event_replay.py` の機能は
`engine/portfolio_engine.py`（建玉上限3・70万・同一銘柄両サイド禁止・優先順位・未約定記録）と
`day_builder.py`（イベント組み立て）に集約。本番注文コードは流用せず独立実装。

## 主要モジュール
- `config.py` 設定の単一真実源 / `data_loader.py` 読み取り専用ローダ / `market_calendar.py` 営業日
- `features.py` SymbolDay（銘柄日のバー束・ギャップ・初動5分足） / `day_builder.py` 組み立て
- `cost_model.py` / `short_sale_rules.py`（価格規制・約定方向）/ `borrow_availability.py`（区分A/B/C/D）
- `engine/execution_engine.py`（A/B/C）/ `signal_engine.py` / `portfolio_engine.py`
- `strategies/`（longs/shorts/base）/ `analytics/`（metrics/first_five_minutes/long_short_comparison/walk_forward）
- `run.py`（日次チェックポイント）/ `aggregate.py` / `param_sensitivity.py` / `reports/`

## 成果物（`backtest16/reports/`）
`backtest_16strategies.html` / `data_quality.html` / `long_vs_short.html` / `first_5min_analysis.html` /
`strategy_summary.csv` / `trades.csv` / `daily_pnl.csv` / `short_sale_constraints.csv` /
`excluded_signals.csv` / `parameter_sensitivity.csv` / `final_report.md` / `summary.json`
