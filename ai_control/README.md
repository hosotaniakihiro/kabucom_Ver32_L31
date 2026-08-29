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
| 1 | ログ監視 → Codex 解析 → **自己検証** → `inbox/task.json` 生成 | **実装済み** |
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

# 自己検証を止めて、診断 1 回だけにする（Codex 呼び出しを半分にする）
.venv\Scripts\python.exe ai_control\supervisor.py --no-verify

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
| `verify_prompt.md` | 検証役へのプロンプト雛形（診断を自分で裏取りさせる） |
| `verify_schema.json` | 検証結果の形式（`--output-schema` で強制） |
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

## Codex には NAS を読ませられない（2026-08-29 に判明）

`codex exec` の read-only サンドボックスは、**ネットワークドライブを読めない**。
ドライブレター (`W:`)、UNC (`\192.168.0.22\...`)、
`-c sandbox_permissions=["disk-full-read-access"]` のいずれでも読めなかった。

これに気づかないまま運用していた間、**Codex はソースを読めず、ログだけで
推測していた**。401 の解析で Codex 自身が
「UNCパスがPermissionDeniedとなり、実際に読んで裏取りできなかった」と
報告して発覚した。

対策として、`ReadOnlyWorktree` がローカルディスクに本番 HEAD の checkout を
作り、Codex にはそちらを見せる（`codex.worktree_dir`）。

同じ 401 を、条件だけ変えて解析させた結果:

| Codex の作業ルート | status | confidence |
|---|---|---|
| NAS（ソースを読めない） | `NEED_MORE_INFO` | 0.35 |
| ローカル checkout（読める） | `FIX_REQUIRED` | **0.91** |

### HEAD と稼働中コードのズレを Codex に伝える

診断用 checkout は HEAD の内容だが、**稼働中のプロセスが実行しているのは
本番の作業ツリー**（未コミットの変更を含む）である。
`supervisor.py` は `git status --porcelain` で未コミットのファイル名を集め、
プロンプトに「これらは読む内容と動いている内容が食い違う可能性がある」と
明記する。実際に Codex はこれを読み取り、未コミットの
`kabu_api/token_bootstrap.py` を上書きしないよう指示に含めてきた。

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

## Codex には NAS を読ませられない（2026-08-29 に判明）

`codex exec` の read-only サンドボックスは、**ネットワークドライブを読めない**。
ドライブレター (`W:`)、UNC (`\192.168.0.22\...`)、
`-c sandbox_permissions=["disk-full-read-access"]` のいずれでも読めなかった。

これに気づかないまま運用していた間、**Codex はソースを読めず、ログだけで
推測していた**。401 の解析で Codex 自身が
「UNCパスがPermissionDeniedとなり、実際に読んで裏取りできなかった」と
報告して発覚した。

対策として、`ReadOnlyWorktree` がローカルディスクに本番 HEAD の checkout を
作り、Codex にはそちらを見せる（`codex.worktree_dir`）。

同じ 401 を、条件だけ変えて解析させた結果:

| Codex の作業ルート | status | confidence |
|---|---|---|
| NAS（ソースを読めない） | `NEED_MORE_INFO` | 0.35 |
| ローカル checkout（読める） | `FIX_REQUIRED` | **0.91** |

### HEAD と稼働中コードのズレを Codex に伝える

診断用 checkout は HEAD の内容だが、**稼働中のプロセスが実行しているのは
本番の作業ツリー**（未コミットの変更を含む）である。
`supervisor.py` は `git status --porcelain` で未コミットのファイル名を集め、
プロンプトに「これらは読む内容と動いている内容が食い違う可能性がある」と
明記する。実際に Codex はこれを読み取り、未コミットの
`kabu_api/token_bootstrap.py` を上書きしないよう指示に含めてきた。

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

## 診断の自己検証（第1段階の2パス目）

1 回目の診断は、**思い込みで `CONFIRMED` を書く**ことがある。そこで
同じ Codex にもう一度、**自分の診断を検証させる**。

```
ログ → Codex 診断 (task_schema.json)
         → Codex 検証 (verify_schema.json)  ← 主張を1つずつ CONFIRMED/REFUTED/UNVERIFIABLE
         → confidence を付け直す
         → policy 分類 → 第2段階のゲート
```

検証役には、診断では渡していないものを 1 つ足して渡す。
**稼働中の作業ツリーの未コミット差分**である。診断役が読むのは HEAD なので、
実際に動いているコードとのズレはここでしか見えない。

### なぜ入れたか（2026-08-29 の実測）

kabu Station API の 401 を解析させた診断（confidence 0.91）を検証させたところ、
**主張 2 件が `REFUTED` になり、confidence が 0.72 に下がった**。

| 覆った主張 | 検証で分かったこと |
|---|---|
| 「認証は `run_system_startup()` が最初」 | `sitecustomize.py:600-602` から `bootstrap_startup_token()` が先に走る |
| 「未コミット版 `token_bootstrap.py` がトークンを無効化した可能性」 | あの差分は回復挙動を変えるだけで、無効化する処理は無い |

どちらも修正方針を左右する。検証が無ければ、この誤った前提のまま
修正担当へ渡っていた。

同時に、コードから決められないもの（実行時のスレッド順序、実際の時刻）が
`UNVERIFIABLE` として切り分けられ、`enable_token_runtime()` が
**リポジトリのどこからも呼ばれていない**という新事実も出てきた。

### 判定の扱い

| verdict | 扱い |
|---|---|
| `DIAGNOSIS_HOLDS` | `revised_confidence` を採用 |
| `DIAGNOSIS_NEEDS_REVISION` | `revised_confidence` を採用（下がればゲートで落ちる） |
| `DIAGNOSIS_WRONG` | **status を `NEED_MORE_INFO` へ落とし、confidence を 0.3 以下にする**。修正へ進ませない |
| 検証が失敗（`UNVERIFIED`） | confidence を **0.5 に下げる**。安全網が無い状態で自動修正させない |
| 検証が無効（`--no-verify` / `enabled: false`） | 診断をそのまま使う |

`REFUTED` と `UNVERIFIABLE` の内容は、修正担当（Claude Code）の
プロンプトへ **「この前提を使ってはいけません」** として明示的に渡される。

### コスト

診断 1 件あたり Codex の呼び出しが **2 回**になる。
検証するのは `status=FIX_REQUIRED` のときだけで、
`NO_ACTION` / `NEED_MORE_INFO` はどのみち修正へ進まないので呼ばない。

### 未コミット差分の渡し方でしくじった記録（2026-08-29）

最初の実装は「診断が `files` に挙げたファイル」だけの未コミット差分を探し、
見つからなければ **「未コミットの変更はありません」と検証役に断言**していた。

401 の検証で実害が出た。診断は `kabu_api/token_bootstrap.py` を `files` に
挙げなかったが、そのファイルには 170 行の未コミット変更が実在した。
検証役は「変更なし」という誤った前提を渡され、

> 本番の kabu_api/token_bootstrap.py にはHEAD外変更があり、差分確認が必要である

という主張を `REFUTED` にした。**この否認は誤りで、原因は監視側のバグである。**

いまは `_uncommitted_section()` が

1. 作業ツリーの未コミットファイルを**全部**列挙し
2. 診断が挙げたものを先に、それ以外を後ろに置いて
3. `max_diff_chars` まで詰める

という順で渡す。上限に当たっても重要な側は必ず残る。
差分が取れない場合も「変更なし」とは言わず、ファイル名だけでも渡す。

**検証役に嘘の前提を渡すと、検証は嘘の結論を出す。** ここは削らない。

## Codex にできないこと（2026-08-29 に判明）

Codex は read-only サンドボックスで動くため、**実験ができない**。
ログとソースを読んで推論することはできるが、
「リクエストを投げて挙動を比べる」ことはできない。

この日、起動時のトークン検証が必ず 5 秒でタイムアウトし
`main_database.py` が abort し続ける障害が約 2 時間続いた。
原因は `urllib.request` が必ず送る `Connection: close` で、
kabu Station がこのヘッダーの付いたリクエストに応答を返さないことだった。

Codex は 401 の診断で「トークンが無効になった理由はコードからは特定できない」
と `UNVERIFIABLE` を返していた。**これは正しい判断だった。**
答えがコードの外（HTTP ヘッダーと相手サーバーの相性）にあったからである。

特定できたのは `http.client` でヘッダーを 1 つずつ変えて実測したときだけ:

| 送ったヘッダー | 結果 |
|---|---|
| 最小 (X-API-KEY のみ) | HTTP 401 **0.01s** |
| + `Content-Type` | HTTP 401 0.00s |
| + `Accept-Encoding: identity` | HTTP 401 0.00s |
| **+ `Connection: close`** | **応答なし 5.00s** |

### この日の誤診の記録

同じ障害について、実測にたどり着くまでに 3 回外している。

| 立てた仮説 | 実際 |
|---|---|
| ポート除外 18080 が原因 | 除外は kabu Station 自身のもの。無関係 |
| kabu Station が応答していない | `requests` からは 0.01 秒で応答していた |
| `main.py` の飽和が原因 | 単独起動でも同じだった |

ログとコードを読むだけでは届かない障害がある、ということ。
**パイプラインが `NEED_MORE_INFO` や `UNVERIFIABLE` を返したときは、
それ以上ログを読ませても答えは出ない。** 実験に切り替える合図として扱う。

### 追加したトリガ

この障害クラスを次回は機械的に捕まえるため、以下を追加した。

| トリガ | 拾うもの |
|---|---|
| `startup_abort` | `[MAIN DATABASE] abort` — 起動できずに落ちた |
| `token_preflight_failed` | `preflight_ng` / `TOKEN REQUEST FAILED` / `got NO RESPONSE` — abort の一歩手前 |
| `entry_blocked_auth` | `[ENTRY BLOCKED: AUTH]` — 認証異常で発注を止めた |
| `positions_auth_failed` | `[POSITIONS AUTH FAILED]` — 401 を建玉ゼロと誤認する経路 |

実ログで検証済み。障害時のログで 6 件検出、復旧後のログで 0 件。
