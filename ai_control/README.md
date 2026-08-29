# ai_control — ログ監視 → Codex 診断 → Claude Code 修正

稼働中の売買システムのログを監視し、異常を Codex に解析させ、修正指示書を作る仕組み。

**現在は第2段階まで実装済み。本番の作業ツリーには一切書き込まない。**
**main.py の停止・再起動・本番復帰は実装していない（第4段階）。**

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
| 2 | `task.json` → Claude Code 自動修正（worktree 上） | **実装済み** |
| 3 | DRY-RUN 起動・本番反映の判断 | 未着手 |
| 4 | `main.py` の自動停止・再起動 | 未着手 |
| 5 | 本番への自動反映 | 未着手 |

rollback の基準点はタグ **`baseline-20260828`**（commit `ca5520e`）。
2026-08-28 時点で稼働していた版であり、自動修正はここを起点にする。

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

# 第2段階（Claude Code 修正）を止めて、監視と診断だけにする
.venv\Scripts\python.exe ai_control\supervisor.py --no-repair

# 既存の task.json から修復だけを走らせる（第2段階の検証用）
.venv\Scripts\python.exe ai_control\supervisor.py --repair-from <task.json>
```

## ファイル

| ファイル | 役割 |
|---|---|
| `supervisor.py` | 司令塔。これだけを起動する |
| `logtail.py` | NAS 上のログを prefix ごとに最新 1 本ずつ追尾 |
| `triggers.py` | 正規表現による異常検出、signature、回数制限 |
| `codex_client.py` | `codex exec` の呼び出しと JSON 受け取り |
| `policy.py` | GREEN / YELLOW / RED の分類 |
| `repair.py` | 第2段階の司令塔。ゲート判定 → worktree → Claude → 検査 → テスト → コミット |
| `worktree.py` | AI 修正専用 worktree の作成と安全装置 |
| `claude_client.py` | `claude -p` の呼び出し。**Bash を禁止**して起動する |
| `tests_runner.py` | compileall と import smoke（denylist つき） |
| `audit.py` | 監査ログ（`history/audit.jsonl`） |
| `claude_prompt.md` | 修正役へのプロンプト雛形 |
| `config.yaml` | 監視対象・トリガ・除外・上限 |
| `policy.yaml` | 分類規則と禁止事項 |
| `codex_prompt.md` | 診断役へのプロンプト雛形 |
| `task_schema.json` | Codex の出力形式（`--output-schema` で強制） |
| `inbox/task.json` | 最新の指示書。第2段階はこれを読む |
| `history/` | 全指示書とプロンプトの記録 |
| `status/state.json` | signature ごとの解析回数・クールダウン |
| `locks/SAFE_STOP` | 同じ症状を規定回数解析しても直らなかった印 |
| `locks/repair.lock` | 修復中の印。二重起動と並列修正を防ぐ |
| `history/audit.jsonl` | 1 行 1 サイクルの監査ログ |
| `history/*.diff` | Claude が作った差分の原文 |

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
| `data_collectors_runner` | **2026-08-29 追加**。46MB のログで 4,391 件検出したが、大半が重複だった。この系統は子プロセス（`ranking_collector` / `summary_database` / `push_receiver`）のログを `[CHILD:xxx]` で包んで再出力する多重化ログで、子はそれぞれ自分のログを持つ。`exclude_patterns` に `\[CHILD:...\]` を追加して 4,391 → **0 件**。runner 自身が出す行（子の起動・停止・異常終了）は残る |
| `yahoo_complement_runner` | **2026-08-29 追加**。1.9MB で 1 件（`db_lock`）。妥当 |

`--- DISCONNECTED expected_rotation ---` は正常系なので除外している。

トリガを変えたら必ず `--replay <logfile> --list-only` で誤検出量を確認すること。

## 監視系統の取りこぼし（2026-08-29）

再起動後にログディレクトリを見直したところ、監視対象から
`data_collectors_runner` と `yahoo_complement_runner` が漏れていた。
**新しいプロセスが増えたら `watch.prefixes` を更新すること。**
漏れていると、その系統の異常は永久に検出されない。

確認方法:

```powershell
# 当日動いているログ系統を一覧する
ls \192.168.0.22\AutoStockBuyAndSell\Logs\console | ForEach-Object {
  $_.Name -replace '_\d{8}_\d{6}_\d+\.log$',''
} | Sort-Object -Unique
```

## 実測値

- `codex exec` 1 回あたり **約 6 分**（プロンプト約 62,000 文字、reasoning effort high）
- 解析が遅いので「リアルタイム修正」にはならない。異常検出から指示書ができるまで数分かかる前提で運用する

## 第2段階の設計

### 本番ツリーを Claude に触らせない

本番の作業ツリー = いま稼働しているプロセスが読んでいるファイルそのもの。
そこを直接編集させると、テストが終わる前の中途半端なファイルを稼働中の
`main.py` が読む可能性がある。そのため **別ディレクトリの git worktree** で修正する。

```
本番ツリー (稼働中)  ← 触らない
      │
      │ git worktree add
      ▼
../ai_fix_worktree   ← Claude Code はここだけを編集する
      │
      ├ 変更ファイルの検査（禁止領域なら全部破棄）
      ├ compileall / import smoke
      └ 合格 → auto-fix/<日時> ブランチにコミット
```

`worktree.py` には安全装置が 3 つある。どれか 1 つでも破れたら例外を投げて何もしない。

1. worktree のパスが本番リポジトリの内側にあってはならない
2. ブランチ名は `auto-fix/` で始まらなければならない
3. `reset --hard` / `clean` は `git rev-parse --show-toplevel` で
   「そこが本当に目的の worktree か」を確認してからしか実行しない

### Claude Code に Bash を渡さない

```
claude -p --output-format json --permission-mode acceptEdits
       --allowedTools "Read Edit Write Grep Glob"
       --disallowedTools "Bash WebFetch WebSearch Task"
       --max-turns 30
```

git 操作・テスト・再起動はすべて `supervisor` 側が握る。
Claude にできるのは「ファイルを読んで直す」ことだけになる。

### Codex の申告を信じない（事後チェック）

`repair.py` は Claude の修正後に、**実際に変更されたファイル**を
`policy.yaml` の `red_path_patterns` に照らす。
禁止領域のファイルが 1 つでも変更されていたら、
差分を監査ログに残したうえで **`reset --hard` で全部破棄**する（`POLICY_VIOLATION_REVERTED`）。

Codex が category を過小申告しても、Claude が指示外のファイルを触っても、ここで止まる。

### ゲート（どれか 1 つでも引っかかれば Claude を起動しない）

| 条件 | 結果 |
|---|---|
| `status != FIX_REQUIRED` | `NO_FIX` |
| policy = RED | `HUMAN_REVIEW_REQUIRED` |
| policy が `allow_levels` 外 | `BLOCKED` |
| `confidence < 0.7` | `BLOCKED` |
| 1 時間に 5 回修正済み | `BLOCKED` |
| 同一 signature で 3 回修正済み | `BLOCKED`（SAFE_STOP） |
| `repair.lock` が存在 | `BLOCKED`（二重起動・並列修正の防止） |

### 最終判定

| verdict | 意味 |
|---|---|
| `READY_TO_APPLY` | GREEN・テスト合格。人が本番へ反映する |
| `DRY_RUN_ONLY_HUMAN_APPLY` | YELLOW・テスト合格。人の確認が必須 |
| `TESTS_FAILED` | テスト不合格。ブランチに残るがコミットしない |
| `POLICY_VIOLATION_REVERTED` | 禁止領域を触ったので破棄 |
| `CLAUDE_REFUSED` | Claude が修正しなかった（NEED_MORE_INFO 等） |
| `HUMAN_REVIEW_REQUIRED` / `BLOCKED` / `NO_FIX` | ゲートで停止 |

**どの verdict でも本番の作業ツリーは書き換わらない。** 反映は人の操作。

### テスト

pytest 基盤が無いため、いまは 2 段階だけ。

- `compileall` … 構文チェック。コードを実行しないので完全に安全。常に実行する
- `import smoke` … 対象モジュールを 1 つずつ別プロセスで import する

import smoke は**コードを実行する**ため、`tests_runner.py` の `IMPORT_DENY` で
起動系（`main.py` / `main_database.py` / `run_*.py`）、発注系（`sell.py` /
`cancel_order.py` / `SEND_exit_order.py`）、`batch/` `scripts/` `TaskSchedule/`
`test_script/` `backtest/` を除外している。1 モジュールごとにタイムアウト付き。

本番 API への発注、DB の破壊的操作は行わない。

## worktree はローカルディスクに置くこと

`worktree_dir` を NAS 上（`../ai_fix_worktree`）にしたところ、
`git worktree add` が **600 秒でタイムアウトした**（2,111 ファイル / 16.5MB、
`.git` は 7.7GB）。撤去コマンド自体も数分では終わらなかった。

worktree は毎回 git から作り直す使い捨てなので、ローカルディスクで良い。
`C:/Users/hosot/AppData/Local/ai_fix_worktree` に変更したところ、初回作成
約 2 分、2 回目以降は再利用されるため数秒で済む。

もう 1 つ、NAS 特有の問題がある。worktree のメタデータ
（`.git/worktrees/...`）は SMB 上にあり所有者情報を持たないため、
git が `dubious ownership` で実行を拒否する。
`worktree.py` の `_git()` が `-c safe.directory=...` を毎回付けて回避している。
**グローバル設定は書き換えていない。**

## 監視系統の取りこぼし（2026-08-29）

再起動後にログディレクトリを見直したところ、監視対象から
`data_collectors_runner` と `yahoo_complement_runner` が漏れていた。
**新しいプロセスが増えたら `watch.prefixes` を更新すること。**
漏れていると、その系統の異常は永久に検出されない。

確認方法:

```powershell
# 当日動いているログ系統を一覧する
ls \192.168.0.22\AutoStockBuyAndSell\Logs\console | ForEach-Object {
  $_.Name -replace '_\d{8}_\d{6}_\d+\.log$',''
} | Sort-Object -Unique
```

## 実測値

- `codex exec` 1 回あたり **約 6 分**（プロンプト約 62,000 文字、reasoning effort high）
- `claude -p` 1 回あたり **約 1.5 分**（GREEN の 1 ファイル修正・19 行）
- worktree の初回作成 **約 2 分**（ローカルディスク）
- 解析が遅いので「リアルタイム修正」にはならない。
  異常検出から指示書ができるまで数分かかる前提で運用する

## 第2段階の実機テスト結果（2026-08-29）

| # | ケース | 結果 | Claude 起動 |
|---|---|---|---|
| 1 | RED（category=order） | `HUMAN_REVIEW_REQUIRED` | なし |
| 2 | RED（path=order_executor.py、category は logging） | `HUMAN_REVIEW_REQUIRED` | なし |
| 3 | `NEED_MORE_INFO` | `NO_FIX` | なし |
| 4 | confidence 0.40 | `BLOCKED` | なし |
| 5 | 同一 signature 3 回到達 | `BLOCKED`（SAFE_STOP） | なし |
| 6 | 1 時間 5 回到達 | `BLOCKED` | なし |
| 7 | `repair.lock` 保持中 | `BLOCKED` | なし |
| 8 | 事後チェック `_violations()` 単体 | 禁止パス 5 種を検出。混在ケースも捕捉 | — |
| 9 | **GREEN エンドツーエンド** | `READY_TO_APPLY`・テスト 2/2 合格 | **あり** |
| 10 | **YELLOW エンドツーエンド** | `DRY_RUN_ONLY_HUMAN_APPLY`・テスト 2/2 合格 | **あり** |

### 9 の内容

`core/logging/console_tee.py` の `_AsyncFileWriter._drain()` は
`except Exception: pass` で NAS 書き込み失敗を握りつぶしており、
キュー溢れ（`_dropped`）は数えているのに**書き込み失敗で失った行は数えていなかった**。

Claude Code の修正（+19 / -1、1 ファイル）:

- `_write_failed` カウンタを追加
- 失敗時にロック内で加算
- 復旧時に既存の `dropped` 警告と同じ形式で 1 回だけ出力
- `stats()` に追加
- 例外は再送出せず、「NAS が落ちても売買スレッドを止めない」既存挙動を保持
- 無関係な変更なし

`auto-fix/20260829-001540` の `4514681` にコミット済み。**本番へは未反映。**

### 10 の内容

同じ欠陥を `category: summary`（= `dry_run_only`）で投入し、判定だけが変わることを確認した。
修正は行われる（+24 / -2）が、verdict は `DRY_RUN_ONLY_HUMAN_APPLY` になり、
`READY_TO_APPLY` にはならない。`auto-fix/20260829-001755` の `4c5a5ef`。

GREEN と YELLOW の差は「本番へ反映してよいと機械が言うか」だけで、
**第2段階ではどちらも本番に反映しない**。反映は人の操作。
