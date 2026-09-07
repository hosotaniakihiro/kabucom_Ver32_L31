# TASK_STATUS

---

# 第1ラウンド — P0-1〜P0-6 / P1 対応記録

対象ログ: `console_20260826_132625_75076.log` (117,105 行)
作業日: 2026-08-26
実注文: **0 件** (dry-run / mock のみ。本番発注経路には触れていない)
本番プロセス: **未再起動**。以下の修正はすべて次回再起動で有効になる。

---

## P0-1 NAS SQLite `disk I/O error`

### 原因

本番ログの `disk I/O error` 100 件のうち **90 件** がスタックトップで同じ 1 行を指していた。

```
File "database\session.py", line 185, in set_sqlite_pragma
    cursor.execute(f"PRAGMA cache_size={SQLITE_CACHE_SIZE};")
```

`ENGINE_KWARGS` は `poolclass=NullPool`。つまり **クエリのたびに接続を作って捨てる**。
その接続ごとに connect イベントで PRAGMA を 5 本発行し、さらに
`cache_size=-50000` (= 50MB のページキャッシュ) を確保していた。
NullPool なので接続は再利用されず、この 50MB は一度も使われないまま解放される。

SMB クライアント設定の実測:

```
Get-SmbClientConfiguration -> MaxCmds : 50
```

`MaxCmds` は 1 コネクションで同時に投げられる SMB コマンドの上限。
毎クエリ 5 往復 + 50MB 確保を NAS 越しに投げ続ければここが飽和する。
飽和は SQLite からは `disk I/O error`、Python からは `OSError(24) Too many open files`
として現れる (作業中に私自身のファイルコピーも同じ理由で失敗した)。

エラー発生時刻は 14:44 に 60 件、14:56〜14:59 に 106 件と、場中の高負荷時に集中していた。

### 変更ファイル / 内容

**`database/session.py`**

| 項目 | before | after |
|---|---|---|
| 接続ごとの PRAGMA | 5 本 (`busy_timeout` / `temp_store` / `cache_size` / `synchronous` / `foreign_keys`) | **4 本** (`cache_size` のみ削除) |
| `cache_size` | 毎接続 `-50000` (50MB) | **既定で発行しない** (env `SQLITE_CONN_CACHE_SIZE` を明示したときだけ) |
| `busy_timeout` / `temp_store` / `synchronous` / `foreign_keys` | 毎接続で設定 | **そのまま維持** |
| PRAGMA 失敗時 | `finally` で cursor を閉じるだけ。例外はそのまま上へ | **DB path / pragma 名 / thread / conn_id / elapsed / 例外** をログに出し、接続を破棄 (fail-safe) |
| import | — | `threading` / `time` を追加 |

### 途中で自分が入れた重大な後退 (検出して修正済み)

最初の実装で `synchronous` と `foreign_keys` も
「WAL では DB ファイル側の永続属性だから接続ごとに不要」として connect フックから外した。
**これは誤りだった。**

ローカル一時 DB で実測して確認:

```
接続1で journal_mode=WAL / synchronous=NORMAL / foreign_keys=ON を設定
 -> 接続2 で読み直すと
     journal_mode = wal   (永続。DB ヘッダに入る)
     synchronous  = 2     (FULL。NORMAL は引き継がれない)
     foreign_keys = 0     (OFF。ON は引き継がれない)
 -> さらに foreign_keys=0 の状態で FK 違反の INSERT がそのまま通った
```

永続するのは `journal_mode` だけ。外した影響は

- `synchronous` NORMAL → **FULL**: 書き込みごとの fsync が増え SMB 越しでは遅くなる (目的と逆)
- `foreign_keys` ON → **OFF**: **FK 制約が強制されなくなる** (データ整合性の後退)

どちらも意図した変更ではないので戻した。削るのは `cache_size` だけ。
これは本番ログで実際に落ちていた行であり、かつ NullPool では
一度も再利用されないため、外しても失うものが無い唯一の PRAGMA。

#### 同じ時期に観測した page error (**原因は特定できず / 私の変更とは結び付かなかった**)

`test_rotation_and_ranking_flush.py::test_resolve_register_targets_does_not_raise_name_error`
が **`Windows fatal exception: page error`** でプロセスごと落ちていた。

```
Windows fatal exception: page error
  sqlalchemy/engine/default.py, line 952 in do_execute
  database/session.py, line 287 in _table_exists
  database/session.py, line 473 in _bootstrap_summary_schema
  database/session.py, line 544 in init_engines
  trading/position/open_position_sync.py, line 216 in load_open_positions_from_db
  ...
  trading/push/push_stream/rotation_stability.py, line 359 in resolve_register_targets
```

PRAGMA を戻したあと同じテストを再実行 → **1 passed (490.68s)**。

このテストは NAS の銘柄解決を大量に叩くため素で 8 分かかる。
最初にこれを「ハングしている」と判断したのは誤りで、
2 分のタイムアウトで打ち切っていただけだった。

**ただし、これを「PRAGMA の後退が原因」と断定してはいけない。**
クラッシュ前 1 回 / 修正後 1 回の観測しかなく、しかも NAS は不安定である。

そこで A/B の切り分けを行った。修正後の実装は `cache_size` 以外すべて元どおりなので、
`SQLITE_CONN_CACHE_SIZE=-50000` を与えれば元の 5 PRAGMA を完全再現できる。
クラッシュしたスタックと同じ経路
(`get_protected_symbols` → `sync_open_positions_from_db` → `init_engines`
 → `_bootstrap_summary_schema` → `_table_exists`) を別プロセスで実行した結果:

| 条件 | exit | page error |
|---|---|---|
| NEW (4 PRAGMA / cache_size なし) | 0 | **なし** |
| OLD 相当 (5 PRAGMA / cache_size=-50000) | 0 | **なし** |

**どちらの構成でも再現しなかった。**
したがって page error は PRAGMA の構成とは結び付かず、
NAS/SMB 側の一過性の障害 (`STATUS_IN_PAGE_ERROR` = マップしたページを
バッキングストアから読めなかった) と見るのが妥当。
`disk I/O error` と同じ SMB 飽和の症状群に属する可能性は高いが、
今回の変更が原因であるという根拠は無い。

PRAGMA を戻した理由はあくまで
**`foreign_keys` が OFF になり FK 制約が強制されなくなっていたこと** であり、
page error とは独立した話である。

修正後の検証 (`p1fix_test.py` 14/14 OK):
実際の SQLite 接続にフックを当てて `synchronous=1(NORMAL)` / `foreign_keys=1(ON)` /
`busy_timeout=60000` を確認し、**FK 違反の INSERT が `FOREIGN KEY constraint failed` で
弾かれること**まで確認した。

新しいログ形式:

```
[DB PRAGMA] failed db=X:\data\summary_20260826.db pragma=busy_timeout
            thread=MainThread conn_id=2098358346576 elapsed=0.000s
            err=OperationalError('disk I/O error')
```

例外は握り潰さず再送出する (`SQLITE_PRAGMA_FAILSAFE=0` で従来動作へ戻せるが既定は fail-safe)。

### before / after 実測

単一接続のコスト (`X:\raw_data\yahoo\stock_data1.db`, 20.5GB, 15 回):

| | median | mean | max | 合計(15回) |
|---|---|---|---|---|
| OLD (5 PRAGMA + cache_size) | 0.0712s | 0.0676s | 0.1133s | 1.014s |
| NEW (2 PRAGMA) | **0.0510s** | 0.0494s | 0.0841s | **0.741s** |

負荷テスト (24 スレッド × 15 秒 × 3 ラウンド、OLD/NEW をラウンドごとに入れ替えて順序バイアスを除去、
本番 main.py 稼働中、ranking + push DB)。
**修正後の NEW = 4 PRAGMA (`cache_size` のみ削除)** で再測定:

| | 接続成功数 | disk I/O error | OSError | median | p95 | max |
|---|---|---|---|---|---|---|
| OLD (5 PRAGMA) | 11,669 | 0 | 0 | 0.0195s | 0.0968s | 4.2219s |
| NEW (4 PRAGMA) | **17,967** (**1.54倍**) | 0 | 0 | **0.0158s** (-19.1%) | **0.0519s** | 3.3723s |

`synchronous` / `foreign_keys` を戻した 4 PRAGMA 版のほうが、
外しすぎた 2 PRAGMA 版 (1.13倍 / -11.7%) より **良い数字が出た**。
効いているのは PRAGMA の本数ではなく、**50MB のページキャッシュ確保をやめたこと**だと分かる。
p95 が 0.0968s → 0.0519s とほぼ半減しているのが大きい。

### DB I/O テスト結果についての注記 (重要)

**この負荷テストでは OLD / NEW どちらでも `disk I/O error` を再現できなかった (両方 0 件)。**
テスト実施時刻は 15:40 以降、つまり **大引け後** でありプロセス側の NAS 負荷が
エラー発生時 (14:44〜14:59) より大幅に低い。したがって

- 「PRAGMA を減らすとスループットが上がり接続時間が下がる」ことは実測で確認済み
- 「これで `disk I/O error` が 0 になる」ことは **今回の負荷テストでは実証できていない**

`cache_size` が失敗箇所であるという根拠は本番ログのスタック 90/100 件であり、
負荷テストではない。**30 分の場中負荷テストは次営業日の場中に必要**。

### 併せて発見した問題 (未対応 / 要判断)

**WAL が checkpoint されずに肥大している。**

```
ranking20260826.db      464.3 MB
ranking20260826.db-wal  845.9 MB   ← 本体より大きい
ranking20260824.db-wal  867.8 MB   ← 8/24 の残骸が残っている
push20260826.db-wal      57.2 MB
```

ネットワーク共有上で本体より大きい WAL を抱えると、読み取りのたびに
WAL インデックスを走査するため全体が遅くなり、`disk I/O error` も出やすくなる。
checkpoint は書き込み操作なので本番稼働中に私の判断では実行していない。

**`Set-SmbClientConfiguration -MaxCmds` の引き上げ**も、この症状に対する標準的な対処だが
マシン全体に効く管理者権限の変更なので実施していない。どちらも判断を仰ぎたい。

---

## P0-2 1 分 Summary worker のハング

### 原因

`generation=18 owner=tick:20260826145200` が 14:56 時点で `age=241秒`、
`done=False running=True cancelled=False thread_alive=True`。

ワーカーが **どのステージで止まったのかログから判別できなかった**。
`_job_one_source` は `job_summary()` を丸ごと呼ぶだけで、内部の段の計測が無かった。
scheduler 側の inflight メタデータを reclaim しても、ワーカー本体は走り続ける。

### 変更ファイル / 内容

**`scheduler_jobs/summary/stage_tracker.py` (新規)**

- `worker(kind, generation, owner, budget)` — ワーカーの生存期間。既定予算 **25 秒**
- `stage(name)` — 1 ステージの開始/終了/elapsed を generation 付きで出す
- `should_stop(label)` — 予算超過 / cancel を worker 自身が確認して降りる
- `cancel(ctx, reason)` — 外から協調キャンセルを要求 (Thread は殺さない)
- `snapshot()` — 現在のステージ内訳

**`scheduler_jobs/summary/parallel_intervals.py`**
`_job_one_source` を `stage_tracker.worker(...)` で包む。generation = inflight token、
owner = `tick:YYYYMMDDHHMMSS`。`finally` の `_inflight_release` は従来どおり維持。

**`scheduler_jobs/summary/runner_core.py`**
`job_summary` / `_job_summary_original_heavy` に 8 ステージを計測:

```
tier1_direct_memory / tier2_light_runner / push_runner / normalize /
filter_push_like / db_fallback / db_write / entry_dispatch / display
+ freshness_timeline (P0-6)
```

協調停止を入れた位置: `after_tier1` / `after_tier2` / **`before_db_fallback`**。
NAS DB fallback の手前で降りるのが要点で、ここが一番重い。

**Entry dispatch は予算切れでも実行する。** ここを飛ばすと注文が消えるため、
降りるのは NAS I/O の手前までに限定した。

### ログ出力例

```
[SUMMARY STAGE] worker begin kind=push:1 generation=18 owner=tick:20260826145200
                thread=... tid=... budget=25.0s
[SUMMARY STAGE] stage_end name=push_runner kind=push:1 generation=18
                elapsed=0.052s total=0.104s remaining=24.896s
[SUMMARY STAGE] cooperative stop kind=push:1 generation=18 owner=... label=before_db_fallback
                reason=budget_exceeded requested_reason=... elapsed=25.1s budget=25.0s
                thread=... stages=[tier1=..,tier2=..,push_runner=..]
[SUMMARY STAGE] worker end kind=push:1 generation=18 total=25.1s over_budget=True
                stopped_reason=budget_exceeded stages=[...]
```

### テスト結果

- `p2_test.py` 単体 37/37 OK
- `p2_int.py` 統合 (実 `job_summary` を stub 付きで実行) 30/30 OK
  - 8 ステージすべての elapsed が generation 付きで出る
  - 予算超過時に NAS DB fallback 呼び出し = **0 回** (従来は必ず呼んでいた)
  - 予算内なら fallback は従来どおり動き、Entry dispatch も実行される
- 計測オーバーヘッド: 200 回で計測なし 0.0221s / 計測あり 0.0147s → **実質ゼロ**

---

## P0-3 timeout 後の daemon worker

### 原因

```
[RANKING ENTRY SCHEDULE] hard timeout seq=45 timeout_sec=28.0s;
    return 0 to release scheduler slot, worker continues in daemon thread
```
ログ中 16 回。`elapsed=317.370s` / `197.484s` の worker が生き残っていた。

決定的だったのは **内側の `RANKING ENTRY BUILD` に `guard_kind` が付いていなかった**こと。
外側 (`RANKING_ENTRY`) を cancel しても、build スレッドだけが世代管理の外で走り続けていた。

### 変更ファイル / 内容

**`trading/entry_exit/tasks.py`**

1. `_entry_worker_should_stop(label)` を追加。worker_guard の `check()` を呼び、
   worker 自身が return 判断をする (外から Thread を kill しない)
2. 内側 build に `guard_kind="RANKING_ENTRY_BUILD"` を付与
3. `_run_ranking_entry_safe_body` のフェーズ間に協調チェックを 3 箇所:
   - `ranking_loop_start`
   - `ranking_before_build` (重い build に入る前)
   - `ranking_after_build` — ただし **`created<=0` かつ `after_pending<=0` のときだけ**。
     pending があるときは止めずに dispatch する (注文を消さないため)
4. `_dispatch_entry_controller` に `guard_kind="ENTRY_CONTROLLER:<source>"` を付与。
   同一 source の controller が 2 本同時に走らない。source ごとに分けているので
   RANKING と TONOSAMA は干渉しない。
   **controller dispatch 自体には停止チェックを入れていない** — ここは pending を
   発注へ渡す経路なので、上流の予算切れを理由に飛ばすと注文が消える
5. hard timeout のログを実態に合わせて書き換え:

```
before: return 0 to release scheduler slot, worker continues in daemon thread
after : cancel requested to worker (cooperative), scheduler slot released.
        next generation is refused while this one is alive. active=[{...}]
```

これで `_run_callable_with_timeout` / `_run_callable_with_timeout_thread` の
**全呼び出し 5 箇所に `guard_kind` が付いた** (テストで確認)。

### テスト結果

`p3_test.py` 29/29 OK

- timeout した worker は cancel を受け取り、200 フェーズ中 **16 で自分から降りた**
- 前世代が生きている間は同種 worker を起動しない (`previous_generation_alive`)
- `on_refused` でロックが解放される (デッドロック回帰防止)
- 前世代が終われば次が普通に起動する
- 別 source の controller は干渉しない
- `guard_kind` 無し / worker_guard 無しでも従来どおり動く

---

## P0-4 Entry hot path から NAS DB full scan を除外

### 原因

`_rsrc_load_from_ranking_db_core` は候補テーブルを **全部**
`SELECT * ... ORDER BY rowid DESC LIMIT 1500` で読み、その後で一番新しいものを選んでいた。
NAS 混雑時に `deadline hit elapsed=120.2s`。30 秒予算に対して論外。

### 測定でわかったこと (当初案は誤りだった)

`X:\raw_data\kabu_station\ranking\ranking20260826.db` (464MB / WAL 846MB) 実測:

| テーブル | latest | `SELECT * ORDER BY rowid DESC LIMIT 1500` | `WHERE datetime>=MAX LIMIT 900` |
|---|---|---|---|
| ranking_snapshot_1min | 15:20 | 0.022s | 0.015s |
| ranking_raw_1min | 15:20 | 4.327s | **0.097s** (40倍速) |
| ranking_summary_1min | 14:01 | 0.948s | **5.048s** (遅くなる) |
| ranking_summary_5min | 11:25 | 0.756s | **5.528s** (遅くなる) |

**「`WHERE datetime>=?` を使えば常に速い」は誤り。**
`ranking_summary_1min` の `idx_..._datetime` は covering ではないため
1 行ごとにランダム参照が発生し、SMB 越しでは rowid 順の連続読みより遅い。

本質は **「どのテーブルを読むか」を先に決めること**。`MAX(datetime)` は
covering index scan なので 0.004〜0.059 秒と極めて安い。

### 変更ファイル / 内容

**`trading/ranking/entry_from_ranking.py`**

1. `_rsrc_latest_per_table()` — 各テーブルの `MAX(datetime)` だけを取る (合計 0.138s)
2. `_rsrc_load_pick_latest()` — 最新のテーブルを **1 本** に絞る
3. `_rsrc_read_one_table()` — その 1 本だけを LIMIT 付きで読む。
   index 経路を先に試し、**予算超過なら rowid 経路へ落とす**
4. `_rsrc_budget_guard()` — SQLite の `set_progress_handler` で
   クエリに **実時間の上限 (既定 2.0 秒)** を掛ける。
   超えたら SQLite が `interrupted` を投げるので、握り潰さず理由をログに出して安い経路へ

env: `RANKING_ENTRY_SOURCE_DB_PICK_LATEST_TABLE=0` で従来動作へ戻せる。

### before / after 実測

| | 現行 (全テーブル走査) | 新 (最新1本) |
|---|---|---|
| 単発 | 2.137s | **0.452s** (4.7倍) |
| 連続 5 回 | — | 0.048 / 0.093 / 0.070 / 0.109 / 0.060 → **max 0.109s** |
| 取得データの最新時刻 | 15:22 | **15:22 (同一)** |

**古いデータを掴んでいないことを確認済み** — 現行方式と同じテーブル・同じ最新時刻を選ぶ。

予算超過時のふるまい (`ranking_summary_1min`, budget=0.3s):

```
indexed read aborted table=ranking_summary_1min elapsed=0.364s budget=0.3s
    err=interrupted -> fallback to rowid scan
rowid read table=ranking_summary_1min rows=1500 elapsed=0.021s
合計 0.421s / データは取れる
```

### インデックス確認

| テーブル | datetime index | symbol+datetime |
|---|---|---|
| ranking_snapshot_1min | あり | あり |
| ranking_raw_1min | `idx_ranking_raw_1min_datetime` (covering で効く) | `(ranking_type, market, datetime)` |
| ranking_summary_1min | `idx_..._datetime` | `uq_..._symbol_datetime` (UNIQUE) |
| ranking_summary_5min | `idx_..._datetime` | `idx_..._symbol_datetime` |

必要なインデックスは揃っている。**追加は不要**。問題はインデックスの有無ではなく
「全テーブルを読んでいたこと」だった。

### テスト結果

`p4_test.py` (本番 DB に対して読み取り専用で実行) 21/21 OK

---

## P0-5 PUSH の設計統一

### 結論: 現在の構成は **意図どおり**。変更していない。

```
[PUSH STREAM] db_write=False order_book_write=False memory_only=True
              flush_alive=False writer_ready=False total_flushed=0
```

`data_collectors/split_mode.py` `_main_memory_only_enabled_core()`:

> main.py をエントリー専用・メモリ専用として扱うか。
> **V6 既定では main.py は DB writer を持たない。**

つまり V6 の分離設計そのもの:

- `main.py` = Entry 専用 / メモリのみ / DB writer を持たない
- `main_database.py` → `push_receiver_runner` = DB writer

**writer 側が実際に書いていることを確認済み**:
`push20260826.db` の更新時刻 15:25:06 (確認時刻 15:25:36)。止まっていない。

main.py で `db_write=True` にすると二重書き込みになり、
「main.py へ NAS 重処理を戻さない」という方針にも反するので変更しない。

### ただし指摘された「中途半端に writer OFF / reader DB 依存」は実在する

main.py は PUSH を書かない (正しい) が、**読む側が DB に落ちる**経路が残っていた。
これは前ラウンドの `push_df` writeback (`memory_latest_1m.py`) と
今回の P0-4 (DB fallback を 2 秒予算に収める) で対処済み。
どの段で memory から DB に落ちたかは P0-6 のタイムラインで可視化される。

---

## P0-6 Summary 鮮度タイムライン

### 変更ファイル / 内容

**`scheduler_jobs/summary/freshness_timeline.py` (新規)**
**`scheduler_jobs/summary/parallel_intervals.py`** — summary tick から呼び出し (既定 20 秒に 1 回へ間引き)

8 段の最新時刻を 1 行にまとめる:

```
[FRESHNESS] now=15:25:30
  websocket_latest=15:25:28(age=2.0s)
  memory_push_latest=15:25:27(age=3.0s)
  push_db_latest=15:25:00(age=30.0s)
  raw_df_latest=15:25:00(age=30.0s)
  1m_ohlc_latest=15:25:00(age=30.0s)
  summary_history_latest=15:24:30(age=60.0s)
  merged_summary_latest=15:24:30(age=60.0s)
  reason=push:1 -> all within target
```

目標を超えた段に `!` が付き、行全体が WARNING になる:

```
... summary_history_latest=14:30:00(age=3330.0s)
    merged_summary_latest=14:09:00(age=4590.0s)!
    -> OVER_TARGET=merged_summary_latest(age=4590.0s>90s)
```

目標値 (完成条件どおり):

| 段 | 目標 |
|---|---|
| websocket_latest | < 5 秒 |
| memory_push_latest | < 5 秒 |
| 1m_ohlc_latest | < 90 秒 |
| merged_summary_latest | < 90 秒 |

**鮮度判定 (STALE DROP 等) の挙動は一切変更していない。表示だけを足した。**

### テスト結果

`p6_test.py` 37/37 OK。本番の症状 (merged が 14:09 で止まる) を再現させると、
上流 (WebSocket age=2.0s) が正常で merged だけが落ちていることが 1 行で読める。
1 段が例外を投げても他の段は出る。

---

## P1 candidate_id による一貫追跡

### 原因

`approved_count=1 pending_moved_without_order` は
`_normalize_run_entry_pipeline_result` が **pending の件数の差** だけを見て
「減ったのに発注していない」と推定していた。

```python
approved_count = max(0, before_pending - after_pending, after_inflight - before_inflight)
...
elif pending_decreased:
    skip_reason = "pending_moved_without_order"
```

消えた候補そのものを追っていないので、**どの処理が pending を消費したのか特定できなかった**。

### 変更ファイル / 内容

**`trading/entry/candidate_id.py` (新規)**
`ensure()` / `note()` / `trace()` / `summary()` / `log_vanished()` / `collect_ids()`

**`trading/entry/pending_manager.py`**
`_prepare_new_entry` で `candidate_id.ensure(out, stage="pending_created")`。
上流 (Ranking) で採番済みならその ID を維持する。

**`trading/handlers/entry_controller.py`**
1. `_entry_timeline_log` に `candidate_id=` を追加し、同時に `candidate_id.note_item` で記録
2. `_pipeline_pending_candidate_ids()` を追加 — pending の ID 集合を取る
3. `_normalize_run_entry_pipeline_result(..., before_ids=...)` — 実行前後の ID 集合の差から
   **消えた候補を特定**して出す。結果 dict に `vanished_candidate_ids` を追加

```
[CANDIDATE VANISHED] id=RANKING-141A-20260826152917-2 last_stage=gate_approved
    age=12.3s reason=pending_moved_without_order
    stages=[pending_created(+0.0s),controller_picked(+2.1s),gate_approved(+11.9s)]
```

ID が付く前に消費された場合も、それと分かるログを出す。

**従来の判定は一切変更していない** — `skip_reason` / `approved_count` /
`pending_moved_without_order` はそのまま。記録を足しただけ。

### dry-run Entry timeline (実注文 0 件)

`place_entry_buy` をモックに差し替え、実際の
`_prepare_new_entry` / `_entry_timeline_log` / `_execute_candidate_stale_ok` を実行:

```
candidate_id = RANKING-141A-20260826154805-1

  1. ranking_candidate    +0.00s  symbol=141A source=RANKING
  2. pending_created      +0.00s  symbol=141A source=RANKING
  3. controller_picked    +0.00s  candidate_age=0.0s  bar_age=30.0s
  4. gate_approved        +0.00s  candidate_age=0.0s  bar_age=30.0s
  5. executor_reached     +0.00s  candidate_age=0.0s  bar_age=30.0s
  6. pre_sendorder        +0.00s  candidate_age=0.0s  bar_age=30.0s  qty=100
  7. dry_run_order        +0.00s  order_id=MOCK-ORDER-1 qty=100

freshness: candidate_age=0.0s bar_age=30.0s -> ok=True
実注文 : 0
mock   : 1
```

安全側の挙動が維持されていることも確認:

```
300 秒前の候補 -> candidate_age=300s -> ok=False
[ENTRY EXEC TIMEOUT GUARD] STALE_CANDIDATE_SKIP reason=candidate_age
止まった候補は発注へ行かない (mock 発注数は 1 のまま)
```

### テスト結果

- `p1c_test.py` 単体 44/44 OK
- `p1_e2e.py` 統合 (実 `_prepare_new_entry` / `_entry_timeline_log` /
  `_normalize_run_entry_pipeline_result`) 35/35 OK
- `p_dryrun.py` dry-run 27/27 OK

### dry-run で見つけて直した自分の不具合

`ensure()` が「ID が既にある場合」に早期 return していたため、
Ranking で採番済みの候補では **`pending_created` が trace から抜け落ちていた**。
どの段でも必ず記録するよう修正。

---

## 変更ファイル一覧

| ファイル | 種別 | 対応 |
|---|---|---|
| `database/session.py` | 変更 | P0-1 |
| `scheduler_jobs/summary/stage_tracker.py` | **新規** | P0-2 |
| `scheduler_jobs/summary/runner_core.py` | 変更 | P0-2 |
| `scheduler_jobs/summary/parallel_intervals.py` | 変更 | P0-2 / P0-6 |
| `scheduler_jobs/summary/freshness_timeline.py` | **新規** | P0-6 |
| `trading/entry_exit/tasks.py` | 変更 | P0-3 |
| `trading/ranking/entry_from_ranking.py` | 変更 | P0-4 |
| `trading/entry/candidate_id.py` | **新規** | P1 |
| `trading/entry/pending_manager.py` | 変更 | P1 |
| `trading/handlers/entry_controller.py` | 変更 | P1 |

---

## 既存テストの回帰確認

| ファイル | 結果 |
|---|---|
| test_summary_tick_backpressure.py | 20 passed |
| test_entry_pipeline.py / test_ai_entry_gate.py / test_no_undefined_names.py / test_score_breakdown_flags.py | 17 passed |
| test_summary_contract.py / test_summary_loader.py / test_summary_loader_basic.py / test_push_dataframe_concat.py / test_ranking_enrich_cache.py / test_summary_full_pipeline.py / test_summary_full_pipeline_3m_5m.py | 14 passed, 4 skipped |
| test_runtime_system.py / test_ranking_ma_slopes.py / test_ranking_turnover_units.py / test_daily_mtf_cache.py / test_bar_cache.py | 23 passed |
| test_daily_mtf_stale_warning.py / test_unified_close_ma.py / test_bool_series.py | 28 passed |
| test_force_exit.py / test_trigger.py | 1 passed |
| test_rotation_and_ranking_flush.py::test_ranking_flush_writes_rows (DB 書き込み経路 = session.py を通る) | 1 passed |
| test_rotation_and_ranking_flush.py::test_rdl_type_counts_returns_counts | 1 passed |

**合計 106 passed, 4 skipped, 0 failed**

PRAGMA 修正後に主要スイートを再実行して再確認済み
(summary tick / entry pipeline / AI gate = 22 passed、
summary contract / loader / full pipeline / no_undefined_names / score_breakdown /
ranking_flush_writes_rows = 18 passed 2 skipped)。

`test_rotation_and_ranking_flush.py::test_resolve_register_targets_does_not_raise_name_error`
は PRAGMA 修正後に **1 passed (490.68s)**。
このテストは NAS の銘柄解決を大量に叩くため素で 8 分かかる。

未確認 2 件: 同ファイルの
`test_resolve_monitor_symbols_does_not_raise_name_error` /
`test_rotation_selects_targets_from_active_symbols`。
同じく 1 件あたり数分かかるため単独完走を確認できていない。
ただし上記の `resolve_register_targets` は内部で `resolve_monitor_symbols` →
`update_active_symbols` を呼んでおり、落ちていた経路はそこなので、
実質的に同じコードパスが通ったことになる。

### 補足 (単体テスト)

今回追加したモジュールの検証スクリプトは scratchpad に残してある:

| スクリプト | 内容 | 結果 |
|---|---|---|
| `p1_test.py` | session.py の PRAGMA 削減 / fail-safe | 21/21 |
| `p1_load2.py` | NAS 負荷 (OLD/NEW 交互 3 ラウンド) | 実測値は P0-1 参照 |
| `p2_test.py` | stage_tracker 単体 | 37/37 |
| `p2_int.py` | 実 job_summary 統合 | 30/30 |
| `p3_test.py` | worker guard / 協調停止 | 29/29 |
| `p4_test.py` | 本番 ranking DB 実測 | 21/21 |
| `p6_test.py` | 鮮度タイムライン | 37/37 |
| `p1c_test.py` | candidate_id 単体 | 44/44 |
| `p1_e2e.py` | candidate_id 実コード統合 | 35/35 |
| `p_dryrun.py` | dry-run Entry timeline | 27/27 |

### 途中で自分が入れた回帰 (検出して修正済み)

`test_job_releases_even_when_body_raises` が
`AttributeError: 'NoneType' object has no attribute 'strftime'` で落ちた。
`_job_one_source` の `now` が `None` で呼ばれる経路があり、診断用の
owner 文字列生成で本処理を巻き添えにしていた。try/except で保護して解消。

---

## 完成条件に対する現状

| 条件 | 状態 |
|---|---|
| 30 分負荷テストで disk I/O error = 0 | **未達** — 大引け後のため場中負荷を再現できず。24スレッド×20秒×3ラウンドでは OLD/NEW とも 0 件。次営業日の場中に要実施 |
| Summary 1m worker < 10 秒 (目標 < 5 秒) | 予算 25 秒で協調停止する仕組みを実装。**実測は本番再起動後** |
| 30 秒超 worker = 0 | 同上。予算超過で自分から降りる |
| timeout 後の daemon worker = 0 | 全 5 箇所に guard_kind、フェーズ間に協調チェック。テストで 200→16 フェーズで降りることを確認 |
| Ranking DB fallback < 2 秒 | **達成** 実測 max 0.109s (連続5回) / 単発 0.452s |
| Ranking Entry 全体 < 10 秒 | DB 部分は達成。全体の実測は本番再起動後 |
| Tonosama < 10 秒 | 実測は本番再起動後 |
| WebSocket latest age < 5 秒 | タイムラインで可視化。実測は本番再起動後 |
| memory PUSH latest age < 5 秒 | 同上 |
| merged 1m latest age < 90 秒 | 同上 |
| 3m/5m も最新完成足 | 前ラウンドの `long_tf_db_publish.py` で対応済み。実測は再起動後 |
| candidate_id 追跡 | **達成** dry-run で 7 段すべて同一 ID |
| dry-run で order executor 直前 / mock order まで | **達成** |
| 実注文を出さない | **達成 (0 件)** |

---

## 次にやるべきこと

1. **本番再起動** — P0-1〜P0-6 / P1 および前ラウンド分がすべて未反映
2. **次営業日の場中に 30 分負荷テスト** — disk I/O error = 0 の確認 (今回は再現条件が作れなかった)
3. **WAL の checkpoint 方針** — `ranking*.db-wal` が 846MB。書き込み操作なので判断待ち
4. **`Set-SmbClientConfiguration -MaxCmds`** — マシン全体に効く管理者権限の変更なので判断待ち
5. 再起動後、実測ログで各 worker の elapsed / 各 timestamp を再確認


---

# 第2ラウンド — P0-1〜P0-5 / P1

対象ログ: `console_20260826_151027_115684.log` (22,011 行 / 15:10:27〜15:32:15)
作業日: 2026-08-26
実注文: **0 件** (dry-run / mock のみ)

---

## 前提: 本番に載っていたコードの範囲

本番プロセスは **15:10:27** 起動。前ラウンドの成果のうち、この時刻より前に
配置できていたものだけが動いていた。

| 修正 | 配置時刻 | このログで動いていたか |
|---|---|---|
| `database/session.py` PRAGMA | 15:07 | **動いていた** → disk I/O error 0 件 |
| `worker_guard` (Ranking Entry) | 前ラウンド | **動いていた** → Ranking Entry 0.12s |
| `stage_tracker.py` | 15:12 | ×(起動後) |
| `runner_core.py` stage | 15:13 | × |
| `tasks.py` P0-3 | 15:16 | × |
| `entry_from_ranking.py` P0-4 | 15:22 | × |
| `freshness_timeline.py` | 15:27 | × |
| `pending_manager` / `entry_controller` candidate_id | 15:29-15:30 | × |
| `parallel_intervals.py` | 15:32 | × |

ログに `[SUMMARY STAGE]` / `[FRESHNESS]` / `[CANDIDATE]` が 0 件なのはこのため。
**今回の残件は、前ラウンドの stage 計測が載る前の状態で観測されたもの。**

また、本番が載せている `session.py` は **2 PRAGMA 版** (foreign_keys OFF)。
その後 16:15 に 4 PRAGMA 版へ修正したので、**FK 強制の復旧には再起動が必要**。

---

## P0-2 SUMMARY SETTER ENRICH 67.80 秒 (最大の原因だった)

### 実測した値

ユーザー報告の 67.80s よりさらに悪い値が出ていた。

| context | rows | elapsed |
|---|---|---|
| global-set_merged_summary | 446 | **217.38s** |
| global-set_push_merged_summary | 126 | **67.80s** |
| global-set_push_merged_summary | 102 | 17.19s |
| global-set_push_merged_summary | **871** | **0.96s** |
| global-set_push_merged_summary | 118 | 0.36s |

**行数と時間が相関していない。** 871 行が 0.96 秒で、126 行が 67.8 秒。
待っていたのは行数ではなく NAS だった。

### 原因

```
_setter_enrich -> sc._enrich -> enrich_summary_latest
  -> attach_ranking_for_display -> _get_ranking_source_df
  -> _read_ranking_df_from_db -> _read_ranking_df_from_db_uncached
```

ここが NAS 上の ranking DB へ **同期で** これを投げていた:

```sql
SELECT * FROM ranking_snapshot_1min ORDER BY datetime DESC LIMIT 20000
```

`ranking_snapshot_1min` は **108 列**。20,000 行 × 108 列を SMB 越しに引く。
キャッシュ TTL は 30 秒なので、毎分の Summary で必ず 1 回は実読みが走る。

enrich が実際に使う列は **8 種類** (symbol / rank_type / rank_position /
change_rate / turnover / trading_volume / market / datetime) とその別名だけ。
`build_ranking_aggregate` が見る列も全部この別名集合の中にある (ソース確認済み)。

### 変更内容 — `trading/summary/controller_enrich.py`

1. **列を絞る** — `_ranking_select_columns()`。
   `_build_ranking_enrich_df` と `build_ranking_aggregate` が探す別名を全部含めるので、
   **行の集合も選ばれる列も変わらない**。1 つも当たらなければ `SELECT *` へフォールバック。
2. **hot path で同期実読みしない** — キャッシュがあれば TTL 切れでもまず返し、
   更新は background worker (single-flight) に投げる。
3. **クエリに実時間の上限** — `set_progress_handler` で既定 3 秒。
   超えたら握り潰さず理由を出す。
4. **stale を使い続けない** — `SUMMARY_ENRICH_RANKING_STALE_MAX_SEC` (既定 300 秒) を
   超えたキャッシュは同期で読み直す。
5. **空読みで既存キャッシュを潰さない**。
6. **段を割って計測** — `[SUMMARY ENRICH STAGE]` に daily_mtf / ranking / total、
   `[SUMMARY ENRICH STAGE][RANKING]` に source_load / aggregate / merge。

### before / after 実測

列の投影 (交互実行で順序バイアスを除去、6 往復):

| | median | rows | cols |
|---|---|---|---|
| 現行 `SELECT *` | 0.246s | 20000 | 108 |
| 投影 | **0.062s** | 20000 | **19** |

**4.0 倍**。最新スロットの内容は完全一致 (rows=1355 / symbols=549 / ranking types=7)。

hot path (TTL 切れ・stale_max 内) の実測:

| | elapsed |
|---|---|
| 本番ログ (最悪) | **217.38s** |
| 本番ログ | 67.80s |
| 修正後 hot path 単発 | **0.0027s** |
| 修正後 hot path 連続 10 回 | **max 0.0046s / median 0.0012s** |

注: 4.0 倍という数字は静穏時のもの。測定時点で WAL が checkpoint 済み (846MB→0MB) で、
本番が 67.8s/217.4s を出していた混雑時とは条件が違う。
**本質的な効果は倍率ではなく「hot path が NAS を待たなくなったこと」**。

### テスト

- `q2_test.py` 実 DB に対して 33/33 OK
- 既存 `test_ranking_enrich_cache.py` を更新して **12 passed** (元は 6 テスト)

---

## P0-1 Summary 1m worker のハング

### 原因

```
generation=6 owner=tick:20260826152900
15:30  age=60.1   done=False running=True cancelled=False thread_alive=True
15:31  age=120.2  done=False running=True cancelled=False thread_alive=True
```

**`cancelled=False` が本質。** parent は 25 秒で timeout しているのに、
ワーカーには「降りろ」が一切伝わっていなかった。

前ラウンドで `stage_tracker.cancel()` は作ったが、**それを呼ぶ側が居なかった**。
Ranking Entry 側は `worker_guard.begin()` が前世代へ cancel を送るので効いていた
(だから Ranking Entry は 0.12s で健全)。同じ仕組みを Summary にも入れる。

### 変更内容

**`scheduler_jobs/summary/parallel_intervals.py`**

1. `_InflightEntry` に `ctx` / `cancel_requested` を追加
2. `_inflight_attach_ctx()` — ワーカー開始時に stage_tracker の ctx を登録
3. `_inflight_request_cancel()` — parent から協調キャンセルを送る (Thread は殺さない)
4. 次 tick が busy を見つけたら、**回収の前に必ず cancel を送る**
5. `_inflight_diagnose` に `cancel_requested` / `ctx_cancelled` / `stopped_reason` /
   `stages` / `elapsed` を追加 → どの stage に居るかがログで分かる

**`global_state.py`**

6. `_setter_enrich` の入口で `should_stop()` を確認。
   cancel 済みなら enrich に入らない (NAS を触る手前の最後の降り場所)
7. setter を `stage_tracker.stage(context)` で計測

### 併せて見つけて直した欠陥

`_inflight_try_reclaim` の `never_submitted` / `queued_but_executor_gone` に
**`thread_alive` の確認が無かった**。future の attach に失敗しただけで
ワーカー本体は走っている、という状態で登録を回収すると二重起動になる。
両方に `and not info.get("thread_alive")` を追加。

### テスト (`q1_test.py` 31/31 OK)

本番症状を再現したワーカーに対して:

| | before | after |
|---|---|---|
| worker が回った stage 数 | 600/600 (止まらない) | **6/600 で自分から降りた** |
| `cancelled` | False | **True** |
| `cancel_requested` | — | **True** |
| `stopped_reason` | — | `superseded_by_tick:next` |

- Thread は kill していない (worker が自分で return)
- 生きている worker の登録は回収しない / 二重起動もしない (維持)
- 世代違いの cancel は無視する

---

## P0-3 RANKING SUMMARY BG 239 秒

### 原因

```
15:29  bg_still_running elapsed=179.559s
15:30  bg_timeout      elapsed=239.621s timeout_streak=3 cooldown_sec=90.0
```

timeout を宣言するだけで worker は放置されており、180 秒 timeout 後も
239 秒まで走り続けていた。さらに:

```
[RANKING SUMMARY BG] core ranking job start at=2026-08-26 15:18:10
(done が 1 度も出ない)
```

### 変更内容

**`core/startup/scheduler/bootstrap.py`**

1. `_ranking_bg_task` を `stage_tracker.worker("ranking_summary_bg", ...)` で包む。
   generation / deadline / cancel を持たせる。予算は既定 60 秒
   (`RANKING_SUMMARY_BG_BUDGET_SEC`)。既存 timeout 180 秒より内側なので
   **timeout を伸ばす修正ではない**
2. `ranking_bg_request_cancel()` を追加
3. timeout 判定時に **cooldown へ入る前に必ず cancel を送る**

**`trading/ranking/summary/runner.py`**

4. `import time` を追加 (無かった)
5. `[RANKING SUMMARY STAGE]` で `base_df` (ranking DB resolve/read/7 type load/aggregate) と
   `technical` を分けて計測。3 秒超は WARNING
6. base_df の後に協調停止チェック

---

## P0-5 3m/5m が止まる (根本原因を特定)

### publisher は正しかった

```
LONG TF DB PUBLISH interval=5 no publish reason=stale:age=3000.7s>max=1200.0s
                              latest=2026-08-26 14:40:00
```

publisher は「古いから publish しない」と正しく言っているだけ。
**そもそも 5m が作られていなかった。**

DB を直接確認 (`ranking_summary20260826.db`, mtime=15:26:28):

| テーブル | latest |
|---|---|
| ranking_summary_1min | 2026-08-26 14:44:00 |
| ranking_summary_3min | 2026-08-26 14:42:00 |
| ranking_summary_5min | **2026-08-26 14:40:00** |

### 原因: modulo ゲートと cooldown のずれ

`_job_ranking_summary_all_core` は BG 開始時刻の `now` を 1 回だけ取り

```python
if force or now.minute % 3 == 0:  -> 3m
if force or now.minute % 5 == 0:  -> 5m
```

BG は timeout/cooldown でスケジュールがずれる。実測の起動時刻:

| 起動 | minute | %3 | %5 | 3m | 5m |
|---|---|---|---|---|---|
| 15:18:10 | 18 | 0 | 3 | ○ | × |
| 15:22:10 | 22 | 1 | 2 | × | × |
| 15:26:10 | 26 | 2 | 1 | × | × |
| 15:32:10 | 32 | 2 | 2 | × | × |

**1 度も `minute%5==0` に当たっていない。**
cooldown でずれた起動時刻が modulo の格子から外れ、3m/5m が構造的に飢える。
ログに `[RANKING SUMMARY JOB] start interval=5` が 1 度も出ないのはこのため。

### 変更内容 — `scheduler_jobs/summary/ranking_summary_jobs.py`

「今の分が N で割り切れるか」ではなく
**「前回作った足より新しい完成足があるか」** で判定する。

- `_latest_completed_slot(now, interval)` — 直近の**完成した**足の開始時刻
  (15:32:10 / 5m → 15:25。15:30 の足はまだ確定していない)
- `_long_tf_due()` / `_long_tf_mark_built()`
- 空結果なら「作った」と記録しないので次回また試す
- `[RANKING SUMMARY JOB] long tf due/built/skipped` を出す

**これは閾値の緩和ではない。** 作るべきものを作るようにするだけで、
publisher 側の stale 判定 (`age>max なら publish しない`) はそのまま維持する。

### テスト (`q5_test.py` 24/24 OK) — 本番の起動時刻列で検証

| | 5m が作られた回数 |
|---|---|
| 旧ロジック (15:18/15:22/15:26/15:32) | **0 回** (本番の症状を再現) |
| 新ロジック (同じ時刻列) | **4 回** |

- 同じ完成足なら作り直さない (`slot_unchanged`)
- 次の足が完成したら作る
- `force=True` は常に作る
- env `RANKING_SUMMARY_LONG_TF_SLOT_GATE=0` で旧ロジックへ戻せる

---

## P0-4 1m 鮮度 (P0-2 の下流だった)

### 実測した因果関係

1m tick の elapsed が**極端に二極化**していた:

| 時刻 | rows | latest_dt | elapsed |
|---|---|---|---|
| 15:21:25 | 118 | 15:18:00 | **84.727s** |
| 15:22:00 | 100 | 15:21:00 | 0.730s |
| 15:28:44 | 126 | 15:21:00 | **344.496s** |
| 15:31:04 | 126 | 15:21:00 | **124.207s** |

遅い tick と enrich の時刻を突き合わせると完全に一致する:

```
15:21:20  SETTER ENRICH elapsed=17.19s
15:21:25  job done      elapsed=84.727s      <- 17.19s を含む

15:26:41  SETTER ENRICH elapsed=217.38s
15:28:25  SETTER ENRICH elapsed=67.80s
15:28:44  job done      elapsed=344.496s     <- 217.38 + 67.80 = 285s を含む
```

速い tick (0.730s) の enrich は 0.28〜0.36 秒。

**tick が 84〜344 秒かかると、その間の tick は
`previous_job_still_running` で全部落ちる。** だから 1m latest が 15:21 で止まり
age≈500 秒になっていた。

したがって **P0-4 は P0-2 と P0-1 の下流**。
別個の修正は入れていない。P0-2 (enrich を hot path から外す) と
P0-1 (cancel が worker に届く) で解消する見込み。

鮮度の可視化は前ラウンドの `freshness_timeline.py` (websocket / memory push /
push db / raw df / 1m OHLC / history / merged を 1 行で出す) が担当する。
これは本番未ロードなので、再起動後に実測で確認する。

---

## P1 worker 設計の統一

Ranking Entry で効いている `generation` / `deadline` / `cancelled` / `timed_out` を
他のワーカーへ広げた。

| worker | 仕組み | 状態 |
|---|---|---|
| Ranking Entry | worker_guard | 既存 (0.12s / 健全) — **触っていない** |
| Tonosama | worker_guard | 既存 (0.25〜1.5s) — **触っていない** |
| Summary 1m tick | stage_tracker + inflight ctx cancel | **今回追加** |
| Ranking Summary BG | stage_tracker worker | **今回追加** |
| Entry Controller | worker_guard (source 別) | 前ラウンド追加 |

速くなっている Tonosama / Ranking Entry には手を入れていない。

---

## 変更ファイル一覧 (第2ラウンド)

| ファイル | 対応 |
|---|---|
| `trading/summary/controller_enrich.py` | P0-2 |
| `scheduler_jobs/summary/parallel_intervals.py` | P0-1 |
| `global_state.py` | P0-1 |
| `core/startup/scheduler/bootstrap.py` | P0-3 |
| `trading/ranking/summary/runner.py` | P0-3 |
| `scheduler_jobs/summary/ranking_summary_jobs.py` | P0-5 |
| `test_script/test_ranking_enrich_cache.py` | P0-2 (契約変更に追随) |

---

## テスト結果

### 新規検証

| スクリプト | 内容 | 結果 |
|---|---|---|
| `q2_test.py` | enrich ranking 読み込み (実 DB) | 33/33 |
| `q1_test.py` | Summary worker への cancel 伝播 | 31/31 |
| `q5_test.py` | 長い足のゲート (本番時刻列で再現) | 24/24 |
| `p_dryrun.py` | dry-run Entry timeline | 27/27 |

### 既存 pytest

| ファイル | 結果 |
|---|---|
| test_summary_tick_backpressure.py / test_no_undefined_names.py | 30 passed |
| test_ranking_enrich_cache.py | **12 passed** (元 6 テスト → カバレッジ増) |
| test_entry_pipeline.py / test_ai_entry_gate.py / test_summary_contract.py / test_summary_loader.py / test_summary_full_pipeline.py / test_summary_full_pipeline_3m_5m.py | 9 passed, 3 skipped |

変更した全モジュールの import 確認: **ALL IMPORT OK**

### 契約を変えたテスト (隠していないので明記)

`test_expired_ttl_rereads` は「TTL 超過したら**同期で**読み直して新しいデータを返す」
という旧契約を検証していた。P0-2 で hot path から同期読みを外したので、
この契約は成立しなくなった。

テストを消さずに、新契約を検証する形へ書き換えた:

- `test_expired_ttl_serves_stale_then_refreshes_in_background`
  — hot path は stale を返す / background では**必ず**読み直す /
    次の呼び出しで新しいデータになる
- `test_bg_refresh_can_be_disabled` — env で従来の同期読みへ戻せる
- `test_too_old_cache_forces_sync_read` — stale を使い続けない
- `test_empty_read_keeps_existing_cache` — 空読みで既存を潰さない
- `test_projection_*` 3 件 — 必要列を落としていない / フォールバック / 無効化

6 テスト → 12 テスト。カバレッジは落としていない。

---

## dry-run Entry timeline (実注文 0 件)

```
candidate_id = RANKING-141A-...

  1. ranking_candidate    symbol=141A source=RANKING
  2. pending_created      symbol=141A source=RANKING
  3. controller_picked    candidate_age=0.0s  bar_age=30.0s
  4. gate_approved        candidate_age=0.0s  bar_age=30.0s
  5. executor_reached     candidate_age=0.0s  bar_age=30.0s
  6. pre_sendorder        candidate_age=0.0s  bar_age=30.0s  qty=100
  7. dry_run_order        order_id=MOCK-ORDER-1 qty=100

freshness: candidate_age=0.0s bar_age=30.0s -> ok=True
実注文 : 0
mock   : 1
```

安全側の挙動も維持:
```
300 秒前の候補 -> STALE_CANDIDATE_SKIP reason=candidate_age -> 発注へ行かない
```

---

## 完成条件に対する現状

| # | 条件 | 状態 |
|---|---|---|
| 1 | disk I/O error = 0 を維持 | ログ上 0 件を確認。**ただし下記の注記あり** |
| 2 | MAIN PRICE DB FEED < 2 秒 | 触っていない (0.5〜1.3s) |
| 3 | Ranking Entry < 2 秒 | 触っていない (0.12s) |
| 4 | Tonosama < 5 秒 | 触っていない (0.25〜1.5s) |
| 5 | Summary 1m worker < 10 秒 (目標 <5) | enrich を 217s→0.005s にしたので見込みあり。**実測は再起動後** |
| 6 | Summary worker 30 秒超 = 0 | 予算 25 秒 + cancel 伝播。**実測は再起動後** |
| 7 | Ranking Summary BG < 10 秒 | 予算 60 秒 + stage 計測を入れた段階。まず内訳を取る必要がある。**未達** |
| 8 | timeout 後生存 worker = 0 | cancel 伝播をテストで確認 (600→6 stage で降りる) |
| 9 | 1m latest age < 90 秒 | P0-2/P0-1 の下流。**実測は再起動後** |
| 10 | 3m/5m latest 正常 | ゲート修正をテストで確認 (0 回→4 回)。**実測は再起動後** |
| 11 | enrich 通常 <1 秒 / cache miss でも <5 秒 | **達成** hot path max 0.0046s / cache miss 同期読みも 0.06s |
| 12 | dry-run で candidate→pending→Controller→Gate→executor | **達成** |
| 13 | 実注文禁止 | **達成 (0 件)** |

### 条件 1 についての注記

本番プロセスのログでは 0 件だった。ただし作業中、私が
`summary20260826.db` (454MB) を読もうとしたときに **18:10 に disk I/O error が発生**した。

```
--- summary20260826.db ---
   OPEN FAILED OperationalError('disk I/O error')
```

同じ probe で `summary20260825.db` の `MAX(datetime)` 1 本に **174.10 秒**かかっている。
`session.py` の PRAGMA 削減で圧力は下げたが、**NAS/SMB 側の脆さは残っている**。
「0 件を維持」は達成と言い切れない。場中の 30 分負荷テストは引き続き必要。

---

## 次にやるべきこと

1. **本番再起動** — 第1ラウンド (stage_tracker / freshness_timeline / candidate_id /
   worker guard 配線 / FK 復旧) と第2ラウンドの全部が未反映
2. 再起動後、実測ログで確認する項目
   - `[SUMMARY STAGE]` の stage 別 elapsed → 1m worker が 10 秒以内か
   - `[SUMMARY ENRICH STAGE]` → enrich が 1 秒以内か
   - `[RANKING SUMMARY STAGE]` → BG の内訳。10 秒に収まらなければ base_df の中をさらに割る
   - `[RANKING SUMMARY JOB] long tf built interval=5` → 5m が作られるか
   - `[FRESHNESS]` → 1m/3m/5m の age
   - `[SUMMARY PARALLEL CANCEL]` → timeout 時に cancel が届いているか
3. **場中の 30 分負荷テスト** — disk I/O error の確認 (まだ未達)
4. WAL の checkpoint 方針 / `Set-SmbClientConfiguration -MaxCmds` (前回から継続、判断待ち)

---

# 第3ラウンド — 再起動後の実測 (2026-08-27)

本番は 08:59:17 に再起動され、第1・第2ラウンドの修正がすべてロードされた。

## 実測: 大幅に改善

### Summary 1m worker (完成条件5: <10s / 6: 30s超=0)

| generation | total | over_budget |
|---|---|---|
| 3〜7 (寄り付き直後) | 26.7〜50.2s | True (budget_exceeded) |
| 8, 9 | 15.8s / 11.4s | False |
| **10〜17 (安定後)** | **0.69 / 5.53 / 6.20 / 5.06 / 8.01 / 7.51 / 6.15 / 9.32s** | **全て False** |

前日は 84.7s / 124.2s / **344.5s** だった。安定後は **全て 10 秒以内**。

寄り付き直後の 5 本は予算超過したが、**budget_exceeded で自分から降りている**
(前日はそのまま走り続けていた)。30 秒超も安定後は 0 件。

### Ranking Summary BG (完成条件7: <10s)

| | elapsed |
|---|---|
| 前日 | **239.6s** (timeout_streak=3, cooldown 90s) |
| 今日 gen 8〜15 | 3.2 / 5.3 / 6.8 / 5.7 / **11.9** / 5.6 / 6.6s |

bg_timeout **0 件**。1 本だけ 11.9s で目標超過だが、桁が違う。

### disk I/O error

**0 件** (`[DB PRAGMA] failed` も 0 件)。

### 3m/5m

P0-5 のゲート修正は効いた。`long tf due interval=5` が毎 tick 出るようになった
(前日は 1 度も出なかった)。ただし rows=0 が続く — 原因は下記の token 問題。

---

## 今日見つけて直した自分の欠陥 2 件

### 1. FRESHNESS が全部 None だった

```
[FRESHNESS] now=09:01:35 websocket_latest=None memory_push_latest=None ... (全部 None)
```

`global_data.push_df` を **属性** として読んでいたが、実体は
`global_data.get_push_df()` という **メソッド**。getattr が束縛メソッドを返し、
`isinstance(df, DataFrame)` が False になって黙って None になっていた。

対処: 属性・メソッド両方を試す `_resolve_value()` を入れ、
**どの accessor で取れたかをログに残す** ようにした (推測を残さない)。

### 2. 診断自体が遅かった

```
stage freshness_timeline  n=7  median=2.653s  max=7.801s
```

`push_db_latest` の probe が毎 tick NAS の push DB を開いていた。
25 秒予算に対して無視できないコスト。

対処: push DB probe を TTL 60 秒でキャッシュ + background 更新にし、
collect() 全体に 1 秒の上限を掛けた。診断自身の elapsed もログに出す。

実測: **2.653s → 0.0012s (median) / 0.0019s (max)**

### 3. tier2 のチェックポイント不足

gen 3 は 25 秒予算に対し 50.2s かかっていた。stage 境界にしか
チェックポイントが無く、tier2 (max 33s) が走り切ってから気づくため。

対処: `build_light_1m_summary` の中を
push_runner / fallback / normalize / ai_submit に割り、
重い段の手前で降りられるようにした。AI/Entry submit は予算切れでも実行する
(飛ばすと候補が消えるため)。

### 4. P0-5 のゲート修正が不完全だった

`_job_ranking_summary_all_core` の modulo ゲートは直したが、
`_run_interval` の中の `_should_run_by_clock()` にも同じ
`now.minute % interval == 0` が残っていた。

```
[RANKING SUMMARY JOB] long tf due interval=5 ...
[RANKING SUMMARY JOB] skip by clock interval=5 now=09:12:16   <- ここで二重に弾かれる
```

対処: `_should_run_by_clock` も同じ「新しい完成足があるか」判定に揃えた。

---

## 最優先の問題: kabu API トークンが拒否されている (コードではなく運用)

```
[MAIN DATABASE] kabu token preflight auth failed endpoint=/positions status=401
    content={"Code": 4001009, "Message": "APIキー不一致"} token_len=32
[MAIN DATABASE] token bootstrap failed: preflight rejected token; children will not start
[MAIN DATABASE] abort because token bootstrap/preflight failed.
```

**`main_database.py` が 09:00:25 に起動を中断した。**
その結果 `ranking_collector_runner` / `push_receiver_runner` /
`summary_database_runner` が **1 つも起動していない**。

実害:

| | 最終更新 | 現在 |
|---|---|---|
| `ranking20260827.db` | 08:56:01 | 09:17 (17 分停止) |
| `push20260827.db` | 08:55:58 | 09:17 (21 分停止) |
| ranking_collector ログ | **今日は存在しない** | |

- 3m/5m が rows=0 なのはこれが原因 (`ranking snapshot empty trade_date=2026-08-27`)
- main.py 側のログにも 401 が **1350 件**

原因の推定: kabu Station は 08:58:10 に起動しており稼働中 (:18080 も Listen)。
その再起動で以前のトークンが無効化されたが、
`token_startup_policy` は運用方針として

> Do not refresh the token automatically on 401 / APIキー不一致.

を明示的に持っており、`settings.ini` の古いトークンを使い続けている。

**これは認証まわりの運用方針なので、私の判断では変更していない。**
関係する env: `KABU_TOKEN_REFRESH_ON_STARTUP` / `KABU_TOKEN_ALLOW_RUNTIME_REFRESH`。

**この状態では 3m/5m・ranking・Entry の実測は意味を持たない。**

---

## 変更ファイル (第3ラウンド)

| ファイル | 内容 |
|---|---|
| `scheduler_jobs/summary/freshness_timeline.py` | accessor 修正 + probe コスト削減 |
| `scheduler_jobs/summary/light_tick_1m.py` | tier2 のチェックポイント追加 |
| `scheduler_jobs/summary/ranking_summary_jobs.py` | 2 つ目の modulo ゲート修正 |

## テスト

- `p6_test.py` 48/48 (method accessor / 属性 / 例外時の回帰テストを追加)
- `r_fcost.py` 8/8 (collect コスト / background / 予算)
- pytest: test_summary_tick_backpressure + test_no_undefined_names +
  test_ranking_enrich_cache = **42 passed**
- 実注文 **0 件**

第3ラウンドの 3 ファイルは **次回再起動で有効**になる。

---

# 第4ラウンド — main_database.py 起動時 kabu Station 認証エラー (2026-08-27)

対象: `main_database.py` が起動直後に `APIキー不一致` (Code=4001009) で `exit code=1`
実注文: **0 件** (認証と read-only API の確認のみ。発注経路には一切触れていない)
本番プロセス: **未再起動**。以下は次回再起動で有効になる。

第3ラウンドで

> `token_startup_policy` は運用方針として "Do not refresh the token automatically on 401" を
> 明示的に持っており、`settings.ini` の古いトークンを使い続けている。
> **これは認証まわりの運用方針なので、私の判断では変更していない。**

として保留した論点。今回、運用方針を壊さずに復旧経路だけを開けた。

---

## 原因

実ログの並びがそのまま原因になっている。

```
[TOKEN MANAGER] parent refresh skipped by ttl / age=285.9s ttl=3600.0s
[TOKEN STARTUP ONCE] runtime refresh suppressed / using settings.ini token
    ↓
GET /positions    status=401  {"Code":4001009,"Message":"APIキー不一致"}
GET /wallet/cash  status=401  {"Code":4001009,"Message":"APIキー不一致"}
    ↓
abort because token bootstrap/preflight failed   process exit code=1
```

`token_manager._refresh_token_core()` は起動時のトークン有効判定を
**TTL だけ**で行っていた。

```python
ttl_sec = _env_float("KABU_TOKEN_PARENT_REFRESH_TTL_SEC", 3600.0)
age_sec = _settings_file_age_sec()          # settings.ini の mtime
if existing_token and age_sec <= ttl_sec:
    return _publish_token(existing_token)   # /token を呼ばない
```

`age_sec` は **settings.ini の更新時刻**でしかない。
kabu Station を再起動するとサーバ側のトークンはその場で失効するが、
ローカルのファイルは「まだ新しい」ので TTL は有効と判定し続ける。

そこへ、意図的に入れてある 2 つの抑止が重なる。

| 層 | 挙動 |
|---|---|
| `token_startup_policy.refresh_token_startup_once` | `KABU_TOKEN_ALLOW_RUNTIME_REFRESH=0` なので `/token` を呼ばず settings.ini のトークンを返す |
| `install_requests_401_retry_patch` / `_patch_requests_no_401_refresh` | 401 を受けても再取得しない |

結果、**古いトークンを掴んだまま復旧手段が 1 つも無い**状態で preflight が 401 になり、
abort する以外に道が無かった。TTL は「ファイルが新しい」ことしか証明できず、
**サーバ側で有効かどうかは実際に API を叩くまで分からない**、というのが本質。

### ログの矛盾も同じ原因

```
[TOKEN MANAGER] parent refresh skipped by ttl                          ← 取得していない
[TOKEN STARTUP ONCE] parent startup bootstrap token refreshed/saved    ← 取得した、と言っている
```

`_startup_bootstrap_token()` が「`original_refresh()` が空文字を返さなかった」だけで
`refreshed/saved` と書いていた。内側の TTL 層が既存トークンを返しても同じ文言になる。
**本当に `/token` を叩いたのかログから判別できなかった。**

---

## 変更ファイル

| ファイル | 内容 |
|---|---|
| `kabu_api/token_bootstrap.py` | **新規**。起動時トークンのステートマシン本体 (preflight / 復旧 / 失敗分類) |
| `token_manager.py` | Ver33 → **Ver34**。`/token` 失敗の型分け、`request_fresh_token()`、`read_settings_token()`、TOKEN_SOURCE/ACTION ログ |
| `kabu_api/token_startup_policy.py` | V3 → **V4**。起動時 bootstrap を TTL から preflight 駆動へ。矛盾ログを実測値に置換 |
| `main_database.py` | V11 → **V12**。認証ゲートをステートマシンへ委譲。**起動順を変更** |
| `test_script/test_token_startup_recovery.py` | **新規**。ステートマシン単体 (mock) 30 件 |
| `test_script/test_token_startup_recovery_e2e.py` | **新規**。スタブ kabu Station に対する実ソケット E2E 8 件 |
| `test_script/test_token_startup_policy_bootstrap.py` | **新規**。起動時ポリシー配線 8 件 |
| `test_script/test_main_database_token_gate.py` | **新規**。認証ゲートと起動順 18 件 |
| `test_script/test_push_dataframe_concat.py` | 本件とは無関係。pandas 3.0.5 で当該 FutureWarning が廃止され前提が崩れていたので修正 |

---

## token state machine

`kabu_api/token_bootstrap.bootstrap_startup_token()`

```
          settings.ini の token を読む
                    │
        ┌───────────┴───────────┐
     token あり                token なし
        │                         │
   preflight (read-only)          │
   /positions → /wallet/cash      │
        │                         │
  ┌─────┼──────────┐              │
 200   401/403    到達不能         │
  │    4001009      │             │
  │      │          │             │
  │      │      ★ abort           │
  │      │   preflight_transport  │
  │      │   (/token は消費しない) │
  │      │                        │
  │      └────────┬───────────────┘
  │               │
  │        POST /token を 1 回だけ
  │        (プロセス全体で最大 1 回)
  │               │
  │      ┌────────┼────────┐
  │    200    401/4001009  到達不能
  │      │        │          │
  │  settings.ini │      ★ abort
  │  へ atomic    │      token_api_transport_error
  │  save + 検証  │
  │      │    ★ abort
  │  token/env/   api_password_mismatch
  │  global_data  ("TOKEN REQUEST FAILED")
  │  へ publish
  │      │
  │  preflight を 1 回だけ再実行
  │      │
  │  ┌───┴───┐
  │ 200     401
  │  │       │
  │  │   ★ abort  fresh_token_rejected (retry しない)
  └──┴───────┐
             │
    ★ startup 継続 → PUSH writer → children
```

不変条件:

- `/token` 呼び出しは **起動時に最大 1 回**。cached が有効なら **0 回**。
- 401 ごとの再取得ループは存在しない。preflight は前後に 1 回ずつだけ。
- 到達不能のときは `/token` を消費しない (kabu Station が落ちているのに枠を潰さない)。
- `_STARTUP_RECOVERY_ATTEMPTED` はプロセス単位。import 時の bootstrap と
  `main()` のゲートが両方走っても合計 1 回に収まる。
- runtime の無制限 refresh は**維持したまま**。
  `startup stale token recovery = ON` / `runtime arbitrary refresh = OFF`。
  旧挙動へ戻す場合は `KABU_TOKEN_STARTUP_PREFLIGHT_RECOVERY=0`。

### A / B の区別

| | 現象 | failure_kind | ログ |
|---|---|---|---|
| **A** cached token stale | `/positions` 401 → `/token` 200 | (成功するので無し) | `TOKEN_SOURCE=startup_401_recovery` |
| **B** apipassword 不一致 | `/token` **自体**が 401 / 4001009 | `api_password_mismatch` | `TOKEN REQUEST FAILED ... NOT a stale cached token` |

`_post_token_api()` が `TokenApiPasswordMismatch` / `TokenApiTransportError` /
`TokenApiError` を投げ分けるので、この 2 つは型レベルで混ざらない。

### 起動順 (main_database.main)

```
before                              after
──────────────────────────────      ──────────────────────────────
db startup scope defaults           db startup scope defaults
cpu guard env                       cpu guard env
summary wal checkpoint   ← thread   summary stale guard
summary stale guard                 mark data collector
PUSH DB writer 起動      ← 重い     ══ 認証ゲート ══          ★
mark data collector                 summary wal checkpoint
══ 認証ゲート ══          ← ここ     PUSH DB writer
children                            children
```

ゲートより前は env 設定だけにした (スレッド・ソケット・DB アクセスなし)。
認証で落ちる場合に PUSH DB writer と WAL checkpoint ループを起動して捨てることが無くなる。

### ログ語彙

秘匿値は出さない。`token_len` / `token_tail` (末尾 4 文字) /
`api_password_source` / `password_present` / `password_len` のみ。

- `TOKEN_SOURCE` = `settings_ini_cached` | `fresh_token_api` | `startup_401_recovery` | `inherited_parent`
- `TOKEN_ACTION` = `reused` | `api_requested` | `api_received` | `saved` | `preflight_ok` | `preflight_ng`
- `token_manager.token_api_call_count()` = 実際に `/token` を叩いた回数 (推測でなく実測)

---

## before / after ログ

### before (本番実ログ)

```
[TOKEN MANAGER] parent refresh skipped by ttl
                using recent settings.ini token
                token_len=32 age=285.9s ttl=3600.0s
[TOKEN STARTUP ONCE] runtime refresh suppressed
                     using settings.ini token
[TOKEN STARTUP ONCE] parent startup bootstrap token refreshed/saved      ← 矛盾
[FULL PIPELINE STABILITY] token published source=token_manager.refresh_token
PUSH DB WRITER started                                                   ← 認証前に起動
GET /positions    status=401 {"Code":4001009,"Message":"APIキー不一致"}
GET /wallet/cash  status=401 {"Code":4001009,"Message":"APIキー不一致"}
abort because token bootstrap/preflight failed
process exit code=1
```

### after — cached が stale (kabu Station 再起動相当。スタブサーバで実測)

```
[TOKEN BOOTSTRAP] main_database TOKEN_SOURCE=settings_ini_cached TOKEN_ACTION=reused token_len=31 token_tail=00ab
[TOKEN BOOTSTRAP] main_database/cached TOKEN_ACTION=preflight_ng endpoint=/positions   status=401 content={"Code": 4001009, ...}
[TOKEN BOOTSTRAP] main_database/cached TOKEN_ACTION=preflight_ng endpoint=/wallet/cash status=401 content={"Code": 4001009, ...}
[TOKEN BOOTSTRAP] main_database cached token rejected by kabu API (stale cached token). kind=auth_rejected ...
[TOKEN BOOTSTRAP] main_database TOKEN_SOURCE=startup_401_recovery TOKEN_ACTION=api_requested
[TOKEN MANAGER]   api_password_source=settings.ini[aukabu] password_present=True password_len=7 reason=startup_401_recovery
[TOKEN MANAGER]   TOKEN_SOURCE=fresh_token_api TOKEN_ACTION=api_requested reason=startup_401_recovery
[TOKEN MANAGER]   TOKEN_SOURCE=fresh_token_api TOKEN_ACTION=api_received token_len=31 token_tail=7f3c
[TOKEN MANAGER]   settings.ini token saved/verified section=aukabu token_len=31 token_tail=7f3c
[TOKEN MANAGER]   TOKEN_SOURCE=fresh_token_api TOKEN_ACTION=saved token_len=31 token_tail=7f3c
[TOKEN BOOTSTRAP] main_database TOKEN_SOURCE=startup_401_recovery TOKEN_ACTION=api_received token_len=31 token_tail=7f3c
[TOKEN BOOTSTRAP] main_database/fresh TOKEN_ACTION=preflight_ok endpoint=/positions status=200 token_len=31 token_tail=7f3c
[TOKEN BOOTSTRAP] main_database RESULT TOKEN_SOURCE=startup_401_recovery TOKEN_ACTION=preflight_ok token_api_calls=1 token_len=31 token_tail=7f3c
```

`token_api_calls=1` が「本当に 1 回だけ叩いた」ことを示す。
トークン値も apipassword も 1 行も出ていない。

### after — 実機 (2026-08-27 09:54 / kabu Station は API 応答なし)

```
[MAIN DATABASE] api_password_source=settings.ini password_present=True password_len=11
[TOKEN BOOTSTRAP] main_database TOKEN_SOURCE=settings_ini_cached TOKEN_ACTION=reused token_len=32 token_tail=96c6
[TOKEN BOOTSTRAP] main_database/cached TOKEN_ACTION=preflight_ng endpoint=/positions   transport_error=timed out
[TOKEN BOOTSTRAP] main_database/cached TOKEN_ACTION=preflight_ng endpoint=/wallet/cash transport_error=timed out
[TOKEN BOOTSTRAP] main_database abort: kabu API preflight could not connect. ... Confirm kabu Station API is listening on localhost:18080.
[MAIN DATABASE] abort: kabu Station API could not be reached on http://localhost:18080/kabusapi.
                ok=False TOKEN_SOURCE=settings_ini_cached token_api_calls=0 failure_kind=preflight_transport_error
```

`token_api_calls=0` / settings.ini のトークンは書き換わっていない (tail=96c6 のまま)。
到達不能時に `/token` を消費しない fail-safe が実機で確認できた。

---

## テスト結果

```
test_script/test_token_startup_recovery.py          30 passed   (mock)
test_script/test_token_startup_recovery_e2e.py       8 passed   (実ソケット / スタブ kabu Station)
test_script/test_token_startup_policy_bootstrap.py   8 passed
test_script/test_main_database_token_gate.py        18 passed
────────────────────────────────────────────────────────────────
test_script 全体                                   177 passed, 5 skipped
```

指定された 6 ケースの対応:

| | シナリオ | mock | E2E (実ソケット) |
|---|---|---|---|
| CASE1 | cached 有効 → `/token` 0 回 → 起動成功 | OK | OK |
| CASE2 | cached stale → 401 → `/token` 1 回 → 200 → 起動成功 | OK | OK |
| CASE3 | cached stale → `/token` 自体 401 → password mismatch で abort | OK | OK |
| CASE4 | cached 無し → `/token` 取得 → preflight 成功 | OK | OK |
| CASE5 | 再取得後も 401 → retry せず abort | OK | OK |
| CASE6 | network error → 理由を明示して fail-safe | OK | OK + **実機**で確認 |

追加で検証したもの:

- 同一プロセスで bootstrap を 2 回走らせても `/token` は合計 1 回
  (import 時のポリシー bootstrap + `main()` のゲート)
- 復旧枠を使い切った後は 2 回目の `/token` を呼ばずに abort
- 認証失敗時に PUSH DB writer / WAL checkpoint / 子プロセスが 1 つも起動しない
- preflight が触るのは `/positions` と `/wallet/cash` だけ。
  スタブに発注エンドポイントを実装していないので、このテスト群は物理的に発注できない
- ログ・`BootstrapResult.summary()` にトークン値も apipassword も出ない
- `resolve_apipassword_with_source()` は settings.ini を runtime キャッシュより優先する
  (古いキャッシュが訂正済みの settings.ini を覆い隠す事故を防ぐ)

---

## 未確認 / 残件

- kabu Station が落ちている場合、import 時の bootstrap と `main()` のゲートで
  preflight が 2 回走るため abort までに最大 20 秒かかる
  (2 エンドポイント × 5 秒 × 2 回)。`KABU_TOKEN_PREFLIGHT_TIMEOUT_SEC` で調整可能。

---

## 実機確認 (2026-08-27 10:12 / 追記)

kabu Station 起動後に `main_database.py` が本番起動し、**CASE1 を実機で確認できた。**

`X:\logs\console\main_database_20260827_101248_88232.log`

```
10:12:48 [INFO]    ========== MAIN DATABASE BOOT START ==========
10:12:48 [WARNING] [MAIN DATABASE] api_password_source=settings.ini password_present=True password_len=11
10:12:48 [WARNING] [TOKEN BOOTSTRAP] main_database TOKEN_SOURCE=settings_ini_cached TOKEN_ACTION=reused token_len=32 token_tail=672f
10:12:49 [WARNING] [TOKEN BOOTSTRAP] main_database/cached TOKEN_ACTION=preflight_ok endpoint=/positions status=200 token_len=32 token_tail=672f
10:12:49 [WARNING] [TOKEN BOOTSTRAP] main_database RESULT TOKEN_SOURCE=settings_ini_cached TOKEN_ACTION=preflight_ok token_api_calls=0 token_len=32 token_tail=672f
10:12:49 [WARNING] [MAIN DATABASE] token bootstrap OK for data collectors ok=True ... token_api_calls=0
10:12:50 [WARNING] [MAIN DATABASE] push db writer singleton ensured ok=True     ← ゲート通過の 1 秒後
10:13:56 [INFO]    [CHILD:db_prepare] ...                                       ← 子プロセス開始
10:15:16 [INFO]    [CHILD:push_receiver] / [CHILD:yahoo_complement] / [CHILD:summary_database]
10:15:35 [INFO]    [CHILD:ranking_collector]
```

確認できたこと:

| 完成条件 | 結果 |
|---|---|
| `main_database.py` が正常起動 | **OK** (10:12:48 起動、以後継続稼働) |
| cached token が有効なら `/token` 0 回 | **OK** `token_api_calls=0` |
| preflight 200 の後にだけ children 開始 | **OK** ゲート 10:12:49 → writer 10:12:50 → children 10:13:56 |
| runtime の 401 自動 refresh 抑止を維持 | **OK** 全子プロセスで `no_runtime_refresh=True parent_only=True` |
| 子プロセスは `/token` を呼ばない | **OK** 全子で `child bootstrap skipped` |
| 実注文 | **0 件** |

起動後 28 分時点のログ集計:

```
4001009 / APIキー不一致 : 0 件
実際の 401              : 0 件 ([KABU API REQUESTS 401 RETRY] の banner 5 行のみ)
/token を叩いた回数     : 0 回
```

PUSH は 287 銘柄で `connected=True`、DB 書き込みも継続中。

全プロセスに `V4-TOKEN-STARTUP-PREFLIGHT-STALE-RECOVERY` が乗っていることも確認:

```
[TOKEN STARTUP ONCE] installed version=V4-TOKEN-STARTUP-PREFLIGHT-STALE-RECOVERY
                     token_manager=True requests=True auth_retry=0 startup_refresh=1 parent_only=1
```

### 実機で確認できていないもの

- **CASE2 (stale → 401 → `/token` 1 回 → 復旧) の実機確認は未実施。**
  再現には kabu Station を再起動して `main_database.py` を上げ直す必要があり、
  稼働中の本番を止めることになるので実施していない。
  E2E (実ソケット / スタブ kabu Station) では確認済み。
- CASE3/4/5/6 も同様に mock + E2E まで。CASE6 のみ 09:54 の実機ログで確認済み。

### 調査中に判明したこと (本件の原因ではない)

- **売買システム稼働中は、別プロセスから kabu Station API を叩けない。**
  稼働中のシステムが `/positions` で 200 を受けている同時刻に、
  別プロセスからの GET は timeout か `WinError 10054` になる。
  疎通確認は新しくプローブを立てず、稼働中プロセスのログの
  `preflight_ok ... status=200` で判断する。
  なお 18080 の listen は PID 4 (System / http.sys) が持つので、
  `Get-NetTCPConnection` の OwningProcess から kabu Station の生死は判断できない。
- `main.py` / `main_database.py` / 各 runner がプロセス一覧に 2 個ずつ見えるのは
  **重複起動ではない**。`36712 → 115804 (main_database) → 88232 (main_database)
  → 102260 (push_receiver) → 76708 (push_receiver)` という親子の re-exec で、既存の設計。

### 自分が入れた不具合 (検出して修正済み)

`test_script/test_main_database_token_gate.py` が
`_bootstrap_kabu_token_for_data_collectors()` を直接呼んでおり、その中の
`_ensure_basic_logging()` が **本番のログディレクトリ** `X:\logs\console` へ
`main_database_*.log` を作っていた。失敗系のテストが本番の abort ログと
見分けがつかず、実機ログの読み取りを一度誤らせた
(09:56 / 10:05 のログはテスト実行によるもので、本番の起動失敗ではない)。

autouse fixture で `_ensure_basic_logging` を no-op に差し替えて修正。
修正後、テスト実行前後で `X:\logs\console` のファイル数が変わらないことを確認 (39 → 39)。

`X:\logs\console` に残っているテスト由来のファイル (削除は未実施):

```
main_database_20260827_095610_84596.log
main_database_20260827_095729_80940.log
main_database_20260827_095927_113568.log
main_database_20260827_100518_86176.log
```

---

# 第6ラウンド — リソースリーク (2026-08-27 12:33-13:00)

対象ログ: `console_20260827_112935_118240.log` (438,248 行)

報告された件数より実際はさらに多かった:

| 症状 | 報告 | 実測 |
|---|---|---|
| Too many open files | 18 | **83** (全て importlib = プロセスの fd 枯渇) |
| disk I/O error | 134 | **7,013** |
| unable to open database file | 51 | **3,546** |
| tables=[] | — | **28,803** |

---

## 真因1: `with sqlite3.connect(...)` は接続を閉じない

これが fd リークの本体。

`with sqlite3.connect(path) as conn:` は **トランザクション**の context manager
であって closing ではない。実測で確認:

```
with sqlite3.connect(p) as conn:
    conn.execute("SELECT 1")
conn.execute("SELECT 1")      -> まだ使える = 閉じていない

with 版を 300 回まわす -> Connection オブジェクトが 300 個生存
try/finally close 版    -> 生存 0
```

コードベース自身がこの罠を知っていた (`controller_enrich.py:419`):

> `with sqlite3.connect(...)` は close しないため使わず finally で閉じる。

しかしランタイム経路 (ats/trading/core/scheduler_jobs/database) に
**106 箇所** この形が残っていた。

最も呼ばれていたのが `ats/ats_ranking/db_loader.py:854`:

```
[ATS RANKING] db tables path=...ranking20260827.db tables=[]   ... 28,803 件
ats.ats_ranking.db_loader からの disk I/O error                ...    444 件
```

**実 DB は 147MB / 33 テーブルで健全**だった (直接読んで確認)。
「DB が空」ではなく、読めていないだけ。
例外パスでは traceback がフレームを保持するため解放がさらに遅れ、
最終的に importlib がモジュールを開けなくなっていた。

### 対処

新規 `database/sqlite_util.py`:

- `closing_connect()` — finally で必ず close。例外時も閉じる
- `readonly_connect()` — `file:...?mode=ro` + **存在しなければ FileNotFoundError**
  (reader が空 DB を作らない)
- `stats()` — opened / closed / outstanding を返す (leak 監視用)

テスト `s2_test.py` 17/17:

| | opened | closed | outstanding | Connection 生存 |
|---|---|---|---|---|
| `with sqlite3.connect` 300 回 | — | — | — | **+300** |
| `closing_connect` 300 回 | 300 | 300 | **0** | **+0** |

適用済み (hot path):

| ファイル | 呼ばれ方 | 変更 |
|---|---|---|
| `ats/ats_ranking/db_loader.py` | 28,803 回 | `readonly_connect` へ |
| `ats/ats_ranking_source.py` | symbol_flags | `readonly_connect` へ |
| `trading/push/latest_price_db_feeder.py` | 2 秒ごと | read-only URI へ |

**残り ~100 箇所は未適用**。ほとんどが AI/tools 等のオフラインスクリプトだが、
ランタイム経路にも残っている。次ラウンドで順次置換が必要。

---

## 真因2: positions.db への PRAGMA リトライストーム

`[DB PRAGMA] failed` 3,355 件は **全て同一**:

```
db=\192.168.0.22\AutoStockBuyAndSell\trading\runtime\positions\positions.db
pragma=synchronous
```

スレッド別:

| thread | 件数 |
|---|---|
| exit_fill_confirm_loop | 1,782 |
| push-rotation-worker | 480 |
| ats-register-loop | 454 |
| position-sync-loop | 328 |
| exit_closing_reconcile_loop | 309 |

発生ペースは **毎分 ~120 件が 20 分以上継続** (バーストではない)。

構造: `poolclass=NullPool` で毎クエリ新接続 → 5 ループが同じ positions.db を
NAS 越しに開く → `PRAGMA synchronous` 失敗 → **接続破棄** → 再接続 → 失敗…
毎秒 2 接続ぶんの SMB オープンが延々と続き fd を食い潰していた。

さらにエラーログ自体が **同じ NAS 上のログファイル** に書かれるため、
NAS 障害がログ I/O を増やしてさらに NAS を圧迫する自己増幅になっていた。

### 対処 (`database/session.py`)

1. **PRAGMA を重要度で分けた**
   - `foreign_keys` = データ整合性 → 失敗したら接続を捨てる (従来どおり)
   - `busy_timeout` / `temp_store` / `synchronous` / `cache_size` = チューニング
     → 記録して続行。捨てて再接続を繰り返すより害が小さい

   握り潰しではない。DB 名・pragma・thread・elapsed・例外・累計件数を必ず残す。

2. **ログをレート制限** (最初 5 件 + 以降 200 件ごと、累計付き)
   実測: 500 回失敗 → **7 行**

3. **読み取り専用エンジンには書き込み系 PRAGMA を投げない**
   `mark_engine_readonly(engine)` で明示。writer は従来どおり全部設定。

テスト `s1_test.py` 23/23。

---

## P0-7 symbol_flags の fail-open を止めた

`filter_symbols_by_symbol_flags` は 3 経路すべてで `return symbols`
= **全銘柄をそのまま通す** だった (DB 無し / 空 / 例外)。

市場種別 (プライム/スタンダード/グロース) と ETF 除外を決める
**新規 Entry の対象フィルタ**なので、DB 障害で全銘柄許可は危険。

変更:

| 状況 | 変更前 | 変更後 |
|---|---|---|
| DB read 成功 | fresh filter | fresh filter + last-good 保存 |
| DB 一時失敗 | **全銘柄通過** | **last-good で絞る** (経過時間をログ) |
| last-good なし | **全銘柄通過** | **fail-closed ([])** |

テスト `s4_test.py` 19/19。接続リークも解消 (50 回で outstanding=0)。
`ATS_SYMBOL_FLAGS_FAIL_OPEN=1` で従来動作へ戻せるが既定ではない。

---

## P0-5 feeder の無限リトライを止めた

```
[MAIN PRICE DB FEED] cycle failed streak=817
```

固定 2 秒 interval で失敗しても変わらず、817 回 × 2 秒 ≈ 27 分間
NAS が見えないのに接続を試み続けていた。さらに毎回 `logger.exception()` で
フルトレースバックを NAS 上のログへ書いていた。

対処:
- exponential backoff: **2 → 2 → 5 → 10 → 30 秒 (上限)**、成功で即リセット
- ログ: 最初 3 回はトレースバック、以降は間引いて 1 行
- read-only 接続にして空 DB を作らない

テスト `s5_test.py` 20/20。streak=817 でも待ちは 30 秒で頭打ち。

memory PUSH 経路 (`last_recv_age=0.1s` / memory 1m `age_sec=0.0`) は正常なので、
feeder は補助経路のまま。DB 不通で Realtime Entry を止めない。

---

## P0-8 Exit — これはバグではなく意図的な既定値

ユーザーの見立て (boolean 判定の不備) は **該当しなかった**。
`_bfe_env_bool` は仕様どおり正しい:

```python
v is None            -> default
'1','true','yes','y','on'          -> True
'0','false','no','n','off',''      -> False
```

skip の原因は既定値そのもの:

```python
def _bfe_exit_temp_disabled() -> bool:
    # 今回の運用要望: EXITを一時停止。解除したい時だけ AUTOSTOCK_TEMP_DISABLE_EXIT=0 を指定する。
    return _bfe_env_bool("AUTOSTOCK_TEMP_DISABLE_EXIT", True) or ...
```

**既定が True (= Exit 停止)** で、env は未設定なので停止し続ける。
コメントのとおり運用judgment で入れられたもの。

これを反転させると **実際の返済注文が有効になる**ため、
私の判断では変更していない。解除は `AUTOSTOCK_TEMP_DISABLE_EXIT=0`。

---

## 壊していないことの確認

| 指標 | 実測 | 状態 |
|---|---|---|
| PUSH WebSocket | `last_recv_age=0.1〜0.4s` | 維持 |
| memory 1m Summary | `latest_dt=12:34:00 age_sec=0.0` | 維持 |
| direct 1m Summary | `elapsed=1.404s` | 維持 (<5s) |
| 実注文 | 0 件 | 維持 |

---

## 変更ファイル (第6ラウンド)

| ファイル | 内容 |
|---|---|
| `database/sqlite_util.py` | **新規** closing_connect / readonly_connect / stats |
| `database/session.py` | PRAGMA 重要度分離・ログ間引き・readonly 対応 |
| `ats/ats_ranking/db_loader.py` | 接続リーク + 空 DB 作成の修正 |
| `ats/ats_ranking_source.py` | fail-open 廃止 + last-good + リーク修正 |
| `trading/push/latest_price_db_feeder.py` | backoff + read-only |

## テスト

| スクリプト | 内容 | 結果 |
|---|---|---|
| `s1_test.py` | PRAGMA ストーム対策 | 23/23 |
| `s2_test.py` | sqlite_util | 17/17 |
| `s4_test.py` | symbol_flags fail-closed | 19/19 |
| `s5_test.py` | feeder backoff | 20/20 |
| pytest | backpressure / no_undefined / enrich_cache | **42 passed** |

実注文 **0 件**。

---

## 残件

1. **残り ~100 箇所の `with sqlite3.connect`** — ランタイム経路のものを順次置換
2. **30 分の handle 監視** — 実施中 (60 秒ごと)。現時点 3 サンプルでは横ばい。
   結論には継続観測が必要
3. **P0-8 Exit の有効化** — 運用判断 (`AUTOSTOCK_TEMP_DISABLE_EXIT=0`)
4. **positions.db の owner 明確化** — writer 1 系統化は未着手。
   今回は PRAGMA ストーム側だけを止めた
5. 第4〜6ラウンドの修正は **次回再起動で有効**

---

# 第7ラウンド — 再起動後の実測 (2026-08-27 12:56 起動 / PID 75344)

配置時刻はすべて起動 (12:56:12) 前なので、第4〜6ラウンドの修正が全て載っている。

## 結果: リーク症状は全て 0

| 症状 | 再起動前 (438,248行) | 再起動後 (9,777行) |
|---|---|---|
| Too many open files | **83** | **0** |
| disk I/O error | **7,013** | **0** |
| unable to open database file | **3,546** | **0** |
| tables=[] | **28,803** | **0** |
| MAIN PRICE DB FEED cycle failed | **817** | **0** |

feeder は成功に転じた:

```
[MAIN PRICE DB FEED] symbols=26 updated=26 rowid=29729 read=3.381s
```

## handle / thread の推移 (P1: 増え続けないこと)

60 秒ごと × 22 サンプル (12:49:57 〜 13:10:58):

| pid | 起動 | handles 初→終 (max) | threads | 判定 |
|---|---|---|---|---|
| 88232 (main_database) | 10:11 | 415 → 420 (420) | 54 → 54 | **横ばい** |
| 57660 (push_receiver) | 12:37 | 293 → 309 (372) | 12 → 9 | **横ばい** |
| 59100 (ranking_collector) | 12:36 | 263 → 265 (266) | 6 → 4 | **横ばい** |
| 84676 | 10:13 | 257 → 285 (394) | 3 → 3 | 横ばい |
| 76840 | 10:13 | 235 → 240 (246) | 2 → 2 | 横ばい |
| 75344 (新 main.py) | 12:56 | 174 → 393 | 27 → 59 | 起動直後で上昇中 (要継続観測) |

**21 分稼働している 5 プロセスは全て横ばい。** 線形増加は見られない。
新 main.py は起動 15 分でまだ初期化中のため、plateau の確認が必要。

---

## 自分が入れた劣化を 1 件見つけて修正

P0-6 (reader が空 DB を作らない) のために `file:...?mode=ro` を使ったが、
**活発に書かれている WAL DB では mode=ro のほうが遅い**。

実測 (push20260827.db 250MB / -wal 60MB、交互実行で順序バイアス除去):

| 方式 | median | mean | max |
|---|---|---|---|
| mode=ro | 5.496s | 5.866s | 17.123s |
| query_only=ON | **3.830s** | 4.753s | 10.331s |

→ query_only が **1.43 倍速い**。
mode=ro の接続は WAL の -shm 共有インデックスに通常の形で参加できないため。

本番でも feeder の `read=` が 2.6s → 15.5s まで伸びていた
(その後 2.9s まで戻ったので NAS 競合との複合)。

### 修正

空 DB を作らない保証は **`os.path.exists()` のチェック**で既に得ている。
そこは残したまま、接続は通常接続 + `PRAGMA query_only=ON` に戻した。
`SQLITE_READONLY_USE_URI_MODE=1` で mode=ro に切り替えられる。

修正後の実測: `readonly_connect` median=**4.070s** (query_only 相当まで回復)。

---

## 追加で修正した hot path

前ログで最も活発だった logger 上位:

| logger | ログ行数 | 対応 |
|---|---|---|
| ats.ats_ranking.db_loader | 103,850 | 第6ラウンドで修正済 |
| ats.ats_ranking.db_path | 15,537 | **今回 readonly_connect へ** |
| ats.ats_filters | 9,321 | **今回 readonly_connect へ** |
| ats.ats_ranking_source | 1,219 | 第6ラウンドで修正済 |

---

## 未達 / 継続観測が必要な項目

### Ranking Summary BG が 214〜279 秒 (完成条件15: <10秒)

```
RANKING SUMMARY STAGE] interval=1 base_df=138.417s rows=17386
RANKING SUMMARY STAGE] budget exhausted after base_df interval=1 elapsed=138.848s
                       -> technical を飛ばして base_df を使う
RANKING SUMMARY STAGE] cooperative stop before base_df interval=3 (予算切れのため着手しない)
RANKING SUMMARY STAGE] cooperative stop before base_df interval=5 (予算切れのため着手しない)
```

**ただし「0.258 秒からの劣化」ではない。**
0.258 秒はユーザー自身が指摘したとおり **入力 DB が空だった**ときの値で、
今は ranking DB に実データ (147MB / 33 テーブル / 17,386 行) が入っている。
第5ラウンドで特定した `base_df` / `technical` のボトルネックが、
実データが流れて初めて本来の姿で出ている。

第5ラウンドの「完成した base_df を捨てない」修正は**正しく効いている**
(`technical を飛ばして base_df を使う` が出ている)。
3m/5m は予算切れで着手されないままなので、**base_df の 138 秒を潰す必要がある**。

### memory 1m age / direct 1m

| 指標 | 再起動前 | 再起動後 |
|---|---|---|
| memory 1m age_sec | 0.0 | 0 → 60 → 120 (上昇) |
| direct 1m elapsed | 1.128s | 3.888 → 0.662 → 23.187s |

feeder が 20 分の失敗 (streak=817) から復帰して rowid を追いかけている最中
(29098 → 30049) なので、バックログ消化中の可能性が高い。
ただし完成条件 13 (<90秒) / 14 (<5秒) は現時点で未達。**継続観測が必要。**

---

## 変更ファイル (第7ラウンド)

| ファイル | 内容 |
|---|---|
| `database/sqlite_util.py` | readonly を query_only 方式へ (WAL 性能回復) |
| `trading/push/latest_price_db_feeder.py` | 同上 |
| `ats/ats_ranking/db_path.py` | readonly_connect へ (15,537 行の hot path) |
| `ats/ats_filters.py` | readonly_connect へ (9,321 行の hot path) |

## テスト

- `s2_test.py` 17/17 / `s4_test.py` 19/19 / `s5_test.py` 20/20
- 変更モジュール 7 件すべて **ALL IMPORT OK**
- 実注文 **0 件**

第7ラウンドの修正は次回再起動で有効。

## 第7ラウンド 追記 — 31 分観測の確定結果 (13:20)

### 完成条件の達成状況

| # | 条件 | 実測 | 判定 |
|---|---|---|---|
| 1 | Too many open files = 0 | **0** (20,937 行) | 達成 |
| 2 | disk I/O error = 0 | **0** | 達成 |
| 3 | unable to open database file = 0 | **0** | 達成 |
| 4 | handle が時間で増え続けない | 下表 | 達成 |
| 5 | thread が増え続けない | 下表 | 達成 |
| 8 | MAIN PRICE DB FEED 連続失敗 = 0 | **0** (前 817) | 達成 |
| 9 | Ranking DB にテーブルが存在 | 147MB / 33 テーブル | 達成 |
| 10 | reader が空 DB を作らない | readonly_connect + exists チェック | 達成 |
| 11 | symbol_flags fail-open しない | fail-closed 実装 + テスト 19/19 | 達成 |
| 13 | memory 1m age < 90 秒 | **0.0s** (バックログ消化後) | 達成 |
| 14 | direct 1m < 5 秒 | 5.849s | ほぼ達成 (要観測) |
| 15 | Ranking Summary BG < 10 秒 | 214〜279s | **未達** |
| 16 | 実注文 = 0 | **0** | 達成 |

### handle / thread (60 秒 × 32 サンプル / 31 分)

| pid | 起動 | handles 初→終 | 前半 max / 後半 max | threads | 判定 |
|---|---|---|---|---|---|
| 75344 (新 main.py) | 12:56 | 174 → 377 | 381 / 393 | 27 → 62 | 初期化後 **plateau** |
| 88232 (main_database) | 10:11 | 415 → 415 | 419 / 421 | 54 → 54 | **横ばい** |
| 57660 (push_receiver) | 12:37 | 293 → **278** | 372 / 345 | 12 → 10 | **減少** |
| 59100 (ranking_collector) | 12:36 | 263 → 266 | 265 / 268 | 6 → 4 | **横ばい** |

**線形増加なし。** 新 main.py も初期化フェーズを終えて頭打ちになった。

### memory 1m / direct 1m はバックログ消化で回復した

前回「未達・継続観測」としたが、その後回復を確認:

| 時刻 | memory 1m age | direct 1m elapsed | rows |
|---|---|---|---|
| 13:03:01 | 60.0s | 0.662s | 164 |
| 13:04:41 | 120.0s | **23.187s** | 45 |
| 13:13:07 | 60.0s | 5.849s | 168 |
| 13:14:53 | **0.0s** | — | 169 |

feeder の `read=` も回復:

| 局面 | read |
|---|---|
| バックログ消化中 | 3.4 〜 15.5s |
| 消化後 | **0.773 〜 2.245s** |

原因は feeder が 20 分の失敗 (streak=817) から復帰して
rowid 29098 → 30049 を追いかけていたバックログで、
**コードの劣化ではなかった**。前回の「未達」判定を上記のとおり訂正する。

### 残る唯一の未達: Ranking Summary BG

`base_df=138.417s` (rows=17386) が予算を食い切り、3m/5m が着手されない。
第5ラウンドで特定した base_df / technical のボトルネックそのもの。
**次の作業対象はここ。**

---

# 第8ラウンド — historical / realtime の分離 (2026-08-27 13:5x)

対象ログ: `console_20260827_125946_75344.log` (70,968 行)

## merged が 13:26 で凍っていた真因

ログに決定的な 1 行があった:

```
[MERGED SET] tf=1 source=push caller=global_state.set_merged_summary
             空 sanitize 結果で既存を潰さない
             in_rows=22155 sanitized_rows=0 kept_rows=49 kept_latest=2026-08-27 13:26
```

**22,155 行を渡す -> `_sanitize_summary_df` が 0 行に落とす ->
第2ラウンドで入れた「空 sanitize で既存を潰さない」ガードが古い 49 行を保持
-> merged が 13:26 で凍結。**

sanitize の内訳:

```
[SUMMARY NON-TODAY DROP] label=merged_sanitize before=330 non_today_removed=55
                         after=275 non_today_latest=2026-08-26 15:24:00
[SUMMARY STALE DROP]     label=merged_sanitize before=330 after=26
```

- 22,155 行 = 330 銘柄 x 約 67 本の履歴
  (`attach_display_ready rows=22155 cols=281 display_symbols=330`)
- そのうち **55 銘柄は 2026-08-26 の行** (P0-3 の裏付け)
- stale drop で 26 行まで落ちる

## 対処: publish 境界に realtime_projection を置いた

`global_state.set_merged_summary` / `set_push_merged_summary` の入口で
**realtime_projection** に落とす。境界なので呼び出し元がどこでも効く。

1. `trade_date == today` の行だけ残す (過去日は indicator seed 専用)
2. symbol ごとに datetime 最大の 1 行だけ残す
3. 1,200 行以下なら素通し (既に projection 済みとみなす)

`_setter_enrich` より **前** に置いたので、
22k フレームへの重複 enrich (DAILY MTF / SCORE BREAKDOWN / display rebuild) も
同時に解消する。

### 実測 (`u1_test.py` 23/23)

| | 値 |
|---|---|
| 入力 | 22,165 行 / 330 銘柄 (本番と同形状) |
| 出力 | **330 行 / 330 銘柄** |
| 所要 | **0.021 秒** |
| 過去日 | **0 行** (55 銘柄ぶんを除去) |
| symbolname | 元の社名を保持 |
| symbol 重複 | 0 |

閾値・stale 判定・sanitize は変更していない。渡すフレームの形だけを直した。
sanitize 側の NON-TODAY DROP / STALE DROP はそのまま安全網として残る。

env `SUMMARY_MERGED_REALTIME_PROJECTION=0` で従来動作へ戻せる。

---

## P0-9 unaccounted elapsed

指定どおり `unaccounted = worker_total - sum(stage_elapsed)` を出す。

```
[SUMMARY STAGE] worker end ... total=0.401s stage_sum=0.101s unaccounted=0.301s ...
[SUMMARY STAGE] unaccounted elapsed kind=push:1 generation=3 unaccounted=0.251s
                total=0.271s stage_sum=0.020s
                (stage 計測の外で時間を使っている。計測点を足す必要がある)
```

**入れ子 stage の二重計上を避ける**ため、トップレベルの stage だけを合計する
(実測: outer 0.15s + inner 0.10s -> stage_sum=0.151s。二重計上なら 0.251s)。

既定 2.0 秒超で WARNING。snapshot にも `stage_sum` / `unaccounted` を追加。

テスト `u2_test.py` 13/13。

---

## P0-8 175,094 行の発生源 (特定・未対処)

```
[summary_runtime] DB seed multi-day-direct tf=3 rows=239880 symbols=4147 bars=120
[summary_runtime] direct multi-day seed merged tf=5 rows=175094
```

発生源は **multi-day seed** (`db_seed_multiday_sqlite`)。
実行は 1 セッションで **3 回** (tf=1 13:32 / tf=3 13:39 / tf=5 13:45)、
timeframe ごとに 1 回で、`DB seed already done -> skip` のガードも存在する。

つまり P0-11 が懸念した「毎分再実行」は **起きていない**。
ただし 175k〜363k 行の処理が場中に走り、realtime pipeline と帯域を奪い合う。
**次ラウンドの対象。**

---

## P0-4 symbolname は現ログで再現しなかった

```
1401 | エムビーエス
1407 | ウエストホール...
141A | トライアルホー...
blank_symbolname=0
[KABU REGISTER SYMBOL LOGGER] symbol_name_map loaded size=4348
```

企業名は正常。再起動で name_map (4,348 件) が読めたためと思われる。
**動いているものに手を入れて壊すのは避け**、記録のみとする。
再発したら name_map のロード失敗を最初に疑うこと。

---

## 前ラウンドからの継続: SQL lookback の確定値

交互 5 往復で NAS ノイズを平均化:

| | median | mean | max |
|---|---|---|---|
| OFF (全件読み) | 32.921s | 32.686s | 35.471s |
| ON (SQL lookback) | **16.068s** | 18.085s | 27.038s |

**median 2.05 倍 / mean 1.81 倍。**

等価性は凍結スナップショットで証明済み
(共通範囲 57,644 行一致 / symbol+datetime 集合 30,391 一致 / 列数 114 維持)。

---

## 変更ファイル (第8ラウンド)

| ファイル | 内容 |
|---|---|
| `global_state.py` | realtime_projection を publish 境界に追加 |
| `scheduler_jobs/summary/stage_tracker.py` | unaccounted elapsed + 入れ子の二重計上回避 |
| `trading/ranking/summary/loader.py` | SQL lookback (前ラウンド) |
| `trading/ranking/summary/resample.py` | base_df stage 計測 (前ラウンド) |

## テスト

- `u1_test.py` 23/23 (projection)
- `u2_test.py` 13/13 (unaccounted)
- pytest: backpressure / no_undefined / summary_contract / summary_loader /
  full_pipeline = **32 passed, 2 skipped**
- 実注文 **0 件**

## 残件

1. **P0-8** multi-day seed の 175k〜363k 行を realtime と分離 (低優先 background / bounded)
2. **P0-10** Ranking Summary BG の入力件数ログ (DB rows / SQL selected / latest-slot filter)
3. **P0-5/P0-6** fresh-first publish (重い enrichment を待たずに latest projection を先に publish)
4. 第8ラウンドの修正は **次回再起動で有効**

---

# 第9ラウンド — 600秒 worker と Exit owner (2026-08-27 14:0x)

対象ログ: `console_20260827_125946_75344.log` (88,784 行)

## P0-1/P0-2: 600 秒 worker の位置特定

実測:

```
{'key': 'push:1', 'generation': 12, 'owner': 'tick:20260827135400',
 'started_at': '13:54:01', 'age': 724.2,
 'cancel_requested': True, 'ctx_cancelled': True,
 'thread': 'summary-main-tick-local_0',
 'stages': 'freshness_timeline=4.59,tier1_direct_memory=8.40,global-set_merged_summary=2.30'}
```

stage 合計 **15.29 秒** に対し worker total **724 秒**。**709 秒が未計測**。

ログを追うと 13:54:14 に tier1 を抜けて

```
[SUMMARY MAIN LIGHT TICK] light job start interval=1
call runner=trading.summary.summary_controller.SummaryController.diff_update
```

へ入り、戻ってきていない。ただし**ログ行にスレッド名が無い**ため、
どのログが worker のものか切り分けられなかった。

### 検証して否定した仮説

600 秒の間の logger 別件数は
`MERGED GET` 331 + `MERGED GET FALLBACK` 243 + `SUMMARY HISTORY GET` 186 = **760 回**。
`_log_df_profile` は 1 回につき 13 列ぶんの nonzero 集計と
`to_string()` によるテーブル描画を行うため、これを疑った。

実測すると **1 回 0.0031 秒 / 760 回で 2.39 秒**。709 秒の説明にはならない。
**パッチする前に測って否定できた。**

### 対処: worker heartbeat

worker 自身に位置を報告させる。

```
[SUMMARY WORKER HEARTBEAT] generation=12 kind=push:1 owner=tick:20260827135400
  beat=1 current_stage=tier2_light_runner stage_elapsed=0.2s total_elapsed=0.2s
  stage_sum=0.0s unaccounted=0.2s cancelled=True deadline_remaining=... thread=... tid=...
```

- 5 秒ごと (env `SUMMARY_WORKER_HEARTBEAT_SEC`)
- stage の外に居るときは **`current_stage=<between stages>`** と名指しする
  -> 未計測区間そのものを特定できる
- **cancel 後も出continue する**のでゾンビ化しても位置が分かる
- worker 終了・例外時に heartbeat スレッドも必ず止まる (テストで確認)

テスト `v1_test.py` 19/19。

---

## P0-8: Exit owner — 指示と異なる対処をした理由

実測:

| 系統 | 実装 | 間隔 | 状態 |
|---|---|---|---|
| A | `trading/exit/exit_loop.py` `exit_loop_5s` | 5 秒 | **424 回正常完走** |
| B | `trading/handlers/exit_handler.py` `run_exit_pipeline` | 1 秒 | TEMP STOP で skip 53 回 |

**Exit は A で実際に動いている** (ユーザー報告の「Exit正常」もこれ)。

`EXIT TEMP STOP` が出ているのは B のほう。その原因は前ラウンドで特定したとおり
`_bfe_exit_temp_disabled()` の **既定値が True** であること
(`_bfe_env_bool` 自体は None/''/0/false を正しく扱えており、boolean 判定の不具合ではない)。

### なぜ boolean を反転しなかったか

指示は「boolean を明示判定にして再修正」だったが、
**それを行うと A と B の 2 系統が同時に返済注文を出す**。
これは同じ指示の中の

> Exit owner を明確化。同じ Exit 処理を二重 scheduler で走らせない。

に正面から反する。実注文の面でも危険。

そこで owner を **A (exit_loop_5s) に確定**し、
main.py 側の B の 1 秒登録をやめた。

```python
_owner = (os.getenv("EXIT_PIPELINE_OWNER") or "exit_loop_5s").strip().lower()
if _owner != "run_exit_pipeline":
    logger.warning("[EXIT SCHEDULER] run_exit_pipeline は登録しない owner=%s ...")
    return True
```

これで:

- Exit owner が一系統になる (完成条件14)
- 誤解を招く `EXIT TEMP STOP` ログが 53 件 -> 0 になる (完成条件13)
- **新しい発注経路を増やさない**

B を正規 owner にしたい場合は `EXIT_PIPELINE_OWNER=run_exit_pipeline`。
ただしその場合は A 側を止める必要がある (両方 ON にはしない)。

---

## 変更ファイル (第9ラウンド)

| ファイル | 内容 |
|---|---|
| `scheduler_jobs/summary/stage_tracker.py` | worker heartbeat + current_stage |
| `main.py` | Exit owner を一系統化 (run_exit_pipeline の 1 秒登録を既定で停止) |

## テスト

- `v1_test.py` 19/19 (heartbeat)
- 実注文 **0 件**

## 残件

1. **709 秒の正確な位置** — heartbeat で次回起動時に確定する
2. **P0-3** bounded 処理 (chunk 化して cancel 後 5 秒以内に return)
3. **P0-5** 363,984 行 seed の realtime 排除
4. **P0-7** fast lane / heavy lane 分離
5. **P0-9** Ranking Entry technical_attach 15 秒
6. 第9ラウンドの修正は **次回再起動で有効**

---

# 第10ラウンド — P0-9 Ranking Entry (technical_attach 30 秒 / loop 71 秒)

対象ログ: `X:\logs\console\console_20260827_125946_75344.log` (145,466 行)

## 1. まず全体を計測した

`[RANKING ENTRY LOOP] done ... elapsed=` を集計:

| 指標 | 値 |
|---|---|
| n | 41 |
| median | **14.80s** |
| mean | 21.06s |
| max | **71.28s** |
| budget_sec | 30.0 |

phase ログで説明できていたのは 2 つだけだった:

| phase | n | median | sum |
|---|---|---|---|
| technical_attach | 40 | 3.96s | 304.6s |
| prepare_rows | 41 | 0.10s | 5.8s |
| per-symbol scoring | 9 | 0.00s | 0.0s |

**median 14.80 − 3.96 − 0.10 = 約 10.7 秒がどの phase にも属していなかった。**
Summary worker と同じ「未計測区間」問題が Ranking Entry にもあった。

---

## 2. technical_attach = 304.6s の内訳

```
[RANKING TECH] readonly tech skipped reason=table_missing   40 / 40 回 (100%)
[RANKING TECH] readonly tech loaded ... elapsed=            n=40 median=1.248s sum= 88.7s
[RANKING TECH] technical save skipped rows=# readonly_hit=0 n=40 median=1.492s sum=108.7s
```

### 真因: 誰も書かないテーブルを毎 tick NAS へ探しに行っていた

DB を直接確認した:

```
db     = \\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\ranking\ranking20260827.db
tables = [..., 'ranking', 'ranking_ma_1min', 'ranking_snapshot_1min',
          'ranking_raw_1min', 'ranking_summary_1min']
ranking_technical_1min -> **存在しない**
```

`RANKING_ENTRY_SKIP_TECH_SAVE=1` が既定なので、`_ensure_schema` を通る唯一の経路
(書き込みパス) が一度も走らない = テーブルが永久に作られない。
その結果:

1. readonly lookup が毎回 `table_missing` で 1.248s を捨てる (sum 88.7s)
2. 全銘柄が missing になる (`readonly_hit=0`)
3. summary history fallback に落ちる
4. fallback は **1 回の Entry につき `GC.get_summary_history(tf)` を tf=1/3/5 の 3 回**
   呼び、しかも全フレームを `.copy()` していた
   (600 秒窓で `SUMMARY HISTORY GET` が 3,003 行 = 約 1,001 回出ていた正体)

304.6 − 108.7 = **195.9 秒が 4. の fallback**。

### 修正

**(a) `trading/ranking/ranking_technical_store.py`**

```python
# table_missing の負のキャッシュ (恒久 skip ではない。TTL 経過後は再確認する)
os.environ.setdefault("RANKING_ENTRY_TECH_TABLE_PROBE_TTL_SEC", "60")
_TECH_TABLE_MISSING_AT: Dict[str, float] = {}
```

- `_tech_table_known_missing(db)` / `_tech_table_mark_missing` / `_tech_table_mark_present`
- 書き込み経路 (`_ensure_schema` を通る) が走ればその場で `mark_present` するので、
  書き込みモードへ戻せば自動的に拾い直す
- **fail-open ではない。** テーブルが無いという事実は変わらず、
  「無いことを毎秒 NAS に確かめ直さない」だけ

**(b) 接続 leak の修正 (同ファイル 2 か所)**

```python
- with sqlite3.connect(db, timeout=1.5) as conn:      # close しない
+ with readonly_connect(db, timeout=1.5, busy_timeout_ms=1200) as conn:

- with sqlite3.connect(path, timeout=30.0) as conn:   # close しない
+ with closing_connect(path, timeout=30.0) as conn:
+     ...
+     conn.commit()
```

Ranking Entry は毎 tick 走るので、ここは FD leak の温床だった。

**(c) `trading/ranking/entry_from_ranking.py` — tech map の memory cache**

```python
_RANKING_TECH_CACHE: Dict[int, Any] = {}
_RANKING_TECH_CACHE_LOCK = threading.Lock()

def _ranking_tech_cache_ttl() -> float:
    return max(0.0, _env_float("RANKING_ENTRY_TECH_FALLBACK_CACHE_TTL_SEC", 30.0))
```

- `_ranking_entry_fix_get_gc_history(tf, symbols=...)` — **必要銘柄に絞ってから copy**
- 要求銘柄が **全部そろっている時だけ** cache を使う
  (部分ヒットで返すと欠けた銘柄の technical を捨てることになるため)
- env 名は `RANKING_ENTRY_TECH_CACHE_TTL_SEC` (既存・既定 90 秒、
  `ranking_technical_store` の DB lookup cache 用) と**衝突するので分けた**。
  Entry に実際に渡る値なので既定は短い 30 秒

---

## 3. 未計測だった 10.7 秒の正体 — ranking_source

`_get_ranking_source_df()` は `start` ログ直後にあり、計測点が無かった。
中身は **NAS ranking DB fallback**。

```
[RANKING ENTRY SOURCE DB FALLBACK] probe_elapsed=  n=38 median=4.54s max=27.32s sum=253.9s
```

per-table のログから内訳を分解した:

| 項目 | n | median | sum |
|---|---|---|---|
| `MAX(datetime)` ranking_snapshot_1min | 39 | 0.081s | 6.0s |
| `MAX(datetime)` ranking_raw_1min | 39 | 0.084s | 6.5s |
| `MAX(datetime)` ranking_summary_1min | 39 | 0.109s | 8.6s |
| **SQL 合計** | | | **21.0s** |
| **probe_elapsed 合計** | | | **253.9s** |
| **差 = SQL ではない部分** | | | **232.9s (92%)** |

232.9 秒の正体は 1 loop あたりの metadata 往復:

```
sqlite3.connect(NAS)                                  1 回
_rsrc_candidate_tables -> SELECT 1 FROM sqlite_master  6 回 (候補 6 テーブル)
_rsrc_table_columns    -> PRAGMA table_info            3 回
```

SMB 越しに 10 往復。**しかもスキーマは日中変わらない。**
`picked latest table` は 39/39 回すべて `ranking_snapshot_1min` だった。

### 修正

**(a) スキーマキャッシュ** (`RANKING_ENTRY_SOURCE_DB_SCHEMA_TTL_SEC` 既定 300 秒)

```python
_RSRC_SCHEMA_CACHE: Dict[str, Dict[str, Any]] = {}   # db_path -> {ts, tables, cols}
```

TTL 経過後は実際に問い合わせ直すので、テーブルが後から出来ても拾える。

**(b) 接続 leak**

```python
- with sqlite3.connect(str(db_path), timeout=sqlite_timeout) as conn:
-     conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
-     conn.execute("PRAGMA query_only=ON")
+ with readonly_connect(str(db_path), timeout=sqlite_timeout,
+                       busy_timeout_ms=busy_timeout_ms) as conn:
```

**(c) 死んでいた df キャッシュを直した**

```python
- cached = _rsrc_cache_get(str(getattr(conn, "_rsrc_db_path", "")), table)
+ cached = _rsrc_cache_get(db_path or _rsrc_conn_db_path(conn), table)
```

`sqlite3.Connection` には属性を代入できない (`conn._rsrc_db_path = ...` は
try/except で握られて常に失敗していた) ため、db_path が常に `""` になり
**10 秒 df キャッシュが一度もヒットしていなかった**。

**(d) `MAX(datetime)` の二度引きをやめた**

`_rsrc_latest_per_table()` が引いた値を `_rsrc_read_one_table()` へ渡す。
cutoff の計算式も結果も変えていない。

---

## 4. さらに判明した無駄 (次ラウンドの計測対象)

```
indexed read (成功)     n=15 median=0.303s sum=  9.3s
indexed read aborted    n=27 median=2.440s sum= 87.5s   budget=2.0s err=interrupted
  -> rowid read (成功)  n= 5 median=1.409s sum=  6.7s
  -> rowid read failed  n=22 median=2.470s sum= 76.7s   err=interrupted
deadline exceeded -> use last-good                n=22
```

**39 loop 中 22 loop は、約 5 秒 (2.44 + 2.47) NAS を叩いて何も得られず、
結局 last-good (age 243〜394 秒) を使っていた。**

また memory の `latest_ranking_snapshot` が stale と判定される理由は
**ranking DB 自体が 6 分遅れている**ことだった:

```
stale/unknown source=latest_ranking_snapshot rows=900 latest=2026-08-27 13:02:00
    age=384.8 col=datetime max_age=300.0s -> try DB refresh
```

DB の MAX(datetime) も同じ 13:02。つまり Entry は毎回
「DB にも新しいものが無い」ことを 5〜8 秒かけて確かめ直していた。

これは ranking collector の書き込み遅延であって Entry 側の問題ではない。
**timeout を伸ばす修正は禁止**なので、
スキーマキャッシュで NAS 往復を削った効果を先に実測してから次を決める。

---

## 5. Ranking Entry loop の phase 計測を追加した

未計測区間を残さないため、Summary worker と同じ手口で計測点を置いた:

```
[RANKING ENTRY LOOP] phase ranking_source=%.2fs prepare_rows=%.2fs prefilter=%.2fs ...
[RANKING ENTRY LOOP] phase breakdown total=%.2fs phase_sum=%.2fs unaccounted=%.2fs
    ranking_source=... prepare_rows=... prefilter=... technical_attach=...
    scoring=... reset_histories=... pending_add=...
```

`unaccounted > 2.0s` で WARNING。**処理も判定も変えていない。計測だけを足した。**

---

## 変更ファイル (第10ラウンド)

| ファイル | 内容 |
|---|---|
| `trading/ranking/entry_from_ranking.py` | tech map cache / history の symbol 絞り込み / スキーマキャッシュ / 接続 leak 修正 / df キャッシュの db_path 修正 / MAX 二度引き解消 / loop phase 計測 |
| `trading/ranking/ranking_technical_store.py` | table_missing 負キャッシュ (TTL 付き) / 接続 leak 修正 (read・write 両方) |
| `test_script/test_ranking_entry_tech_cache.py` | **新規** 16 tests |
| `test_script/test_ranking_entry_source_schema.py` | **新規** 10 tests |

## テスト結果

- `test_ranking_entry_tech_cache.py` **16 passed** (127.53s)
  - `[P0-9] build=23.8ms cached=0.0ms speedup=2804.6x rows=22110`
  - cache 結果が build 結果と **完全一致** することを検証 (痩せていない)
  - 部分ヒットでは cache を使わないことを検証
  - `outstanding == 0` (接続 leak なし)
  - table_missing は TTL 経過後に **再確認する**ことを検証 (恒久 skip ではない)
- `test_ranking_entry_source_schema.py` **10 passed** (121.14s)
  - `table_info` / `sqlite_master` の往復が 2 回目に消えることを検証
  - TTL 経過後は再度問い合わせることを検証
  - `RANKING_ENTRY_SOURCE_DB_SCHEMA_TTL_SEC=0` で無効化できることを検証
  - 存在しない DB を **新規作成しない**ことを検証
- `test_no_undefined_names.py` 10 passed
- 実注文 **0 件** (dry-run のみ)

## 禁止事項の順守

| 禁止事項 | 本ラウンドでの扱い |
|---|---|
| timeout を単純に延長 | していない (budget 2.0s / deadline 8.0s / 30s は据え置き) |
| 閾値・売買条件の緩和 | していない (max_age 300s / failopen 1800s も据え置き) |
| fail-open で誤魔化す | していない。table_missing は「無い」まま。TTL 付きで再確認する |
| エラーの握り潰し | していない。abort/interrupted はそのまま WARNING に出る |
| Entry 安全ガードの除去 | していない |
| 大規模リファクタリング | していない (計測点の追加と接続/キャッシュの最小修正のみ) |

## 残件

1. **P0-9 実測確認** — 再起動後に `phase breakdown` で
   ranking_source / technical_attach の before-after を確定する
2. **709 秒の正確な位置** — heartbeat で確定 (第9ラウンドの仕込み)
3. **P0-3** bounded 処理 (chunk 化して cancel 後 5 秒以内に return)
4. **P0-5** 363,984 行 seed の realtime 排除
5. **P0-7** fast lane / heavy lane 分離
6. **ranking collector の書き込み遅延** (DB の MAX(datetime) が 6 分遅れ) —
   Entry 側ではなく collector 側の課題
7. 第9・第10ラウンドの修正は **次回再起動で有効**

### 補正: db_path の紐づけを thread-local へ移した

最初の実装は `_RSRC_CONN_DB_PATH[id(conn)] = db_path` という辞書だったが、
`with` の中に `return` が 2 か所 (no candidate table / cache hit) あり、
例外でも抜けるため **pop に到達しない経路があった**。
`id()` は解放後に再利用されるので、
**別の接続に古い db_path が紐づき、別 DB のスキーマをキャッシュから返す**危険があった。

thread-local + try/finally に変更した。return でも例外でも必ず戻る。

```python
_RSRC_TLS = threading.local()

@contextlib.contextmanager
def _rsrc_bind_db_path(db_path: str):
    prev = getattr(_RSRC_TLS, "db_path", "")
    _RSRC_TLS.db_path = str(db_path or "")
    try:
        yield
    finally:
        _RSRC_TLS.db_path = prev
```

DB 読みは背景スレッド 1 本で直列に走るのでこれで足りる。
束ねが解けることを test で明示的に検証した
(`test_bind_restores_on_exception` / `test_bind_restores_on_return` /
`test_bind_nests` / `test_core_unbinds_db_path`)。

## 最終テスト結果 (第10ラウンド)

| テスト | 件数 | 結果 |
|---|---|---|
| `test_ranking_entry_tech_cache.py` | 16 | passed |
| `test_ranking_entry_source_schema.py` | 16 | passed |
| `test_no_undefined_names.py` | 10 | passed |
| `test_entry_pipeline.py` / `test_ranking_enrich_cache.py` ほか | — | passed |
| 併走まとめ実行 (ranking + entry 8 ファイル) | 63 | passed (exit 0) |
| 最終まとめ実行 (5 ファイル) | 39 | passed (exit 0) |

実注文 **0 件**。プロセスは起動しておらず、すべて静的検証と単体テストのみ。

## 未対応で残した接続 CM (次ラウンド候補)

`with sqlite3.connect(...)` が残っている箇所のうち、
hot path の可能性があるもの (mirror ベースの一覧なので live で要再確認):

- `core/startup/summary/push_incremental_ma75.py` (3 か所)
- `core/startup/push/bootstrap.py` (2 か所)
- `core/startup/bootstrap/db.py` (3 か所)

`AI/train/*` `AI/analysis/*` はオフライン学習用スクリプトなので優先度は低い。

---

# 第11ラウンド — positions DB `disk I/O error` の真因特定 (2026-08-27 15:30 ログ)

対象ログ: `X:\logs\console\console_20260827_125946_75344.log` (259,489 行 / 61.8 MB)
プロセス: 12:59:46 起動 / PID 75344、ログ末尾 15:30:54 (大引け後に停止済み)
作業日: 2026-08-27
実注文・実返済注文: **0 件** (プロセス停止中。全テストは mock / 一時DB / 読み取り専用コピー)
本番プロセス: **未再起動**。以下はすべて次回再起動で有効になる。

## 前回から維持できている項目 (回帰確認)

| 項目 | 実測 |
|---|---|
| `Too many open files` | **1 件** (15:30:37 — 下記 P0-1 の最終症状。前回 0 から回帰) |
| APIキー不一致 | 0 |
| `ORDER SUBMIT` | 0 |
| `pending_moved_without_order` | 実イベント **2 件** (15:14:25 / 15:21:37。文字列 47 は同一 traceback の重複) |
| Ranking Entry build / schedule / worker | 0.13s / 0.289s / 0.54s |
| broker positions API | total=29 ok=29 ng=0 |

`Too many open files` と `pending_moved_without_order` は 0 ではなかった。両方とも下に記録した。

---

## P0-1 positions DB `disk I/O error` — 原因

### 推測を潰した順序 (再現ハーネスによる実測)

原因を推測で決めないため、まず候補を実測で潰した。ハーネスは
`scratchpad/tests/repro_nas_sqlite.py` / `repro_handle_pressure.py` / `repro_handle_leak.py`。

| 仮説 | 実測 | 判定 |
|---|---|---|
| NAS(SMB) 上の WAL が壊れている | positions.db を journal_mode=WAL/TRUNCATE/DELETE の3通りで 8スレッド x 20秒、389〜411 q/s。**err=0** | **棄却** |
| 並列度が高すぎる | 40スレッド / 8ライター / 25秒、485 q/s。**err=0** | **棄却** |
| NAS のハンドルを大量に握っている | NAS 上に 200→4000 ファイルを open したまま同じ負荷。**err=0** | **棄却** |
| プロセス全体のファイル/ハンドル枯渇 | 下記の同時多発と `Errno 24` で確認 | **採択** |

### 決め手となったログ

`disk I/O error` は positions.db 固有ではなかった。**同時刻に全 NAS DB が落ちている**。

```
15:21:44,407 [ERROR] database.session: [DB PRAGMA] failed
    db=\\192.168.0.22\...\positions\positions.db pragma=synchronous
    err=OperationalError('disk I/O error')
15:21:47,224 [ERROR] trading.push.latest_price_db_feeder: [MAIN PRICE DB FEED] cycle failed
    latest_price_db_feeder.py line 110 in _connect
    sqlite3.OperationalError: unable to open database file      <- push DB
15:21:48,048 [ERROR] trading.exit.executor: [EXIT CLOSING RECONCILE] loop error
    sqlalchemy/pool/base.py __connect
    sqlite3.OperationalError: unable to open database file      <- positions DB
...
15:30:37,423 [ERROR] trading.ranking.logging.ranking_logger: [ranking_logger] csv save failed
    OSError: [Errno 24] Too many open files: 'logs/ranking/ranking_1m_20260827_153037.csv'
                                              ^ ローカル相対パス。NAS ですらない
```

`disk I/O error` (SQLITE_IOERR) と `unable to open database file` (SQLITE_CANTOPEN) は
どちらも「OS がファイル操作を拒否した」ときの SQLite 側の表現で、
最後はローカルパスの `open()` まで `EMFILE` で落ちている。
つまり **プロセスが保持するファイル/ハンドルが枯渇していた**。

補強データ: 本作業シェルは UNC への SMB セッションを持っておらず
(`os.path.isdir(r'\\192.168.0.22\AutoStockBuyAndSell')` -> False)、
その状態で同じ DB を開くと **`unable to open database file` と `disk I/O error` が再現する**
(ドライブレター `X:` 経由なら同じ DB が正常に開ける)。
NAS へ到達できないときに出る症状が本番ログのそれと一致する。

### リーク元

`trading/runtime_persistence/runtime_state_store.py` — **全 11 関数**が

```python
with _connect(path) as conn:      # close() がどこにも無い
```

の形だった。`with sqlite3.Connection` は **トランザクションを commit/rollback するだけで
接続は閉じない** (Python 公式仕様)。参照が traceback / cursor / `logger.exception` の
フレームに残ると GC まで生き残る。

実測 (`repro_handle_leak.py`):

| 形 | Win32 ハンドル増加 (500 接続) |
|---|---|
| `with` のみ・参照保持 | **+1502** (1 接続 = 3 ハンドル) |
| 明示 `close()` | **+0** |

`BASE_DIR` は NAS (`\\192.168.0.22\...\runtime_state`)。
さらに `save_*` が毎回 `ensure_runtime_state_db()` を呼んでいたため
**1 回の保存で NAS へ接続 2 本**走っていた
(実測 `[RUNTIME STATE] schema ensured` **420 回**)。

`NullPool` で positions.db を毎クエリ開き直す 6 系統がその上に乗っていたため、
positions.db が最初に音を上げた。

### 障害の時系列

| 項目 | 値 |
|---|---|
| disk I/O 最初の発生時刻 | **2026-08-27 13:30:07** (単発、`exit_closing_reconcile_loop`) |
| 最後の発生時刻 | **2026-08-27 15:30:50** (ログ末尾まで継続) |
| タイムスタンプ付きイベント | 40 件 (`disk I/O error` 256 行 + `unable to open` 127 行は同一 traceback の重複を含む) |
| 独立障害推定回数 | **5 エピソード** (間隔 30 秒以上で分割) |
| エピソード内訳 | 13:30:07 (1) / 15:21:44–15:22:30 (46s) / 15:25:09 (1) / 15:26:00–15:27:46 (106s) / 15:28:29–15:30:50 (141s) |
| ログ間引きの実態 | `fail_count` が 15:21:56 の 5 -> 15:28:58 に **200**。422 秒で 195 回 = **0.46 回/秒**。ログは間引かれていただけ |

### positions DB path / engine / session 構成 (実測)

| 項目 | 値 |
|---|---|
| positions DB path | `\\192.168.0.22\AutoStockBuyAndSell\trading\runtime\positions\positions.db` |
| サイズ / 行数 | 159,744 bytes / 31 行 (全件 `CLOSED`) |
| journal_mode / synchronous / busy_timeout | `wal` / `2 (FULL)` / `30000` |
| engine 数 | `database/session.py` `_position_engine` **1 本** (`sqlite:///` + `NullPool`) |
| Session | `sessionmaker(bind=_position_engine)`、`Session_position` proxy 経由 |
| pool | **NullPool** = クエリごとに新規接続・接続再利用なし |
| 同じ DB への複数 engine | なし (1 DB 1 engine を確認) |
| 失敗していた PRAGMA | `synchronous` のみ (plan の中で最初にページャ/スキーマを開く行) |

`journal_mode=WAL` は**変更していない**。上表のとおり WAL は原因ではないと実測で確認したため、
NAS 上の稼働中 DB へ journal_mode 変換をかけるリスクを取る理由がない。

### positions DB query/sec 実測 (13:00–15:30 = 9,000 秒)

| 経路 | 回数 | rate |
|---|---|---|
| `OPEN POSITION SYNC] DB scan` | 1,620 | 0.18 /s |
| `EXIT CLOSING RECONCILE] helper positions` | 1,392 | 0.155 /s |
| `EXIT LOOP] no open positions` | 1,487 | 0.165 /s |
| `PositionSync] OPEN positions updated` | 428 | 0.048 /s |
| 合計 (positions.db を触る読み) | **約 4,900** | **約 0.55 /s** |

NullPool なので これがそのまま **NAS への新規接続 0.55 本/秒**。

---

## P0-1 修正

### 1. `trading/runtime_persistence/runtime_state_store.py`

- `_connect()` を `@contextlib.contextmanager` にし、**必ず `close()`** する
  (commit / rollback / close を finally で保証)
- `journal_mode` は DB に永続するので毎接続で投げ直さない (NAS 往復を削減)
- `_SCHEMA_DONE` キャッシュを追加。`ensure_runtime_state_db()` は 1 日 1 回。
  `save_* / load_* / mark_*` の 9 か所を `_ensured_path()` に切替え、
  **1 保存あたりの接続を 2 本 -> 1 本**へ
- `connection_stats()` で opened / closed / outstanding を公開 (リーク検知用)

### 2. `database/session.py`

- engine の `connect` / `close` / `close_detached` イベントで接続を実数カウント。
  `connection_stats()` が DB ごとに opened / closed / outstanding / peak_outstanding を返す
  (NullPool では SQLAlchemy の checkedin/checkedout が意味を持たないため)
- `[DB PRAGMA] failed` 時に `[IO ERROR CHRONOLOGY]` を必ず添える (下記 P0-6)

`SQLITE_JOURNAL_MODE` / `SQLITE_SYNCHRONOUS` は**変更していない**。

---

## P0-2 / P0-3 / P0-4 positions read の一本化と fail-safe

### 新規 `trading/position/position_state_service.py`

positions の **read owner を 1 本**にした (`PositionStateService`)。

- TTL キャッシュ (既定 1.0s) + **singleflight**。全 consumer は同じスナップショットを共有
- DB 読みは `load_open_positions_from_db_strict()` = **失敗は例外**。`{}` を返さない
- DB 障害時は **bounded exponential backoff** (1->2->5->10->30 秒上限)
- **DB が落ちていても broker の建玉確認は毎回走る** (安全確認を止めない)
- `POSITIONS_STATE` ログに `db_ok` / `broker_ok` / `source_used` / `last_good_age` /
  `entry_allowed` / `positions_known` / `count` / `symbols` を必ず出す

broker fallback 判定表:

| ケース | DB | broker | source_used | positions | entry_allowed |
|---|---|---|---|---|---|
| A | OK | OK | `db+broker` | マージ | True |
| B | disk I/O | OK (空) | `broker_authoritative` | 空 (broker が正) | True |
| C | disk I/O | OK (建玉あり) | `broker_authoritative` | **保持** -> EXIT 対象 | True |
| D | disk I/O | NG | `last_good` | **last-known を保持** (0 件にしない) | **False (fail-closed)** |

### `trading/position/open_position_sync.py`

- `load_open_positions_from_db_strict()` を追加 (`PositionsDbUnavailable` を送出)
- 互換版 `load_open_positions_from_db()` は `{}` のままだが `_mark_db_read_failed()` を記録し、
  `db_read_ok()` / `db_read_status()` で **empty と failure を区別できる**ようにした
- `is_symbol_in_open_position()` を **fail-closed** 化。建玉状態が unknown なら
  「保有している」を返して二重建玉を防ぐ。判断前に必ず `force=True` で取り直す

### `core/position_sync.py`

- DB 失敗時に bounded backoff (1->2->5->10->30)。旧実装は 10 秒ごとに無条件再試行
- 失敗時は `global_data.open_positions` に**一切触れない** (last-known を保持)

### `trading/exit/exit_utils.py`

- `get_open_positions_safe()` が 4 ソースとも空だった場合、
  PositionStateService の last-known を復元してから返す
- `POSITIONS_STATE` を EXIT ループからも出す
- 建玉状態が unknown のときは `no open positions` ではなく
  **`建玉状態 unknown。0 件とは扱わない`** を ERROR で出す

### `entry_allowed()` は必ず force 読み

負荷試験で、DB と broker が**同時に落ちた直後の TTL 1 回分**だけ
「障害前の状態」で ENTRY が許可される窓が **102 件**観測された。
発注ゲートである `entry_allowed()` は `force=True` で必ず取り直すようにし、
キャッシュ経由のフィールドも齢が `POSITION_STATE_ENTRY_MAX_AGE_SEC` (既定 1.0s) を
超えたら `entry_allowed=False` へ降格させる。修正後の違反は **0 件**。

---

## P0-5 WAL / PRAGMA

**変更していない。** 理由は上記のとおり WAL が原因ではないと実測で確認したため。
現状の PRAGMA 方針 (前ラウンドから継続):

- 永続するのは `journal_mode` だけ。`synchronous` / `foreign_keys` は接続ごとに設定
- 読み取り専用 engine (`mark_engine_readonly`) には書き込み系 PRAGMA を投げない
- `cache_size` は既定で設定しない (NullPool で無意味かつ SMB を圧迫するため)
- 重要 PRAGMA は `foreign_keys` のみ。他は失敗しても接続を捨てない
  (捨てて再接続するとリトライストームになる)

`runtime_state_store` 側で毎接続の `journal_mode` 発行をやめたのは、
値の変更ではなく **NAS 往復の削減**が目的。

---

## P0-6 chronology instrumentation

新規 `core/diagnostics/resource_monitor.py`。`main()` の先頭で起動する。

30 秒ごと (`RESOURCE_MONITOR_INTERVAL_SEC`) に記録:

- `handles` (psutil `num_handles` / なければ Win32 `GetProcessHandleCount`)
- `open_files` 数と **ディレクトリ別内訳** (どの領域を掴んでいるか)
- `net_connections` / `num_threads` / スレッド名の接頭辞別内訳 / `rss_mb`
- SQLAlchemy pool (`checkedin` / `checkedout` / `overflow` / `size`)
- engine ごとの接続 `opened` / `closed` / `outstanding` / `peak_outstanding`
- `runtime_state_store` の接続 opened/closed/outstanding
- PositionStateService の db_reads / db_errors / db_error_episodes / cache_hits

`disk I/O error` 発生時は `log_io_error_chronology()` が
**直前 5 秒のリングバッファ**を吐く。ハンドルが警告水位 (既定 4000) を超えたら
リーク元候補を名指しで ERROR に出す。

`psutil` は venv に未インストールだったので導入し (`psutil 7.2.2`)、
`requirements.txt` にも追記した。psutil が無くても ctypes で動く。

Windows handle / thread metrics 動作確認 (実測):
```
handles=192 num_threads=7 open_files=1 net_connections=0 rss_mb=57.4
pools={'position': {'pool': 'NullPool'}}
engine_conns={'t.db': {'opened': 8, 'closed': 8, 'outstanding': 0, 'peak_outstanding': 1}}
```

---

## P0-7 push:1 zombie / cancel latency

### 実測 (修正前)

```
15:24:00,183 worker begin kind=push:1 generation=30 owner=tick:20260827152400 budget=25.0s
15:24:02,078 stage_end freshness_timeline   elapsed=1.864s
15:24:09,213 stage_end tier1_direct_memory  elapsed=7.104s
15:25:00,125 cancel requested               elapsed=59.942s  reason=superseded_by_tick
15:27:49,221 cooperative stop               elapsed=229.038s
15:30:40,222 stage_end light_push_runner    elapsed=390.939s
15:30:41,132 worker end                     total=400.948s over_budget=True
```

**push:1 cancel latency = 15:25:00 -> 15:30:41 = 340 秒。** 原因は
`light_push_runner` (= summary controller 本体) の中に降り場所が 1 つも無かったこと。

### 修正 (`scheduler_jobs/summary/stage_tracker.py`)

- `SummaryWorkerCancelled(BaseException)` を追加。
  `BaseException` 継承なのは、途中の `except Exception:` に握り潰されて走り続けないため
- `stage()` の**入口で cancel を判定**し、cancel 済みなら例外で降りる
- `checkpoint(label)` を追加。ループ内側 (銘柄ごと / チャンクごと) の降り場所
- `worker()` が `SummaryWorkerCancelled` を受け止めて外に漏らさない
- `cancelled_at` / `cancel_latency` / `cancel_sla_breached` を記録し、
  `worker end` ログに `cancel_latency=` `sla_breached=` を必ず出す
- **cancel watchdog**: SLA (既定 5 秒) を超えても降りられていなければ、
  どの stage で止まっているかを ERROR で出し、**そのスレッドのスタックをダンプ**する
- `parallel_intervals._job_one_source()` は cancel されても戻り値の形
  `(source, interval, DataFrame)` を保つよう修正

### 実測 (修正後 / テスト)

| ケース | cancel latency |
|---|---|
| stage 境界で降りる (0.2 秒 stage x 200) | **0.101 s** |
| stage 内 `checkpoint()` で降りる (0.01 秒ループ x 2000) | **0.010 s** |

いずれも SLA 5 秒に対して 2 桁の余裕。SLA 違反フラグは立たず。

---

## P0-8 / P0-9 22,851 行の realtime 混入

### 22,851 発生 call chain (実測)

| logger | ログ | 回数 |
|---|---|---|
| `trading.summary.controller_layers` | `[SUMMARY CONTROLLER READY MTF PATCH]` | 507 |
| `global_state` | `[SUMMARY SETTER ENRICH]` | 295 |
| `trading.summary.mtf.daily_runtime` | `[DAILY MTF RUNTIME]` | 189 |
| `trading.summary.mtf.daily_ma_mtf` | `[DAILY MTF]` | 189 |
| `core.global_context.context` | `[MERGED SET]` | 169 |
| `core.global_context.context` | `[GC SUMMARY REPAIR]` | 169 |
| `trading.summary.controller_layers` | `[SUMMARY PUBLISH MTF MERGED]` | 159 |
| `trading.summary.controller_cache` | `attach_display_ready rows=22851 cols=281 display_symbols=349` | — |

実体は **349 銘柄 x 平均 65 本** の履歴フレーム x 281 列。
`summary_controller._diff_update_core()` が `history-base` を作り、
indicator -> scoring -> display -> MTF -> publish の全段をこの行数で通していた。
`[GC SUMMARY REPAIR]` に至っては `for idx, row in out.iterrows():` で 22,851 行を全走査。

`[summary_controller] merged-history interval=1 symbols=349 rows=22851
 hist_len[min=1 p25=45 med=75 p75=82 max=117 mean=65.48] latest_dt=2026-08-27 14:08:00`

### 修正 — 新規 `trading/summary/realtime_history_guard.py`

realtime publish 境界で `rows > 2000` (`REALTIME_HISTORY_GUARD_MAX_ROWS`) なら

1. 当日のみに絞る
2. `datetime` でソートし `symbol` ごとに最新 1 行へ畳む
3. **WARNING `[REALTIME_HISTORY_LEAK]`** を
   `caller` / `rows_before` / `rows_after` / `latest_before` / `latest_after` 付きで出す

履歴フレーム自体は storage / indicator seed には必要なので消さない。
守るのは「realtime consumer へ渡す前」だけ。

配線した箇所 (P0-8 の指定リストを網羅):

| 指定 | 配線先 |
|---|---|
| `rebuild_display_ready` / `attach_display_ready` / `rebuild_technical_ready` | `controller_layers.apply_layers()` の入口と出口 (5 関数すべてがここを通る) |
| `GC SUMMARY REPAIR` (realtime) | `core/global_context/context._repair_merged_summary_from_history()` |
| `DAILY MTF` (realtime) | `trading/summary/mtf/daily_ma_mtf.attach_daily_ma_mtf_to_summary()` |
| `set_merged_summary` / `set_push_merged_summary` | `global_state.GlobalDataCompat._realtime_projection()` に共通ガードを前置 |
| Entry / Tonosama / AI latest / Display | 上記 publish 境界の下流なので 349 行しか届かない |

### 実測 (負荷試験)

23,034 行 x 281 列 -> **349 行**、median **0.0567 秒**。
既に 349 行のフレームは素通しで **0.003 ms/回** (実質ゼロコスト)。

---

## P0-10 / P0-11 1m freshness

### 原因 1 — `global_data.push_df` の逆流

```
15:04:41 [SUMMARY MAIN PUSH DF WRITEBACK] latest 14:08:10 -> 15:03:03  rows=5000
15:11:09 [SUMMARY FORCE DIRECT 1M] reject stale push memory latest=14:08:10 rows=644
15:13:02 [SUMMARY MAIN PUSH DF WRITEBACK] latest 14:08:10 -> 15:11:04  rows=5000
15:17:07 [SUMMARY FORCE DIRECT 1M] reject stale push memory latest=14:08:10 rows=644
15:19:11 [SUMMARY MAIN PUSH DF WRITEBACK] latest 14:08:10 -> 15:16:44  rows=5000
15:24:08 [SUMMARY FORCE DIRECT 1M] reject stale push memory latest=14:08:10 rows=644
```

書き戻すたびに「更新前」が **毎回 14:08:10 に戻っている**。
`_writeback_push_df()` 自身は単調ガードを持っていたが、
その外側で誰かが 644 行 / 14:08:10 のフレームで上書きしていた。

**修正**: `global_state.GlobalDataCompat.push_df` を **データ記述子 (property)** にした。
データ記述子はインスタンス辞書より優先されるので、
`setattr(global_data, "push_df", x)` を含む**すべての書き込み**がセッターを通る。

- 現在より古い `latest` のフレームでの上書きを**拒否**し、書き手 (caller) を名指しする
- 同じ `latest` でより行数が少ないフレームも拒否 (縮退での置換防止)
- `latest` 判定不能なフレームでは潰さない
- `PUSH_DF_MONOTONIC_GUARD=0` で従来動作

### 原因 2 — main.py 自身の PUSH WS バッファが 14:08:10 で止まっていた

```
15:24:09 reject stale push memory source=push_stream.get_push_dataframe
         latest=2026-08-27 14:08:10 age=4550.0s rows=2488
15:28:23 [PUSH MONITOR] connected=True ws_alive=True df_rows=16129 total_received=16129
         total_flushed=0 flush_alive=False writer_ready=False memory_only=True
         last_recv=2026-08-27T15:28:23   <- 到着は新しい
```

`last_recv` は新しいのに、フレームの `datetime` 最大値は 14:08:10 のまま。
`connection_generation` は 335 -> 392 (WS 張り直しが 15:21 以降で毎分 5〜15 回)、
`[PUSH MONITOR][WS STALE]` が反復。張り直しのたびに初期スナップショットだけ受けて
live 更新を掴めていない状態。

一方、`push_receiver` が書く PUSH DB は健全:

```
push20260827.db stream_data_raw  rows=45,359
  datetime    min=10:16:29  max=15:30:52
  received_at min=10:16:29  max=15:30:52   (datetime == received_at)
```

**修正 — FAST LANE** (`scheduler_jobs/summary/direct_push_1m._candidate_push_frames()`):

メモリ候補が**全部 stale** (> `SUMMARY_FORCE_DIRECT_MAX_OUTPUT_AGE_SEC`) なら
PUSH DB の tail を読んで候補の先頭に入れ、`global_data.push_df` にも書き戻す。
heavy な historical worker は待たない。単調ガードがあるので古い方で潰されない。
`SUMMARY_FORCE_DIRECT_PUSH_DB_FASTLANE=0` で従来動作。

### 349 projection publish 結果 / merged latest (修正前の実測)

```
15:21:22 [SUMMARY STALE DROP] source=push tf=1 label=merged_sanitize
         before=349 after=0 max_age_sec=300 latest_age_sec=4402.2
15:30:41 [SUMMARY STALE DROP] source=push tf=1 label=merged_get
         before=288 after=0 max_age_sec=300 latest_age_sec=5021.0 latest_dt=2026-08-27 14:07:00
```

349 行の latest projection は**作られていた**が、`latest_age` 4,402〜5,021 秒で
全件ドロップされていた (merged latest = 14:07〜14:08)。
`max_age` は**延長していない** (禁止事項)。
直したのは供給側 (fresh な PUSH DB を使うこと) と逆流の防止。

---

## P0-12 EXIT TEMP STOP

### 原因

```python
def _bfe_exit_temp_disabled() -> bool:
    return _bfe_env_bool("AUTOSTOCK_TEMP_DISABLE_EXIT", True) or ...
                                                        ^^^^ 既定 True
```

env 未設定 (`None`) で **True = EXIT 停止**。本番は未設定だったため
`[EXIT TEMP STOP] run_exit_pipeline skipped AUTOSTOCK_TEMP_DISABLE_EXIT=None EXIT_TEMP_STOP=None`
が **3,831 行**。返済パイプラインが一度も走っていない。

### 修正

`_bfe_exit_temp_stop_flag()` を新設。**明示的に真と書かれた時だけ停止**する。

| 値 | 判定 |
|---|---|
| `1` / `true` / `yes` / `y` / `on` | **停止 (True)** |
| `None` (未設定) / `''` / `0` / `false` / `no` / `n` / `off` / `none` / `null` | **実行 (False)** |
| 未知の値 | **実行 (False)** + WARNING |

15 パターンの単体テストで全て期待どおり。

---

## P0-13 Exit owner

`main.py` の owner ガードは **14:12 に追加済み**だったが、本番プロセスは
**12:59:46 起動**だったため旧コードで動いていた。
そのため 14:03:42 に `[EXIT SCHEDULER] run_exit_pipeline registered every 1s` が入り、
`exit_loop_5s` (5 秒) と `run_exit_pipeline` (1 秒) の **2 系統**が並走していた
(`jobs=5` -> `jobs=6`)。

現在のソース状態:

| 経路 | 状態 |
|---|---|
| `core/startup/scheduler/exit_bootstrap.py` `exit_loop_5s` (5 秒) | **唯一の Exit owner** |
| `main.py` `run_exit_pipeline` (1 秒) | `EXIT_PIPELINE_OWNER` 既定 `exit_loop_5s` のため**登録しない** |
| `trading/experimental/scheduler.py` (30 秒) | **どこからも import されていない** (grep 確認済み) |

追加で `_audit_exit_owners()` を実装し、登録直後に
`[EXIT OWNER AUDIT] exit_scheduler_owner_count=N owner=... jobs=[...]` を出す。
1 でなければ ERROR + heartbeat NG。次回ログでそのまま検証できる。

**この項目は再起動して初めて解消する。**

---

## P0-14 dry-run fault injection 結果 (実注文なし)

`scratchpad/tests/test_p0_fixes.py` — mock のみ。発注経路には一切触れていない。

| ケース | 期待 | 実測 |
|---|---|---|
| A: DB OK / broker empty | 通常 | `source=db+broker` `known=True` `entry_allowed=True` `count=0` |
| B: DB disk I/O / broker empty OK | broker authoritative empty | `source=broker_authoritative` `known=True` `count=0`、`db_error` に `disk I/O error` が残る |
| C: DB disk I/O / broker 建玉あり | position 保持・EXIT 対象 | `count=1 symbols=['7203']` `source=broker_authoritative` |
| D: DB disk I/O / broker NG | ENTRY 禁止・last-good 保持 | `known=False` `entry_allowed=False` `count=1 symbols=['6758']` `source=last_good` |

---

## テスト結果

### 単体 / dry-run (`scratchpad/tests/test_p0_fixes.py`)

**total=81 passed=81 failed=0**

| 区分 | 件数 |
|---|---|
| P0-12 EXIT TEMP STOP (15 パターン) | 15 |
| P0-9 realtime history guard | 9 |
| P0-11 push_df 単調ガード | 6 |
| P0-14 A/B/C/D | 24 |
| P0-4 backoff / singleflight | 6 |
| P0-7 cancel latency | 4 |
| P0-1 runtime_state 接続リーク | 6 |
| P0-2 fail-closed ENTRY | 5 |
| P0-2 strict loader | 3 |
| その他 | 3 |

### 負荷試験 + fault injection (`scratchpad/tests/load_test_p0.py`)

**total=16 passed=16 failed=0**

| 試験 | 実測 |
|---|---|
| positions 6 系統 x 20 秒 + disk I/O 注入 | consumer **1,686 回 -> 実 DB read 15 回** (cache_hits 1,666)。障害中のリトライ **3 回**。broker 確認は 20 回継続。**建玉が 0 件に落ちた観測 0 件**。独立エピソード 1 |
| ケース D を 8 秒間 x 6 スレッド | 観測 1,572 件。**ENTRY ゲートが開いた回数 0**。**建玉 0 件化 0 件** |
| history guard 23,034 行 x 281 列 | -> 349 行 / median **0.0567 s**。349 行素通し **0.003 ms** |
| runtime_state 連続保存 2,000 回 | 357 回/s、接続 opened 2,000 / **outstanding 0** / **ハンドル増加 0** (旧実装なら +6,000 相当) |
| NAS positions.db 実アクセス 12 スレッド x 30 秒 | **14,362 クエリ / 478 q/s / err=0**、ハンドル 160 -> 161 |

### import smoke

編集した 17 モジュールすべて import 成功 (failures: 0)。

---

## 変更ファイル

| ファイル | 内容 |
|---|---|
| `trading/runtime_persistence/runtime_state_store.py` | 接続リーク修正 (P0-1 真因) / schema キャッシュ / conn 会計 |
| `trading/position/position_state_service.py` | **新規** read owner 一本化 / fail-safe / backoff / POSITIONS_STATE |
| `trading/position/open_position_sync.py` | strict loader / db_read_ok / ENTRY fail-closed |
| `core/position_sync.py` | backoff / 失敗時に last-known を壊さない |
| `trading/exit/exit_utils.py` | POSITIONS_STATE / unknown を 0 件扱いしない |
| `trading/handlers/exit_handler.py` | EXIT TEMP STOP 既定を「止めない」へ |
| `trading/summary/realtime_history_guard.py` | **新規** REALTIME_HISTORY_LEAK ガード |
| `trading/summary/controller_layers.py` | apply_layers 入口/出口にガード |
| `trading/summary/mtf/daily_ma_mtf.py` | DAILY MTF realtime にガード |
| `core/global_context/context.py` | GC SUMMARY REPAIR にガード |
| `global_state.py` | push_df 単調ガード / realtime projection にガード前置 |
| `scheduler_jobs/summary/stage_tracker.py` | cancel 例外 / checkpoint / SLA watchdog / スタックダンプ |
| `scheduler_jobs/summary/parallel_intervals.py` | cancel 時の戻り値の形を保証 |
| `scheduler_jobs/summary/direct_push_1m.py` | PUSH DB FAST LANE |
| `core/diagnostics/resource_monitor.py` | **新規** P0-6 計測 |
| `core/diagnostics/__init__.py` | **新規** |
| `database/session.py` | 接続会計 / IO ERROR CHRONOLOGY |
| `main.py` | resource monitor 起動 / EXIT OWNER AUDIT |
| `requirements.txt` | psutil 追記 |

---

## 完成条件の現状

| # | 条件 | 状態 |
|---|---|---|
| 1 | disk I/O error = 0 | 真因 (接続リーク) を修正。**再起動後の実測で確認が必要** |
| 2 | position DB failure 時も安全状態保証 | 済 (A/B/C/D テスト) |
| 3 | unknown position で ENTRY 禁止 | 済 (負荷試験で違反 0) |
| 4 | positions DB query storm = 0 | 済 (1,686 -> 15 read) |
| 5 | Exit scheduler owner = 1 | ソース上は済。**再起動で有効**。`EXIT OWNER AUDIT` で検証可能 |
| 6 | EXIT TEMP STOP 誤判定 = 0 | 済 (15 パターン) |
| 7 | push:1 > 30s = 0 | cancel は 0.1 秒で降りる。**実測は再起動後** |
| 8 | cancel 後 5 秒以内終了 | 済 (0.101s / 0.010s) |
| 9 | zombie worker = 0 | 済 (SLA watchdog + スタックダンプで検知可能に) |
| 10 | realtime 22,851 行処理 = 0 | 済 (ガード + LEAK ログ) |
| 11 | latest projection = active symbols | 済 (349 = 銘柄数) |
| 12 | merged 1m age < 90s | FAST LANE + 逆流防止を実装。**再起動後の実測が必要** |
| 13 | PUSH fresh -> Summary fresh < 2s | 同上 |
| 14 | Ranking BG < 10s | **未達**。22,851 行の除去と接続リーク修正の効果を再測定してから判断 |
| 15 | Ranking Entry < 5s 維持 | 維持 (0.54s) |
| 16 | pending_moved_without_order = 0 維持 | **2 件発生** (15:14:25 / 15:21:37)。未対応 |
| 17 | Too many open files = 0 維持 | **1 件発生** (15:30:37)。真因を修正済み |
| 18 | API auth 正常維持 | 維持 |
| 19 | ORDER SUBMIT = 0 | 維持 (0 件) |

## P1 Ranking Summary BG

実測 (修正前): `elapsed=238.832s` / `timeout=180.0s` / `timeout_streak=2` / cooldown 60s。
他サンプルも 180〜240 秒、最悪 `bg done elapsed=2017.639s`。

指示どおり **P0 修正後に再測定してから**判断する。
22,851 行の realtime 処理除去 (P0-8/9) と positions DB リトライストーム除去 (P0-4) は
どちらも同じプロセス内の CPU / NAS を奪っていたので、まずその効果を見る。
それでも 10 秒に届かなければ ranking latest snapshot のみ使用へ切り替える。

## P1 PUSH

`connection_generation` 335 -> 392、`[PUSH MONITOR][WS STALE]` 反復、
`total_flushed=0` / `flush_alive=False`。
15:28:12 時点で `connected=True` / `last_recv_age=0.3s` は正常だったので、
PUSH 自体を Summary stale の原因と決めつけてはいない。
ただし main.py 側の WS バッファが 14:08:10 で止まっていたのは事実 (P0-10)。
FAST LANE で PUSH DB を正としたので Summary は WS 不調から切り離した。
**rotation / reconnect 過多そのものは未対応。次ラウンド。**

## 次ラウンドへの持ち越し

1. **再起動して実測** — 1〜19 のうち「再起動後に確認」としたもの
2. **Ranking BG** の再測定 (P1)
3. **PUSH rotation / reconnect 過多** (`connection_generation` 392 / WS STALE 反復)
4. `pending_moved_without_order` 2 件の原因
5. 残る `with sqlite3.connect(...)` (close なし) の掃討 —
   `core/startup/summary/push_incremental_ma75.py` (7)、
   `core/startup/summary/restore.py` (5)、`core/startup/bootstrap/db.py` (3)、
   `core/startup/push/bootstrap.py` (2)。
   今回は hot path (`runtime_state_store`、実測 420 回/セッション) のみ修正した
6. `summary_controller._diff_update_core()` が履歴フレームで
   indicator / scoring を回す構造そのもの (ガードは publish 境界の防波堤であって、
   計算コストの削減にはなっていない)

---

# 第12ラウンド — 残った遅い経路 (2026-08-28 08:48-08:55 ログ)

対象ログ: `X:\logs\console\console_20260828_084851_60784.log` (9,603 行 / 2.34 MB)
プロセス: 08:48:51 起動 / PID 60784
作業日: 2026-08-28
実注文・実返済注文: **0 件** (NAS 上の本番 DB は読み取り専用でしか触っていない)
本番プロセス: **未再起動**。以下はすべて次回再起動で有効になる。
方針: 大改造なし。残った遅い経路だけを原因特定 -> 局所修正。

## 前ラウンドの成果が維持されていることを確認

| 項目 | 実測 |
|---|---|
| `disk I/O error` | **0** |
| `unable to open database file` | **0** |
| `Too many open files` | **0** |
| `EXIT TEMP STOP` | **0** |
| `22851` (realtime 履歴行) | **0** |
| `pending_moved_without_order` | **0** |
| `ORDER SUBMIT` | **0** |
| Realtime Summary rows | 45〜50 (= active symbols) |
| push:1 zombie | **0**、`worker end` が毎回正常に出る |
| Ranking Summary BG | 238s -> **6.8 / 14.4 / 18.9 / 16.7 / 14.1s** |

前ラウンドで入れた `[SUMMARY STAGE] cancel SLA breach` が
**今回の真因特定をそのまま与えてくれた**（下記 P0-1）。

---

## P0-1 / P0-2 / P0-3 真因 — NAS へのログ同期書き込み

### 決め手: cancel SLA watchdog のスタックダンプ

08:55:07 に SLA 違反として自動ダンプされたスタックの末尾:

```
trading/summary/pipeline/indicator_pipeline.py line 764  _log_summary(out, "final", interval)
trading/summary/pipeline/indicator_pipeline.py line 183  _log_readiness_detail(df, stage, interval)
trading/summary/pipeline/indicator_pipeline.py line 137  logger.info(
python313/Lib/logging/__init__.py line 1520  self._log(INFO, msg, args, **kwargs)
python313/Lib/logging/__init__.py line 1681  self.handle(record)
python313/Lib/logging/__init__.py line 1737  hdlr.handle(record)
python313/Lib/logging/__init__.py line 1026  with self.lock:      <-- ここで止まっていた
```

ワーカーは計算ではなく **logging ハンドラのロック待ち**で止まっていた。

### 機構

`core/logging/console_tee.py` は保存先が NAS:

```
\\192.168.0.22\AutoStockBuyAndSell\Logs\console\console_*.log
```

旧実装は

```python
_console_file = open(path, "a", buffering=1)      # 行バッファ
...
class TeeStream:
    def write(self, data):
        for s in self.streams:
            s.write(data); s.flush()              # 1 行ごとに flush
```

で、**ログ 1 行 = SMB 往復 1 回**。しかも `logging.Handler.handle()` は
`with self.lock:` の中で `stream.write()` を呼ぶので、
**1 スレッドの NAS 書き込みが全スレッドのログをブロックする**。

### 実測 (本番稼働中)

| 計測 | 値 |
|---|---|
| 08:51 の 50 秒 tick で `core.global_context.context` に帰属した時間 | **23.5s / 53.1s = 44.3%** (311 レコード) |
| 同窓の物理行数 | 2,225 行 |
| 1 物理行あたり | **10.56 ms** |
| 別途実測した NAS flush (稼働中) | **7.6 〜 11.1 ms/行** (p95 27〜38ms, max 184ms) |
| ローカルファイルへの同 flush | 0.008 〜 0.013 ms (**約 1000 倍差**) |

稼働中の 2 つの実測値 (10.56 / 7.6〜11.1) が一致するので機構は確定。

参考: 本日 (売買プロセス停止中・NAS アイドル) に再計測すると 0.46 ms/行 まで下がる。
つまりこの問題は **NAS が混んでいるときだけ牙を剥く**。
08:51 は push receiver / summary DB / ranking DB が同じ NAS を叩いていた時間帯。

### ログ量の内訳

| 発生元 | レコード | バイト | 割合 | B/レコード |
|---|---|---|---|---|
| `[INFO] core.global_context.context` | 923 | 1,743,557 | **74.7%** | 1,889 |
| `[INFO] core.startup.bootstrap.schedule_loop` | 366 | 147,152 | 6.3% | 402 |
| `[INFO] trading.summary.controller_projection` | 175 | 35,819 | 1.5% | 204 |
| その他 | — | — | 17.5% | — |

`core.global_context.context` の中身は **DataFrame の表ダンプ**:

| レコード | 回数 | 表バイト |
|---|---|---|
| `[MERGED GET] tf=1 source=push` | 39 | 208,806 |
| `[SUMMARY HISTORY GET] tf=1 source=push` | 37 | 198,094 |
| `[MERGED GET FALLBACK] tf=1 source=push` | 32 | 171,328 |
| `[MERGED GET] tf=3 source=push` | 25 | 133,778 |
| `[MERGED STORED] tf=1 source=push` | 23 | 123,142 |
| ほか | — | — |
| **合計** | **76** | **1,528,872 B = ログ全体の 65%** |

### なぜ出ていたか

`core/global_context/context.py` のネイティブ `_log_df_profile()` には
`SUMMARY_CONTEXT_VERBOSE_TABLE_LOG` (既定 False) のゲートが**あった**。
しかし同ファイル末尾の

```python
if _pipe_summary_display_scope():        # argv に main.py が含まれるとき = 本番
    _install_pipe_summary_display()      # _log_df_profile を差し替える
```

が入れる `_patched_log_df_profile` には**そのゲートが無く**、
`[MERGED GET]` / `[MERGED STORED]` / `[SUMMARY HISTORY GET]` のたびに
20 行 x 22 列の表を無条件で出していた。
env は OS にもコードにも設定されていない (確認済み) のに表が出ていたのはこのため。

### 修正

**1. `core/logging/console_tee.py` — ファイル書き込みを非同期化**

- `_AsyncFileWriter` を追加。呼び出しスレッドは `put_nowait` して即座に戻る
- ライタスレッドが最大 512 行ずつまとめて書き、200ms 間隔で flush
- `buffering=1` (行バッファ) をやめて 64KB バッファへ
- キューが溢れたら**捨てる**（売買スレッドは絶対に止めない）。捨てた数は必ずログに残す
- コンソール表示側は同期のまま（速いし人が見る順序を保つ）
- `CONSOLE_TEE_ASYNC=0` で従来動作、`CONSOLE_TEE_QUEUE_MAX` / `CONSOLE_TEE_FLUSH_MS` で調整可
- `writer_stats()` で queued / written / dropped / alive を公開

**2. `core/global_context/context.py` — 表ダンプをゲートに従わせる**

`_patched_log_df_profile` にネイティブと同じ
`SUMMARY_CONTEXT_VERBOSE_TABLE_LOG` (既定 False) を入れた。
件数・非ゼロ数の 2 行は残す。表だけ既定 OFF。
`SUMMARY_CONTEXT_VERBOSE_TABLE_ROWS` も尊重するようにした。

### 効果 (実測)

| | before | after |
|---|---|---|
| ログ 1 行の呼び出し側コスト (NAS 実測) | 0.463 ms | **0.001 ms** (552x) |
| 500 行の logger.warning (実 NAS, E2E) | — | 呼び出し側 **9.5 ms** / 全 500 行書き込み成功 / dropped=0 |
| `_log_df_profile` 1 回の出力量 | 1,462 B | **346 B** (76% 減) |
| ログ全体に占める表ダンプ | 1,509,664 B (64.4%) | **0 B** |
| NAS が完全に詰まった状態で 5000 write | ブロック | **3 ms で戻る** (溢れ分は破棄・計数) |

08:51 窓に当てはめると、`core.global_context.context` の物理行が
2,225 -> 311 行になり、さらに非同期化で呼び出しスレッドから見たコストは実質ゼロ。

---

## P0-7 fast lane timeout が効いていなかった

### 原因

`scheduler_jobs/summary/direct_push_1m._build_direct()`:

```python
th.join(timeout)                    # timeout=6s。_build_direct_inner だけを縛る
if th.is_alive(): ...               # -> "build timeout timeout=6.000s elapsed=8.380s"
...
if not df: df = _build_from_process_memory(now)   # <-- ここは一切縛られていない
```

FAST LANE (PUSH DB 読み) は `_build_from_process_memory` の中にある。
つまり 6 秒の timeout は**その手前しか止めていなかった**。実ログ:

```
08:51:09 build timeout timeout=6.000s elapsed=8.380s
08:51:50 FAST LANE: PUSH DB を採用 db_rows=983
         stream_data.fastlane elapsed=41.202s
         SUMMARY FORCE DIRECT 1M elapsed=49.588s
```

### 修正

timeout 値は**増やしていない**。fallback にも予算を持たせた。

- `SUMMARY_FORCE_DIRECT_TOTAL_BUDGET_SEC` (既定 `max(2.0, timeout*1.5)`) を全体予算とする
- 予算残が 0.2 秒未満なら fallback に入らず即座に降りる
- fallback もスレッド + `join(remain)` で bounded
- 打ち切り時は**空を返す**。呼び出し側は既存の merged を潰さないので、
  stale データで Entry が fail-open することはない
- 次 tick で再試行する

### 効果 (実測)

30 秒かかる fallback を注入したテストで **2.01 秒で打ち切り**、空を返すことを確認。

---

## P0-2 FAST LANE / PUSH DB の段別計測

`_FastLaneStages` (direct_push_1m) と `_StageTimer` (memory_latest_1m) を追加。

出る項目:

- `[FASTLANE STAGE] label=... total=...s stages=[freshness_scan=...s(n=..,fresh=..) push_db_read=...s(rows=..) latest_dt=...s log_memory_summary=...s push_df_writeback=...s]`
- `[FASTLANE STAGE] label=build_from_process_memory stages=[candidate_frames=...(n=,rows=) normalize_ticks=...(src=,rows_before=,rows_after=) ohlc_aggregate=...(tick_rows=,bar_rows=) latest_projection=...(rows_before=,rows_after=,symbols=)]`
- `[PUSH DB STAGE] label=push_db_recent stages=[connect= pragma= list_tables= columns= query_fetch=(mode=,rows=) ring_merge=(new=,ring=) latest_dt=]`
- **1 段 1 秒以上で `[FASTLANE STAGE] slow` / `[PUSH DB STAGE] slow` の WARNING**

---

## P0-4 / P0-6 PUSH DB の接続共有と増分化

### P0-4 同一 tick 内の重複 open

`_read_push_db_recent()` に共有キャッシュ (`SUMMARY_MAIN_PUSH_DB_SHARE_TTL_SEC`, 既定 5 秒) を追加。
実測: **1 tick 内で 6 回呼んでも DB open は 1 回**。

### P0-6 増分カーソル + リングバッファ

- `last_rowid` を保持し、2 回目以降は `WHERE rowid > ?` だけ読む
- 取得分をメモリのリングバッファ (`SUMMARY_MAIN_PUSH_DB_RING_MAX_ROWS`, 既定 20,000 行) へ合流
- `push_db_read_stats()` で last_rowid / ring_rows / table / age を公開
- 増分に失敗したら full read へ自動フォールバック (理由をログに残す)

実測: full read 20,000 行 40.4ms -> 増分 (60 行/回) **10.2ms (3.9x)**。

### P0-5 SQL 側で先に絞る

`ORDER BY rowid DESC LIMIT n` は既に入っていた (前ラウンド)。
今回はさらに `WHERE rowid > last_rowid` で**必要な分だけ**に絞った。
OHLC correctness を壊さないよう、絞るのは行の範囲だけで列は落としていない。

---

## P0-10 MAIN PRICE DB FEED

### 原因 — 行数と無関係だった

実ログでは**増分で 1〜34 行しか読んでいない**のに `read=` が 2.5〜13.9 秒。
1 サイクルを分解計測した結果 (NAS アイドル時、push20260828.db 101.8MB):

| 段 | mean | 割合 |
|---|---|---|
| `os.path.exists` | 17.4 ms | 1.5% |
| `sqlite3.connect` | 16.0 ms | 1.4% |
| PRAGMA | 0.04 ms | 0.0% |
| **`SELECT name FROM sqlite_master`** | **521.5 ms** | **45.5%** |
| `PRAGMA table_info` | 3.4 ms | 0.3% |
| **増分クエリ (実際に必要な仕事)** | **93.5 ms** | **8.2%** |
| **`conn.close()`** | **495.1 ms** | **43.2%** |
| 合計 | 1,146.9 ms | |

**88% が open / スキーマ探索 / close**、実際のクエリは 8% だけ。
毎サイクル (2 秒ごと) にスキーマを探し直し、100MB の WAL DB を NAS 越しに開閉していた。

### 対策を実測で比較

| 案 | mean |
|---|---|
| 現行 (毎回 open + schema + close) | 548.5 ms |
| A: schema キャッシュのみ | 686.1 ms (**効かない**) |
| B: schema キャッシュ + **接続再利用** | **24.1 ms (22.8x)** |

### 修正

`trading/push/latest_price_db_feeder.py` に read session の共有を実装。

- 読み取り専用接続を **1 本だけ**保持して使い回す (`query_only=ON`)
- `MAIN_PUSH_PRICE_DB_FEED_CONN_MAX_AGE_SEC` (既定 300 秒) で作り直す
- 例外時は必ず閉じてスキーマキャッシュも捨て、次回作り直す
- テーブル名・列一覧をパス単位でキャッシュ
- `feeder_conn_stats()` で opened / closed / outstanding を公開
  (**常に 1 本**なので前ラウンドで潰した「接続リーク」にはならない)
- `MAIN_PUSH_PRICE_DB_FEED_REUSE_CONN=0` で従来動作

### 計測ログ (P0-10 の要求項目)

```
[MAIN PRICE DB FEED LATENCY] connect_ms=.. schema_ms=.. query_ms=.. fetch_ms=..
    total_ms=.. rows=.. db_size_mb=.. rowid_delta=.. conn_reused=.. conn_stats={..}
```

total_ms >= 1000 で WARNING。

### 効果 (NAS 実測、本日)

| | before | after |
|---|---|---|
| 1 サイクル | 486.7 ms (max 776.6) | **32.2 ms (max 236.6)** |
| 8 サイクルでの DB open 回数 | 8 | **1** |

---

## P0-9 3m publish 47 秒

### 実ログ

```
08:51:47 [LONG TF DB PUBLISH] interval=3 published hist_rows=50 latest_rows=50
         symbols=50 latest_dt=08:48 age=227.8s elapsed=47.293s
08:55:14 [LONG TF DB PUBLISH] interval=3 published hist_rows=83 latest_rows=50
         symbols=50 latest_dt=08:51 age=254.1s elapsed=73.568s
```

### 原因

50 行しかないのに 47〜73 秒。この 47 秒の窓 (08:51:00-08:51:47) は
**push:1 の 50 秒 tick と完全に重なっている**。同じプロセス内で
`core.global_context.context` のログが 23.5 秒 (窓の 44.3%) を占めていたので、
主因は P0-1 と同じ **NAS ログの同期書き込みによるロック待ち**。

加えて、このファイル自体に 2 つの実バグがあった。

**1. 接続を閉じていない**

```python
with sqlite3.connect(db, timeout=...) as conn:      # close() が無い
```

前ラウンドで潰した `runtime_state_store` と同じパターン。
`with sqlite3.Connection` は commit/rollback するだけで接続を閉じない。

**2. `date(col)=?` で index が効かない**

```sql
WHERE "datetime">=? AND date("datetime")=? AND "datetime"<=?
```

列に関数を掛けているので index が使えない。

### 修正

- `try/finally` で必ず `conn.close()`
- `date(col)=?` を範囲述語 `col >= '当日 00:00:00' AND col < '翌日 00:00:00'` へ
- `[LONG TF DB READ] interval=.. connect=.. schema=.. max_slot=.. rows_query=.. total=..`
  を追加 (total >= 2s で WARNING)

### 効果 (実測)

| | before | after |
|---|---|---|
| rows クエリ (summary20260828.db, 1,387 行) | 16.2 ms | **6.5 ms (2.5x)** |
| 同 (今朝 731 行時点の別計測) | 349.0 ms | **73.4 ms (4.8x)** |
| EXPLAIN | — | `SEARCH ... USING INDEX idx_stock_summary_3min_datetime (datetime>? AND datetime<?)` |
| 接続 | 開きっぱなし | opened=1 / closed=1 |

3m/5m を main.py で再計算しない方針は維持している (読み取り + publish のみ)。

---

## P1 Ranking Summary BG

`stage_sum=0 / unaccounted=14.4s` で内訳が取れていなかったので、
`core/startup/scheduler/bootstrap._run_ranking_summary_core_job()` に stage を入れた。

- `ranking_build_kwargs`
- `ranking_job_all` (本体 `job_ranking_summary_all`)
- `ranking_announce`

次回ログで `stage_sum` と `unaccounted` が分離できる。
BG は 238s -> 6.8〜18.9s まで落ちているので、
これ以上の分解は次ラウンドで実測を見てから行う。

---

## P1 resource

前ラウンドで入れた `[RESOURCE]` が 13 回出ている。
`peak_handles=406` / `handle_delta=+12` 程度で安定、`Too many open files=0`。
今回の変更で接続は **減る**方向 (price feeder 8 open -> 1、push DB 6 open -> 1)。

---

## P1 market closed behavior

08:50 前は `EXIT SCHEDULER market closed skip` / Tonosama も
`outside market session skip`。正常。この動作には触っていない。

---

## テスト結果

### dry-run (`test_script/p0_round12/test_r12_fixes.py`)

**total=38 passed=38 failed=0**

| 区分 | 件数 |
|---|---|
| P0-1 console_tee 非同期化 (呼び出し側が待たない / 全行到達 / まとめ書き) | 4 |
| P0-1 キュー溢れでも呼び出し側を止めない | 2 |
| P0-1 表ダンプの env ゲート | 3 |
| P0-2/4/6 PUSH DB 増分・共有・リングバッファ | 7 |
| P0-2 段別計測ログ / slow WARNING | 4 |
| P0-7 fallback bounded timeout | 3 |
| P0-9 接続クローズ + 範囲述語 | 4 |
| P0-10 接続再利用 + latency 計測項目 | 11 |

### 実測ベンチ (`test_script/p0_round12/load_test_r12.py`)

**total=7 passed=7 failed=0** (NAS 実 DB は読み取り専用)

### 回帰確認

| スイート | 結果 |
|---|---|
| 第11ラウンド 単体 (`p0_round11/test_p0_fixes.py`) | **81/81 passed** |
| 第11ラウンド 負荷 (`p0_round11/load_test_p0.py`) | **16/16 passed** |
| import smoke (`main` を含む 10 モジュール) | **failures: 0** |
| console_tee E2E (実 NAS へ 500 行) | 呼び出し側 **9.5 ms** / 500 行全書き込み / dropped=0 |

---

## 変更ファイル

| ファイル | 内容 |
|---|---|
| `core/logging/console_tee.py` | **NAS ログ書き込みの非同期化** (P0-1/2/3/9 の主因) |
| `core/global_context/context.py` | `_patched_log_df_profile` の表ダンプを env ゲート下へ |
| `scheduler_jobs/summary/direct_push_1m.py` | fallback の bounded timeout (P0-7) / FAST LANE 段別計測 (P0-2) |
| `scheduler_jobs/summary/memory_latest_1m.py` | PUSH DB 段別計測 / 共有キャッシュ / 増分カーソル + リングバッファ (P0-2/4/6) |
| `trading/push/latest_price_db_feeder.py` | read session 共有 + schema キャッシュ + latency 計測 (P0-4/10) |
| `scheduler_jobs/summary/long_tf_db_publish.py` | 接続クローズ + 範囲述語 + 段別計測 (P0-9) |
| `core/startup/scheduler/bootstrap.py` | Ranking BG の stage 計測 (P1) |

---

## 完成条件の現状

| # | 条件 | 状態 |
|---|---|---|
| 1 | push:1 通常 <2s | 主因 (NAS ログ同期書き込み) を除去。**再起動後の実測が必要** |
| 2 | push:1 最大 <5s | 同上 + fallback bounded (2s 打ち切りを実測) |
| 3 | stream_data.fastlane <2s | 接続共有 15.1x / 増分 3.9x / 段別計測。**実測は再起動後** |
| 4 | 983 rows で 40 秒処理 = 0 | 原因はログの NAS 同期書き込みと判明。除去済み |
| 5 | timeout 超過 worker = 0 | fallback を bounded 化 (実測 2.01s で打ち切り) |
| 6 | worker zombie = 0 | 維持 (今回のログでも 0) |
| 7 | merged 1m age <90s | FAST LANE 高速化で改善見込み。**再起動後の実測が必要** |
| 8 | realtime rows = active symbols | 維持 (45〜50 rows) |
| 9 | 22851 rows = 0 | 維持 (0) |
| 10 | 3m DB publish <5s | 接続クローズ + 範囲述語 (2.5〜4.8x) + ログ主因除去。**実測は再起動後** |
| 11 | MAIN PRICE DB FEED 通常 <3s | 486.7ms -> 32.2ms を実測。**本番実測は再起動後** |
| 12 | Ranking BG <10s | 6.8〜18.9s。stage 計測を追加。**再測定が必要** |
| 13 | disk I/O = 0 | 維持 (0) |
| 14 | unable DB = 0 | 維持 (0) |
| 15 | EXIT TEMP STOP = 0 | 維持 (0) |
| 16 | Too many open files = 0 | 維持 (0)。今回の変更で接続数はさらに減る |
| 17 | pending_moved_without_order = 0 | 維持 (0) |
| 18 | ORDER SUBMIT = 0 | 維持 (0) |

**1〜3, 7, 10〜12 は「主因を除去した」までで、本番実測は再起動後**。
数字の裏付けは上の各ベンチにあるが、本番で確認するまで達成とは書かない。

## 次ラウンドへの持ち越し

1. **再起動して実測** — 1〜3, 7, 10, 11, 12
2. `[CONSOLE TEE] queue overflow` が出ていないか (出るなら NAS が想定以上に遅い)
3. Ranking BG の stage 内訳を見て、5s 目標に届くか判断
4. `schedule_loop` のハートビートが 366 レコード / 402 B と多い
   (ジョブスナップショットを毎回全部出している)。次に削るならここ
5. `indicator_pipeline._log_column_health` が列ごとに INFO を出している
   (SLA スタックが捕まえた地点)。非同期化で痛みは消えたが本数は多い
6. 残る `with sqlite3.connect(...)` (close なし) の掃討 —
   `core/startup/summary/push_incremental_ma75.py` (7)、
   `core/startup/summary/restore.py` (5)、`core/startup/bootstrap/db.py` (3)、
   `core/startup/push/bootstrap.py` (2)

---

# 第13ラウンド — FAST LANE hit/miss と CPU 競合 (2026-08-28 09:40-09:52 ログ)

対象ログ: `X:\logs\console\console_20260828_094022_82112.log` (11,298 行)
プロセス: 09:40:22 起動 / PID 82112
作業日: 2026-08-28
実注文・実返済注文: **0 件** (NAS 本番 DB は読み取り専用のみ)
本番プロセス: **未再起動**。以下はすべて次回再起動で有効になる。
方針: 大規模設計変更なし。局所修正のみ。

## 維持できている項目

| 項目 | 実測 |
|---|---|
| `disk I/O error` | 0 |
| `unable to open database file` | 0 |
| `Too many open files` | 0 |
| `EXIT TEMP STOP` | 0 |
| `ORDER SUBMIT` | 0 |
| `22851` realtime rows | 0 |
| `CONSOLE TEE] queue overflow` | 0 |
| POSITIONS_STATE | `db_ok=True broker_ok=True positions_known=True entry_allowed=True` |
| stale overwrite guard | `old_latest=09:45 new_latest=09:43` を blocked (正しい) |
| `[RESOURCE]` | handles=410〜422 / open_files=16〜19 / threads=70〜76 |

第12ラウンドで入れた非同期ログと接続再利用も効いている
(`MAIN PRICE DB FEED` は rows=7 で 42.9ms、rows=101 で 1.14s)。

---

## P0-1 FAST PATH hit / miss の真因

### 実測

```
09:46  FAST LANE hit
       push_db_recent total=0.827s / mode=incremental new_rows=2239 ring_rows=7834
       built via fresh robust/process memory rows=200 latest_dt=09:45 elapsed=2.209s
       tier1 hit rows=200 elapsed=2.247s
       worker end total=2.414s              <- 高速経路は完成している

09:47  FAST LANE miss
       [PUSH DB STAGE] slow name=latest_dt        elapsed=2.706s
       [FASTLANE STAGE] slow name=latest_dt       elapsed=4.945s
       [FASTLANE STAGE] slow name=push_df_writeback elapsed=4.772s
       [FASTLANE STAGE] slow name=candidate_frames elapsed=9.930s n=2 rows=11522
       build timeout timeout=6.000s elapsed=6.096s
       fallback timeout budget=9.000s elapsed=9.139s
       tier1=9.430s -> light_push_runner=123.077s
       worker total=132.739s / cancel_latency=72.60s
```

### 原因は判断ロジックではなかった

同じ `_latest_dt()` が 09:46 は速く 09:47 は 4.9 秒。
これは処理内容の差ではないので、**同一プロセス内の CPU / GIL 競合**を疑い、
09:47 前後に何が走っていたかを突き合わせた。

09:47 時点で同時に走っていたもの:

| worker | 実測 |
|---|---|
| `ranking_summary_bg` gen2 | `interval=1 base_df=20.535s rows=21764` + `technical=32.157s` -> `ranking_job_all=151.377s` |
| `push:1` gen2 | `light_push_runner=81.637s` |
| `LONG TF DB PUBLISH interval=5` | `hist_rows=1256 elapsed=25.271s` |
| `RANKING_ENTRY_BUILD` gen2 | `elapsed=49.31s timed_out=True` |
| `RANKING_ENTRY` gen3 | `elapsed=36.18s timed_out=True` |
| `TONOSAMA_ENTRY` | 併走 |

`[RESOURCE] threads=76 rss_mb=1417〜1691`。

さらに ranking DB 読みが CPU 不足で自壊していた:

```
[RANKING ENTRY SOURCE DB FALLBACK] indexed read aborted elapsed=2.076s budget=2.0s err=interrupted
[RANKING ENTRY SOURCE DB FALLBACK] rowid read failed  elapsed=5.185s err=interrupted
[RANKING ENTRY SOURCE DB FALLBACK] db read empty
```

裏付けとして、同じデータを競合の無い状態で測り直すと

| 処理 | 09:47 の実測 | 競合なしの実測 |
|---|---|---|
| `pd.to_datetime(...).max()` (8,522 行, 実 push DB) | 2.7〜4.9 s | **40 ms** |
| `pd.to_datetime(...).max()` (20,000 行) | — | **90 ms** |

**結論: 高速経路は壊れていない。CPU を奪われて予算内に終われなかっただけ。**
したがって直すべきは「判断」ではなく「競合」と「main tick が heavy を待つ設計」。

### FAST_PATH_DECISION ログを追加

`scheduler_jobs/summary/runner_core.py` に 1 行で分かるログを追加した。

```
[FAST_PATH_DECISION] tick=... result=HIT|MISS elapsed=..s rows=.. symbols=..
    freshest_latest=.. age=.. technical_ready=.. required_cols_missing=[..]
    reject_reason=.. fallback_reason=.. hit=.. miss=.. hit_rate=..
```

`fast_path_stats()` で hit 率を取得できる (完成条件 1)。

---

## P0-2 main tick を heavy fallback で待たせない

### 原因

`runner_core.job_summary()` は tier1 (FAST LANE) が空だと、
**同じスレッドで tier2 = `light_push_runner` を回していた**。
これが 81.6s / 123.1s。しかも他の heavy worker と CPU を奪い合うので
競合をさらに悪化させる。

### 修正

tier1 miss 時は:

- main tick は **待たない** (空を返す)
- 空なので呼び出し側は既存 merged を潰さない -> 直近の fresh projection が残る
- Entry は fresh が無いので fail-closed 側へ倒れる (閾値は一切変えていない)
- heavy rebuild が要るなら **background で 1 本だけ** 走らせる
  (`SUMMARY_MAIN_HEAVY_BG_MIN_GAP_SEC` 既定 60 秒、多重起動なし)
- `SUMMARY_MAIN_SYNC_HEAVY_FALLBACK=1` で従来動作へ戻せる

### 実測 (dry-run)

20 秒かかる tier2 を注入しても **main tick は 0.001〜0.002 秒で戻る**。
heavy rebuild は background で起動していることを確認。

---

## P0-3 cancel latency 72.60 秒

### 原因

cancel SLA breach スタックが 2 回とも同じ場所を指していた:

```
summary_controller._diff_update_core
  -> controller_cache.choose_merged_cache_payload
  -> controller_utils.normalize_summary_df
  -> controller_utils.ensure_symbol
  -> controller_utils.sanitize_df
  -> pandas/core/generic.py copy()
```

```
summary_controller._diff_update_core
  -> summary_controller._ensure_reason_and_ai_columns
  -> controller_utils.normalize_summary_df
  -> controller_utils.normalize_price_aliases
  -> controller_utils.coalesce_first_numeric
  -> controller_utils.sanitize_df
  -> pandas/core/generic.py copy()
```

`normalize_summary_df` は heavy pipeline の最も密な funnel だが、
そこに降り場所が無かった。

### 修正

`controller_utils.normalize_summary_df()` の入口に
`stage_tracker.checkpoint()` を置いた。
`_ensure_reason_and_ai_columns` は `normalize_summary_df` を呼ぶので、
`_diff_update_core` 内の約 10 箇所すべてが自動的に降り場所になる。

cancel されていないときのコストは thread-local の getattr 1 回。

### 実測 (dry-run)

3,000 行 x 60 列の `normalize_summary_df` ループを cancel したとき
**cancel から 0.004 秒で停止** (SLA 5 秒)。

---

## 性能 sanitize_df の多重コピー (P0-3 / P0-6 の共通原因)

上のスタックが示していた本当のコスト。

`normalize_summary_df()` は 1 回の呼び出しで `sanitize_df()` を **9 回**呼んでいた:

| 呼び出し元 | 回数 |
|---|---|
| `normalize_summary_df` 入口 | 1 |
| `ensure_symbol` | 1 |
| `ensure_datetime` | 1 |
| `normalize_price_aliases` | 1 |
| `coalesce_first_numeric` x5 (close/open/high/low/volume) | 5 |

`sanitize_df` は毎回 frame 全体の deep copy + 全数値列の inf 置換を行う。

### 修正

内部呼び出しには `_pre_sanitized=True` を渡し、コピーを 1 回にした。
外から直接呼ばれたときの挙動は変えていない (public 契約は不変)。

### 実測 (250 列)

| rows | before | after | 倍率 |
|---|---|---|---|
| 881 | 131.5 ms | **21.5 ms** | 6.1x |
| 7,953 | 536.9 ms | **107.7 ms** | 5.0x |
| 20,693 | 1,319.1 ms | **292.6 ms** | 4.5x |
| 実コピー回数 | 9 | **1** | — |

出力の同一性 (symbol/datetime dtype, OHLC 列, 入力フレーム不変) をテストで確認。

---

## P0-4 / P0-5 realtime history leak と projection の共通化

### 実測

```
[REALTIME_HISTORY_LEAK] caller=global-set_merged_summary interval=1
    rows_before=20693 rows_after=1076
[REALTIME_HISTORY_LEAK] caller=GC_SUMMARY_REPAIR interval=3 rows_before=4266 rows_after=713
[REALTIME_HISTORY_LEAK] caller=GC_SUMMARY_REPAIR interval=5 rows_before=3306 rows_after=713
```

ガード自体は働いている (第11ラウンドで入れたもの)。
しかし **閾値未満は素通り**していた:

```
[summary_pipeline] postprocess done interval=1 rows=881 cols=46 symbols=214
[SUMMARY SETTER ENRICH] done context=global-set_merged_summary rows=881 elapsed=9.50s
```

`SUMMARY_MERGED_PROJECTION_MAX_ROWS` (1200) / `REALTIME_HISTORY_GUARD_MAX_ROWS` (2000)
のどちらも下回るので 881 行がそのまま enrich へ流れていた。
936 行 / 230 銘柄も同じ。

### 修正

realtime merged の定義は「1 symbol = 最新完成足 1 行」なので、
**行数の大小に関係なく `rows > symbols` なら畳む**ようにした
(`GlobalDataCompat._realtime_projection`)。
共通ガード `realtime_history_guard.to_latest_projection` を先に通す構造は維持。

`SUMMARY_MERGED_PROJECTION_ALWAYS=0` で旧挙動へ戻せる。

### 実測 (dry-run)

| 入力 | 出力 |
|---|---|
| 881 行 / 214 銘柄 | **214 行** |
| 936 行 / 230 銘柄 | **230 行** |
| 214 行 / 214 銘柄 | 214 行 (素通し) |

最新 datetime が保存されることも確認 (完成条件 6, 7)。

---

## P0-6 setter enrich

### 実測 (第12ラウンドで入れた stage 計測が効いた)

```
context=global-set_merged_summary       rows=881 daily_mtf=6.682s ranking=2.410s total=9.093s
context=global-set_push_merged_summary  rows=133 daily_mtf=0.068s ranking=0.495s total=0.563s
context=before-display                  rows=214 daily_mtf=0.012s ranking=0.057s total=0.069s
[SUMMARY ENRICH STAGE][RANKING] source_load=2.202s source_rows=20000   <- cold
[SUMMARY ENRICH STAGE][RANKING] source_load=0.003s source_rows=20000   <- warm
```

**同じ enrich が 214 行なら 0.069 秒、881 行で 9.093 秒**。
4 倍の行数に対して 130 倍。線形ではないので原因は行数ではなく
**cold cache での NAS 読み込み**。tick の最初の enrich がそれを引く。

### 修正

setter の責務を projection / validation / atomic publish に戻す。

- `SUMMARY_SETTER_ENRICH_BUDGET_SEC` (既定 0.5 秒) を超えたら
  enrich を待たずに publish する
- enrich は冪等で、後段の `before-display` / `cache-latest` でも走るので
  そこで cache が温まる。値が欠けるのではなく「その tick では付かない」だけ
- `=0` で従来動作

P0-5 の projection と合わせて、setter に入る行数自体が 881 -> 214 になる。

### 実測 (dry-run)

10 秒かかる enrich を注入 -> **0.30 秒で打ち切り**、入力をそのまま publish。
予算内の enrich は従来どおり結果が反映される。

---

## P0-7 / P0-8 pending_moved_without_order

### 原因

```
09:48:14 🧹 pending pruned symbol=6030 reason=RANKING_FINAL_NG:ATR_1M_FILTER_NG
09:48:14 [CANDIDATE VANISHED] id=RANKING-6030-20260828094701-1
         last_stage=pending_created age=72.8 reason=pending_moved_without_order
```

**明示的に棄却しているのに vanish 扱いされていた。**
同じパターンが 3994 / 4443 / 4776 / 2590 でも発生。
`prune_entries()` は pending を消すだけで candidate_id 側に何も残さないため、
後段の「pending 件数が減ったのに発注していない」監査が誤検出していた。

### 修正 — candidate state machine

`trading/entry/candidate_id.py` に状態を明示:

```
CREATED / EVALUATING / APPROVED / SUBMITTING
SUBMITTED / REJECTED / EXPIRED / CANCELLED      <- 後半4つが terminal
```

- `mark_terminal(cid, state, reason=...)` / `mark_terminal_symbol(symbol, ...)`
- `log_vanished()` は terminal 済みを **vanished 対象外**にし、
  `[CANDIDATE RESOLVED]` として INFO で残す。本当に vanish した cid だけを返す
- terminal でない state を渡したら REJECTED へ倒して WARNING

`trading/entry/pending_manager.py` の prune で terminal を記録する。
理由から状態を分類 (判定・閾値は一切変えていない):

| reason に含まれる語 | state |
|---|---|
| expire / timeout / stale / too_old / age | EXPIRED |
| cancel / abort | CANCELLED |
| それ以外 (NG / reject 等) | REJECTED |

### 実測 (dry-run)

`reason=RANKING_FINAL_NG:ATR_1M_FILTER_NG` で prune -> terminal=REJECTED、
`log_vanished()` が空を返すことを確認。symbol 逆引きも動作。

---

## P0-10 / P0-11 Ranking Summary BG 152 秒

### 実測

```
gen1: interval=1 base_df=14.040s rows=20693 / technical=17.275s / total=31.316s
      interval=3 base_df=55.065s rows=7953  -> budget exhausted -> technical skip
      interval=5 cooperative stop before base_df (着手すらできない)
      ranking_job_all=155.835s
gen2: interval=1 base_df=20.535s rows=21764 / technical=32.157s / total=52.691s
      ranking_job_all=151.377s
gen3: interval=1 base_df=18.170s rows=23385 / technical=24.198s / total=42.367s
```

### 原因

`run_ranking_summaries_all()` は 1m / 3m / 5m を順に回し、
そのたびに `build_ranking_summary_base_df()` -> `load_ranking_snapshot_1min()` が
**同じ snapshot を NAS から読み直していた**。

実 DB での実測 (`ranking20260828.db` 225.8 MB):

| | 実測 |
|---|---|
| 1 回の `load_ranking_snapshot_1min(lookback=240)` | **36.6 〜 50.5 秒** / 82,000〜84,000 行 |
| 1m/3m/5m の 3 回合計 | **125.4 秒** |

これが `ranking_job_all` 151 秒のほぼ全部。

### 修正

`load_ranking_snapshot_1min()` に短命の共有キャッシュを入れた。
キーは `(trade_date, lookback_minutes, symbols, db_path)`、
TTL は `RANKING_SNAPSHOT_CACHE_TTL_SEC` (既定 45 秒)。
DB は永続保存用であって、1 回の tick で 3 回読む必要はない。
返す内容は同一 (`a.equals(b) == True` を実測確認)。
`=0` で従来動作。

### 実測

| | before | after |
|---|---|---|
| 1m/3m/5m の 3 回合計 | **125.43 s** | **50.49 s** (2.5x) |
| 2 回目以降 | 36.6 s | **0.28 ms** |
| 内容の同一性 | — | `equals()` True / 同一オブジェクト |
| lookback 違い | — | 別キーとして扱う (59,261 行 vs 84,245 行) |

**ただし 10 秒目標には届いていない。** 1 回目の読み込みが 36〜50 秒あるため。
これは 82,000 行を毎回 NAS から読むこと自体が原因で、
P0-9 が求める「symbol x 105 bars の memory bounded cache による incremental 化」
までやらないと解消しない。今回は大規模設計変更を避ける方針なので、
**重複読み込みの除去 (2.5x) までに留めた**。次ラウンドの最優先候補。

---

## 未着手 / 部分対応で残したもの

正直に記録する。

| 項目 | 状態 |
|---|---|
| P0-9 Ranking Entry 36 秒 | **未着手**。`ranking_source=8.06s` / `technical_attach=2.48s` は 09:46 の CPU 競合時の値で、09:48 は `ranking_source=0.23s` と正常。P0-2 の競合除去の効果を見てから判断する |
| P0-10 の incremental cache (symbol x 105 bars) | **未着手**。今回は重複読み込み除去のみ |
| P0-11 5m が base_df に着手できない | 1m/3m の重複読み込み除去で予算が空くはずだが **未実測** |
| P0-12 5m publish 25.271s | **未着手**。DB 読みは第12ラウンドで改善済み (`[LONG TF DB READ] total=1.4〜7.4s`) なので、残りは repair/MTF/daily/projection/publish 側。stage 計測がまだ無い |
| 前メッセージの P0-11 settings conflict | **未着手** |
| 前メッセージの P0-12 stale_fail_open 監査 | **未着手** |

---

## テスト結果

### dry-run (`test_script/p0_round13/test_r13_fixes.py`)

**total=53 passed=53 failed=0**

| 区分 | 件数 |
|---|---|
| sanitize 多重コピー除去 (出力同一性含む) | 4 |
| P0-4/P0-5 realtime projection | 5 |
| P0-6 setter enrich budget | 4 |
| P0-3 cancel checkpoint | 2 |
| P0-2 heavy fallback を同期で回さない | 5 |
| P0-1 FAST_PATH_DECISION | 13 |
| P0-7/P0-8 terminal state | 12 |
| P0-7 prune が terminal を記録 | 4 |
| P0-10/P0-11 ranking snapshot cache | 4 |

### 回帰確認

| スイート | 結果 |
|---|---|
| 第12ラウンド (`p0_round12/test_r12_fixes.py`) | **38/38 passed** |
| 第11ラウンド 単体 (`p0_round11/test_p0_fixes.py`) | **81/81 passed** |
| 第11ラウンド 負荷 (`p0_round11/load_test_p0.py`) | **16/16 passed** |
| import smoke (`main` 含む 10 モジュール) | **failures: 0** |

合計 **188 テスト全通過**。

---

## 変更ファイル

| ファイル | 内容 |
|---|---|
| `trading/summary/controller_utils.py` | sanitize 多重コピー除去 (9->1) / cancel checkpoint |
| `scheduler_jobs/summary/runner_core.py` | **main tick が heavy fallback を待たない** / FAST_PATH_DECISION / hit 率 |
| `global_state.py` | realtime projection を常時適用 / setter enrich の予算打ち切り |
| `trading/entry/candidate_id.py` | candidate state machine (terminal) |
| `trading/entry/pending_manager.py` | prune 時に terminal を記録 / 理由から state を分類 |
| `trading/ranking/summary/loader.py` | ranking snapshot の短命共有キャッシュ |

---

## 完成条件の現状

| # | 条件 | 状態 |
|---|---|---|
| 1 | FAST LANE hit 率を記録 | 済 (`[FAST_PATH_DECISION] ... hit_rate=`) |
| 2 | main synchronous light runner = 0 | 済 (実測 0.001s で戻る)。**本番実測は再起動後** |
| 3 | push:1 normal <3s | 主因 (CPU 競合 + 同期 heavy fallback) を除去。**実測は再起動後** |
| 4 | push:1 max <5s | 同上 |
| 5 | cancel latency <5s | 済 (実測 0.004s) |
| 6 | realtime rows = symbols | 済 (881->214 / 936->230) |
| 7 | 936/881 history leak = 0 | 済 |
| 8 | merged 1m age <90s | **再起動後の実測が必要** |
| 9 | Ranking BG <10s | **未達**。125.4s -> 50.5s まで。1 回目の 36〜50 秒が残る |
| 10 | Ranking Entry <5s | **未着手** |
| 11 | pending_moved_without_order = 0 | 済 (terminal 記録)。**本番実測は再起動後** |
| 12 | terminal reason 必須 | 済 |
| 13 | 5m publish <5s | **未着手** |
| 14 | disk I/O = 0 維持 | 維持 |
| 15 | EXIT TEMP STOP = 0 維持 | 維持 |
| 16 | ORDER SUBMIT = 0 | 維持 |

## 次ラウンドへの持ち越し

1. **再起動して実測** — 2, 3, 4, 8, 11
2. **Ranking BG の incremental 化** (P0-9/P0-10) — symbol x 105 bars の
   memory bounded cache。今回の 50.5 秒の内訳はほぼ「82,000 行の初回読み込み」
3. Ranking Entry 36 秒 (P0-9) — CPU 競合除去後に再測定してから
4. 5m publish 25.271 秒の stage 計測 (P1)
5. settings conflict / 設定 owner 一本化 (前メッセージ P0-11)
6. `stale_fail_open=1` の call path 監査 (前メッセージ P0-12)
7. 残る `with sqlite3.connect(...)` (close なし) の掃討

---

# 第14ラウンド — PUSH owner 停止の真因 (2026-08-28 11:13 停止)

対象ログ:
- `console_20260828_094022_82112.log` (main, 09:40-12:08)
- `console_20260828_120935_93604.log` (main, 12:09-)
- `push_receiver_runner_20260828_110649_81680.log` (**11:06:49 -> 11:13:05 で終了**)
- `data_collectors_runner_20260828_093953_60464.log` (親 supervisor, 09:39-12:07)

作業日: 2026-08-28
実注文・実返済注文: **0 件**
本番プロセス: **稼働中**。以下はすべて次回再起動で有効になる。
方針: 大規模変更なし。最優先は Summary ではなく PUSH owner。

## 維持できている項目

| 項目 | 実測 |
|---|---|
| `disk I/O error` | 0 |
| `Too many open files` (アプリ側) | 0 |
| `EXIT TEMP STOP` | 0 |
| `pending_moved_without_order` | 0 |
| `ORDER SUBMIT` | 0 |
| main synchronous heavy fallback | 外れている (`fallback_reason=heavy_rebuild_bg_skipped`) |
| fresh 無し Entry | fail-closed (`rows=802 -> 0 / no fresh rows`) |
| setter enrich budget | `budget=0.50s` で動作 |
| stale overwrite guard | `old_latest=12:45 new_latest=12:43` を blocked |
| POSITIONS_STATE | `db_ok=True broker_ok=True positions_known=True` |

前ラウンドの「main tick は heavy fallback を待たない」は効いている
(push:1 worst 132.7s -> 約 9.4s)。

---

## P0-1 PUSH owner が 11:13 で止まった原因

### 停止の事実

| 項目 | 値 |
|---|---|
| PUSH owner | `push_receiver_runner.py` (main.py は WS owner ではない) |
| 停止したプロセス PID | **81680** (起動 11:06:49) |
| 最後のログ | **11:13:05** |
| 最後の heartbeat ログ | **11:11:14** (その後 30 秒周期が来ていない) |
| push DB 最終書き込み | **11:13:04** |
| 次に owner が起動したのは | **12:11:18** (PID 81488) = **約 58 分の空白** |
| 12:15 時点の stale | `db_stale=3748.7s / memory_stale=3511.4s` |

### 真因

子プロセスは **自分のスクリプトすら読めずに即死**していた。

```
OSError: [WinError 6] ハンドルが無効です。:
    'W:\...\.venv\Lib\site-packages\pytz'
python.exe: can't open file
    '\\192.168.0.22\business\...\scripts\push_receiver_runner.py'
```

親 supervisor ログ (09:39-12:07 の 1 プロセス分) の実測:

| 事象 | 回数 |
|---|---|
| `WinError 4` (Too many open files **in system**) | **183** |
| `WinError 6` (ハンドルが無効) | **42** |
| `WinError 59` (予期しないネットワークエラー) | **14** |
| `can't open file` | **102** |
| `start child name=push_receiver` | **152** |

つまり **W: (ソース + .venv がある SMB 共有) のハンドル/セッションが枯渇**し、
子プロセスが起動できなくなっていた。
これは第11ラウンドで X: (データ共有) に対して直した問題と同じ種類だが、
今回は **W: (実行ファイル側)** で起きている。

### 悪化させていたのは再起動ループ自身

```
11:13:12 child exited  pid=71264 returncode=1 -> restart
11:13:17 child started pid=84884
11:13:24 child exited  pid=84884 returncode=2 -> restart
11:13:29 child started pid=56344
11:13:35 child exited  pid=56344 returncode=2 -> restart
11:13:40 child started pid=90344
11:13:46 child exited  pid=90344 returncode=2 -> restart
...
```

**5〜6 秒間隔で延々と再起動していた。**

原因は supervisor の分岐:

```python
# 旧: 「起動には成功したがすぐ死んだ」場合は fail_counts を増やさない
new_proc = _start_child_safe(...)
if new_proc is None:
    _schedule_retry(name)      # ここでしか backoff しない
else:
    fail_counts[name] = 0      # 3 秒後に死んでも「成功」扱い
```

spawn 自体が成功していれば backoff が **一切かからない**。
1 回の spawn は NAS 上の venv から pandas / numpy / sqlalchemy を import するので
**数千回の SMB open** を伴う。
つまり **リトライが SMB 枯渇を悪化させる自己増幅ループ**だった。

### もう 1 つの原因: パス表記の混在

```
interpreter = W:\...\ソースコード\.venv\Scripts\python.exe          (ドライブレター)
script      = \\192.168.0.22\business\...\scripts\push_receiver_runner.py  (UNC)
```

`PROJECT_ROOT = Path(__file__).resolve()` が **マップドライブを UNC へ展開する**ため、
同じ共有に 2 種類の表記で触っていた。
Windows はこれを別セッションとして扱うので、SMB セッション/ハンドルを二重に消費する。

### main ループが例外で抜けうる構造

`data_collectors/push_runtime.run_forever()` の `while True:` は

```python
now = time.time()
if now - last_hb >= 30:
    write_heartbeat(...)        # <- try の外。書き先は NAS
    logger.info(...)            # <- try の外
```

で、heartbeat 書き込みが try の外にあった。
SMB 障害時にここで例外が出ると **ループを抜けてプロセスが終了する**。
実際 heartbeat ログの間隔は 30s -> **114s / 62s / 53s** と伸びたあと
11:11:14 で止まり、11:12:23 から interpreter shutdown が始まっている。

---

## P0-1 修正

### `scripts/data_collectors_runner.py`

1. **即死を失敗として数える**
   `CHILD_MIN_HEALTHY_SEC` (既定 30 秒) 未満で死んだら `died_fast` として
   `_schedule_retry()` に回す。生存が確認できたら `fail_counts` をリセットし
   `[DATA COLLECTORS] child healthy name=... alive=..s` を出す。
   exit ログに `lifetime=` `died_fast=` を追加。

2. **spawn 前プローブ**
   `_probe_paths_readable()` でインタプリタとスクリプトを 1 バイト読むだけの
   安価な確認を行う。読めないなら **spawn しない**
   (spawn は NAS venv から数千ファイルを読むので、枯渇中の再試行は逆効果)。
   `AUTOSTOCK_CHILD_SPAWN_PROBE=0` で従来動作。

3. **SMB 障害と app 障害の区別**
   `_looks_like_smb_fault()` が WinError 4/6/59/64/121/1450、errno 24、
   `can't open file` を判定し、ログと heartbeat に `smb_fault=` を出す。

4. **パス表記の統一**
   `_normalized_child_path()` が子スクリプトをインタプリタと同じ共有表記へ揃える。
   `.resolve()` は使わない (マップドライブが UNC へ展開されて意味が無くなるため)。
   `AUTOSTOCK_CHILD_PATH_NORMALIZE=0` で従来動作。

   実測:
   ```
   before: script = \\192.168.0.22\business\...\push_receiver_runner.py
   after : script = W:\BUSINESS\...\scripts\push_receiver_runner.py
   interpreter    = W:\BUSINESS\...\.venv\Scripts\python.exe
   ```

### `data_collectors/push_runtime.py`

**main ループを絶対に抜けさせない。**
`write_heartbeat` / `logger.info` を try で包み、ループ本体全体も
`except Exception` で受けて `continue` する。
NAS が書けなくても PUSH 受信は続ける。

---

## P0-2 / P0-4 PUSH OWNER HEALTH

`push_runtime` に 10 秒ごとの健康ログを追加した。

```
[PUSH OWNER HEALTH] pid=.. ws_connected=.. ws_thread_alive=..
    last_recv_age=.. last_write_age=.. received_total=.. written_total=..
    queue=.. registered_count=.. rotation=.. token_tail=.. db_path=.. fault=..
```

`write_heartbeat("push_owner_health", ...)` で JSON にも落とすので、
main 側から共有ファイル経由で確認できる。

**P0-4 の切り分け** (`_classify_push_fault`):

| WS recv | DB write | 判定 |
|---|---|---|
| fresh | stale | `writer_problem` |
| stale | stale | `websocket_or_subscription_problem` |
| fresh | fresh | `owner_ok` (main が古いなら reader 側の問題) |

多重 WS は張らない。owner プロセス内の再接続のみ (既存動作を維持)。

---

## P0-5 FAST PATH の設計は維持 (12:30 再開で自動復旧を確認)

前ラウンドの「main tick は heavy fallback を待たない」はそのまま。

**昼休み明け 12:30 の実測 — PUSH が fresh になったら FAST LANE は自動で HIT へ戻った:**

```
12:29:00 result=HIT  elapsed=5.668s rows=156 symbols=156 age=60.0 technical_ready=156
12:30:00 result=HIT  elapsed=3.195s rows=156 symbols=156 age=60.0 technical_ready=156
12:31:00 result=HIT  elapsed=4.716s rows=156 symbols=156 age=60.0 technical_ready=156
12:32:00 result=MISS elapsed=9.174s reject_reason=tier1_empty_or_timeout
```

**高速経路そのものは正常で、input freshness がボトルネックだった**という
前ラウンドの結論がそのまま裏付けられた。

---

## P0-6 fresh input が無いときの 9 秒待ちを止める

MISS でも毎回 9.0〜9.4 秒 (build timeout 6s + fallback budget 9s) かかっていた。
だが「PUSH owner が死んでいて push DB が 56 分古い」ことは数百 ms で分かる。

`runner_core._fresh_input_hopeless()` を追加。
push DB の `MAX(datetime)` だけを見て、
`SUMMARY_FRESH_INPUT_HOPELESS_AGE_SEC` (既定 300 秒) より古ければ即 MISS。
結果は 5 秒キャッシュする。判定できないときは従来どおり探索する
(fail-open ではなく「分からない」)。閾値は緩めていない。

### 実測 (dry-run)

| | before | after |
|---|---|---|
| push DB が 1 時間古いときの MISS | 9,000 ms | **119 ms** |
| 重い tier1 の呼び出し | 1 回 | **0 回** |
| 戻り値 | 空 | 空 (Entry は fail-closed のまま) |

---

## P0-7 setter enrich スレッドの積み上げ (前ラウンドで入れた回帰)

### 実測

```
12:33 threads_top={'setter-enrich-global-set_merged': 4,
                   'setter-enrich-global-set_push_merged': 3, ...}
rss_mb 1774.6 (12:26) -> 2416.8 (12:33) -> 4344.6 (12:33) -> 6282.0 (12:34)
```

**原因は第13ラウンドで自分が入れた実装。**
「呼び出しごとに Thread を作って `join(budget)`」にしたため、
予算超過したスレッドが **放置されたまま走り続け**、
DataFrame を掴んだまま NAS を読み続けて毎 tick 積み上がっていた。

### 修正

context ごとに **同時 1 本だけ**に制限。
前の enrich がまだ走っているなら新しく起動せず即 publish する
(enrich は冪等なので次 tick で反映される)。

### 実測 (dry-run)

| | before | after |
|---|---|---|
| 10 回呼んだときの enrich スレッド数 | 10 | **1** |
| スレッド総数の増分 | +10 | **+1** |

---

## P0-14 昼休みと 11:13 停止の切り分け

- 12:15 の `EXIT SCHEDULER market closed skip` / `no fresh rows` は
  **日本株の昼休み (11:30-12:30) なので正常**。この fail-close 動作は維持。
- **11:13 の停止は昼休みとは無関係**。市場時間中 (11:13 < 11:30) に
  PUSH owner プロセスが SMB 障害で死に、58 分間復帰しなかった。
- **12:30 の再開後は自動復旧している** (上記 FAST_PATH_DECISION の HIT)。

---

## テスト結果

### dry-run (`test_script/p0_round14/test_r14_fixes.py`)

**total=49 passed=49 failed=0**

| 区分 | 件数 |
|---|---|
| P0-1 SMB 障害の判定 | 8 |
| P0-1 spawn 前プローブ | 4 |
| P0-1 即死 backoff | 5 |
| P0-1 パス表記統一 | 3 |
| P0-2/P0-4 PUSH OWNER HEALTH | 18 |
| P0-1 main ループが抜けない | 3 |
| P0-6 fresh 無しの即 MISS | 6 |
| P0-7 スレッド積み上げ防止 | 2 |

### 静的検証

| 項目 | 結果 |
|---|---|
| 本セッションで編集した 29 ファイルの構文検査 | **failures: 0** |
| 編集モジュールの import smoke (9 モジュール) | **failures: 0** |

### 回帰スイート — **今回は実行できていない**

第11〜13ラウンドのスイートは **本番プロセス稼働中 (12 プロセス / main.py 4.5GB)**
のため timeout した。数秒で終わっていたものが 240 秒でも終わらない。
これ自体が「CPU 競合で軽い処理が数十倍になる」という今回の診断の裏付けでもある。

**市場終了後に再実行が必要。** 現時点では静的検証と R14 スイートのみで確認している。

---

## 変更ファイル

| ファイル | 内容 |
|---|---|
| `scripts/data_collectors_runner.py` | 即死 backoff / spawn プローブ / SMB 障害判定 / パス表記統一 |
| `data_collectors/push_runtime.py` | main ループを抜けさせない / PUSH OWNER HEALTH / 障害分類 |
| `scheduler_jobs/summary/runner_core.py` | fresh input 無しの即 MISS |
| `global_state.py` | setter enrich スレッドを context ごと 1 本に制限 |

---

## 完成条件の現状

| # | 条件 | 状態 |
|---|---|---|
| 1 | push owner heartbeat 取得 | 済 (`[PUSH OWNER HEALTH]` + `push_owner_health.heartbeat.json`) |
| 2 | 11:13 停止原因を断定 | **済** — W: の SMB ハンドル枯渇 (WinError 4/6/59) で子プロセスが起動不能、かつ即死 backoff 欠如で自己増幅 |
| 3 | market 中 last_recv_age <5s | **再起動後の実測が必要** |
| 4 | last_write_age <5s | 同上 |
| 5 | main feeder rows が継続増加 | 同上 |
| 6 | FAST hit 率 >90% | 12:29-12:31 は 3/4 HIT。**再起動後の実測が必要** |
| 7 | FAST miss <0.5s | 済 (実測 119ms) |
| 8 | push:1 normal <3s | 12:30 で HIT 3.195〜5.668s。**未達** |
| 9 | summary-force-direct thread <=2 | setter-enrich は 1 本に制限。force-direct 自体は **未着手** |
| 10 | RSS 安定 | setter-enrich リークは修正。**他要因は未確認**。実測 6.2GB まで増加中 |
| 11 | Ranking BG <10s | **未着手** (前ラウンドで 125.4s -> 50.5s まで) |
| 12 | Ranking cancel <5s | **未着手** |
| 13 | Ranking DB sync enrich=0 | **未着手** |
| 14 | Ranking AI input <1000 rows | **未着手** |
| 15 | pending_moved_without_order=0 | 維持 (0) |
| 16 | disk I/O=0 | 維持 (0) |
| 17 | ORDER SUBMIT=0 | 維持 (0) |

## 未着手として明示するもの

今回は PUSH owner に集中したため、以下は手を付けていない。

- P0-8 メモリ増加追跡 (largest DataFrames の列挙)
- P0-9 Ranking Summary 190 秒 / `technical merge probe rows=57250`
- P0-10 Ranking cache owner 一本化
- P0-11 Ranking cancel SLA 10.12 秒
- P0-12 Summary enrich の NAS ranking DB 同期 read (`source_load=6.46s`)
- P0-13 Ranking AI input 43,431 行
- P1 startup recent liquidity の fail-open 監査
- 9 番 `summary-force-direct` スレッド自体の上限

## 次ラウンドへの持ち越し

1. **市場終了後に第11〜13ラウンドのスイートを再実行** (今回 timeout)
2. **再起動して PUSH owner の復旧を実測** (完成条件 3,4,5,6,8)
3. RSS 6.2GB の残り要因 (setter-enrich 以外) の特定
4. Ranking Summary 190 秒 / 57,250 行の旧経路除去 (P0-9/P0-10)
5. Summary enrich から NAS ranking DB 同期 read を外す (P0-12)
6. Ranking AI input 43,431 行の順序修正 (P0-13)
7. `summary-force-direct` スレッド上限 (完成条件 9)

---

# 第15ラウンド — PUSH writer 停止の断定と追加P0-A/P0-B (2026-08-28 13:43 停止)

対象ログ:
- `push_receiver_runner_20260828_121118_81488.log` (12:11:18 -> **13:45:06**)
- `push_receiver_runner_20260828_135714_30496.log` (13:57:14 -, 新コード)
- `console_20260828_135120_73328.log` (main, 13:51-13:55)

作業日: 2026-08-28
実注文・実返済注文: **0 件**
本番プロセス: **稼働中**。以下はすべて次回再起動で有効になる。

## 維持できている項目

`disk I/O error` / `Too many open files` / `EXIT TEMP STOP` /
`pending_moved_without_order` / `ORDER SUBMIT` はいずれも 0 件。
RSS 223〜349MB / threads 61〜63、`summary-force-direct=10` の再発なし。
main.py は PUSH websocket を開かない (split owner 維持)。

---

## P0-2 PUSH OWNER HEALTH は実装済みだった (ただし全項目 None)

指摘の「未実装」は正確には **実装済みだが値が取れていなかった**。

- `push_runtime.py` の編集時刻 **12:33**
- 指摘ログの main プロセス起動は 12:09、その push_receiver は **12:11:18 起動** = 旧コード
- `[PUSH OWNER HEALTH]` は push_receiver 側のログに出るので、main の console ログには元々出ない

**13:57:14 起動の新 receiver では実際に 6 回出力されていた:**

```
13:58:07 [PUSH OWNER HEALTH] pid=30496 ws_connected=False ws_thread_alive=True
         last_recv_age=None last_write_age=None received_total=None written_total=None ...
```

ただし **全項目 None**。原因は自分が属性名を推測で書いたこと
(`connected` / `total_received` などと書いたが、実際は
`_connected_event` / `_total_received`)。

### 修正

`trading/push/push_stream/state.py` の実際の名前に合わせて全面的に書き直した。

| 出力項目 | 参照元 |
|---|---|
| ws_connected | `state._connected_event.is_set()` |
| ws_thread_alive / flush_thread_alive | `state._ws_thread` / `_flush_thread` |
| received_total / written_total | `state._total_received` / `_total_flushed` |
| dropped_total / error_total | `state._total_dropped` / `_total_errors` |
| queue | `state._push_queue.qsize()` |
| last_recv_at/age | `state._last_message_at` |
| last_flush_age | `state._last_flush_at` |
| last_connect_at / last_disconnect_at / last_error_at | 同名 state 属性 |
| memory_rows | `len(state._push_df)` |
| writer_ready | `state._stream_writer is not None` |
| **db_max_rowid / db_rowid_delta / db_max_datetime / last_write_age** | **push DB を直接プローブ** |
| uptime_sec / pid / token_tail / rotation | プロセス情報 |

writer の内部カウンタが取れない場合の最後の砦として
`_probe_push_db_progress()` が push DB の `MAX(rowid)` / `MAX(datetime)` を直接見る。
**DB が実際に伸びているか**で writer の生死を断定できる。

---

## P0-1 / P0-3 13:43 停止の断定 — **case B: writer_problem**

### 決定的な証跡

`push_receiver_runner_20260828_121118_81488.log` の最後:

```
13:44:58 [PUSH MONITOR] connected=True ws_alive=True queue=160 df_rows=17166
         total_received=17166 total_flushed=17004 dropped=0 flush_alive=True
13:45:04 [PUSH MONITOR] connected=True ws_alive=True queue=176 df_rows=17182
         total_received=17182 total_flushed=17004 dropped=0 flush_alive=True
```

**`total_received` は増え続け (17166 -> 17182)、`total_flushed` は 17004 で固着、
`queue` は増加 (160 -> 176)。**

`flush_alive=True` なのでスレッドは生きている。プロセスも死んでいない
(`interpreter shutdown` = 0、13:45:06 まで正常にログ出力)。

### つまり分類は B

| ケース | 条件 | 今回 |
|---|---|---|
| A | recv fresh / write fresh | — |
| **B** | **recv fresh / write stale** | **該当** |
| C | recv stale / write stale | — |
| D | owner heartbeat 自体なし | — |

**WS 受信は生きているが DB writer が書けていない。**

### flush の詰まりは断続的で、13:17 頃から続いていた

| 時刻 | queue | received | flushed |
|---|---|---|---|
| 13:17:04 | 52 | 15262 | 15110 |
| 13:19:25 | 120 | 15330 | **15110 (固着)** |
| 13:22:49 | 340 | 15551 | **15110 (固着)** |
| 13:28:49 | 617 | 16037 | **15320 (固着)** |
| 13:31:37 | **720** | 16167 | 15446 |
| 13:40:53 | 0 | 16875 | 16776 (一時回復) |
| 13:44:03 | 88 | 17093 | 17004 |
| 13:45:04 | 176 | 17182 | **17004 (固着)** |

キューが 52 -> 720 まで膨らんでは戻る、を繰り返している。
**writer は死んでいないが NAS 書き込みに追いつけていない。**

### 併発: WS 再接続ストーム

| 項目 | 値 |
|---|---|
| CONNECTED 総数 | **604** |
| DISCONNECTED 総数 | 597 |
| 期間 | 94 分 (12:11-13:45) |
| 頻度 | **6.4 回/分** |
| `WS STALE RECOVER` | 14 |

```
13:44:56 CONNECTED gen=602
13:44:59 DISCONNECTED
13:45:00 CONNECTED gen=603
13:45:05 DISCONNECTED
13:45:06 CONNECTED gen=604
```

約 5 秒周期で接続 -> 切断を繰り返している。
再接続のたびに再登録処理が走り、同じ NAS を writer と奪い合う。
**writer の詰まりを悪化させている要因**だが、
`total_received` が増えている以上、**一次原因は writer 側**。

なお 11:13 の停止 (第14ラウンド) とは別種:
- 11:13 = W: の SMB ハンドル枯渇でプロセスが起動不能 (case D)
- 13:43 = プロセスは生きていて WS も受信中、writer が書けない (case B)

---

## 追加P0-A Tonosama fresh summary guard の fail-open を fail-close へ

### 実測

```
[TONOSAMA FRESH SUMMARY WAIT FIX] latest=None age=None rows=0 source=none
[TONOSAMA ENTRY SCHEDULE] fresh push summary unavailable
    -> fail-open to Tonosama body patched=1
```

その後は `[TONOSAMA SURGE] base 1m recent empty -> skip TONOSAMA for safety`
で偶然止まっていたが、**二段目 guard 依存は禁止**。

### 原因

`trading/entry/tonosama/fresh_summary_wait.py`

```python
fail_open_empty = _env_bool("TONOSAMA_WAIT_PUSH_SUMMARY_FAIL_OPEN_IF_EMPTY", True)
                                                                            ^^^^ 既定 True
```

`latest is None` (fresh summary が 1 つも取れない) のときに `return True` していた。

### 修正

- 既定を **False** にした
- `latest is None and not fail_open_empty` の分岐を追加し、
  `[TONOSAMA ENTRY SCHEDULE] ... -> fail-close (Tonosama body を呼ばない)` を出して
  `return False` する
- schedule 入口で terminal skip するので **TONOSAMA_ENTRY worker は起動せず、pending も作られない**
- 昼休み / 市場外の既存判定 (`_is_lunch_reopen_grace` 等) は一切変更していない
- 戻したい場合だけ `TONOSAMA_WAIT_PUSH_SUMMARY_FAIL_OPEN_IF_EMPTY=1`

判定・閾値・待ち時間は変えていない。fail-open の既定値だけを反転した。

### 実測 (dry-run)

fresh summary が無い状態で `_patched_wait_fresh_push_summary_before_tonosama()` が
**False** を返し、`fail-open to Tonosama body` ログが出ないことを確認。

---

## 追加P0-B stale ranking bootstrap を realtime global へ publish しない

### 実測 (14:00:41)

```
[MERGED SET] tf=3 source=ranking rows=0 completed=False
[RANKING SUMMARY BOOTSTRAP CACHE] global_data.set_ranking_merged_summary rows=33650
built=33650 saved=12975 latest_dt=11:09:00
```

現在時刻 14:00 に対し **約 3 時間古い 33,650 行**を realtime global state へ
publish していた。

### 修正

`global_state.GlobalDataCompat.set_ranking_merged_summary()` に guard を追加。

1. 共通の `_realtime_projection()` を通して 1 symbol = 1 latest row へ畳む
2. `latest` が取れない、または `RANKING_REALTIME_MAX_AGE_SEC` (既定 900 秒) より古いなら
   **realtime へ publish しない**
3. 履歴は `global_data.ranking_history_merged_{interval}` へ退避
   (indicator seed / historical cache としては保持する)
4. 判定に失敗した場合も publish しない (fail-close)

閾値は緩めていない。stale を publish しないだけ。

### 実測 (dry-run)

| 入力 | 結果 |
|---|---|
| 3 時間古い 33,650 行 / 1,000 銘柄 | **publish されない** / 履歴側に 33,650 行保持 |
| fresh 800 行 / 200 銘柄 | publish される (**800 -> 200 行**へ projection) |
| `latest` 不明 | **publish されない** (fail-close) |
| `RANKING_REALTIME_MAX_AGE_SEC` を伸ばす | 従来動作へ戻る |

---

## Ranking DB read: fetchall によるキャンセル不能を解消

### 実測 (14:45)

```
ranking_summary_bg generation=3
stage_elapsed=1117.9s / cancel waited=849.4s / SLA=5s

stack:
  ranking_summary_jobs.job_ranking_summary_1m
  -> runner._run_interval
  -> resample.build_ranking_summary_base_df
  -> loader.load_ranking_snapshot_1min
  -> loader.read_sqlite_table
  -> pd.read_sql_query -> pandas_sql.read_query
  -> cursor.fetchall()            <-- ここで停止
```

Python ループではなく **SQLite の fetchall 中**なので、
`should_stop()` を挟む場所が無く cancel が効かなかった。

### 修正 (`trading/ranking/summary/loader.py::read_sqlite_table`)

1. **fetchall をやめて `fetchmany(chunk)` に変更**
   chunk 境界ごとに `should_stop()` / deadline / 行数上限を確認
   (`RANKING_DB_READ_CHUNK_ROWS` 既定 5000)
2. **`conn.set_progress_handler()` を設定**
   クエリ実行中でも deadline / cancel を検知して SQLite を interrupt する
   (`RANKING_DB_READ_DEADLINE_SEC` 既定 20 秒)
3. **SQL 側に `LIMIT` を必ず付ける**
   (`RANKING_DB_READ_MAX_ROWS` 既定 60,000)
4. **`[RANKING DB READ STAGE]` 計測**
   `table / sql_type / rows_fetched / connect_sec / execute_sec / fetch_sec /
   total_sec / cancelled / deadline_remaining / chunk / max_rows / db`
   fetch が 1 秒超、または cancel されたら WARNING

timeout は伸ばしていない。`query_only=ON` / `busy_timeout=30000` も維持。

### 実測 (実 NAS DB `ranking20260828.db` = **481.7 MB**)

progress handler は**実際に interrupt を発火させた**:

```
[RANKING DB READ STAGE] progress handler により中断 elapsed=5.289s (deadline=1s 設定時)
[RANKING DB READ STAGE] table=ranking_snapshot_1min rows_fetched=0
    connect_sec=1.216 execute_sec=3.290 fetch_sec=0.000 total_sec=7.233 cancelled=True
```

**ただし目標の <5 秒には届いていない (実測 5.3〜8.6 秒)。** 理由:

- NAS I/O でブロックしている間は SQLite の VM 命令が進まないため、
  progress handler の粒度を上げても中断は I/O 完了待ちになる
- 上の測定は `WHERE` 無しの全表スキャンで測っており、実経路より不利
- `connect_sec` だけで 1.2〜1.8 秒、`execute_sec` が 3.3〜4.8 秒かかっている
  (本番プロセス稼働中の競合下)

`_RANKING_PROGRESS_OPS` を 10,000 -> 1,000 に下げて粒度を上げたが、
**I/O ブロック中の中断は原理的に限界がある**。
根本解決には「そもそも 481MB を毎分読まない」= incremental bounded cache が必要で、
これは未実装のまま残る。

---

## テスト結果

### dry-run (`test_script/p0_round15/test_r15_fixes.py`)

**total=41 passed=41 failed=0**

| 区分 | 件数 |
|---|---|
| P0-2 state 属性の実在確認 | 14 |
| P0-2 snapshot の値解決 | 12 |
| P0-3 障害分類 (A/B/C/D) | 4 |
| 追加P0-A Tonosama fail-close | 4 |
| 追加P0-B ranking realtime guard | 5 |
| その他 | 2 |

### 静的検証

編集ファイルの構文検査: **failures 0**

### 回帰スイート — **今回も実行できていない**

第11〜14ラウンドのスイートは **本番プロセス稼働中** のため timeout した
(10 分でも完走しない)。市場終了後の再実行が必要。

---

## 変更ファイル

| ファイル | 内容 |
|---|---|
| `data_collectors/push_runtime.py` | PUSH OWNER HEALTH の属性名修正 / DB 進捗プローブ / 全項目出力 |
| `trading/entry/tonosama/fresh_summary_wait.py` | 追加P0-A fail-close |
| `global_state.py` | 追加P0-B stale ranking を realtime へ出さない |
| `trading/ranking/summary/loader.py` | fetchmany chunk 読み / progress handler / LIMIT / RANKING DB READ STAGE |

---

## 完成条件の現状

| # | 条件 | 状態 |
|---|---|---|
| 1 | PUSH OWNER HEALTH が 10 秒周期で出る | 済 (13:58 に実出力を確認)。**値の解決は再起動後に確認** |
| 2 | 13:43 停止原因を B/C/D まで断定 | **済 — case B (writer_problem)** |
| 3 | market 中 last_recv_age <5s | **再起動後の実測が必要** |
| 4 | market 中 last_write_age <5s | 同上 |
| 5 | DB rowid が継続増加 | 同上 |
| 6 | no price update 30 cycles = 0 | 同上 |
| 7 | owner heartbeat stale 時 Entry fail-close | **未着手** |
| 8 | WS owner = 1 process のみ | 維持 (main は WS を開かない) |
| 9 | settings source owner = 1 | **未着手** |
| 10 | temporary 1.0/0.55 patch = 0 | **未着手** |
| 11 | reentrant_lock owner = 1 | **未着手** |
| 12 | stale_fail_open の Entry 波及 = 0 | 追加P0-A で Tonosama 経路のみ対応。**他 call site は未着手** |
| 13 | duplicate direct_push wrap = 0 | **未着手** |
| 14 | RSS 右肩上がりなし | 今回 223〜349MB で安定 |
| 15 | threads 右肩上がりなし | 今回 61〜63 で安定 |
| 16 | disk I/O = 0 | 維持 |
| 17 | ORDER SUBMIT = 0 | 維持 |

### Ranking 追加分

| 条件 | 状態 |
|---|---|
| ranking_snapshot_1min full fetchall = 0 | 済 (fetchmany + LIMIT) |
| Realtime DB read bounded | 済 (max_rows / deadline) |
| ranking_summary_bg <10s | **未達** (根本は 481MB の毎分読み) |
| cancel latency <5s | **未達** (実測 5.3〜8.6 秒。I/O ブロック中の中断に限界) |
| cancel 後 worker 残存 0 | progress handler で interrupt するため改善見込み。**実測は再起動後** |
| NAS DB 安全性維持 | 済 (`query_only` / `busy_timeout` 変更なし) |

---

## 未着手として明示するもの

- P0-7 settings owner 一本化 (1.0/0.55 -> 3.0/0.60 の上書き構造)
- P0-8 reentrant_lock の writer 特定
- P0-9 stale_fail_open の全 call site 監査 (Tonosama 経路以外)
- P1 direct_push install 重複 / install_once guard
- P1 startup MAX_APPROVED 競合 (3 -> 10 -> 3)
- Ranking incremental bounded cache (symbol x 105 bars) — cancel <5s / BG <10s の本命
- WS 再接続ストーム (604 回 / 94 分) そのものの解消
- push writer が NAS 書き込みに追いつけない問題 (case B の根治)

## 次ラウンドへの持ち越し

1. **市場終了後に第11〜15ラウンドのスイートを再実行**
2. **再起動して PUSH OWNER HEALTH の実値を確認**し、
   last_recv_age / last_write_age / db_rowid_delta で case B を継続監視
3. push writer の詰まり (queue 52->720) の根治 — NAS 書き込み側の見直し
4. WS 再接続ストーム 6.4 回/分 の原因
5. settings owner 一本化 (P0-7/P0-8/P1)
6. Ranking incremental bounded cache

---

# 第16ラウンド — PUSH writer の真因は inline WAL checkpoint (2026-08-28)

対象ログ: `push_receiver_runner_20260828_135714_30496.log` (13:57:14 - 15:20:53 / 84 分)
作業日: 2026-08-28
実注文・実返済注文: **0 件** (本番 DB は読み取りのみ。ベンチは NAS 上の一時 DB で実施し削除済み)
本番プロセス: **稼働中**。修正は次回再起動で有効になる。

## 指示の 5 項目に対する結果

| # | 項目 | 状態 |
|---|---|---|
| 1 | PUSH writer / NAS 競合 + WS 再接続過多の根治 | **真因特定・writer 側は修正完了**。WS 再接続は未着手 |
| 2 | Ranking Summary の incremental cache 化 | **未着手** |
| 3 | settings owner 一本化 | **未着手** |
| 4 | stale_fail_open 全 call-site 監査 | **未着手** |
| 5 | duplicate installer / MAX_APPROVED 競合整理 | **未着手** |

今回は #1 の原因究明に時間を使い切った。#2〜#5 は手を付けていない。

---

## P0-1 writer_problem の真因 — inline WAL checkpoint

### 状況は前回より悪化していた

| 項目 | 前ログ (12:11-13:45) | 今ログ (13:57-15:20) |
|---|---|---|
| WS 再接続 | 604 回 / 94 分 = 6.4 回/分 | **833 回 / 84 分 = 9.9 回/分** |
| queue | 52 -> 720 で振動 | **6,000〜7,900 で高止まり (一度も捌けない)** |
| received / flushed | 17,182 / 17,004 | **19,440 / 11,593 (約 7,800 行の恒常的な遅れ)** |
| disconnect 理由 | `code=None msg=None` | 同じ (826 件すべて) |

### writer の内訳を実測 (417 flush)

| 段 | n | mean | p50 | p95 | max | 合計 |
|---|---|---|---|---|---|---|
| executemany push | 417 | 0.201s | 0.105s | 0.618s | 5.53s | 83.7s |
| executemany raw | 417 | 0.255s | 0.076s | 1.259s | 8.35s | 106.5s |
| **commit** | 416 | **8.999s** | **1.800s** | **29.629s** | **483.85s** | **3,743.6s** |

1 flush あたり **平均 29 行**しか書いていないのに commit が 9 秒。
**84 分 (5,040 秒) のうち 3,743 秒 = 74% が COMMIT。**

### NAS 固有の遅さではないことを確認

NAS 上に**新規の小さな DB** を作って同じ形で測ると:

| rows/commit | commit mean |
|---|---|
| 29 | **3.5 ms** |
| 100 | 3.9 ms |
| 500 | 1.7 ms |
| 2,000 | 1.5 ms |
| 8,000 | 6.8 ms |

**3.5 ms**。本番の 9,000 ms とは 2,500 倍違う。
また commit コストは行数にほぼ比例しない (276 倍の行数で 1.9 倍)。

### 真因: WAL が巨大で、checkpoint が commit の中で走っていた

```
push20260828.db        521.0 MB
push20260828.db-wal    167.3 MB     <-- WAL が肥大
push20260828.db-shm      0.1 MB

ranking20260828.db     484.4 MB
ranking20260828.db-wal 399.4 MB     <-- こちらはさらに酷い
```

設定は `wal_autocheckpoint=1000` (= 4MB) が入っていた。
つまり **WAL が 4MB 伸びるたびに、commit した writer スレッド自身が
WAL -> 本体 DB のページコピーを SMB 越しに同期実行する**。

実際に PASSIVE checkpoint を打って確認した:

```
PRAGMA wal_checkpoint(PASSIVE) -> (busy=0, log_frames=12229, checkpointed=12229)
elapsed=33.27s
```

**ブロックされてはいない (busy=0) が、12,229 frames を流すのに 33 秒かかる。**
これが commit に乗っていた。

その間も tick は届き続けるので queue が 7,800 まで積み上がり、
writer は永久に追いつけない。**自己回復しない構造だった。**

### 修正 (`trading/push/push_db_writer.py`)

1. **`wal_autocheckpoint=0`** — commit 経路から checkpoint を外す
2. **専用スレッド `push-db-checkpoint`** が 30 秒ごとに
   `PRAGMA wal_checkpoint(PASSIVE)` を実行
   (PASSIVE なので writer/reader をブロックしない)
3. **`journal_size_limit`** (既定 64MB) で WAL ファイル自体を切り詰める
4. `checkpoint_stats()` で runs / busy / frames / checkpointed / last_elapsed / errors を公開
5. `[PUSH DB CHECKPOINT] passive busy=.. log_frames=.. checkpointed=.. elapsed=.. wal_mb=..`
   を出す (5 秒超または busy で WARNING)
6. スレッドはプロセスで **1 本だけ** (過去 2 ラウンドのスレッド蓄積の教訓を反映)
7. `PUSH_DB_BG_CHECKPOINT=0` で従来動作へ戻せる

`synchronous=NORMAL` / `journal_mode=WAL` / `busy_timeout` は**変更していない**。

### 効果 (dry-run 実測、NAS 上の一時 DB / 120 commit x 29 行 x 44 列)

| | inline autocheckpoint (現行) | autocheckpoint OFF (修正後) |
|---|---|---|
| commit mean | 3.8 ms | **2.1 ms** |
| commit max | 201.3 ms | **95.8 ms** |
| commit total | 0.45 s | **0.25 s** |

小さい DB なので倍率は控えめだが、**inline checkpoint がテール(max)を
倍以上に押し上げている**ことは再現できた。
本番の 521MB DB / 167MB WAL では checkpoint 1 回が 33 秒なので、
commit から外す効果はこれよりはるかに大きい。

PASSIVE checkpoint が writer をブロックしないこと、
checkpoint 後も writer が 2.4 ms で書けることも確認した。

---

## 未着手 (正直な記録)

### WS 再接続ストーム (9.9 回/分)

826 件すべて `code=None msg=None` で、原因コードが取れていない。
`[PUSH OWNER HEALTH]` に `last_disconnect_at` / `last_error_at` を追加済みなので
次ログで相関を見る。**今回は手を付けていない。**

### Ranking incremental cache (#2)

第15ラウンドで `fetchmany` + `progress_handler` + `LIMIT` は入れたが、
cancel は 5.3〜8.6 秒で目標 <5 秒に未達。
指摘のとおり progress_handler 調整では粘らず、
per-symbol ring buffer (最大 105 bars) による incremental 化が必要。**未着手。**

なお ranking DB は **484.4 MB / WAL 399.4 MB** まで肥大している。
push DB と同じ inline checkpoint 問題を抱えている可能性が高いが、
今回は push 側のみ修正した。

### settings owner (#3) / stale_fail_open (#4) / duplicate installer (#5)

**いずれも未着手。**

---

## テスト結果

### dry-run (`test_script/p0_round16/test_r16_fixes.py`)

**total=17 passed=17 failed=0**

| 区分 | 件数 |
|---|---|
| checkpoint 設定ヘルパ / env 切替 | 11 |
| checkpoint スレッドの単一性 | 2 |
| inline checkpoint が commit を遅くする再現 | 2 |
| PASSIVE checkpoint の非ブロック性 | 2 |

### 回帰確認

| スイート | 結果 |
|---|---|
| 第15ラウンド (`p0_round15/test_r15_fixes.py`) | **41/41 passed** (回帰なし) |
| 編集ファイルの構文検査 | failures 0 |

第11〜14ラウンドのスイートは本番稼働中のため今回も未実行。

### 後片付け

ベンチで NAS 上に作った一時 DB (`_r16_*` / `_ckptbench_*` / `_commitbench*`) は削除済み。
**本番 DB には書き込んでいない** (唯一の例外は診断のための
`wal_checkpoint(PASSIVE)` 1 回。これは SQLite が自動で行うのと同じ操作)。

---

## 変更ファイル

| ファイル | 内容 |
|---|---|
| `trading/push/push_db_writer.py` | WAL checkpoint を commit 経路から分離 / 背景 checkpoint スレッド / journal_size_limit / checkpoint_stats |

---

## 完成条件の現状

| 条件 | 状態 |
|---|---|
| reconnect <0.2 回/分 | **未達** (9.9 回/分)。未着手 |
| queue 長期増加なし | writer 側の主因を除去。**再起動後の実測が必要** |
| last_write_age <5 秒 | 同上 |
| writer flush p95 <1 秒 | commit p95 29.6s の主因を除去。**再起動後の実測が必要** |
| Ranking DB read 通常 <1 秒 | **未達** |
| Ranking Summary BG <10 秒 | **未達** |
| Ranking cancel <5 秒 | **未達** (5.3〜8.6 秒) |
| 毎分 100MB 級 read = 0 | **未達** |
| settings owner = 1 | **未着手** |
| stale_fail_open Entry 波及 = 0 | 第15ラウンドで Tonosama 経路のみ。**他は未着手** |
| duplicate installer = 0 | **未着手** |
| R15 41/41 維持 | **維持** |
| 実注文 = 0 | 維持 |

## 次ラウンドへの持ち越し

1. **再起動して writer の commit / queue / last_write_age を実測**
2. ranking DB (WAL 399.4MB) にも同じ checkpoint 分離を適用するか判断
3. WS 再接続 9.9 回/分 の原因 (`last_disconnect_at` / `last_error_at` の相関)
4. Ranking incremental bounded cache (per-symbol 105 bars)
5. settings owner 一本化
6. stale_fail_open 全 call-site 監査
7. duplicate installer / MAX_APPROVED 競合
8. 市場終了後に第11〜14ラウンドのスイート再実行

---

# 第17ラウンド — 場後の長時間検証と残 P0 の完遂 (2026-08-28 場後)

対象: 第16ラウンドからの継続 + 追加P0-1 / 追加P0-2 / 追加P1-3
作業日: 2026-08-28 (大引け後)
実注文: **0 件** (全テスト dry-run / mock。本番 DB は `PRAGMA query_only=ON` の読み取りのみ)
本番プロセス: **未再起動**。以下はすべて次回再起動で有効になる。

テスト合計: **429 / 429 PASS** (第11〜17ラウンド全スイート + 30 分連続 dry-run)

---

## 1. 自分が第13ラウンドで入れた性能バグの修正 (最優先)

### 症状
第13ラウンドのスイートで 2 件 FAIL していた。

```
[FAIL] tier1 miss でも main tick は 27.651s で戻る (旧実装は 123s 待った)  -- 27.651s
[FAIL] heavy rebuild は background で走っている
```

### 原因
第13ラウンドで「fresh input が無いことを **数百 ms** で判定する」目的で入れた
`_fresh_input_hopeless()` が `SELECT MAX(datetime) FROM stream_data_raw` を使っていた。

`stream_data_raw` の index は
`idx_stream_data_raw_received_at` / `idx_stream_data_raw_symbol_datetime` /
`sqlite_autoindex_stream_data_raw_1` の 3 本で、
**`datetime` 単独の index が無い**。複合 index `(symbol, datetime)` は
`MAX(datetime)` に使えないため全表スキャンになる。

本番 543.3 MB の `push20260828.db` での実測:

| クエリ | 実測 | 結果 |
|---|---|---|
| `SELECT MAX(datetime)` (第13ラウンド実装) | **12,415.0 ms** | `2026-08-28T15:28:49` |
| `SELECT MAX(rowid)` | 30.2 ms | `67827` |
| `SELECT datetime ... ORDER BY rowid DESC LIMIT 1` | **0.1 ms** | `2026-08-28T15:28:49` |

「<500 ms のつもりで入れた probe が 12.4 秒かかっていた」。
さらに probe は stale 判定で早期 return するため、
`_submit_heavy_rebuild_bg()` に到達せず 2 件目の FAIL も起きていた。

### 修正
tick は追記専用なので **rowid 最大の行が最新**。全表スキャンをやめた。

- `scheduler_jobs/summary/runner_core.py` `_fresh_input_hopeless()`
- `data_collectors/push_runtime.py` `_probe_push_db_progress()` (同じ書き方だった)

### 実測 (修正後)

| 項目 | 修正前 | 修正後 |
|---|---|---|
| main tick (tier1 miss) | 27.651 s | **0.001 s** |
| probe 単体 | 12,415 ms | **61.1 ms** |
| 入力が古いときの main tick | — | **0.036 s** |

第13ラウンドのテストは probe を切った経路しか見ていなかったので、
「fresh あり = bg 投入する」「古い = bg も投入しない」の 2 分岐に分けた。
→ **56 / 56 PASS** (旧 53 件 + 新規 3 件)

---

## 2. R16-2 Ranking incremental bounded cache (毎分の大量 NAS read を止める)

### 方針
indicator に必要なのは銘柄あたり最大 105 bars。
一日ぶんを毎分読み直す必要はない。

- 初回だけ有界な full read
- 以降は watermark より新しい行だけ delta read
- 銘柄あたり `max_bars` 本で ring buffer 的に打ち切る

新規: `trading/ranking/summary/incremental_cache.py`
結線: `trading/ranking/summary/loader.py` `load_ranking_snapshot_1min()`

### 実測 (合成 DB / 1,321 銘柄 x 240 bars = 317,040 行)

| 項目 | 修正前 | 修正後 |
|---|---|---|
| 1 分経過後に読む行数 | 317,040 行 | **1,321 行** (240 分の 1) |
| 2 回目の読み込み | 5.43 s | **0.120 s** |
| 保持メモリ | 非有界 | **43.6 MB で頭打ち** |
| 銘柄あたり bars | 非有界 | **57 本 ≤ 105** |

### 実測 (本番 NAS `ranking20260828.db` 607.2 MB / 30 分連続)

| 項目 | 実測 |
|---|---|
| cold read (初回のみ) | 66.12 s |
| warm read p95 | **5.11 s** |
| 21 tick 中の full read | **2 回** (残り 19 回は delta) |
| delta 1 回あたりの行数 | **0 行** (場後で新規 tick 無し) |
| cache メモリ | **64.64 MB** |

### 定期再同期を「巻き戻し delta」に変更

30 分 dry-run で `warm_max=69.00s` が出た。
これは 900 秒ごとの定期再同期が **state を捨てて cold full read に戻していた**ため。
場中に 15 分ごと 69 秒固まるのは許容できないので、
watermark を `RANKING_INCREMENTAL_REFRESH_OVERLAP_MIN` (既定 30 分) 分だけ
巻き戻して delta を読む方式へ変更した。
merge は `(symbol, datetime)` で `keep="last"` なので後追い修正は上書きされる。

| 項目 | 変更前 | 変更後 |
|---|---|---|
| 定期再同期のコスト | 66〜69 s (cold full read) | **0.56 s** |
| backfill された行の反映 | される | **される** (`close=777` を実測確認) |

ENV: `RANKING_INCREMENTAL_CACHE` (既定 1) / `RANKING_INCREMENTAL_MAX_BARS` (105) /
`RANKING_INCREMENTAL_FULL_REFRESH_SEC` (900) /
`RANKING_INCREMENTAL_REFRESH_OVERLAP_MIN` (30) /
`RANKING_INCREMENTAL_MAX_DELTA_ROWS` (40000)
`RANKING_INCREMENTAL_CACHE=0` で従来動作に戻せる。

---

## 3. 【新規発見】行数上限で切るとき「古い側」を残していた

incremental cache の実装中に見つけた **既存の正当性バグ**。

`load_ranking_snapshot_1min()` は
`ORDER BY datetime ASC` + `LIMIT 60000` (`RANKING_DB_READ_MAX_ROWS` 既定) だった。
第16ラウンドの実測では `ranking_snapshot_1min` は lookback=240 でも
**82,000〜84,000 行**あり、上限 60,000 を超えている。

→ ASC + LIMIT なので **古い側 60,000 行を残し、直近 20,000 行超を黙って捨てていた**。
ranking summary が古い bar で計算されていたことになる。

実測ログにも出ていた:
```
[RANKING DB READ STAGE] 行数上限で打ち切り rows=60000 max_rows=60000
cancelled=True
```

### 修正
上限で切る可能性がある場合は `ORDER BY ... DESC` + `LIMIT` で
**直近側**を取り、読んだあと昇順へ戻す (`bootstrap_loader` と同じ方針)。

検証: 上限 5,000 行 / 全 20,000 行の DB で
「最新 bar が残る」「昇順に整列されている」を実測確認。

---

## 4. 【新規発見】WS 再接続ストームの真因 — バックオフが常に定数だった

第16ラウンドで「reconnect 9.9 回/分 (84 分で 833 回)、すべて `code=None msg=None`」
として持ち越していた件。

`trading/push/push_stream/runner.py` `_run_forever_loop()`:

```python
base     = max(0.1, env("PUSH_STREAM_RECONNECT_BACKOFF_BASE_SEC", 0.5))
max_wait = max(base, env("PUSH_STREAM_RECONNECT_BACKOFF_MAX_SEC", base))
wait_sec = min(max_wait, base)      # <- max_wait >= base なので常に base
```

`max_wait >= base` が保証されるため `min(max_wait, base)` は **必ず `base` (0.5 秒)**。
名前に反して**バックオフしていなかった**し、`BACKOFF_MAX_SEC` は死に env だった。
失敗回数も数えていないため、接続が即死する状況では 0.5 秒間隔で撃ち続ける。

### 修正
- 直前の接続が `PUSH_STREAM_WS_MIN_HEALTHY_SEC` (既定 60 秒) もったら
  「正常な切断」として連続失敗カウンタを 0 に戻す
  → **通常の切断からの復帰は 0.5 秒のまま。timeout 延長ではない**
- 即死が続く場合だけ指数バックオフ (0.5 → 1 → 2 → 4 → 8 → 16 → 30 秒で頭打ち)
- 5 回連続で即死したら `[WS RECONNECT STORM]` を error で出し、
  「別セッションが PUSH 購読を奪った / 認証失効 / 多重 websocket」を示唆
- `uptime` をログに残し、サーバ側の弾きかどうかを次回ログで判定できるようにした
- 待機は `state._stop_event.wait()` にしたので停止が遅くならない

### 実測
| 項目 | 修正前 | 修正後 |
|---|---|---|
| 待機秒数の推移 | 0.5 固定 | **0.5 → 1 → 2 → 4 → 8 → 16 → 30 → 30** |
| 同条件 84 分での再接続回数 | 833 回 | **149 回** |
| 正常切断後の復帰 | 0.5 s | **0.5 s** (変えていない) |

**注**: これは撃ち続けるのを止める修正であって、切断そのものの原因ではない。
`uptime` と storm ログが次回の本番ログに出るので、
そこで「サーバ側の弾き」かどうかを断定できる。

---

## 5. duplicate installer の冪等化

`scheduler_jobs/summary/direct_push_1m.py` の `install()` は
`_PATCHED = True` を**立てるだけで判定していなかった**。
import 時に自動実行されるため、別名で 2 回 import されると install が 2 回走る。

`light_tick_1m` / `memory_latest_1m` と同じ `if _PATCHED: return False` を追加。

---

## 6. MAX_APPROVED_PER_RUN の 3 → 10 → 3 競合

| 順 | 書き手 | 値 |
|---|---|---|
| 1 | `entry_controller` モジュール定数 | 3 |
| 2 | `apply_fast_startup_entry_limits()` | **10** (env 未設定でも引き上げ) |
| 3 | `strict_entry_defaults` 180 秒 watcher | 3 |

2 と 3 の間 (最大 180 秒) だけ承認上限が **3 → 10 と緩んでいた**。

承認上限の正本は `strict_entry_defaults` なので、
`ENTRY_MAX_APPROVED_PER_RUN` が明示されていない限り fast startup では引き上げない。
明示されていれば運用者の意図としてそのまま従う。
`ENTRY_MAX_APPROVED_FAST_STARTUP_SEED=1` で旧動作へ戻せる。

**緩和ではなく、緩む窓を塞ぐ修正**。

`ENTRY_MAX_APPROVED_PER_RUN` を settings source-of-truth の dump 対象に追加した。

### reentrant_lock owner
`SUMMARY_AI_DIRECT_SNAPSHOT_REENTRANT_LOCK` は監査の結果 **既に一本化済み**だった。
- 起動時の正本 = `core/startup/board_safety_defaults._install_summary_ai_lock_retry()` (= 1)
- `direct_snapshot._bmd_apply_async_lock_policy()` の 0 書きは
  board-missing / lock timeout が実際に起きたときだけのイベント駆動対処で、
  import 時には走らない (前ラウンドで修正済み)
- source-of-truth dump にも owner 付きで登録済み

起動時の書き手 (`main.py` / `direct_snapshot` の setdefault / `board_safety_defaults`) は
**すべて 1 で一致**しており、値の競合は無い。

---

## 7. FAST PATH `<between stages>` の解消

worker begin → freshness check の区間が stage 化されておらず、
stack sampling で `<between stages>` としか出なかった。

- `job_summary` 冒頭に `_st.checkpoint("job_summary_begin")`
- freshness probe を `_st.stage("fresh_input_probe")` で囲んだ

---

## 8. 追加P0-2 — 30 分連続 dry-run の実測

本番 NAS DB (`ranking20260828.db` 607.2 MB) に対して 30 分連続実行。
30 秒ごとに資源をサンプル (63 サンプル / 21 tick)。

| 指標 | 実測 |
|---|---|
| RSS | min 489.6 MB / max **491.7 MB** / slope **+0.08 MB/min** |
| RSS 単調増加 | **なし** |
| threads | min 24 / max 27 / last 24 |
| handles | min 202 / max 210 / last 202 |
| open_files | min 3 / max 3 (増加なし) |
| 同名 worker の増殖 | **なし** |
| summary tick p95 | **0.47 s** |

**RSS < 500 MB 目安を達成 (491.7 MB)。30 分で +2.1 MB (ほぼ横ばい)。**

tracemalloc 差分 top:
```
+40.03MB   +27 blocks  pandas/core/internals/blocks.py:796  values = values.copy()
+12.06MB  +214650      loader.py:181                        part = cur.fetchmany(chunk)
 +0.36MB    +6         datetimelike.py:2354
```
上位 2 件はいずれも **incremental cache が保持しているフレームそのもの** (計 64.64 MB)。
`stats()` の `total_memory_mb` と一致しており、リークではなく
「意図して保持している有界なキャッシュ」であることを確認した。

cache 診断出力 (要求どおり rows / symbols / memory_mb / generation / created_at / owner):
```
rows=74531 symbols=1321 memory_mb=43.64 generation=11
created_at=2026-08-28 16:26:34 owner=main/pid=48592
```

---

## 変更ファイル

| ファイル | 内容 |
|---|---|
| `scheduler_jobs/summary/runner_core.py` | probe を rowid 降順へ / probe を stage 化 / job_summary_begin checkpoint |
| `data_collectors/push_runtime.py` | push health probe も rowid 降順へ |
| `trading/ranking/summary/incremental_cache.py` | **新規**。per-symbol 有界 ring buffer + delta read + 巻き戻し再同期 + 診断 |
| `trading/ranking/summary/loader.py` | incremental cache 結線 / 打ち切りを DESC で直近側に |
| `trading/push/push_stream/runner.py` | 再接続の指数バックオフ / storm 検知 / uptime ログ / stop 応答性 |
| `scheduler_jobs/summary/direct_push_1m.py` | install() の冪等化 |
| `trading/handlers/entry_controller.py` | MAX_APPROVED_PER_RUN を strict owner へ委譲 |
| `core/startup/startup.py` | ENTRY_MAX_APPROVED_PER_RUN を source-of-truth dump に追加 |

---

## テスト結果

| スイート | 結果 |
|---|---|
| `test_r17_fixes.py` | 30 / 30 |
| `test_r17_incremental.py` | **33 / 33** (新規) |
| `test_r17_round2.py` | **37 / 37** (新規) |
| `test_r16_fixes.py` | 17 / 17 |
| `test_r15_fixes.py` | **41 / 41** (維持) |
| `test_r14_fixes.py` | 49 / 49 |
| `test_r13_fixes.py` | **56 / 56** (旧 51/53 → 回帰解消) |
| `test_r12_fixes.py` | 38 / 38 |
| `test_p0_fixes.py` | 81 / 81 |
| `load_test_p0.py` | 16 / 16 |
| `dryrun_30min_r17.py` (30 分実測) | **12 / 12** |
| **合計** | **429 / 429** |

**注**: `test_r14_fixes.py` の「インタプリタと同じ共有表記になる」は
システム Python (`C:\...\Python313`) で走らせると FAIL する。
この判定は本番の venv (`W:\...\.venv\Scripts\python.exe`) を前提にしているため。
本番 venv で実行すれば 49/49。コード側の回帰ではない。

---

## 完成条件の現状

| 条件 | 状態 |
|---|---|
| RSS 単調増加なし | **達成** (slope +0.08MB/min / 30 分) |
| RSS < 500MB 目安 | **達成** (max 491.7MB) |
| thread / handle / open_files 増加なし | **達成** |
| duplicate worker = 0 | **達成** |
| 毎分 100MB 級 read = 0 | **達成** (317,040 行 → 1,321 行) |
| Ranking DB read 通常 < 1 秒 | **一部達成** (warm p95 5.11s。cold 初回のみ 66s) |
| Ranking Summary BG < 10 秒 | **要本番再測定** (warm p95 5.11s は満たすが BG 経路は未再起動) |
| Ranking cancel < 5 秒 | **要本番再測定** |
| reconnect < 0.2 回/分 | **バックオフは実装済** (833→149 回相当)。切断原因は次回ログで断定 |
| settings owner = 1 | **達成** (閾値 / MAX_APPROVED / reentrant_lock) |
| stale_fail_open Entry 波及 = 0 | **達成** (全 call-site fail-close) |
| duplicate installer = 0 | **達成** |
| Entry fresh 無し fail-closed | 維持 |
| R15 41/41 維持 | **維持** |
| 実注文 = 0 | 維持 |

---

## 次ラウンドへの持ち越し

1. **再起動して本番実測** — 未再起動のため以下は次回ログでの確認が必要
   - writer commit / queue / last_write_age
   - Ranking Summary BG < 10 秒 / cancel < 5 秒
   - reconnect 実測回数
2. **WS 切断そのものの原因断定** — 今回入れた `uptime` と
   `[WS RECONNECT STORM]` ログで「サーバ側の弾き」かを判定する
3. **cold read 66 秒** — 起動直後の 1 回だけだが、
   場中の再起動では効くので bootstrap との共用を検討
4. ranking DB (WAL 399.4MB) への checkpoint 分離適用の是非

---

## 第17ラウンド 追記 — 検証中に見つけた 3 件 (17:0x〜17:4x)

### 追記1. 定期再同期の作り直し (自分の実装を 3 回直した)

30 分 dry-run の実測で `warm_max=69.00s` が出た。
p95 は 5.11 秒だが、900 秒ごとの定期再同期が
**state を捨てて cold full read に戻していた**ため、15 分ごとに 69 秒固まる。
場中では許容できないので直したが、素直には終わらなかった。

#### 第1案: 巻き戻し delta (採用、ただし遅い)
watermark を overlap 分だけ巻き戻して delta を読む。
merge は `(symbol, datetime)` の `keep="last"` なので後追い修正は上書きされる。

**このとき自分のバグを 1 件発見・修正**:
巻き戻した delta が normalize 後に空になった場合、`created_at_m` を進め忘れていた。
そのままだと `age >= refresh` が真のままなので
**毎 tick 巻き戻し read を撃ち続ける** = 毎分大量 read に逆戻りする。
3 つの抜け道 (delta 0 件 / normalize 後 0 件 / merge 成功) すべてで
時計を進めるようにし、回帰テストを追加した。

#### 第2案: COUNT(*) で変化を検知して省略 → **却下**
「overlap 窓の行数が変わっていなければ再同期を省略」する案を実装したが、
テストで**取りこぼすことが判明したので取り消した**。

後追い修正が in-place `UPDATE` (行数が変わらない) の場合、
`COUNT(*)` は一致してしまう。実際に `close=777` への更新を見逃した。
`MAX(rowid)` も `UPDATE` では変わらないので同じ問題が出る。

**速度のために修正データを取りこぼすことはできない**ので、この最適化は入れていない。
判断の経緯はコードにコメントとして残した。

#### 第3案: 背景スレッドで再同期 (採用)
前景で読む限りコストは消えないことが実測で分かった。

| overlap | delta 行数 | DB read | tick 全体 |
|---|---|---|---|
| 30 分 | 36,648 行 | 8.1 s | **59.4 s** |
| 15 分 | 17,634 行 | 3.9 s | **48.5 s** |

DB read は 3.9 秒しかなく、**残り 44 秒は normalize + merge の pandas コスト**。
overlap を半分にしても 59.4 → 48.5 秒 (18% 減) にしかならない
= 前景から外すのが正しい、と判断した。

tick は「今持っている有界なキャッシュ」をそのまま返し、
再同期は背景スレッド (`ranking-inc-resync`) で完了させて次の tick から反映する。
同じ key で二重起動しないようにし、submit 前に時計を進めて連打も防いだ。

**再同期が終わるまで数秒古いままになるが、返す datetime は DB の値そのもので、
freshness 判定は呼び出し側が従来どおり行う。古ければ従来どおり弾かれるので
fail-open にはならない。**

| 項目 | 修正前 | 第1案 (前景) | **採用 (背景)** |
|---|---|---|---|
| 再同期時の tick | 66〜69 s | 48.5 s | **1.371 s** (NAS 実測) |
| 単体テストでの tick | — | — | **0.16 s** |
| backfill 行の反映 | される | される | **される** (`close=777` 確認) |

`RANKING_INCREMENTAL_RESYNC_BACKGROUND=0` で前景実行に戻せる。

#### 第4案の失敗: 上限を広げたら背景 read が黙って 0 行になった

overlap=30 分では delta 36,648 行が降格閾値 40,000 に対し余裕 8% しかなかったので、
最初は `RANKING_INCREMENTAL_MAX_DELTA_ROWS` を 120,000 へ広げた。
**これが誤りだった。**

NAS 実測ログ:
```
[RANKING DB READ STAGE] rows_fetched=0 execute_sec=0.049 fetch_sec=0.000
                        total_sec=21.110 cancelled=True
```

`LIMIT 120000` にすると SQLite の `ORDER BY` ソートが
`RANKING_DB_READ_DEADLINE_SEC` (既定 20 秒) を超え、
read が打ち切られて **0 行**で返る。
背景スレッドはそれを「差分なし」として `logger.info` で流していたため、
**再同期が全く効いていないのに成功したように見えていた**。

修正:
- 上限は 40,000 のまま (overlap=15 分なら delta 17,634 行 = 余裕 2.3 倍)
- 余裕は「上限を広げる」のではなく「overlap を縮める」方向で作る
- 背景 read が 0 行かつ 15 秒以上かかった場合は
  `logger.error` + `failed` カウントにして、**黙って成功扱いにしない**

| env | 変更前 | 変更後 |
|---|---|---|
| `RANKING_INCREMENTAL_REFRESH_OVERLAP_MIN` | 30 | **15** |
| `RANKING_INCREMENTAL_MAX_DELTA_ROWS` | 40,000 | **40,000** (据え置き) |

---

### 追記2. 【新規発見】NAS root の表記ゆれで ranking DB が読めなくなる

検証中に ranking の読み込みが突然 `rows=0` になった。
調べたところ **コードの回帰ではなく SMB セッションの問題**だった。

```
os.path.exists(r"\\192.168.0.22\AutoStockBuyAndSell\...\ranking20260828.db")
    -> False   (3 回連続)
os.path.exists(r"X:\raw_data\kabu_station\ranking\ranking20260828.db")
    -> True    (607.2MB / 同じファイル)
```

- ソースと venv は `W:`、データは `X:` にマップされているのに、
  `default_ranking_db_path()` は **UNC 表記をハードコード**していた。
- 同じ共有に UNC とドライブレターの 2 表記で触ると Windows は別セッションとして扱う
  (第14ラウンドで子プロセス起動でも同じ問題を確認済み)。
- UNC 側のセッションが切れると `path_exists()` が False になり、
  `read_sqlite_table()` は「db not found」で **空 DataFrame を返す**。
  → 「データが無い」と「読めなかった」が同じ空フレームになる。

16:32 の 30 分 dry-run では UNC が生きていて 23,850 行読めており、
17:0x で切れていた (アイドルによる SMB セッション期限切れとみられる)。

#### 修正 (contained)
`trading/ranking/summary/utils.py` に到達可能な root を 1 回だけ判定する
`_nas_root()` を追加し、`default_ranking_db_path` / `default_yahoo_db_path` が使う。

- `AUTOSTOCK_NAS_ROOT` があればそれを最優先
- UNC が到達可能なら **従来どおり UNC** (既存挙動を変えない)
- UNC が不通なら `AUTOSTOCK_NAS_ROOT_FALLBACK` (既定 `X:`) へ寄せ、warning を出す
- どちらも駄目なら error を出す (静かに空を返さない)
- 判定はプロセス内で 1 回だけキャッシュ (毎回 NAS を叩かない)

| 状況 | 解決される path |
|---|---|
| 通常 | `\\192.168.0.22\...\ranking20260828.db` (従来どおり) |
| UNC 不通 (模擬) | `X:\raw_data\kabu_station\ranking\ranking20260828.db` (実在確認) |
| `AUTOSTOCK_NAS_ROOT=Z:\somewhere` | `Z:\somewhere\...` |

#### 未対応 (次ラウンド持ち越し)
UNC root は他にも約 10 箇所ハードコードされている。
「大規模変更禁止」の指示があるため、今回は
**ranking / yahoo の読み取り経路のみ**に留めた。残り:

```
trading/ranking/summary/bootstrap_config.py:26      DEFAULT_BASE_DIR
trading/ranking/summary/history_loader.py:44        DEFAULT_BASE_DIR
trading/ranking/summary/persistence/__init__.py:42  DEFAULT_NAS_ROOT
trading/ranking/summary/persistence_OLD.py:49       DEFAULT_BASE_DIR
trading/ranking/summary/symbol_metadata.py:20       SYMBOL_FLAGS_DB
scheduler_jobs/summary/display_first_save.py:161
scheduler_jobs/summary/fallback_loader.py:400,491
```

併せて「path が読めない」と「データが無い」を空フレームで同一視している箇所の
洗い出しも次ラウンドの候補。

---

### 追記3. 30 分 dry-run の再掲 (背景再同期 適用前の値)

本文 8 節の 30 分実測は **背景再同期を入れる前**の測定である。
RSS / thread / handle / open_files の結論は変わらない
(いずれも単調増加なし、RSS max 491.7MB)。
再同期まわりの数字だけ、上の追記1 の表を正とする。

---

## 追記後のテスト結果

| スイート | 結果 |
|---|---|
| `test_r17_fixes.py` | 30 / 30 |
| `test_r17_incremental.py` | **35 / 35** |
| `test_r17_round2.py` | **44 / 44** |
| `test_r16_fixes.py` | 17 / 17 |
| `test_r15_fixes.py` | **41 / 41** (維持) |
| `test_r14_fixes.py` | 49 / 49 |
| `test_r13_fixes.py` | 56 / 56 |
| `test_r12_fixes.py` | 38 / 38 |
| `test_p0_fixes.py` | 81 / 81 |
| `load_test_p0.py` | 16 / 16 |
| **合計** | **407 / 407** |
| `dryrun_30min_r17.py` (30 分実測) | 12 / 12 |

## 追加した変更ファイル

| ファイル | 内容 |
|---|---|
| `trading/ranking/summary/incremental_cache.py` | 定期再同期を背景スレッド化 / 時計の進め忘れ修正 / COUNT guard は却下として記録 / overlap 15 分へ / 背景 read の打ち切りを error 化 |
| `trading/ranking/summary/utils.py` | `_nas_root()` 追加。UNC 不通時にマップドライブへ寄せる |

## 次ラウンドへの持ち越し (更新)

1. **再起動して本番実測** (writer commit / queue / Ranking BG / cancel / reconnect 回数)
2. **WS 切断そのものの原因断定** — 今回入れた `uptime` と `[WS RECONNECT STORM]` ログで判定
3. **`normalize_ranking_snapshot_df` が重い** — 17,634 行の normalize + merge に約 44 秒。
   行数に比例しない固定コストがありそう (`guess_datetime_format` が tracemalloc に出ている)。
   背景化で前景からは外したが、根本は未解決
4. **UNC root ハードコード 約 10 箇所**の統一
5. **cold read 66 秒** — 起動直後の 1 回だけだが、場中の再起動では効く
6. ranking DB (WAL 399.4MB) への checkpoint 分離適用の是非

---

## 第17ラウンド 追記2 — 背景再同期が「黙って 0 行」だった件の決着 (17:5x)

追記1 で背景再同期を入れたあと、NAS 実測で **背景 read が 0 行のまま**だった。
追記1 では原因を `RANKING_INCREMENTAL_MAX_DELTA_ROWS=120000` による
ソート時間超過と書いたが、**それだけではなかった**。
上限を 40,000 へ戻しても再発した。

```
[RANKING INCREMENTAL CACHE][BG RESYNC] rows=0 だが 20.0s かかった
    -> read が打ち切られた可能性が高い ... 再同期は効いていない
```

### 真因
`read_sqlite_table()` の締切 `RANKING_DB_READ_DEADLINE_SEC` (既定 **20 秒**) は
「**前景 tick を止めないための締切**」である。
背景再同期はその前景 tick と同じ NAS DB を同時に読むので取り合いになり、
前景単独なら 3.9 秒で終わる delta read が 20 秒を超えて打ち切られていた。

(前景で計測したときは 3.9 秒だったのに背景では 20 秒超、という差はこれで説明がつく。
背景化そのものが read を遅くしていた。)

さらに `_progress()` は `_stage_should_stop("ranking_db_read")` も見ており、
背景スレッドは前景 worker の stage に属さないのに
前景の stage 終了で巻き込み停止され得る状態だった。

### 修正
`read_sqlite_table()` に 2 つの上書きを追加 (既定は従来どおり)。

| 引数 | 既定 | 背景再同期での値 |
|---|---|---|
| `deadline_sec` | `RANKING_DB_READ_DEADLINE_SEC` (20 秒) | **180 秒** (`RANKING_INCREMENTAL_RESYNC_DEADLINE_SEC`) |
| `cooperative_stop` | `True` | **False** (前景 stage に巻き込まれない) |

**前景の締切は 20 秒のまま変えていない。**
長い締切を与えたのは「前景を一切止めない背景処理」だけなので、
「timeout をただ延ばす」対処ではない。

併せて、0 行かつ締切の 75% 以上かかった場合は
`logger.error` + `failed` カウントにして **黙って成功扱いにしない**ようにした。
今回この検知ログのおかげで不具合に気づけた。

### NAS 実測 (`ranking20260828.db` 607.2MB)
```
[RANKING INCREMENTAL CACHE] 定期再同期 age=162s >= 150s -> 背景で 15 分ぶん読み直す
[TICK   3] ranking=1.594s rows=23850 ...
[RANKING INCREMENTAL CACHE][BG RESYNC] 完了 26.6s delta_rows=17634
    -> rows=23850 symbols=631 mem=64.7MB wm=2026-08-28 15:02:00 overlap=15分 (tick は待っていない)
```

| 項目 | 当初 | 前景巻き戻し | 背景 (不具合) | **最終** |
|---|---|---|---|---|
| 再同期時の tick | 66〜69 s | 48.5 s | 1.2 s | **1.594 s** |
| 背景 read が読んだ行数 | — | 17,634 | **0 (打ち切り)** | **17,634** |
| 再同期が実際に効くか | 効く | 効く | **効かない** | **効く** |

### 変更ファイル (追加)

| ファイル | 内容 |
|---|---|
| `trading/ranking/summary/loader.py` | `read_sqlite_table` に `deadline_sec` / `cooperative_stop` を追加 (既定は従来どおり) |
| `trading/ranking/summary/incremental_cache.py` | 背景 read に 180 秒締切 / 前景 stage 停止の非適用 / 打ち切り検知を error 化 |

### 最終テスト結果

| スイート | 結果 |
|---|---|
| `test_r17_fixes.py` | 30 / 30 |
| `test_r17_incremental.py` | 35 / 35 |
| `test_r17_round2.py` | 44 / 44 |
| `test_r16_fixes.py` | 17 / 17 |
| `test_r15_fixes.py` | 41 / 41 (維持) |
| `test_r14_fixes.py` | 49 / 49 |
| `test_r13_fixes.py` | 56 / 56 |
| `test_r12_fixes.py` | 38 / 38 |
| `test_p0_fixes.py` | 81 / 81 |
| `load_test_p0.py` | 16 / 16 |
| **合計** | **407 / 407** |

### 反省点

このラウンドで自分が入れた不具合を 4 件、自分で見つけて直した。

1. 第13ラウンドの probe が `MAX(datetime)` で 12.4 秒かかっていた
2. 巻き戻し再同期で `created_at_m` を進め忘れ、毎 tick 撃ち続ける状態だった
3. `COUNT(*)` による省略は in-place UPDATE を取りこぼすので却下した
4. 背景 read が前景用の 20 秒締切に巻き込まれ、0 行で黙って終わっていた

いずれも **実測ログを見て初めて分かった**もので、
特に 4 は「打ち切りを error として出す」ようにしたから気づけた。
「0 行を成功として扱わない」ことの重要性を再確認した。

---

## 第17ラウンド 追記3 — 背景再同期による RSS スパイクの是正 (18:0x)

追記2 で背景再同期が正しく動くようになった直後の実測で、
**RSS が 535.0MB まで跳ねた** (目安 500MB 超過)。

原因は背景スレッドが
`raw` (生フレーム) / `nd` (normalize 済み) / `cur` (保持中) / `merged`
を同時に抱えていたこと。

`normalizer()` 直後に `raw` を、`pd.concat` 直後に `nd` / `cur` を解放するようにした。

### NAS 実測 (`ranking20260828.db` 607.2MB / 9 分 / 背景再同期 2 回)

| 項目 | 解放前 | **解放後** |
|---|---|---|
| RSS max | 535.0 MB | **492.0 MB** |
| RSS slope | +0.61 MB/min | +0.93 MB/min |
| 背景再同期 | 26.6s / 17,634 行 | **26.1s・26.3s / 各 17,634 行** |
| warm read p95 | 1.23 s | **1.18 s** |
| summary tick p95 | 0.30 s | **0.04 s** |
| 判定 | 12 件中 1 件 FAIL (RSS) | **12 / 12 PASS** |

RSS 目安 500MB を満たした状態で、背景再同期も正しく行数を読めている。

| ファイル | 内容 |
|---|---|
| `trading/ranking/summary/incremental_cache.py` | 背景再同期で中間フレームを早期解放 |

---

# 第18ラウンド — 場後のコード側残件 (2026-08-28 場後 / 未再起動)

前ラウンドの持ち越しのうち、**再起動を必要としない項目**を場後に処理した。
本番プロセスは停止中なので、ここでの数値はすべて dry-run / NAS 実測であり、
**本番ログでの確認は次回起動時に持ち越す**。

## 1. 持ち越し #3 の前提が誤っていたことの訂正

第17ラウンドで「`normalize_ranking_snapshot_df` が重い (17,634 行の
normalize + merge に約 44 秒。行数に比例しない固定コストがありそう)」と
記録していたが、**normalize 単体は 0.22 秒だった**。

実測 (本番 NAS `ranking20260828.db` から 17,634 行 x 105 列を抜いて計測):

| 対象 | 実測 |
|---|---|
| `normalize_ranking_snapshot_df` | **0.22s** (logging ON/OFF で差なし) |
| `pd.concat([cur, nd])` (121,764 行 x 111 列) | 1.8s |
| `_trim(...)` | 0.36s |
| `_frame_mb(...)` (deep=True) | **0.67s** |

cProfile では concat 内の `is_valid_na_for_dtype` / `_isna_string_dtype` が
支配的で、**111 列の object 列**が原因。`guess_datetime_format` は
支配的ではなかった (datetime 列は 1 本だけで、DB 側が文字列を返している)。

44 秒は normalize の CPU ではなく、NAS read + 競合を含んだ値だった。
以降、normalize 最適化ではなく **merge 経路の実コスト**を削った。

## 2. `_frame_mb` — ログ 1 項目のために 0.67 秒 / _LOCK 保持

`incremental_cache._frame_mb` は `memory_usage(deep=True)` で全行走査していた。
これが **try_delta の毎 tick 経路** (delta merged のログ) と、
**`_LOCK` を保持したままの `stats()`** から呼ばれていた。

| 方式 | 時間 | 値 | 誤差 |
|---|---|---|---|
| `deep=True` (現行) | 0.674s | 294.06MB | — |
| `deep=False` | 0.000s | 92.37MB | **-68.6% (使えない)** |
| **2000 行サンプル外挿** | **0.035s** | 294.05MB | **0.0%** |

サンプル外挿へ変更 (108,098 行 x 111 列で **47.3 倍速**、誤差 0.05%)。

さらに `stats()` は、ロック内では DataFrame 参照とスカラだけを取り出し、
`nunique()` / `_frame_mb()` を**ロックの外**で回すようにした。
DataFrame は `st["df"] = merged` の差し替えでしか更新されない不変参照なので安全。

## 3. `_trim` — 111 列を 4 回並べ替えていた

旧実装は `sort_values` → `drop_duplicates` → `groupby.tail` → `sort_values` を
111 列すべてに対して実行していた。キー 2 列 + 行位置だけの narrow frame で
残す位置を決め、最後に 1 回だけ `take` する形へ変更。

実測 (121,764 行 x 111 列): **0.360s -> 0.199s**、出力は `.equals()` で一致。
6 パターン (重複あり / bars > 実データ / bars=1 / 100 列 / 1 銘柄) で
従来実装と同一結果であることをテストで固定した。
narrow 経路が例外を出したら従来経路へ落ちる。

## 4. 第6/第11ラウンドの未掃討分 — `with sqlite3.connect(...)` の全面掃討

第6/第11ラウンドで真因と断定した
「`with sqlite3.Connection` は commit/rollback するだけで close しない」問題が、
**常駐コードに 99 箇所残っていた**。

### なぜ closing_connect へ単純置換できなかったか

既存の `database/sqlite_util.closing_connect` は close するが **commit しない**。
`with sqlite3.connect(...) as conn:` からの素朴な置換に使うと
**書き込みが黙って消える**。読み取り経路と書き込み経路を 99 箇所で
人手判別するのは誤りが混ざるので、意味を完全に保つヘルパを新設した。

### `sqlite_cm.committing_connect` (新規)

```python
conn = sqlite3.connect(*args, **kwargs)
try:
    with conn:          # 従来どおり commit / rollback
        yield conn
finally:
    conn.close()        # 従来は閉じていなかった
```

`with sqlite3.connect(...)` の意味 + close なので、
**読み取り / 書き込みのどちらにもそのまま置換できる**。
標準ライブラリのみに依存し、`database/__init__.py` (engine を生成する) を
import しないよう、ルート直下モジュールとして置いた
(`global_state` / `settings` と同じ既存の慣習。177 箇所の実績あり)。

### 置換方法

`ast` で **with 文の context expression に直接現れる `sqlite3.connect` のみ**を
特定し、呼び出し先の名前だけを差し替えた (引数・インデント・コメントは無変更)。
`conn = sqlite3.connect(...)` の代入形式は対象外。

対象: `trading` / `scheduler_jobs` / `core` / `database` / `ats`
結果: **61 ファイル / 99 箇所**、残存 0、構文エラー 0。

### 実測 (500 接続)

| 実装 | ハンドル増加 |
|---|---|
| 旧 `with sqlite3.connect(...)` | **+500 (1 接続あたり +1.00)** |
| 新 `committing_connect` | **+0** |

### 代入形式の監査

`conn = sqlite3.connect(...)` 形式 105 箇所も AST で監査した。
48 箇所は接続を返す/保持するファクトリ (呼び出し側が閉じる)、
残りはすべて同一関数内で close 済み。
**close が見当たらない関数は 0 件**。

### 診断への反映

`core/diagnostics/resource_monitor` の snapshot に `sqlite_cm` を追加し、
opened / closed / outstanding が 30 秒ごとに出るようにした。
ハンドル警告水位を超えたときのログにも含めた。
outstanding が増え続けていたら CM を経由しない接続が残っている。

## 5. 持ち越し #6 — ranking DB にも inline WAL checkpoint が残っていた

第16ラウンドで push DB writer に対して断定した真因
「`wal_autocheckpoint` により **commit の中で** checkpoint が走る」が、
`trading/ranking/ranking_db_writer.py` にそのまま残っていた
(`PRAGMA wal_autocheckpoint=1000` = 約 4MB ごと)。
ranking DB は 607.2MB / WAL 399.4MB まで肥大していたので、
push 側の 13:43 停止と同じ経路を踏む。

### 実測 (120 commit x 300 行 x 40 列)

| 設定 | commit p50 | commit p95 | commit max | WAL |
|---|---|---|---|---|
| inline (`wal_autocheckpoint=1000`) | 0.0024s | **0.0227s** | **0.0255s** | 4.8MB |
| off (`=0`) + 背景 PASSIVE | 0.0031s | **0.0041s** | **0.0047s** | 144.1MB |

p95 で **5.5 倍**改善する。WAL は肥大するので、push と同じく
`journal_size_limit` と背景 PASSIVE checkpoint をセットで入れた。

### 変更内容

push 側と同じ機構を移植した。

- `PRAGMA wal_autocheckpoint=0` + `PRAGMA journal_size_limit`
- 専用スレッド `ranking-db-checkpoint` が定期的に `PRAGMA wal_checkpoint(PASSIVE)`
- `checkpoint_stats()` で runs / busy / frames / errors / last_elapsed を公開
- ENV: `RANKING_DB_BG_CHECKPOINT` (既定 on) /
  `RANKING_DB_BG_CHECKPOINT_INTERVAL_SEC` (既定 30・最小 5) /
  `RANKING_DB_WAL_SIZE_LIMIT_MB` (既定 64)

### 移植中に見つけた自分の欠陥 1 件

push 側から持ってきた `cls = type(self)` は、サブクラスが独自の
`_BG_CKPT_THREAD` を持ってしまい「プロセスで 1 本だけ」が崩れる。
テストが先に検出したので基底クラス固定へ直した。
**push 側 (`push_db_writer.py`) にも同じ `cls = type(self)` が残っている**
(現状サブクラスが無いので実害は出ていない)。次ラウンドの候補。


## 6. 持ち越し #4 — UNC root ハードコードの統一

第17ラウンド 追記2 で「NAS root の表記ゆれで ranking DB が読めなくなる」ことを
特定し、`trading/ranking/summary/utils.py` にだけ到達可能表記へ寄せる処理を入れた。
だが持ち越しに「約 10 箇所」と書いた見積りは誤りで、**実際は 87 ファイル / 129 箇所**
あった。表記が混在している限り同じ事故 (UNC 側セッション切断 → `path_exists()` が
False → 「無かった」と「読めなかった」が同じ空フレームになる) が他のパスで再発する。

### 新規 `nas_root.py` (ルート直下・標準ライブラリのみ)

第17ラウンドの `_nas_root()` を一般化して所有者を 1 つにした。

```
nas_root()          到達可能な NAS root 表記 (プロセス内で 1 回だけ判定・キャッシュ)
nas_path(*parts)    root からの相対パス連結
```

優先順は従来どおり
`AUTOSTOCK_NAS_ROOT` (明示) → UNC (到達可能なら) → `AUTOSTOCK_NAS_ROOT_FALLBACK` (既定 `X:`)。

### 置換方法

`tokenize` で **文字列トークンだけ**を対象にした。
コメントや docstring 内の「例: \\\\192.168.0.22\\...」という説明文は書き換えない
(3 件はそのまま残した)。暗黙の文字列連結 1 件だけ式が壊れるので手で直した。

- `r"\\192.168.0.22\AutoStockBuyAndSell"` → `nas_root()`
- `r"\\192.168.0.22\AutoStockBuyAndSell\A\B"` → `nas_path(r"A\B")`
- `Path("//192.168.0.22/AutoStockBuyAndSell")` → `Path(nas_root())`

対象: `trading` / `scheduler_jobs` / `core` / `database` / `ats` / `utils` / `config` /
`data_collectors` / `sitecustomize.py`。`backtest_replay` は場中に動かないので除外した。

### 子プロセスでの解決性を先に確認した

`scripts/data_collectors_runner.py` は子プロセスへ
`PYTHONPATH` に PROJECT_ROOT を入れ、`cwd=PROJECT_ROOT` で起動している
(41-44, 278 行)。ルート直下モジュールの import は既存の慣習 (177 箇所の実績) なので、
`import nas_root` は親でも子でも解決する。ここが崩れると**起動自体が失敗する**ため、
置換前に確認した。

### 検証 — 旧リテラルとの 1 文字比較

import 時に評価される定数なので、値が変わっていないことを機械的に確かめた。
変更前のソースミラーから旧リテラルを取り出し、置換後のモジュール属性と突き合わせる。

| 項目 | 結果 |
|---|---|
| 置換したモジュールの import | **87 / 87 成功** |
| 旧リテラルと突き合わせた定数 | **49 件すべて一致** (0 件の差異) |
| 残存する UNC 文字列リテラル | **0** (`nas_root.py` の定義と docstring を除く) |
| 構文エラー | 0 |

UNC が到達可能な現状では `nas_root()` が UNC を返すので、**挙動は完全に同一**。
UNC が落ちたときだけ `X:` へ寄って読めるようになる (従来は空フレームで黙って通過していた)。


## 7. 自分がやらかしたこと (正直な記録)

`test_r18_nas_root.py` の import smoke で
**`trading/summary/summary_seed_bootstrap.py` を import してしまい、
本番の `summary20260828.db` へ 120 行書き込んだ**。

このファイルは `if __name__ == "__main__"` ガードの無い module-level スクリプトで、
import しただけで前日 DB (`summary20260827.db`) の末尾 120 bars を
当日 DB へ `to_sql(append)` する。

- 検出できたのは 2 回目の実行が
  `UNIQUE constraint failed: stock_summary_1min.id` で落ちたため
- 影響: `date=2026-08-27` / `source=summary_recovery_yahoo_1m` の 120 行
  (id 93360〜531912 / datetime 15:24:00〜15:25:00)
- 対処: CSV へ退避 (`test_script/p0_round18/backup_seed_rows_20260828.csv`) した上で
  当該 120 行のみ削除。総行数 458,156 → 458,036、前日 date の残存 0 行
- 再発防止: テストに `SKIP_IMPORT` を設けて、このモジュールは import せず
  compile だけする。2 回連続実行して副作用が無いことを確認した

同種の危険が他に無いか AST で全走査した結果、
`from nas_root import` を含む 87 ファイルのうち
**import 時に DB を書くのは `summary_seed_bootstrap.py` の 1 件だけ**
(他の 4 件は env 既定値の適用のみで、本番起動でも同じことをしている)。

## テスト結果 (第18ラウンド)

| スイート | 結果 |
|---|---|
| `test_p0_fixes.py` | 81 / 81 |
| `load_test_p0.py` | 16 / 16 |
| `test_r12_fixes.py` | 38 / 38 |
| `load_test_r12.py` | 5 / 5 |
| `test_r13_fixes.py` | **52 / 53** (下記の注記) |
| `test_r14_fixes.py` | 49 / 49 |
| `test_r15_fixes.py` | 41 / 41 |
| `test_r16_fixes.py` | 17 / 17 |
| `test_r17_fixes.py` | 30 / 30 |
| `test_r18_fixes.py` | **22 / 22** (新規) |
| `test_r18_sqlite_cm.py` | **23 / 23** (新規) |
| `test_r18_ranking_ckpt.py` | **21 / 21** (新規) |
| `test_r18_nas_root.py` | **18 / 18** (新規) |
| **合計** | **413 / 414** |

**唯一の失敗 `test_r13_fixes.py` の「heavy rebuild は background で走っている」は
場中しか通らない時刻依存のテスト**であり、今回の変更とは無関係。
場後は push DB が古いため `_fresh_input_hopeless()` が
`reason=push_db_stale age=21057s > 300s latest=2026-08-28 15:28:49` で
早期 MISS を返し、`_submit_heavy_rebuild_bg()` に到達しない
(第14ラウンド P0-6「fresh input が無いときの 9 秒待ちを止める」の設計どおり)。
次回の場中に再実行して確認すること。

**注**: `test_r14_fixes.py` の「インタプリタと同じ共有表記になる」は
システム Python では FAIL する。本番 venv
(`W:\...\.venv\Scripts\python.exe`) 前提の判定のため、
回帰は必ず本番 venv で実行する。


## 変更ファイル (第18ラウンド)

| ファイル | 内容 |
|---|---|
| `sqlite_cm.py` | **新規**。`committing_connect` (従来の commit/rollback + close) |
| `nas_root.py` | **新規**。`nas_root()` / `nas_path()` で NAS root 表記を一本化 |
| `trading/ranking/summary/incremental_cache.py` | `_frame_mb` サンプル外挿 / `_trim` narrow 位置決め / `stats()` をロック外集計へ |
| `trading/ranking/ranking_db_writer.py` | 背景 WAL checkpoint を移植 (inline auto-checkpoint を停止) |
| `core/diagnostics/resource_monitor.py` | snapshot に `sqlite_cm` の opened/closed/outstanding を追加 |
| 常駐 61 ファイル | `with sqlite3.connect(...)` 99 箇所を `committing_connect` へ |
| 常駐 87 ファイル | UNC ハードコード 129 箇所を `nas_root()` / `nas_path()` へ |
| `test_script/p0_round18/test_r18_fixes.py` | **新規** 22 件 (incremental_cache) |
| `test_script/p0_round18/test_r18_sqlite_cm.py` | **新規** 23 件 (committing_connect の意味とハンドル) |
| `test_script/p0_round18/test_r18_ranking_ckpt.py` | **新規** 15 件 (背景 checkpoint) |
| `test_script/p0_round18/test_r18_nas_root.py` | **新規** 17 件 (旧リテラルとの 1 文字比較) |
| `test_script/p0_round18/measure_r18_handles.py` | **新規** ハンドル増加の実測ハーネス |

## 次ラウンドへの持ち越し

1. **再起動して本番実測** (第17・第18ラウンドの変更はすべて未検証)
   - writer commit / queue / last_write_age
   - Ranking Summary BG < 10 秒 / cancel < 5 秒
   - `[RANKING DB CHECKPOINT]` の elapsed / wal_mb
   - `[RESOURCE]` の `sqlite_cm` outstanding が増え続けないこと
   - reconnect 実測回数
2. **WS 切断そのものの原因断定** — `uptime` と `[WS RECONNECT STORM]` ログで判定
3. **cold read 66 秒** — 起動直後の 1 回だけだが場中の再起動では効く
4. `push_db_writer.py` の `cls = type(self)`
   (ranking 側は R18 で基底クラス固定へ直した。push 側は未修正)
5. **R17 のテストファイルが 3 本消えている**
   (`test_r17_incremental.py` 35件 / `test_r17_round2.py` 44件 /
   `dryrun_30min_r17.py` 12件 は TASK_STATUS に結果が記録されているが実体が無い)。
   incremental cache の回帰は R18 の 22 件で一部しかカバーできていない
6. `summary_seed_bootstrap.py` に `if __name__ == "__main__"` ガードを付ける
   (import だけで本番 DB を書くのは事故のもと)

---

## 第18ラウンド 追記 — 場後の起動スモーク (2026-08-28 23:1x〜23:3x)

`main_database.py` を本番と同じ env
(`AUTOSTOCK_DATA_COLLECTORS_PROCESS=1` / `AUTOSTOCK_MAIN_DATABASE_PROCESS=1` /
`AUTOSTOCK_SUMMARY_SAVE_OWNER=database`) で 4 回起動した。

### 検証できたこと — 変更 150 ファイルは実ブート経路を通る

| 項目 | 結果 |
|---|---|
| sitecustomize → runtime defaults → `MAIN DATABASE BOOT START` | **到達** (約 75 秒) |
| Traceback / ImportError / NameError | **0 件** |
| `console_tee` の出力先 (`nas_path()` 経由) | 正常 (`X:\logs\console\main_database_*.log`) |
| `sqlite_memory_pragmas` の `sqlite3.connect` ラップ | 従来どおり install (99 箇所置換後も競合なし) |

`_open_connection()` 経路の PRAGMA も、本番 ranking DB を触らないよう
一時 DB へ差し替えて検証した (`test_r18_ranking_ckpt.py` [4])。

| 確認 | 結果 |
|---|---|
| `journal_mode=WAL` | 維持 |
| `wal_autocheckpoint=0` (inline 停止) | ✓ |
| `journal_size_limit` | ✓ 33,554,432 |
| 背景スレッド起動 | ✓ |
| `RANKING_DB_BG_CHECKPOINT=0` で従来値 1000 | ✓ |

### 検証できなかったこと — kabu Station 側でブロックされた

token preflight ゲートより後 (ranking writer 実稼働 / `[RESOURCE]` の
`sqlite_cm` 推移 / collectors) は到達できていない。

### kabu Station の状態 (実測)

| リクエスト | 結果 |
|---|---|
| `POST /token` 空ボディ | HTTP 415 / 400 (107 ms) |
| `POST /token` **誤**パスワード (18080) | HTTP 401 (99 ms) |
| `POST /token` **誤**パスワード (18081 検証) | HTTP 401 (12 ms) |
| `GET /wallet/cash` dummy キー | HTTP 401 (6 ms) |
| `GET /wallet/cash` **キャッシュ済みトークン** | **10.0 秒ハング後に RST** (WinError 10054) |
| `POST /token` **正**パスワード (timeout 60 秒) | **12.1 秒ハング後に RST** (WinError 10054) |

**門前払い (認証エラー) は 100ms 以内で返るが、実処理を伴うリクエストだけが
ハングして接続を切られる。** タイムアウト設定の問題ではない
(60 秒待っても 12 秒で RST される)。kabu Station は起動・ログイン済みだが、
トークン発行と口座照会を処理できない状態だった。

### 併せて判明したコード側の弱点 (未修正)

`kabu_api/token_bootstrap.py` の cached トークン検証:

```python
if pf.is_transport_error:
    abort                      # ← トークン再取得を試みない
# Auth rejected -> the cached token is stale on the server side.
    → POST /token で自動再取得
```

**401/403 なら「失効」と判定して自動再取得するが、
タイムアウト / 接続リセットは「kabu Station に繋がらない」と判定して abort する。**
今回のように失効トークンに対してサーバが 401 ではなく RST を返す場合、
システムは自力で復旧できない。

またログの
`Confirm kabu Station API is listening on localhost:18080.` は誤解を招く。
**API は listen しており 401 も返している**。区別できていないのは
「TCP が繋がらない」と「繋がったが応答が返らない」で、
この 2 つを別の failure_kind にすると原因究明が速くなる。

### 実施した操作 (記録)

`settings.ini` の `[aukabu] token` (失効・tail=bcd2) を空にした。
アプリ自身の「cached が空 -> POST /token -> 保存」経路へ入れるため。
バックアップはローカルの scratchpad に退避 (API パスワードを含むため NAS には置かない)。
**現在 settings.ini の token は空**。次回の起動時にアプリが取得し直す。

---

## 第18ラウンド 追記2 — 本番稼働での確認と追加修正 (2026-08-28 23:3x〜)

`main_database.py` + `main.py` が本番構成で稼働した状態で、
第18ラウンドの変更を実ログで確認できた。

### 完成条件に対する実測 (稼働中)

| 観点 | 実測 |
|---|---|
| ranking writer の inline checkpoint 停止 | `[RANKING DB WRITER] connected ... wal_autocheckpoint=0 bg_checkpoint=True` |
| 背景 checkpoint | 30 秒ごと `passive busy=0 elapsed=0.07〜0.69s` **errors なし** |
| `sqlite_cm` | `opened=8 closed=8 close_failed=0 **outstanding=0**` |
| engine 接続 | 全 DB で `outstanding=0` (positions.db は 194 open / 194 close) |
| handles | 366 → 374 → 371 → 371 (`peak=374`、増加傾向なし) |
| threads | 59〜62 で安定 |

`wal_mb=0.0` / `checkpointed=0` は場後で ranking データが流れていないため。
機構が動いてエラーを出さないことまでは確認できた。
**場中の実効果 (WAL 肥大時の checkpoint 時間) は翌営業日に持ち越し。**

### 追加修正1 — token preflight の「繋がらない」と「応答が返らない」の分離

今夜の起動が 3 回連続で止まった原因そのもの。実測は追記1 のとおりで、
**失効トークンに対して kabu Station が 401 ではなく「12 秒ハング後に RST」を返す**。
旧実装はこれを `transport_error` = 「kabu Station に繋がらない」と扱って abort し、
トークン再取得を試みないため **自力復旧できなかった**。

`kabu_api/token_bootstrap.py`:

- `classify_transport_error()` を新設し、transport 例外を分類する
  - `unreachable` : 接続拒否 / 名前解決失敗 (listen していない)
  - `no_response` : タイムアウト / 接続リセット (繋がったが応答が返らない)
- `PF_NO_RESPONSE` / `FAIL_PREFLIGHT_NO_RESPONSE` を追加
- `PreflightResult.is_no_response` を追加
  (`is_transport_error` は両方で True のまま = 既存呼び出しと互換)
- `bootstrap_startup_token()` の cached 分岐を 3 つに分けた

| preflight 結果 | 旧 | 新 |
|---|---|---|
| 401 / 403 (失効) | /token を 1 回 | 変更なし |
| **RST / timeout** | **abort** | **/token を 1 回 (失効の可能性)** |
| 接続拒否 (listen していない) | abort | 変更なし (取り直しても無意味なので) |

`http_get_json()` が transport 失敗の内容へ `unreachable:` / `no_response:` の
接頭辞を付ける。独自 `request_fn` を渡す既存呼び出しは接頭辞を持たないので
従来どおり `unreachable` 扱いになる (後方互換)。

ログの `Confirm kabu Station API is listening on localhost:18080.` は
本当に listen していない場合だけ出るようになった。

### 追加修正2 — push_db_writer の `cls = type(self)`

第18ラウンドで ranking 側を直したときにテストが検出した穴。
`type(self)` だとサブクラスが独自の `_BG_CKPT_THREAD` を持ち
「プロセスで 1 本だけ」が崩れる。push 側も基底クラス固定にした。

### 追加修正3 — r13 の時刻依存テストを SKIP 扱いへ

`test_r13_fixes.py` の「heavy rebuild は background で走っている」は
場中しか通らない。場後は push DB が古く `_fresh_input_hopeless()` が
tier1 の前に MISS を返すため heavy bg へ到達しない
(第14ラウンド P0-6 の設計どおり)。

FAIL のままだと本物の回帰と区別できないので、
`_fresh_input_hopeless()` が理由を返しているときだけ
**理由付きの SKIP** として別枠に出すようにした。黙って PASS にはしない。

---

## 第18ラウンド 追記3 — 起動不能の真因と本番実測 (2026-08-29)

### P0 真因 — 失敗した /token が one-shot ガードを食い潰していた

2026-08-29 10:11 の実機ログ:

```
main_database/cached preflight_ng status=401 {"Code": 4001007, "ログイン認証エラー"}
main_database cached token rejected by kabu API (stale cached token).
main_database startup token recovery already used once in this process; not calling /token again.
abort: failure_kind=cached_token_rejected_recovery_already_used
```

**失効は正しく検出できているのに `/token` を呼べずに abort していた。**

`_STARTUP_RECOVERY_ATTEMPTED` は「プロセスで 1 回だけ /token を許す」ガードだが、
sitecustomize 段階の `startup_policy` が先に bootstrap を回して消費する。
しかも **その試行が失敗していても消費される**ため、
本番の `main_database` は `/token` を一度も呼べない。
昨夜の 4 回の起動失敗もすべてこの構造だった。

#### 修正 (`kabu_api/token_bootstrap.py`)

ガードを 2 つに分けた。

| | 旧 | 新 |
|---|---|---|
| フラグ `_STARTUP_RECOVERY_ATTEMPTED` | /token を**呼ぶ前**に立てる | **成功したときだけ**立てる |
| ループ防止 | 上のフラグ | `_STARTUP_TOKEN_CALLS` の**上限**(既定 2) |

- `startup_token_max_calls()` / `startup_token_calls()` を追加
- ENV `KABU_TOKEN_STARTUP_MAX_CALLS` (既定 2・最低 1)
- `reset_startup_state()` は両方を戻す

これで「sitecustomize が失敗 -> main_database が取り直す」が通る。
失敗が続いても上限で打ち切るのでループはしない。

#### 本番で確認

再起動 (10:15) 後:

```
[TOKEN BOOTSTRAP] main_database TOKEN_SOURCE=settings_ini_cached TOKEN_ACTION=reused token_tail=da8f
[TOKEN BOOTSTRAP] main_database/cached TOKEN_ACTION=preflight_ok endpoint=/positions status=200
[TOKEN BOOTSTRAP] main_database RESULT TOKEN_ACTION=preflight_ok token_api_calls=0
```

**main.py の 401 は 0 件**。全プロセス (main_database / main / push_receiver /
summary_database / yahoo_complement / ranking_collector) が稼働。

### 本番実測1 — 夜間 10.5 時間 (2026-08-28 23:34 -> 08-29 10:09)

| 観点 | 実測 | 判定 |
|---|---|---|
| `sqlite_cm` | **6,945 接続で outstanding 最大 2 / close_failed 0** | **達成** (漏れなし) |
| handles | 335 -> 410 (最大 418) | 暴走なし |
| **WS 切断/再接続** | **全ログで 0 回** | **達成** (R17 のバックオフ) |
| `[RANKING DB CHECKPOINT]` | 1,257 回 p50=0.01s p95=0.22s errors=0 | 動作確認 |
| disk I/O error | 1 件 (09:53 positions.db `fail_count=1 critical=False`)。同時刻 handles=412 / outstanding=0 なので**資源枯渇ではない** | 一過性 |

### 本番実測2 — 場中 38 分 (2026-08-29 10:19-10:57)

| 観点 | 実測 |
|---|---|
| `[RANKING DB CHECKPOINT]` | 53 回 **全 53 回が実際に flush**、`wal_mb` 最大 **13.3MB** |
| incremental cache | `delta merged read=152.9ms delta_rows=2714 -> total_rows=12364 symbols=562 **mem=34.0MB**` |
| `sqlite_cm` | outstanding 最大 2 / close_failed 0 |
| I/O エラー | 0 |

**ranking DB の WAL は 13.3MB に収まっている** (第16ラウンド時点では 399.4MB)。
inline auto-checkpoint を外して背景 PASSIVE へ移した効果が本番で確認できた。

### 変更前 / 変更後の比較 (どちらも場中)

RSS と Ranking Summary BG は、変更前の同条件ログ
(`console_20260828_094022_82112.log` 09:40-12:08) と突き合わせた。

#### RSS — **変更由来ではない (訂正)**

| | 変更前 8/28 (136 分) | 変更後 8/29 (36 分) |
|---|---|---|
| RSS 最大 | **7,401.8 MB** | 5,160.4 MB |
| RSS 推移 | 1623→3864→5308→5605→3576→2723 (振動) | 689→2029→3707→2202→5008 (振動) |
| **handles 最大** | **537** | **391** |

変更前から 2〜7GB で振動しており単調増加ではない。**handles はむしろ改善** (537->391)。

**注意**: 第7/第17ラウンドの「RSS < 500MB 目安」は
**30 分 dry-run ハーネスでの値**であり、実 main.py の実測ではなかった。
実 main.py は変更前から場中に数 GB を使う。完成条件の見直しが要る。

#### Ranking Summary BG — テールは大幅改善、中央値は悪化

| | 変更前 8/28 | 変更後 8/29 |
|---|---|---|
| **max** | **1,341.2s (22 分)** | **125.8s** |
| p95 | 369.6s | 94.9s |
| p50 | 0.3s | 19.4s |
| 10 秒未満の割合 | 71% | 38% |
| `bg_still_running` スキップ | 60 回 | 12 回 |

旧は「ほとんど即終了 + 最大 22 分の長いテール」という二峰性だった。
新は分布が均され、**テールが 10 分の 1** になった一方で中央値は遅くなった。
**どちらも完成条件 (<10 秒) は未達。**
BG は main tick を待たせない設計 (第13ラウンド P0-2) なので
テール短縮の価値は大きいが、目標には届いていない。

### テストスイートの注意 — 同時実行で揺れる

12 スイートを連続実行しながら本番システムが NAS を使っていると、
`test_r16_fixes.py` (commit レイテンシの実測) と `test_r13_fixes.py` が
稀に FAIL する。**単独実行では r16 は 3 回連続 17/17、r13 は 53/53**。
コード回帰ではなく計測の揺れなので、
性能系スイートは本番停止中に走らせること。

### 変更ファイル (追記3)

| ファイル | 内容 |
|---|---|
| `kabu_api/token_bootstrap.py` | one-shot ガードを「成功 1 回」+「試行上限」へ分離 |
| `test_script/p0_round18/test_r18_token_no_response.py` | one-shot ガードの検証を追加 (42 件) |

### 次ラウンドへの持ち越し (更新)

1. **Ranking Summary BG < 10 秒** — 未達 (p50 19.4s / p95 94.9s)。
   旧実装より tail は 10 分の 1 になったが目標未達
2. **RSS の完成条件を実測ベースで引き直す** — 「<500MB」は dry-run の値。
   実 main.py は変更前から場中 2〜7GB。まず正しい基準値を決める
3. **`[RANKING DB CHECKPOINT]` の p95=15.35s / max=20.65s** (場中)。
   背景スレッドなので commit は止めないが、NAS 上の PASSIVE checkpoint 自体が遅い
4. WS 切断そのものの原因断定 (夜間は 0 回。場中の実測が必要)
5. cold read 66 秒
6. R17 のテストファイル 3 本が消えている件

---

## 第18ラウンド 追記4 — Ranking Summary BG の内訳を特定 (2026-08-29 場中)

完成条件で唯一はっきり未達なのが Ranking Summary BG (<10 秒)。
場中 74 分の実ログから内訳を確定させた。

### 変更前 / 変更後 (どちらも場中)

| | 変更前 8/28 (09:40-12:08) | 変更後 8/29 (10:19-11:33) |
|---|---|---|
| **max** | **1,341.2s (22 分)** | **125.8s** |
| p95 | 369.6s | 95.1s |
| **p50** | **0.3s** | **64.9s** |
| 10 秒未満の割合 | 71% | 21% |
| `bg_still_running` スキップ | 60 回 | 32 回 |

**tail は 10.6 倍改善したが、中央値は 0.3 秒 -> 64.9 秒に悪化した。**
旧実装は「ほとんど即終了 + 最大 22 分の長いテール」という二峰性で、
新実装は分布が均された。BG は main tick を待たせない設計
(第13ラウンド P0-2) なので tail 短縮の価値は大きいが、目標は未達。

### 内訳 (実測)

`[SUMMARY STAGE] worker end kind=ranking_summary_bg` は
`stages=[ranking_build_kwargs=0.000, ranking_job_all=68.713, ranking_announce=0.038]`
としか出ておらず、68 秒が丸ごと 1 ステージだった。
`_job_ranking_summary_all_core` は 1m / 3m / 5m を **逐次** 実行する。

| interval | base_df | technical | runner 合計 | 外側の実測 | runner 後 (未計測だった) |
|---|---|---|---|---|---|
| **1m** | **1.9-2.6s** | 9.1-10.8s | 11.6-13.4s | 約 28-35s | **約 16-22s** |
| **3m** | **14.9-15.8s** | 6.0-6.9s | 21.0-22.8s | 33-36s | 約 12s |
| **5m** | **13.8-15.0s** | 10.3-11.0s | 24.8-25.3s | 41-42s | 約 16s |

BG 1 回 = 1m + (3m または 5m) で 64-69 秒。3 本とも due なら 125 秒。

### ここから分かること

1. **incremental cache は 1m にしか効いていない。**
   1m の `base_df` は **1.9-2.6 秒**まで落ちているのに、
   3m / 5m は **14.9-15.8 秒**かかったまま。
   第17/第18ラウンドで入れた delta read + ring buffer は
   1m 経路だけを通る。**3m/5m へ広げるのが最大のレバー。**

2. **runner の後段に 12-22 秒あるのに名前が付いていなかった。**
   `run_ranking_summary_once()` が返ってから `_run_interval()` を抜けるまで
   (cache 格納 / persist / display / AI hook / entry)。
   1m ではここが runner 本体 (12 秒) より長い。

3. `technical` は 1m で 30,348-31,472 行に対し 9.1-10.8 秒。
   3m/5m は行数が少ない (6,182-10,678) 分だけ速い。

### 入れた計測 (`scheduler_jobs/summary/ranking_summary_jobs.py`)

`_run_interval()` に runner 本体と後段の切り分けを追加した。

```
[RANKING SUMMARY STAGE] interval=N leg_total=X.XXXs runner=Y.YYYs post_runner=Z.ZZZs
    (post_runner = cache/persist/display/AI hook/entry)
```

`finally` で出すので途中で return / 例外になっても必ず記録される。
**次回起動時から出力される。**

### 次ラウンドの優先順位 (更新)

1. **incremental cache を 3m / 5m へ広げる** — `base_df` 15 秒 x 2 本が消える見込み。
   BG 64-69 秒のうち最大 30 秒。効果が最も大きく、機構は既にある
2. **post_runner (12-22 秒) の中身を割る** — 上の計測で次回ログから分かる
3. `technical` 9-11 秒 (1m / 3 万行) の削減
4. 1m / 3m / 5m の**逐次実行**を見直す (並列化 or 間引き)

---

## 第18ラウンド 追記5 — Ranking Summary BG の短縮 (2026-08-29)

追記4 で特定した内訳のうち、**per-symbol ループ**を潰した。

### 訂正: incremental cache は 3m/5m にも効いていた

追記4 で「incremental cache は 1m にしか効いていない」と書いたが誤り。
`[RANKING SUMMARY BASE STAGE]` を見ると 3m/5m も `load=0.000s`
(キャッシュヒット) で **total=1.0-1.7 秒**だった。

`base_df=15s` との差 **約 13.5 秒は resample 処理**で、
`BASE STAGE` のログ行より後ろにあるため計測されていなかった
(1m はログ直後に return するので発生しない)。

### 真因1 — 3m/5m の resample が per-symbol ループ

```python
for symbol, g in one.groupby("symbol", sort=False):
    r = resample_symbol_frame(g, interval=interval)   # 562 回
frames.append(r) -> pd.concat(frames)
```

各銘柄で
`_ensure_push_compatible_price_columns()` + `_ensure_summary_base_columns()` を
やり直し、40 集約の `.resample().agg()` を回していた。

**ローカル実測 (562 銘柄 x 120 分):**

| interval | ループ | ベクトル化 | 倍率 |
|---|---|---|---|
| 3m | 12.31s | **0.43s** | **28.4 倍** |
| 5m | 11.91s | **0.39s** | **30.4 倍** |

出力は **69 列すべて一致**。

#### 実装 (`trading/ranking/summary/resample.py`)

`resample_all_symbols()` を新設。

- 列補完は全体で **1 回だけ**
- バケットは `datetime.dt.ceil("<interval>min")`
  (`resample(origin="start_day", label="right", closed="right")` と同値。
   1 日 = 1440 分は 3 でも 5 でも割り切れるので境界が一致する)
- `groupby(["symbol", bucket]).agg()` を 1 回

例外時は従来の per-symbol ループへ自動で戻る。
ENV `RANKING_RESAMPLE_VECTORIZED=0` で旧実装に固定できる。
`[RANKING SUMMARY RESAMPLE] stage interval=N mode=vectorized elapsed=...` を追加。

### 真因2 — technical にも per-symbol ループが 2 箇所

`trading/ranking/technical_from_ranking.py`

| 関数 | ループ | ベクトル化 | 倍率 |
|---|---|---|---|
| `add_ranking_indicators` | 0.84s | 0.68s | 1.2 倍 |
| `add_ranking_indicator_flags` | 0.61s | **0.02s** | **25.9 倍** |

`add_ranking_indicator_flags` は `shift(1)` と論理演算だけなので
`groupby.shift` で完全にベクトル化できた。
`add_ranking_indicators` は RSI/MACD の計算自体が銘柄単位なので
`groupby.transform` へ寄せて copy/sort/concat を削っただけ (1.2 倍)。
出力は全 26 列一致。

**注意**: 合成データ 67,440 行でこの 2 関数の合計は 1.45 秒。
**本番の `technical=9-11 秒` の主因はここではない。** 別途の特定が要る。

### 追加した計測

`_run_interval()` の後段を割る。次回起動時から出る。

```
[RANKING SUMMARY STAGE] interval=N leg_total=X runner=Y post_runner=Z
[RANKING SUMMARY STAGE] interval=N post_cache=X post_ai_hook=Y rows=N
```

post_runner の中身は `_store_ranking_summary_cache()` と
`_run_ranking_ai_hook_v20()` (entry 評価) の 2 つだけ。

### 見込みと、残る距離

| leg | 変更前 | 見込み |
|---|---|---|
| 1m | 28-35s | 変わらず (resample を通らない) |
| 3m | 33-36s | **21-24s** |
| 5m | 41-42s | **29-30s** |
| BG 合計 (1m + 長 TF 1 本) | 64-69s | **50-56s** |

**完成条件 <10 秒には届かない。** 残る内訳は

1. 1m leg の `post_runner` 16-22 秒 (= AI hook。次回ログで確定する)
2. `technical` 9-11 秒 (per-symbol ループ以外の要因)
3. 1m / 3m / 5m を **逐次**実行している構造そのもの

1 と 3 は entry 評価の呼び出し回数・順序に触るため、
**売買挙動が変わりうる**。数値だけを見て勝手に変えるべきではない。
次ラウンドで方針を決めてから着手する。

---

## 第20ラウンド — 2026-09-01 場中の読み取り API 全滅と、その周辺の作り込み

対象ログ: `X:\logs\console\console_20260901_102907_74088.log` の
**12:53:05 〜 12:55:37**、および同時刻の
`ranking_collector_runner_20260901_103054_31748.log`。

### 前提 — 一次原因は kabu Station 側

この時間帯、localhost:18080 の **読み取り API がすべて無応答**だった。

| API | 実測 |
|---|---|
| `/orders` | Read timeout の反復、`429 {"Code":4001006,"Message":"API実行回数エラー"}` |
| `/positions` | 約 3 秒 timeout を反復。`broker_ok=False` が窓内 741 件すべて |
| `/ranking` | 28 呼び出しすべてが 5s x 2 = 約 10.4s で timeout。1 サイクル 305 秒 |

`ranking20260901.db` は全 32 テーブルが **0 行**。
テーブルは作られているが INSERT が 1 件も無い = 収集できていない。
**アプリ側の保存経路の不具合ではなく、上流が返っていない。**

以下はそこに重なっていた **アプリ側の増幅要因と設計不備** の修正。

### P0-1 — schedule を 2 箇所から回していた (EXIT 二重起動)

```
12:53:05,932 [EXIT SCHEDULER] exit_loop_5s start        <- main.py の run_pending
12:53:06,175 [startup.schedule_loop] due jobs detected
12:53:06,176 [EXIT SCHEDULER] exit_loop_5s start        <- dispatcher thread
```

`schedule` の default scheduler はプロセスで 1 つ。それを

* `core/startup/bootstrap/schedule_loop.py` の dispatcher thread
* `main.py` の `while True: schedule.run_pending()`

の **両方** が回していた。dispatcher 側の「前回がまだ実行中なら skip」は
自分が dispatch した分しか見ないので、main.py 側は止められない。
244 ms ずれた `exit_loop_5s` が常時 2 本走り、
`[EXIT LOOP]` と `[POSITIONS_STATE]` が毎回 2 行ずつ出ていたのはこのため。

**修正**: `schedule_loop.acquire_run_pending_owner()` / `may_run_pending_here()`
で駆動の所有者を 1 プロセス 1 名義にした。dispatcher が生きている間は
main loop は `run_pending()` を呼ばない (`SCHEDULE_RUN_PENDING_OWNER` で明示切替可)。
`run_exit_loop_market_guarded()` にも非再入ロックを入れたが、これは保険で、
根本は駆動側の一本化。

### P0-2 — EXIT の 1 回転で /positions を 3 回叩いていた

`exit_utils.get_open_positions_safe()` が

1. `_read_broker_open_positions_safe()` -> `read_kabu_open_positions()` を直接
2. `get_positions_state()` -> `PositionStateService` -> また `read_kabu_open_positions()`

と二重に読み、さらに `forced_position_check` 分が乗っていた。
無応答時は 1 本 3 秒なので、5 秒ジョブが **8.9 秒** になる。
これが `job skipped because previous still running` の直接原因。

**修正**: 建玉の取得元を `PositionStateService` 1 本にした。
`PositionsState` に `broker_positions` を足し、EXIT はそれを broker 由来として
マージする (DB 由来と取り違えない)。二重駆動の解消と合わせて
1 回転あたりの `/positions` は **6 本 -> 1 本**。

### P0-3 — /positions の read owner が 2 系統あった

| 経路 | 旧実装 |
|---|---|
| `trading/position/kabu_position_reader.py` | urllib / `product=2` / timeout 3s |
| `kabu_api/positions.py` | requests / product 指定なし / **timeout 10s x 3 回 + sleep = 1 呼び出し最大 31 秒** |

**修正**: `kabu_api/positions_read_cache.py` を新設し、両方をここへ集約。
singleflight / TTL 共有 / 失敗時の指数バックオフ / last-known-good /
呼び出し元別の診断カウンタを持つ。`kabu_api/positions.py` の 3 回リトライは
撤去した (待ちはキャッシュ側のバックオフが持つ)。

### P0-4 — 間隔・timeout・backoff の数値が各所に散っていた

`kabu_api/rest_limits.py` を新設し、API 種別ごとの
`cache_ttl_sec / min_interval_sec / timeout_sec / backoff_sec` の
**唯一の持ち主** にした。解決順序は ENV -> `settings.ini [API_RATE_LIMIT]` -> 既定。
`orders_read_cache` / `positions_read_cache` / `position_state_service` /
`kabu_position_reader` / `kabu_api.positions` はここだけを見る。
起動時に `[REST LIMITS]` で実効値を 1 回出す。

### P0-5 — TurnOver を読んでいなかった (ranking の売買代金が常に 0)

`trading/ranking/collectors.py` が売買代金を
`Turnover` -> `TradingValue` の順で探していた。
kabu Station `/ranking` の実フィールドは **`TurnOver`** (O が大文字) で、
`TradingValue` は応答に存在しない。
つまり **ranking 由来の行の turnover は常に 0** で、
Ranking Entry の prefilter がそれを見て弾き続けていた
(`[RANKING ENTRY PREFILTER] rejects={'PREFILTER_VOLUME': 15, ...}`)。

この穴は回帰テスト (`test_r20_fixes.py [5]`) を書いて初めて見つかった。

### P0-6 — Summary-AI に 3 分足 / 5 分足を繋ぐ処理が存在しなかった

```
[SUMMARY AI GATE] AI_OK side=BUY symbol=9856 ... 3m=NA|5m=NA|mtf_data=DATA_NOT_READY
[SUMMARY AI WEAK FILTER] zero 3m/5m confirmation ng kind=data_missing
    by_row_kind={'data_not_ready_attach_missing': 7}
```

`AI/entry_gate.py` と `row_adapter.py` は `score_3m` / `score_5m` を読むのに、
**その列を書く処理がリポジトリのどこにも無かった** (`score_3m` の書き手ゼロ)。
一方 3 分足 / 5 分足のデータ自体は同じログの中にある:

```
[SUMMARY HISTORY GET] tf=3 source=push rows=195
[SUMMARY HISTORY GET] tf=5 source=push rows=206
[MERGED GET]          tf=3 source=push rows=47 / tf=5 rows=46
```

つまり「無かった」のではなく「繋がっていなかった」。

**修正**: `trading/entry/summary_ai/mtf_attach.py` を新設。
銘柄コード (int / `9856.0` / str を正規化) と **確定足の時刻** をキーに
3m / 5m を付ける。未確定足と stale 足は採用しない。
本当に無ければ列を作らない (欠損を 0 として通さない = 従来どおり fail-closed)。
`3m_ready` / `5m_ready` / 採用足時刻 / データソースを候補ごとにログへ出す。

### P0-7 — Tonosama の MIN_LATEST_VOLUME が設定値を無視していた

```python
MIN_LATEST_VOLUME = _env_float_floor("TONOSAMA_MIN_LATEST_VOLUME", 50000.0, 50000.0)
#                                                                           ^^^^^ floor
```

第 3 引数が floor なので、ENV / settings で 10000 を指定しても
`max(50000, 10000) = 50000` になり **設定値が一切効かない**。
ログでは base 39 行が 1 行残らず `latest_volume_low` で落ちていた。

**修正**: 想定値 (出来高 10,000 / 売買代金 3,000,000 円) を既定かつ floor にした。
`pending_writer` と `ai_gate` に直書きされていた 50000 も config 参照へ変更。
`_latest_volume` = **最新の確定 1 分足の出来高**、
`_latest_turnover` = **その足の売買代金** と意味を分けて列名も分けた。
起動時に `[TONOSAMA CONFIG]` で実効値と意味を 1 回だけ出す。

### P0-8 — slope はあるのに _slope が無い

```
[TONOSAMA FILTER WARN] stage=primary missing _slope -> slope filter skipped
```

base summary には `slope` 列があるのに、フィルタは `_slope` を見ていた。
**傾きフィルターが一度も効いていなかった。**
`_ensure_actual_movement_cols_core()` (入力境界) で
`slope / slope_atr_scaled / score_slope / ma75_slope` から `_slope` を
**1 回だけ** 作るようにした。`_slope_source` に由来を残す。

### P0-9 — 再計算が計算済みの MACD を NaN で塗り潰していた

```
input      interval=1 col=macd non_null=50 nonzero=46
[IND] interval=1min hist_median=12.0 min_macd=26
after-main interval=1 col=macd non_null=0
```

1 分足の履歴が 12〜14 本しか渡っておらず MACD の必要本数 26 に届かない。
`indicator_calculator` は仕様どおり未成熟を NaN にするが、その NaN が
**すでに計算済みだった macd / signal / rsi / slope / atr / ma25 / ma75 を上書き** し、
続く `indicator_short_history` がそれを 0 で埋めていた。
これが `macd=0 signal=0` の正体で、`technical_ready` も 0 になっていた。

**修正**: `_preserve_prior_indicator_values()` を入れ、
「新しい値が NaN で、古い値があるとき」だけ古い値を残す。
再計算できた値は必ず優先する。復元行は `*_carried_over` で区別できる。
`IND_PRESERVE_PRIOR_ON_SHORT_HISTORY=0` で従来動作へ戻せる。

### P0-10 — range_pct に 2 つの単位が混在していた

| 場所 | 旧 |
|---|---|
| `range_defs.compute_intrabar_range_pct` | 比率 (0.0188) |
| `executor._pick_range_pct` | 比率 |
| `executor._blowoff_range_pct_value` | `raw * 100 if raw <= 0.5 else raw` (推測変換) |
| `SUMMARY_AI_BLOWOFF_DANGER_RANGE_PCT` | **2.5 = 2.5% のつもり** |

`0.0188 >= 2.5` は成立しないので、**blowoff 危険判定は一度も発火していなかった**。

**修正**: `range_pct` を **percent (1.88% = 1.88)** に統一。
`range_defs` / `summary_ai/executor` / `summary_ai/candidates` /
`handlers/entry_order_builder` の計算・閾値・ログ・列名を揃えた。
閾値の既定も percent へ (`0.005 -> 0.5`, `0.004 -> 0.4`)。
古い比率表記が設定に残っていても `_as_percent_threshold()` が
**入力境界で 1 回だけ** 換算し、そのことをログに出す。
内部が比率のままの `entry_order_builder` は
`_rc_percent_to_ratio` / `_rc_ratio_to_percent` の 2 箇所でだけ変換する。

### P0-11 — PUSH 停滞 (約 101 秒) を検知していなかった

`[MAIN PRICE DB FEED LATENCY] rows=0 rowid_delta=0` が 50 サイクル続き、
警告 1 行だけ出してひとりでに再開していた。その間 Summary-AI と Tonosama は
更新の止まった `12:52:00` の足を新しい足として扱っていた。

**修正**: `trading/push/push_stall_guard.py` を新設。
市場時間 / 購読銘柄数 / WS 接続状態を見たうえで、
**安全な再接続を 1 回だけ** 要求し、cool-down 中は連打しない。
購読 0 件のときは「再接続では直らない」と明示して再接続しない。
`data_is_stale()` で下流へ stale を渡さない判定を提供する。
再接続の実体は `push_stream.runner.request_reconnect()` = **現接続を閉じるだけ**。
新しい接続をここで作らない (二重 WS を張らないため)。
所有者でないプロセス (main.py) は要求そのものを出さない。

### P0-12 — positions.db の open 回数

2 分半で約 153 増えていた (接続漏れは無し / `outstanding=0`)。
内訳は「同じ 1 回転で何度も読んでいた」ことで、
二重駆動 (P0-1) と二重読み (P0-2) の帰結。
加えて `load_open_positions_from_db_strict()` に
**TTL 内の重複読み取りをまとめる短期キャッシュ** を入れた
(TTL は `rest_limits` の positions ポリシーと同じ値。失敗はキャッシュしない)。
接続プールは変えていない (NAS 上の SQLite で長時間接続を持ち回さない方針は維持)。

### Discord (第19ラウンド分の確認)

`utils/alerts_util.py` の webhook マスク / 通信ブレーカー / CA バンドル合成は
第19ラウンドで入っており、本ラウンドでも回帰テストが通ることを確認した。
`verify=False` は使っていない。TLS 失敗の実体は
**この PC のセキュリティソフトによる TLS 傍受** で、
差し替え後の証明書の発行元が Windows 証明書ストアにしか無いことによる
(`utils/ca_bundle.py` が certifi + Windows ROOT を合成する)。

### 追加した回帰テスト

`test_script/p0_round20/test_r20_fixes.py` (101 チェック)

1. `/positions` の同時 8 スレッドが実 GET 1 本に集約される
2. `/orders` の同時 8 スレッド + 2 経路 x 3 回が実 GET 1 本
3. 間隔 / TTL / timeout / backoff が `rest_limits` 1 箇所で決まる
4. schedule の駆動所有者が 1 つ / EXIT loop が二重起動しない
5. Ranking API 応答 -> raw / snapshot 保存 -> Summary 読取 (turnover 込み)
6. Ranking 代替経路が通常ランキングと区別されている / breaker
7. Summary-AI 候補への 3m/5m 付与 (型不一致 / 未確定足 / 欠損の扱い)
8. Tonosama の実効閾値 10,000 / 3,000,000 と起動ログ
9. `slope` 列が `_slope` としてフィルターへ渡る
10. MACD / signal が計算され、短履歴で消されない
11. `range_pct` の単位が閾値 2.5 と一致する
12. PUSH 停滞検知 / 1 回だけの再接続 / 復旧 / stale ゲート
13. positions.db の重複読み取りがまとまる
14. EXIT 1 回転で broker `/positions` を二重に読まない

実注文 / 取消 / 返済 / Discord 送信 / kabu Station への本番リクエストは
1 件も発生しない (すべてモック)。

### 未解決 / 持ち越し

1. **kabu Station 本体の無応答** はアプリ側では直せない。
   本修正は「読めない相手を叩き続けて障害を増幅しない」ところまで。
2. 1 分足の履歴が 12〜14 本しか無い (`hist_median=12` / MACD は 26 本必要)。
   warmup の供給そのものは別途。P0-9 は「消さない」だけで「増やす」ではない。
3. `test_r16_fixes.py` の commit レイテンシ 2 件は本番稼働中に走らせると FAIL する
   (今回も同様。単独実行では 17/17)。第18ラウンドの注意書きどおり。
4. 第18ラウンドからの持ち越し (Ranking Summary BG < 10 秒 など) は変わらず未達。

---

## 第21ラウンド — Smart App Control による numba ブロックで main.py が起動不能 (2026-09-02)

### 症状

2026-09-02 08:42:55 / 08:46:39 の 2 回、`main.py` が **終了コード 1** で即停止。
他の 5 プロセス (main_database / push_receiver / yahoo_complement /
summary_database / ranking_collector) は 08:39-08:40 に正常起動していた。

```
main.py:116
  from trading.handlers.exit_handler import build_5s_bar_fast, run_exit_pipeline
    -> trading/handlers/exit_handler.py:  from numba import njit
       -> numba/core/dispatcher.py -> numba._dispatcher
          -> ImportError: DLL load failed while importing _devicearray:
             アプリケーション制御ポリシーによってこのファイルがブロックされました。
```

先行して `core/startup/runtime_env/installer.py` の
`_install_normal_exit_scalp_align()` も同じ import で失敗し、
`[RUNTIME ENV DEFAULTS PATCH] normal exit constants failed` を出していた
(こちらは try/except 済みで startup は止めていない)。

### 根本原因 — Windows Smart App Control (SAC)

| 調査 | 結果 |
|---|---|
| `HKLM\SYSTEM\CurrentControlSet\Control\CI\Policy` | `VerifiedAndReputablePolicyState = 1` (**強制**) |
| CodeIntegrity/Operational | **Event 3033 / 3077**、Policy ID `{0283ac0f-fff1-49ae-ada1-8a933130cad6}` (SAC base policy) |
| ブロック対象 | `.venv\Lib\site-packages\numba\_devicearray.cp313-win_amd64.pyd` (12,288 bytes / 未署名) |
| エラーコード | `WinError 4551` |
| AppLocker | 不在 (Home edition。cmdlet 自体が無い) |
| Microsoft Defender | **Passive Mode** / numba の検出 0 件 |
| Norton | 該当なし (Defender が passive = Norton が実 AV だが検疫記録なし) |

**同じ numba の他の .pyd は全部ロードできる**:

```
LOAD FAIL _devicearray.cp313-win_amd64.pyd -> [WinError 4551]
LOAD OK   _dispatcher.cp313-win_amd64.pyd
LOAD OK   _dynfunc.cp313-win_amd64.pyd
LOAD OK   _helperlib.cp313-win_amd64.pyd
LOAD OK   mviewbuf.cp313-win_amd64.pyd
```

`_dispatcher.pyd` も未署名なのに通るので、**署名ではなくファイル単位の評価 (ISG)** で
弾かれている。C:\ へコピーしても同じ 4551 で落ちるため、
**NAS (W:) やネットワークパスは無関係**。`llvmlite.binding` は正常。

環境: Python 3.13.14 / numba 0.66.0 / llvmlite 0.48.0 / numpy 2.4.6

つまり **numba を入れ直しても再発しうる**。高速化のためだけの依存で
売買システム全体が起動しないのは設計の誤り。

### 修正 — numba を任意依存へ格下げ

プロジェクト全体 2,113 個の .py を走査した結果、
numba の import は **`trading/handlers/exit_handler.py` の 1 箇所だけ**だった。
用途も `calc_ohlc(prices)` の 1 関数のみで、中身は

```python
return prices[0], prices.max(), prices.min(), prices[-1]
```

JIT の有無で結果が変わらない式なので、identity decorator へ落として問題ない。

**`core/compat/numba_compat.py` (新設)** を唯一の窓口にした。

- import 時に絶対に例外を投げない
- `NUMBA_AVAILABLE` / `njit` / `status()` / `log_status_once()` を公開
- numba があれば `numba.njit` そのもの (挙動は完全に従来どおり)
- 無ければ identity decorator。`@njit` と `@njit(cache=True)` の両方の書き方に対応
- `AUTOSTOCK_DISABLE_NUMBA=1` で明示的に切れる (切り分け用)
- 状態は `[NUMBA COMPAT]` で **1 回だけ** ログ (traceback は出さない)

`exit_handler.py` は自前の try/except をやめて compat 層を再エクスポートするだけにし、
「numba をどう扱うか」の分岐が二重にならないようにした。
**EXIT 判定・発注・取消・返済のロジック、計算式は一切変更していない。**

### installer.py

`_install_normal_exit_scalp_align()` は元から try/except + `return False` で、
startup 全体を止める作りではなかった (変更不要)。
numba fallback により import が通るようになり、実測で

```
[NORMAL EXIT PROFIT FLOOR] constants applied
    stop=-0.0030 take=0.0080 trail_start=0.0060 trail_gap=0.0030 max_hold=300
```

が正常に適用されるようになった (`normal exit constants failed` は解消)。

### 付随修正 — /positions の token 未配布時の分類

起動直後に 1 度だけ

```
[POSITIONS READ] /positions read failed streak=1
  err=AttributeError: 'GlobalDataCompat' has no attribute 'token_value'
```

が出ていた。トークンが配られる前の一過性だが、第20ラウンドで入れた
共有 read キャッシュがこれを `unexpected` と分類して健全性フラグを落とし、
バックオフしていた。`getattr(global_data, "token_value", None)` にして
`token_missing` (健全性を落とさない一過性) に分類し直した。

### 変更ファイル

| ファイル | 内容 |
|---|---|
| `core/compat/__init__.py` | 新規。任意依存を吸収するパッケージ |
| `core/compat/numba_compat.py` | 新規。numba 判定と fallback の唯一の持ち主 |
| `trading/handlers/exit_handler.py` | 自前 fallback を廃し compat 層へ委譲 (再エクスポート) |
| `kabu_api/positions.py` | token_value を getattr にし token_missing へ分類 |
| `test_script/p0_round20/test_r20_fixes.py` | `[15]` を追加 (13 チェック) |

### テスト結果

| 項目 | 結果 |
|---|---|
| A `import trading.handlers.exit_handler` | **OK** |
| B `from ... import build_5s_bar_fast, run_exit_pipeline` | **EXIT IMPORT OK** |
| C main.py 起動 | **`[MAIN] start` 到達 / 終了コード 1 にならない** |
| token preflight | `preflight_ok endpoint=/positions status=200` |
| EXIT pipeline | `[EXIT CLOSING RECONCILE] ... rows=1` が約 6 秒周期で継続 |
| Summary / Ranking / Entry | `[RANKING SUMMARY STAGE] rows=1880` / `[IMPORT OK] ranking entry pipeline loaded` |
| round20 | **PASS=114 FAIL=0** (101 + 新規 13) |
| round19 | **PASS=66 FAIL=0** |
| positions_auth (health / entry_auth_gate) | 10/10 / 11/11 |
| test_no_undefined_names | EXIT=0 |

`[15]` は「numba の直接 import が compat 層以外に無い」ことも毎回検査するので、
将来 `from numba import ...` を素で足すと落ちる。

### 手動対応が必要な項目

SAC はコード側では解決できない。**勝手に無効化していない。** 選択肢:

1. **何もしない (推奨)** — JIT 無しで動作継続。`calc_ohlc` は 1 本 5 秒足の
   OHLC を返すだけで、numpy のまま実行しても実測差は無視できる。
2. Windows セキュリティ > アプリとブラウザー制御 > Smart App Control を
   「オフ」にする。**一度オフにすると再有効化には Windows の再インストールが必要**。
3. 署名済み配布の numba (conda-forge など) へ入れ替える。ただし
   別ファイルが同じ理由で弾かれる可能性は残る。

### 未解決 / 注意

- SAC の判定は Microsoft 側の評価で変わるため、**別の .pyd が突然弾かれうる**。
  同種の障害は「その依存を任意依存にする」で対処すること。
- 本ラウンドの検証中、`start_all.py` 経由と PyCharm 経由で **main.py が同時に
  2 インスタンス**起動している時間帯があった。PUSH WS は 1 セッションのみ、
  EXIT も所有者 1 つが前提なので、常用しないこと。

---

## 第22ラウンド — PUSH WebSocket reconnect storm の原因特定 (2026-09-02)

対象ログ: `push_receiver_runner_20260902_091338_25404.log` (09:14-10:34)

### 症状

```
10:09:10 [push_stream] reconnect after 15.0s uptime=4.3s verdict=fast_death consecutive=170
10:09:10 [push_stream][WS RECONNECT STORM] 接続が 170 回連続で 60 秒未満で切れている
```

WS uptime を 23 サイクル実測: **min=1.0s max=6.0s avg=4.1s**。

### 切り分けで否定した仮説

| 仮説 | 結果 |
|---|---|
| 多重 WebSocket / 別プロセスが購読を奪った | **否定**。`owner_pid=25404 role=push_receiver owner=True active_ws=0/1`。netstat でも 18080 への WS は push_receiver の 1 本のみ |
| main.py が register/unregister を撃っている | **否定**。main.py のログに register/unregister の HTTP は 0 件 (`[SUB MANAGER] resolved` はトークン解決のみ) |
| token 再取得で WS が無効化された | **否定**。`settings.ini` は 08:39:56 以降 未更新、token_tail=b7ad が全ログで一貫 |
| 受信スレッドの飢餓 (summary/DB 処理に食われた) | **否定**。重い処理を含むサイクルは 7/23 のみ。残り 16 サイクルは無負荷でも同じ 4 秒で切断 |
| monitor の stale watchdog / rotation が閉じている | **否定**。`WS STALE RECOVER` 0 件、`PUSH_ROTATION_CLOSE_WS_BEFORE_REGISTER=0` で close していない |
| コードが古いまま動いている | **否定**。push_stream 配下の mtime は全て 09-01 以前、プロセス起動は 09:11 |

`--- DISCONNECTED --- code=None msg=None` かつ `on_error` 0 件 =
**kabu Station 側がクローズフレーム無しで TCP を切っている**。

### 根本原因1 — 銘柄登録を行う経路が 1 つも無かった (相互ロック)

```
09:15:10 [push_stream] refresh call reason=rotation_A size=50 ...
09:15:10 [push_stream] refresh skipped reason=rotation_A ws_not_ready      <- ここ
09:15:10 [push_stream] rotation A register returned ok=False
09:15:10 [push_stream] rotation A register failed -> backoff 180.0s failure_count=1
```

`transport._call_refresh()` には

```python
if not allow_ws_closed and (not connected_event.is_set() or not _is_ws_alive()):
    return None                       # -> 呼び出し側は ok=False = 失敗扱い
```

があり、`allow_ws_closed = _is_rotation_reason(reason) and _env_bool("PUSH_ROTATION_REGISTER_WITH_WS_CLOSED", True)`。

**`core/startup/runtime_env/defaults.py` が この env を `"0"` に固定していた。**

rotation の登録は **REST の `PUT /kabusapi/register`** であって WebSocket を使わない
(transport.py REV1.6 のコメントがまさにそう書いている)。
それを WS の生死で止めていたため:

* `PUSH_STREAM_SKIP_AFTER_OPEN_REFRESH=1` により on_open でも登録しない
* rotation は「WS が生きていないと登録しない」
* WS は登録が無いから kabu Station に切られる

= **登録できない → 切られる → 切れているから登録できない**。
起動直後 (09:15:10) の 1 回目から失敗し、この run では登録が一度も成功していない。

### 根本原因2 — 登録 0 件でも backoff が 10 分まで伸びる

失敗のたび 180s → 270s → 405s → 600s。
確定登録が 0 件 = PUSH 全断なのに、次の試行まで最大 10 分待っていた。

### 副次的に判明 — storm が NAS を枯渇させていた

reconnect のたびに ranking DB / symbol_flags を NAS から読み直すため、
storm 中は SMB セッションが枯渇し `Too many open files` が頻発していた。
**トレーディングスタックを停止した 36 秒後に NAS は正常化した**
(10:56:49 停止 → 10:57:25 復旧)。NAS 障害は storm の結果であって原因ではない。

### 修正

| ファイル | 内容 |
|---|---|
| `core/startup/runtime_env/defaults.py` | `PUSH_ROTATION_REGISTER_WITH_WS_CLOSED` を `"0"` → `"1"`。REST 登録は WS に依存しないという本来の設計値へ戻す |
| `trading/push/push_stream/transport.py` | rotation の refresh が ws_not_ready で skip されたら **ERROR** で「reconnect storm になる」「env=1 にせよ」と明示。runtime フラグにも残す |
| `trading/push/push_stream/rotation_core.py` | 確定登録が 0 件のときは failure backoff を 30 秒で頭打ちにする (`PUSH_ROTATION_FAILURE_BACKOFF_EMPTY_MAX_SEC`)。1 件でも登録できていれば従来どおり |

`PUSH_STREAM_SKIP_AFTER_OPEN_REFRESH` は変更していない (設計を勝手に変えない)。

### 修正前 / 修正後 (実測)

| | 修正前 09:14-10:34 | 修正後 11:17-11:33 |
|---|---|---|
| WS uptime | min=1.0s max=6.0s **avg=4.1s** | min=3.9s max=**111.9s** avg=14.6s |
| 60 秒以上 | **0 サイクル** | **1 サイクル (111.9s)** |
| 20 秒以上 | 0 サイクル | 3 サイクル |
| consecutive | 170 まで単調増加 | healthy_disconnect で 0 にリセットされる |
| register 実行 | **skip で 0 回** | 5 回実行 (約 30-60 秒ごと) |
| backoff | 180 → 600s | 30s 固定 (登録 0 件時) |

### 残る原因 — kabu Station 側 (コードでは直せない)

登録は **実行されるようになったが timeout する**。

```
11:19:28 [PUSH REG STATE] register failed label=clear_register reason=timed out confirmed=0
```

同時刻のエンドポイント別の状況:

| エンドポイント | 状態 |
|---|---|
| `GET /ranking` | **正常** (`api_calls=28 api_success=28`) |
| `GET /positions` | timeout (`[POSITIONS READ] backoff 中 streak=7`, `broker_ok=False`) |
| `PUT /register` | timeout (5/5) |

第18ラウンドで記録した kabu Station の挙動
「門前払い (認証エラー) は 100ms 以内で返るが、**実処理を伴うリクエストだけがハングして
接続を切られる**」と同じ症状。ranking はキャッシュ応答なので通り、
建玉照会と銘柄登録だけが刺さる。

**kabu Station の再起動 (手動) が必要。**
登録が通れば、上の 2 つの修正により PUSH は自動で復旧する
(30 秒ごとに再登録を試みる)。

### 追加した回帰テスト

`test_script/p0_round22/test_r22_push_rotation_register.py` (23 チェック)

1. 出荷時の既定値で rotation の REST 登録が WS 閉でも許可される
2. on_open など WS 送信に依存する refresh は従来どおり WS 必須のまま
3. WS 閉のまま rotation refresh が実際に呼ばれる (戻り値が None にならない)
4. 旧設定に戻した場合は ERROR で理由と直し方が残る
5. 確定登録 0 件なら backoff 30 秒以下 / 1 件でもあれば従来どおり 180-600 秒

### 未着手 (PUSH 復旧が前提のもの)

指示の 2/3/5 (warmup history / technical_ready / AI PASSED=0) は、
いずれも **PUSH tick が来ていないことの下流**。
実測 `total_received=8869` (16 分 / 100 銘柄) は正常時の数十分の一で、
1 分足が積み上がらないため

    symbol_hist_len が 1-3 -> SHORT_PATCH=208/228 (0.91)
    -> ma25/ma75/macd/signal non_null=0 -> technical_ready=0 -> AI PASSED=0

という連鎖になる。PUSH を直さずに history 供給側だけを直しても
評価できないため、本ラウンドでは触っていない。

指示の 4 (ranking_score) は、実ログ上
`[SUMMARY ENRICH][RANKING] context=scheduler-push-after_calc_before_db rows=27 hit=13
ranking_score_nonzero 0->13` が出ており、
**display 直前ではなく DB 保存前の enrich 段階で注入されている**ことまでは確認した。
scoring / AI との前後関係の完全なトレースは未了。

指示の 6 (価格フィルタ) は調査のみ実施:
`settings.ini` の `[entry_filter_1min/3min/5min]` はいずれも
`min_price=200 / max_price=7000`。**2500 は settings.ini にもコードにも存在しない**。
したがってログの `200 < close <= 7000` は現行設定どおりで、勝手には変更していない。
ハードコード既定値は `MIN_PRICE=200.0` が 7 箇所、`MAX_PRICE=7000.0` が 2 箇所
(display_runner / display_sorting / summary_ai_entry_hook_v20 / summary_ai.candidates /
trade_universe_filter x2 / yahoo.ranking_follow)。
TONOSAMA だけ `MIN_PRICE=300` (floor 300) で分裂している。

## 第23ラウンド — register の timeout は kabu Station ではなく urllib だった (2026-09-04)

### 症状

第22ラウンドで「kabu Station の再起動が必要」と結論した状態が、再起動しても直らなかった。

* 09:35-10:43 の run: `PUT /register` が **27 回すべて timed out、成功 0 回**。
  受信できていた 171,174 tick は 09-02 に保存された `last_good=50` の残存登録による分で、
  rotation の残り 50 銘柄は最後まで入っていない。`health_status=RECONNECT_STORM`
* kabu Station を 11:13 に手動再起動 → 11:37 の run でも `err=timed out` のまま
  (`REGISTER AUDIT refresh done ok=False elapsed=20.489s confirmed=0`)

### 切り分け

同じ時刻に、エンドポイントではなく **HTTP クライアントで結果が割れていた**。

| 呼び出し | クライアント | 結果 |
|---|---|---|
| `GET /positions` (token preflight) | requests | **200 / 0.77s** |
| `GET /wallet/cash` | requests | **200 / 0.25s** |
| `GET /ranking` | requests | **200** (raw_rows=50) |
| `PUT /register` | **urllib** | timed out (10s) |
| `GET /positions` (kabu_position_reader) | **urllib** | timed out |

「kabu Station は実処理を伴うリクエストだけハングする」という第18/20/22ラウンドの
見立ては誤りで、**urllib で叩いていた経路だけが落ちていた**。

### 根本原因 — urllib.request が送る `Connection: close`

2026-08-29 の commit dddb0a7 で実測済みの現象がそのまま残っていた。

```
最小ヘッダー (X-API-KEY のみ)  -> HTTP 401  0.01s
+ Content-Type                -> HTTP 401  0.00s
+ Accept-Encoding: identity   -> HTTP 401  0.00s
+ Connection: close           -> 応答なし  5.00s   ★これだけ
```

`urllib.request` は毎回 `Connection: close` を送る。kabu Station はこのヘッダーが
付いたリクエストに応答を返さず、接続を掴んだままタイムアウトさせる。
dddb0a7 では token 系 (`token_bootstrap.py` / `token_manager.py`) だけを requests へ
移したため、**残り 5 箇所の urllib 経路が取り残されていた**。

### 修正

| ファイル | 内容 |
|---|---|
| `kabu_api/kabu_http.py` (新規) | kabu Station 宛 HTTP の共通入口。requests で送り、非 2xx は urllib 時代と同じ `urllib.error.HTTPError` (`.code` / `.read()` 付き) にして送出する。理由をここ 1 箇所に集約 |
| `trading/push/subscription_manager/register_ops.py` | `PUT /register` `/unregister/all` を kabu_http へ。`_build_request` (urllib 版) は削除 |
| `trading/position/kabu_position_reader.py` | `GET /positions` を kabu_http へ。401/429 の扱いは従来どおり |
| `trading/exit/executor.py` | EXIT の板読み・引け際の建玉照合 (2 箇所) |
| `trading/handlers/entry_order_builder.py` | ENTRY の板読み。`urllib.parse` は request 経由で入っていたので明示 import に変更 |
| `kabu_api/board.py` | 板取得 (4 市場総当たり)。無応答を「その市場には無い」と区別できず空振りしていた |
| `kabu_api/board_fetcher.py` | 1 秒周期の板キャッシュ更新ループ |
| `kabu_api/info.py` | `fetch_orders()` = `GET /orders`。同じファイルの他の関数は requests なのにここだけ urllib だった |
| `ats/ats_api.py` | ATS の PUT。ステータスコードを返す契約なので `kabu_http.request_text()` を追加して対応 |

URL・ヘッダー・timeout・戻り値・例外の型は変えていない。

最初の調査は `trading/` 配下しか見ておらず 5 箇所と報告したが、実際は 9 箇所だった。
`core/startup/startup.py` の `[BOARD REST API MONITOR]` は urlopen を wrap して
回数を数えるだけの監視層なので修正対象ではない (呼び出しが requests へ移った結果、
今後この監視は board/positions を 0 と表示する)。

root 直下の以下は kabu Station 宛の urllib が残っているが、本体から import されておらず
今日のログにも現れない単体実行用の旧ツールのため、本ラウンドでは触っていない:
`register_cycle.py` (2 箇所) / `kabus_position_sync.py` / `Position.py` /
`yoryoku_genbutsu.py` / `yoryoku_shinyou.py`

### 修正前 / 修正後 (実測)

| | 修正前 11:37 | 修正後 12:06 |
|---|---|---|
| register の応答 | `err=timed out` (10s) | `Code 4002006` を **1.0s** で返す |
| unregister/all | timed out | `ok=True {'RegistList': []}` |
| refresh 全体 | `ok=False elapsed=20.489s` | **`ok=True elapsed=5.046s`** |
| confirmed_registered | 0 | **50** |
| rotation A/B | 一度も成功せず | 12:13 A `ok=True` / 12:14 B `ok=True` (約 60 秒ごとに交代) |
| health_status | RECONNECT_STORM | **HEALTHY** (last_recv_age=0.4s) |
| `/positions` の実測 | `[BOARD REST API MONITOR] positions:total=2 ok=0 ng=2 avg_ms=3012.3` (毎回ちょうど timeout 値 3.0s で全滅) | `[POSITIONS READ STATS] fetches=741 fetch_errors=0` |
| PUSH 受信 (後場) | 10:40:53 で停止 | 13:27 時点 received_total=187,387 継続中 |

register が通ったのは 8/19 以降で初めて。第22ラウンドで入れた
「WS 閉でも REST 登録を許可する」「登録 0 件なら backoff 30 秒」の 2 つは、
この修正で本来の意図どおりに機能するようになった。

### 追加した回帰テスト

`test_script/p0_round23/test_r23_kabu_http_no_connection_close.py` (17 件)

1. requests へ渡すヘッダーに `Connection` を混ぜない
2. 非 2xx が `urllib.error.HTTPError` で上がり `.code` / `.read()` が使える
3. 空応答を例外にしない
4. register_ops / kabu_position_reader が `urlopen` を呼んでいない
5. 8 モジュールが `urllib.request` を import していない (再発すると元の書き方に戻るため)
6. `/positions` の 401 は 1 回だけ token を取り直す / 429 では取り直さない
7. `request_text` が本文を文字列のまま返す (ATS がそれを前提にしている)

`p0_round22` (5 件) も通過を確認済み。

### 残る課題

* **発注は今日もゼロ**。10:11 に 338A が `score=3.9642 < min_score=4.0` で 1 件 block されただけ。
  PUSH が通るようになった状態での ENTRY 評価はまだ観測できていない
* 第22ラウンドの「未着手」項目 (warmup history / technical_ready / AI PASSED=0) は
  PUSH 復旧が前提だったので、次の場中に再評価する

## 第24ラウンド — PUSH DB をローカルに作り、NAS へは複製する (2026-09-05)

### 症状

第23ラウンドで PUSH が実際に流れるようになった結果、DB 書き込みが追いつかなく
なった。**後場が始まった 12:30 ちょうどから**キューが一方的に伸び、15:23 に
上限 200,000 で溢れて 2,004 件を捨てた。

```
12:28  queue=0        受信   8,351 / 書込   8,312   ← 昼休み、余裕
12:30  queue=1,980    受信  12,729 / 書込   9,749   ← 後場開始
15:23  queue=200,000  受信 503,200 / 書込 301,883  dropped=323
15:30  queue=200,000  受信 505,887 / 書込 302,883  dropped=2,004
```

書き込み速度は時間とともに落ちていた:

| 区間 | 速度 |
|---|---|
| 12:33 - 14:58 | 約 1,966 行/分 |
| 14:58 - 15:30 | 約 125 行/分 (ほぼ停止) |

### 切り分け — NAS の I/O が遅いわけではなかった

実物の `push20260904.db` (520MB / 492,385 行) を複製して測った。

| 条件 | 速度 |
|---|---|
| NAS 単独プロセス | 2,751,040 行/分 |
| NAS + 読み手 2 プロセス | 743,021 行/分 |
| ローカル 単独 | 5,432,290 行/分 |
| ローカル + 読み手 2 プロセス | 1,007,964 行/分 |
| **本番の実測** | **約 1,100 行/分** |

**単純な I/O でも、複数プロセスの競合でも本番は再現しなかった** (675 倍の開き)。
ロック待ちでもない — `database is locked` / busy リトライは **0 件**。

分かったのは次の 2 点:

* 遅い flush (10 秒超) は **14:40 以降に集中**する。それ以前はほぼ 0 秒台
* 同じ時刻から、writer とは**別接続の checkpoint スレッドも 40 秒**かかり始める

書き手と、それとは独立した checkpoint が同時に遅くなる以上、原因は SQLite でも
テーブル設計でもなく **SMB 経路が持続負荷で劣化している**側にある
(2026-09-02 の「storm が NAS を枯渇させていた」と同じ筋)。

なお `stream_data_raw` はインデックスを 5 本持つ (うち 1 本は 14 列の
カバリングインデックス `idx_stream_data_raw_summary_cover`)。`stream_data` の
2 本に対して executemany が 0.709s / 26.445s と 37 倍違うのはこれで説明がつくが、
ローカルではこのコストは無視できるので、インデックスは触っていない。

### 修正 — ホットパスを SMB から外す

| ファイル | 内容 |
|---|---|
| `trading/push/push_db_location.py` (新規) | push DB の置き場所を決める唯一の場所。既定でローカル、NAS へは複製。理由をここに集約 |
| `trading/push/push_db_writer.py` | `_resolve_db_path` を委譲。ローテーション時と stop 時に NAS へ複製。場中の定期複製スレッドを追加 |
| `scheduler_jobs/summary/memory_latest_1m.py` | 読み手も同じ解決関数を通す |
| `scheduler_jobs/summary/fallback_loader.py` | 同上 |
| `scheduler_jobs/summary/freshness_timeline.py` | 同上 |

**書き手だけを移すと読み手が前日の NAS 複製を読んでしまう**ので、5 箇所すべてが
同じ関数を通るようにした。回帰テストでこれを固定している。

複製は `sqlite3` のオンラインバックアップ API を使う。稼働中の DB を
`shutil.copy` すると壊れるが、`backup()` は一貫性を保つ。実測 10.3MB / 0.3s、
`integrity_check=ok`、未 commit の行は含まれないことを確認済み。
520MB なら 15 秒程度の見込み。

複製の契機: 日付ローテーション時 / stop 時 / 既定 30 分ごと。

### NAS のハング対策 (2026-09-05 追記)

「最近 NAS がハングすることがある」との報告を受けて、複製が取り込みを
道連れにしないよう作り直した。当初の実装には次の穴があった:

* `stop()` の複製が同期呼び出し -> NAS がハングすると **停止できない**
* ローテーション時の複製が **writer のロックを握ったまま** 同期呼び出し
  -> 日付が変わる瞬間に NAS がハングすると **取り込みが完全に止まる**

SMB のハングは `sqlite3` の timeout では抜けられない (OS の I/O で
ブロックする) ので、時間で見切る作りにした:

| 経路 | ふるまい |
|---|---|
| ローテーション | 待たずに投げっぱなし (`wait=False`)。前日のファイルはもう変化しない |
| stop | 待つが上限 120 秒 (`PUSH_DB_NAS_SYNC_STOP_TIMEOUT_SEC`) |
| 定期 | 上限 180 秒 (`PUSH_DB_NAS_SYNC_TIMEOUT_SEC`) |

* 実処理は daemon スレッドへ出し、上限を過ぎたら放置して呼び出し側は返る
* NAS のパス解決 (`mkdir` を伴う) もワーカーの中に置いた。ここも固まるため
* 複製は同時に 1 本だけ。詰まっている間に要求を重ねない
  (2026-09-02 の storm で SMB セッションが枯渇した実績があるため)
* ただし stop だけは、定期複製と重なったときに最後のぶんを取りこぼさないよう
  実行中の複製の完了を上限つきで待つ

ローカルの DB は複製が失敗しても無傷なので、NAS が戻ってから複製し直せる。

環境変数 (すべて既定値で動く):

    PUSH_DB_LOCAL_STAGING          0 で従来どおり NAS 直書きへ戻す
    PUSH_DB_LOCAL_DIR              既定 %LOCALAPPDATA%\autostock\push
    PUSH_DB_NAS_SYNC_INTERVAL_MIN  既定 30。0 で定期複製を止める

### 追加した回帰テスト

`test_script/p0_round24/test_r24_push_db_local_staging.py` (17 件)

1. 既定がローカルであること / `PUSH_DB_LOCAL_STAGING=0` で NAS へ戻せること
2. `PUSH_DB_PATH` の明示指定が従来どおり最優先
3. **writer と読み手 3 箇所が同じパスを返すこと**
4. 複製が全行を運び `integrity_check=ok` になること
5. **元 DB を開いたまま複製できること** (ファイルコピーに戻すと壊れる)
6. 複製が失敗しても例外を投げないこと (取り込みを止めない)
7. **NAS がハングしても呼び出し側が返ること** / 詰まっている間は複製を重ねないこと
8. ローテーションが NAS を待たないこと / stop の複製に上限があること

### 未検証 — ここは月曜の実データで確かめる

**合成ベンチでは本番の詰まりを再現できなかったので、この修正が効くことは
まだ証明できていない。** 理屈の上ではホットパスが SMB から外れるので
14:40 以降の劣化は起きないはずだが、確認は月曜の場中に行う。

見るべきログ:

* `[PUSH OWNER HEALTH] ... queue=` が場中ずっと 0 付近か (前回は単調増加)
* `dropped_total=0` のままか
* `executemany raw done ... elapsed=` が 14:40 以降も 0 秒台か
* `[PUSH DB NAS SYNC] 完了` が 30 分ごとに出ているか、所要は何秒か
* 大引け後に停止したとき NAS 側の `push20260905.db` が揃うか

ローカルの置き場所は C: (KINGSTON SNV3S2000G)。このドライブは不良ブロックの
実績があるので、**NAS への複製が届いているかは毎日確認する**。届いていれば
NAS 側が正となる。

### 残る課題 (第23ラウンドから持ち越し)

* SUMMARY の SLA 超過 142 件 (`cancel SLA breach` 71+71 / `tick timeout` 37)
* エントリーが 1 件も閾値を超えない (16 件すべて score_low。0.15-2.07 vs 閾値 3.0)。
  PUSH のデータ欠けが直ってから判断する

## 第25ラウンド — ログをローカルへ逃がし、NAS 処理に締め切りを付ける (2026-09-05)

### きっかけ

「最近 NAS がハングすることがある」との報告。第24ラウンドで push DB は
ローカルへ移したが、守れたのはそこだけだった。

**ログを NAS へ直接書いていると、NAS がハングした瞬間に logging が
ブロックして全プロセスが止まる。しかも同時に調査手段そのものを失う。**

`console_tee` は既に非同期ライタを持っていて 1 行ごとの SMB 往復は避けて
いたが、NAS がハングすればライタスレッドが固まり、キューが溢れて
`[CONSOLE TEE] queue overflow -> N 行を破棄した` になる。

### 修正

| ファイル | 内容 |
|---|---|
| `utils/nas_guard.py` (新規) | NAS に触る処理を別スレッドへ出し、締め切りで見切る共通部品。同時実行を 1 本に絞る slot 付き |
| `trading/push/push_db_location.py` | 第24ラウンドで書いた見切り処理を nas_guard へ寄せた (重複した並行制御を 1 箇所に) |
| `data_collectors/config.py` | `LOG_DIR` の既定を `X:\logs\console` から `%LOCALAPPDATA%\autostock\logs\console` へ |
| `data_collectors/log_mirror.py` (新規) | ローカルのログを NAS へ **差分だけ追記**ミラーする。既定 5 分ごと、上限 60 秒 |
| `data_collectors/logging_setup.py` | 書いたログをミラーに登録 |
| `main_database.py` | 同上 |
| `core/logging/console_tee.py` | 既定の出力先をローカルへ。**既定値を import 時ではなく呼び出し時に決める** (import しただけで NAS に触らないため) |

ログは append-only なので、前回どこまで送ったかを覚えておけば差分だけ送れる。
1 日 60MB を毎回まるごと送らずに済む。実測で 18 バイト -> 9 バイトと
差分だけが飛び、最終的な内容が一致することを確認した。

NAS 上のログは今までどおり `X:\logs\console` で見られる (数分遅れる)。

環境変数:

    KABU_CONSOLE_LOG_DIR          従来どおり NAS へ直接書きたいとき
    KABU_LOG_MIRROR_TO_NAS        0 でミラーしない
    KABU_LOG_MIRROR_INTERVAL_SEC  既定 300
    KABU_LOG_MIRROR_TIMEOUT_SEC   既定 60
    KABU_LOG_NAS_DIR              ミラー先 (既定 X:\logs\console)

### 追加した回帰テスト

`test_script/p0_round25/test_r25_log_local_and_nas_guard.py` (13 件)

1. nas_guard が終わらない処理を見切って呼び出し側に返ること
2. 詰まっている間は重ねないこと / 成功後は slot が解放されること
3. `LOG_DIR` の既定がローカルで、NAS でも UNC でもないこと
4. `console_tee` の既定が **import 時に評価されない**こと
5. ミラーが差分だけ送ること / 縮んだら送り直すこと
6. ミラーが固まっても呼び出し側が返ること

p0_round23 / 24 と合わせて **47 件通過**。

### やらなかったこと — ranking DB と summary DB

同じ扱いに広げようとしたが、**パス解決の散らばり方が push とは桁違い**だった。

| DB | パス解決サイト数 |
|---|---|
| push | **4** (writer + 読み手 3) |
| ranking | **30** |
| summary | **36** |

push は 4 箇所を 1 つの解決関数に集約でき、「writer と読み手が同じ場所を返す」
ことをテストで固定できた。ranking は 30 箇所がそれぞれ独自にパスを組み立てて
おり (ハードコードされた候補リストを持つものもある)、**1 箇所でも漏らすと
その読み手だけが古い NAS 側を読む**。ranking DB は銘柄選定を駆動しているので、
古いデータを黙って読む状態は「読めない」より悪い。

場中に検証できない状況で 30 + 36 箇所を一度に移すのは危険と判断して、
本ラウンドでは触っていない。やるなら ranking だけで 1 ラウンド、
場中の実データで確認しながら進めるべき。

なお ranking DB の WAL は 2026-09-04 実測で 201MB / 49,511 frames あり、
NAS への書き込み量としては無視できない。優先度は高い。

### 現状の NAS 依存

| 対象 | 状態 |
|---|---|
| push DB | ローカル + 複製 (第24ラウンド) |
| コンソールログ | ローカル + 差分ミラー (本ラウンド) |
| ranking DB | **NAS 直書きのまま** |
| summary DB | **NAS 直書きのまま** |
| heartbeat json | ローカル (LOG_DIR に従うため本ラウンドで移動) |

NAS がハングした場合、ranking / summary の書き込みは止まる。ただしログは
残るので、**次は何が起きたか追える**ようになった。

## 第26ラウンド — エントリーが 1 件も出ない原因と、候補地点リプレイ (2026-09-07)

### ステップ1 — 原因は閾値ではなく最終段の個別フィルタだった

当初は `[ENTRY GATE] block score_low` (472 件、うち 464 件が `side=None`) が
原因に見えたが、**これは別経路**だった。実際のエントリー経路を追うとこうなる:

```
候補 37 → ai_ok 22 → approved 12 → AI_GATE_OK 12
   ↓ entry_controller の個別フィルタ
   RANGE_5M_FILTER_NG      16
   MA5 THIRD BAR GUARD      7  (before=1 -> after=0 で全滅)
   ATR_1M_FILTER_NG         4
   AI_GATE_NG               3
   SELL_CREDIT_GUARD_NG     1
   ↓
   発注 0   (skip_reason=entry_pipeline_no_order_all_batches)
```

`approved: 3, registered: 3, skip_reason: 'entry_controller_no_order'` まで
到達しており、候補の中身 (side / 価格 / スコア) はすべて正しく埋まっていた。
**スコア閾値を下げても解決しない。**

### ステップ2 — 候補地点リプレイを作った

既存の `trading/backtest_replay/` は `trade_audit` / `executions_runtime`
(= 実発注の記録) を読む作りで、**発注 0 件の日には使えない**。そこで
別の入力で組んだ:

| 用途 | 入力 |
|---|---|
| 候補地点 | コンソールログの `AI_GATE_OK` / `ENTRY_SKIP` / `MA5 GUARD` |
| その後の値動き | `push{YYYYMMDD}.db` の `stream_data` (tick) |
| 手仕舞い条件 | `settings.ini` の `[exit]` |

`test_script/backtest/replay_from_gate_logs.py`。実注文は出さず、DB は
read-only で開くので稼働中でも安全。第24ラウンドで push DB をローカルへ
移したおかげで、tick を高速に読める (66 万行を数秒)。

### 2026-09-07 の結果 (候補 34 件を全部建てた場合)

合計 **-600 円** / 勝率 32.4% / 平均 -0.078%

**ブロック要因ごとに分けると、フィルタの良し悪しがはっきり分かれた:**

| ブロック要因 | 件数 | そのフィルタを外していたら | 勝率 | 評価 |
|---|---|---|---|---|
| RANGE_5M_FILTER_NG | 16 | **-6,400 円** | 18.8% | 正しく損を止めている |
| MA5 THIRD BAR GUARD | 7 | **+4,800 円** | 57.1% | **利益を捨てている** |
| ATR_1M_FILTER_NG | 4 | -2,200 円 | 25.0% | 正しく損を止めている |
| **(全フィルタ通過)** | **4** | **+3,900 円** | **50.0%** | **発注されるべきだった** |
| AI_GATE_NG | 2 | -500 円 | 50.0% | ほぼ中立 |
| SELL_CREDIT_GUARD_NG | 1 | -200 円 | 0.0% | 制度上外せない |

### ステップ3 — 手仕舞い条件のスイープ

利確 4 通り x 損切 3 通り x 保有 4 通り = 48 組を比較した。

最良は 利確 0.5% / 損切 -0.3% / 保有 30 分 で **+600 円** (勝率 29.4%)。
現行 (利確 0.5% / 損切 -0.5% / 保有 5 分) は -600 円。

**差は 1,200 円しかなく、34 サンプル 1 日では有意とは言えない。**
手仕舞い条件をいじっても大勢は変わらない、というのが読み取れることの上限。

### 分かったこと / 次にやるべきこと

1. **全フィルタを通過した 4 件が発注されていない** (+3,900 円 / 勝率 50%)。
   `entry_controller_no_order` は「retry でもロック待ちでもない」ときの
   fallthrough で、理由が記録されていない。**ここが本命のバグ**。
   まず理由を出すログを足すべき
2. **MA5 THIRD BAR GUARD が勝ち筋を落としている** (7 件で +4,800 円、勝率 57%)。
   緩和候補。ただし 7 件なので複数日で確認してから
3. RANGE_5M と ATR_1M は損を止めており、触らないほうがよい
4. 閾値・手仕舞い条件の調整は効果が小さい。優先度は低い

### 注意

1 日 34 サンプルの結果であり、**これだけで設定を変えるべきではない**。
リプレイツールは日付を指定して過去日にも使える (`--date 20260904`)。
NAS 上に 09-01 以降の push DB があるので、複数日で確認してから判断する。
