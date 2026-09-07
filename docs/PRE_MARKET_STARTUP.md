# 寄り付き前 起動チェックリスト

対象: 本番再起動後に自動売買を有効化してよいかを判断する手順。
所要 5〜10 分。**FAIL が 1 つでもあれば自動売買を有効化しない。**

---

## 手順

| # | 手順 | 確認内容 |
|---|------|---------|
| 1 | kabu Station を起動しログイン | API 有効・`localhost:18080` が listen |
| 2 | `start_all.bat` を実行 | main_database.py → 25 秒後 main.py の順に起動 |
| 3 | 60〜90 秒待つ | data collectors (push_receiver / yahoo / summary / ranking) が出揃うまで |
| 4 | ヘルスチェックを実行 | 下記コマンド。**必須項目が全て PASS** であること |
| 5 | ranking_snapshot の増加確認 | 1 分後に再実行し `03` の rows が増えていること |
| 6 | PUSH 最新時刻確認 | `08 PUSH DB 最新` が age < 60s |
| 7 | FAST_PATH HIT 確認 | `14 FAST PATH` が HIT>0 |
| 8 | technical_ready 確認 | `13 technical_ready>0` が PASS |
| 9 | Entry audit 確認 | `15 Entry pipeline` が PASS |
| 10 | 全て満たしたときのみ自動売買を有効化 | FAIL が 1 つでもあれば有効化しない |

### 実行コマンド

```bat
cd /d "W:\BUSINESS\事業\04_株式自動売買システム\ソースコード"
.venv\Scripts\python.exe test_script\pre_market_health_check.py
```

JSON で欲しい場合:

```bat
.venv\Scripts\python.exe test_script\pre_market_health_check.py --json
```

終了コード: `0` = FAIL なし / `1` = FAIL あり（自動売買を有効化しない）。

このスクリプトは **read-only** です。発注・取消・PUSH 登録変更・DB 書き込み・
Discord 送信・`/token` 発行はいずれも行いません。

---

## 判定項目

| # | 項目 | 判定 |
|---|------|------|
| 01 | kabu API 疎通 (`/positions`, `/wallet/cash`) | 両方 200 で PASS |
| 02 | ranking API rows > 0 | 50 行返れば PASS / 429 は WARN |
| 03 | `ranking_snapshot_1min` rows > 0 | 0 なら FAIL |
| 04 | ranking 最新時刻が fresh | 300 秒以内 |
| 05 | PUSH WebSocket connected | `ws_connected=true` |
| 06 | RECONNECT_STORM でない | `health_status` が STORM なら FAIL |
| 07 | confirmed_registered > 0 | 登録銘柄が 0 でない |
| 08 | PUSH DB 最新が fresh | 180 秒以内 |
| 09 | summary 1min rows > 0 | |
| 10 | summary 1min latest が fresh | 300 秒以内 |
| 11 | summary 3min rows > 0 | |
| 12 | summary 5min rows > 0 | |
| 13 | technical_ready > 0 | 指標が計算できている |
| 14 | FAST PATH が HIT する | HIT=0 かつ MISS>0 なら FAIL |
| 15 | Entry pipeline が動いている | `SEND_TO_AI` / `ENTRY READY AUDIT` の出力 |
| 16 | Discord TLS 疎通 | 失敗しても売買には影響しないので WARN |
| 17 | REST coordinator が degraded 固定でない | |

> 立会時間外は 04 / 08 / 10 の鮮度チェックが WARN に落ちます（データが更新されないため）。
> 寄り付き前に実行する場合は、この 3 項目の WARN は許容してください。

---

## データとログの置き場所 (2026-09-05 / 09-07 に変更)

NAS (SMB) の不調でシステムが止まるのを避けるため、**書き込みはローカル**に変えた。
NAS へは定期的に複製する。

| 対象 | 現在の書き先 | NAS への複製 |
|------|------------|-------------|
| コンソールログ / heartbeat | `%LOCALAPPDATA%\autostock\logs\console` | 5 分ごとに差分追記 |
| push DB | `%LOCALAPPDATA%\autostock\push` | 30 分ごと + 停止時 |
| ranking DB | `%LOCALAPPDATA%\autostock\ranking` | 30 分ごと + 停止時 |
| summary DB | **NAS のまま** | - |

**調査は必ずローカル側を見ること。** NAS 側は複製なので数分〜30 分古い。

従来どおり NAS へ直接書きたい場合は
`KABU_CONSOLE_LOG_DIR` / `PUSH_DB_LOCAL_STAGING=0` / `RANKING_DB_LOCAL_STAGING=0`。

## NAS が落ちた / マッピングが外れたとき

2026-09-07 に X: と Y: のマッピングが外れる障害が発生した。実際の症状:

| 症状 | 意味 |
|------|------|
| `returncode=3221225478` で子プロセスが終了 | `STATUS_IN_PAGE_ERROR`。共有上のファイルを掴んだまま SMB が切れた |
| `FileNotFoundError: [WinError 53] ネットワーク パスが見つかりません` | マッピングが外れている |
| `[RANKING DB WRITER] flush failed v6 non-retryable` が連続 | NAS 上の DB へ書けない |
| `[OrderBookDB] open connection before flush failed` が連続 | 同上 |

**重要: `net use` で再マッピングしただけでは復旧しない。**
稼働中のプロセスが古い SQLite 接続を掴んだままなので、再マップ後も失敗が続く
(実測: 再マップ後 5 分で `OrderBookDB` 1,359 回 / `OPEN POSITION SYNC` 723 回の失敗)。

手順:
1. `net use` で X:/Y: が生きているか確認する
2. 外れていれば再マッピングする
3. **システムを停止して再起動する** (これを飛ばすと直らない)
4. ヘルスチェックで FAIL 0 を確認する


## 異常時に見るログ

ログは `X:\logs\console\`（= `\\192.168.0.22\AutoStockBuyAndSell\Logs\console\`）。

| 症状 | 見るログキーワード | ファイル |
|------|------------------|---------|
| ranking 異常 | `[RANKING API CLIENT]` / `[RANKING COLLECT]` / `[RANKING API BREAKER]` | `ranking_collector_runner_*.log` |
| PUSH 異常 | `[PUSH HEALTH]` / `[PUSH REG STATE]` / `[PUSH OWNER HEALTH]` | `push_receiver_runner_*.log` |
| Summary 異常 | `[FAST_PATH_DECISION]` / `[SUMMARY MAIN MEMORY 1M]` / `[SUMMARY FORCE DIRECT 1M]` | `console_*.log` (main.py) |
| REST 逼迫 | `[REST COORD STATS]` / `[REST COORD] degraded` | 各ログ |
| SSL 異常 | `[DISCORD TLS]` / `[CA BUNDLE]` | `console_*.log` |
| 認証異常 | `[TOKEN ISSUE]` / `[TOKEN SETTINGS]` / `[TOKEN PREFLIGHT]` / `[TOKEN REQUEST HEADER]` | `main_database_*.log` |
| WebSocket 同一性 | `[WS OWNERSHIP]` / `[MULTI_WS_DETECTED]` / `[PUSH MONITOR][WS IDENTITY]` | `push_receiver_runner_*.log` |
| 銘柄が全部消える | `[liquidity_filter]` / `[ACTIVE CATEGORY QUOTA]` / `[PUSH FALLBACK]` | `console_*.log` |

### よくある症状と原因

| ログ | 意味 | 対処 |
|------|------|------|
| `breaker open のため skip` が続く | ranking API が連続失敗と判定された | `[RANKING API CLIENT] outcome=` を見る。`rate_limited` なら流量制御なので放置で復帰、`timeout` なら kabu Station を確認 |
| `probe invalid response data_type=NoneType` | 旧コード。失敗種別が潰れている | 修正版では `outcome=` が出る。出ていなければ再起動されていない |
| `all providers returned empty` | ranking 由来の候補が全滅 | 修正版では `[PUSH FALLBACK]` が出て現登録を維持する |
| `empty after volume filter` | 出来高フィルタで全滅 | `[liquidity_filter]` の `col=` と `vol_max` を見る。`cumulative=False` なら上流が 1 本足を渡している |
| `reject stale frame ... age > max_age` | 1 分足の確定足に欠損がある | PUSH の欠損が原因。`[PUSH HEALTH]` を先に見る |
| `RECONNECT_STORM` | WS が短時間で切れ続けている | `[WS OWNERSHIP]` で所有プロセスを確認。2 本張られていないか |

---

## 起動できないとき

| 事象 | 確認 |
|------|------|
| `main_database.py` が `abort: token bootstrap failed` | `[TOKEN PREFLIGHT] status=` を見る。401 なら settings.ini の token が現行でない。kabu Station を再起動して再実行 |
| `Too many open files` が W: / X: で出る | NAS への SMB セッション劣化。売買システムを停止 → `net use W: /delete` → 再マッピング → 再起動 |
| kabu Station の REST が全滅 (timeout) | kabu Station 本体を再起動（再ログイン）。CPU 使用が異常に高いときは特に |
