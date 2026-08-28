# ai_control — ログ監視 → Codex 診断 → Claude Code 修正

稼働中の売買システムのログを監視し、異常を Codex に解析させ、修正指示書を作る仕組み。

**現在は第1段階まで実装済み。本体には一切触れない（読むだけ）。**

## 役割分担

| 役割 | 担当 | できること |
|---|---|---|
| 監視・司令塔 | `supervisor.py` | ログ追尾・異常検出・回数制限・分類・記録 |
| 診断 | Codex (`codex exec -s read-only`) | ログとソースを読んで原因特定と指示書作成。**書き込み不可** |
| 修正 | Claude Code（第2段階） | 指示書に従ってコードを変更 |

Codex に修正させないのは指示文ではなく `-s read-only` サンドボックスで担保している。
「AI 同士が別々の修正をして競合する」事故はこの分離で防ぐ。

## 段階

| 段階 | 内容 | 状態 |
|---|---|---|
| 1 | ログ監視 → Codex 解析 → `inbox/task.json` 生成 | **実装済み** |
| 2 | `task.json` → Claude Code 自動修正 | 未着手 |
| 3 | 自動テスト（compileall / pytest / DRY-RUN） | 未着手 |
| 4 | `main.py` の自動停止・再起動 | 未着手 |
| 5 | Git rollback・GREEN/YELLOW/RED ゲート | 未着手 |

第2段階に進む前に、**このリポジトリの未コミット変更を整理して基準点を作る必要がある**
（2026-08-28 時点で 217 件）。基準が無いと rollback が成立しない。

## 使い方

```powershell
# 常時監視（Ctrl-C で停止）
.venv\Scripts\python.exe ai_control\supervisor.py

# Codex を呼ばずに配線だけ確認する
.venv\Scripts\python.exe ai_control\supervisor.py --no-codex

# 既存ログで検出だけ試す（トリガの調整用。Codex を呼ばないので無料）
.venv\Scripts\python.exe ai_control\supervisor.py --replay <logfile> --list-only

# 既存ログで 1 件だけ実際に Codex にかける
.venv\Scripts\python.exe ai_control\supervisor.py --replay <logfile> --once
```

## ファイル

| ファイル | 役割 |
|---|---|
| `supervisor.py` | 司令塔。これだけを起動する |
| `logtail.py` | NAS 上のログを prefix ごとに最新 1 本ずつ追尾 |
| `triggers.py` | 正規表現による異常検出、signature、回数制限 |
| `codex_client.py` | `codex exec` の呼び出しと JSON 受け取り |
| `policy.py` | GREEN / YELLOW / RED の分類 |
| `config.yaml` | 監視対象・トリガ・除外・上限 |
| `policy.yaml` | 分類規則と禁止事項 |
| `codex_prompt.md` | 診断役へのプロンプト雛形 |
| `task_schema.json` | Codex の出力形式（`--output-schema` で強制） |
| `inbox/task.json` | 最新の指示書。第2段階はこれを読む |
| `history/` | 全指示書とプロンプトの記録 |
| `status/state.json` | signature ごとの解析回数・クールダウン |
| `locks/SAFE_STOP` | 同じ症状を規定回数解析しても直らなかった印 |

## 設計上の判断

### ログ 1 行ごとに AI を呼ばない

1 秒に数百行出るため、料金・速度・誤判定のどれをとっても成立しない。
`triggers.py` で機械的に絞り、Codex には「怪しい 1 件」＋前後の文脈だけを渡す。

### NAS への I/O を絞る

`core/logging/console_tee.py` のコメントにあるとおり、ログの NAS 書き込みは
実測で本体の実行時間の約 25% を占めていた。監視側が同じファイルを叩きすぎると
本体を遅くする。そのため:

- ディレクトリ走査は 60 秒に 1 回。既知ファイルは `stat()` だけ
- 1 ポーリングで読む上限は 256KB。溢れたら**古い側を捨てて追いつく**
- ポーリング間隔は 10 秒

### 分類は二重化する

Codex が申告する `category` だけを信じない。`policy.yaml` の `red_path_patterns` で
ファイルパスからも判定し、**危険側を採用**する。
`category: logging` でも `order_executor.py` を触るなら RED になる。

### 無限修正ループを止める

- 同一 signature は 30 分クールダウン
- 1 時間あたり 6 回まで
- 同一 signature を 3 回解析しても直らなければ `locks/SAFE_STOP` を作って打ち止め

## トリガの調整記録

実ログ（2026-08-28）で検証した結果:

| トリガ | 結果 |
|---|---|
| `push_disconnect` | 初版は誤検出16件。`websocket callbacks wired`（`error=True close=True` を含むだけ）と `[PUSH OWNER HEALTH]` に当たっていた。実装が出す文言（`--- DISCONNECTED --- ` / `[push_stream] mark ws disconnected`）に直し、`exclude_patterns` を追加。残った11件は本物の切断 WARNING |
| `error_burst` | 726KB のログで2件。妥当 |
| その他 | 検出0件（この日のログには該当なし） |

`--- DISCONNECTED expected_rotation ---` は正常系なので除外している。

トリガを変えたら必ず `--replay <logfile> --list-only` で誤検出量を確認すること。

## 実測値

- `codex exec` 1 回あたり **約 6 分**（プロンプト約 62,000 文字、reasoning effort high）
- 解析が遅いので「リアルタイム修正」にはならない。異常検出から指示書ができるまで数分かかる前提で運用する
