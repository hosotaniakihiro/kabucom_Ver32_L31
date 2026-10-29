# PUSH 受信の修正（2026-10-29、ブランチ fix/push-reconnect-20261029）

本番への反映は、ユーザーの承認を得てから行う。この文書は、変更の中身・反映の手順・反映後に見る指標・戻し方をまとめたもの。

## 1. 背景（10/29 の調査）

- 場中の PUSH の WebSocket は、kabu Station 側からの RST（WinError 10054）で数秒〜数十秒ごとに切られる（10/22 から増えた。理由は未特定）。
- こちらの再接続の待ちが 1→2→4→8→15 秒と伸び、60 秒もつ接続が出るまで戻らなかった。昼休み・寄り前の monitor の自己切断も同じ回数に入るため、場が開いた時点で既に 15 秒待ち。場中に受信 0 の秒は 10/27 が 56%、10/28 が 45%（10/14 は 1.6%）。
- 差分登録は、解除の後に「解除後も枠が足りない」と `/unregister/all`（全解除）へ戻っていた（10/29 前場 171 回）。全解除は登録 0 件の瞬間を作り、固定枠の銘柄の受信まで途切れさせる。
- `_Expected10054Filter` がルートのハンドラで 10054 を含む行を全部捨てていたので、自分の `ws reset by peer` の警告も残らず、切断に理由が無いように見えていた。

## 2. 変更点

### 2-1. 再接続の待ち（`trading/push/push_stream/runner.py`、`monitor.py`、`state.py`、`core/startup/runtime_env/defaults.py`）

| 場面 | 変更前 | 変更後 |
|---|---|---|
| 場中（平日 9:00〜11:30・12:30〜15:30） | 1 本の回数で 0.3(初回), 1, 2, 4, 8, 15 秒 | 0.3(初回), 0.3, 0.5, 1, 2 秒・**上限 2 秒** |
| 場中で、その接続で 1 件でも受信していた | 60 秒もたなければ回数 +1 | **回数を 0 に戻す**（次は 0.3 秒） |
| monitor の自己切断（受信が無いことによる stale の強制切断） | 回数 +1 | 場中は**数えない**（`verdict=self_close`） |
| 場外（寄り前・昼休み・引け後・休日） | 場中と同じ回数 | **別の回数**。待ちは従来の 1,2,4,8,15 秒のまま。ただし次に場が開く時刻（9:00 / 12:30）をまたいで待たない |
| 場が開いた最初の切断 | 前の回数のまま（多くは 15 秒） | 場中の回数を 0 に戻してから数える |
| WS RECONNECT STORM の ERROR | 自己切断でも出る | 自己切断のときは出さない |

- 新しい env（defaults.py に既定値を追加。既存の env は場外用として値を変えていない）
  - `PUSH_STREAM_RECONNECT_SESSION_AWARE=1`（0 で変更前の待ち方に完全に戻る）
  - `PUSH_STREAM_RECONNECT_INTRADAY_LADDER_SEC=0.3,0.5,1,2`
  - `PUSH_STREAM_RECONNECT_INTRADAY_MAX_SEC=2.0`
  - `PUSH_STREAM_RECONNECT_RESET_ON_RECEIVE=1`（0 で「受信していたら回数を戻す」をやめる）
- 変えていない値: `PUSH_STREAM_RECONNECT_BACKOFF_BASE_SEC=0.3`、`..._MAX_SEC=15.0`、`..._LADDER_SEC=1,2,4,8,15`、`PUSH_STREAM_WS_MIN_HEALTHY_SEC`（既定 60）。8/28（0.5 秒間隔で 84 分に 833 回）・9/2 の撃ち続けへの対策は、場外ではそのまま残る。場中も 0 秒にはせず、受信 0 の即死が続けば 2 秒間隔になる。

### 2-2. 全解除に戻らない差分登録（`trading/push/subscription_manager/register_ops.py`）

- **はみ出しの原因（10/29 前場のログで確認）**: 「解除後も枠が足りない」154 件を調べると、すべて「こちらの解除は効いていて、解除の応答の一覧に、直前の一覧に無かった銘柄が増えていた」形（増えた銘柄は延べ 198、こちらが解除を頼んだ銘柄は 0）。この プロセス以外で登録できる経路は無い（main.py の ATS 登録は PUSH の所有者でないので止まる）ので、kabu の `/board`・`/symbol` の自動登録と見られる（直接は未確認）。
- **48 と 50**: kabu の本当の上限は 50。10/22（コミット 9c44ddb）に「一覧が 48 件なのに 2 件の登録が 4002006」「全解除の直後でも 50 件の登録が 4002006」が出たので 2 枠空けて 48 にした（`PUSH_REGISTER_HEADROOM=2`）。上の自動登録の分の余裕と読めるので、**48 のまま**にした。
- 変更:
  1. 解除の後に枠が足りないときは、目標に無い銘柄（はみ出し）だけを `/unregister` で銘柄ごとに解除してから追加する（全解除しない）。
  2. 登録の応答の一覧が目標とずれたときも、はみ出しを銘柄ごとに解除する。足りない銘柄は記録だけして、次の周期に入れる（次の周期の current は kabu の一覧なので add に入る）。
  3. 残す銘柄が 0 件の入れ替え（例: 48 → 別の 48）は、解除する銘柄を 1 つ残して先に入れ替え、最後にその 1 つを入れ替える。登録が 0 件になる瞬間を作らない。上限 48 も超えない。
- 全解除が残る場面（手動や異常時の最後の手段）: 銘柄ごとの解除・登録そのものが失敗したとき、全解除の後の登録が 4002006 のとき（既存の件数を減らして入れ直す処理）、`PUSH_REGISTER_DIFF_TRIM_EXTRA=0` のとき、ATS（main.py、通常は所有者でないので動かない）。
- 新しい env: `PUSH_REGISTER_DIFF_TRIM_EXTRA=1`（0 で変更前の「全解除→全登録へ落とす」に戻る）。

### 2-3. ログ（`trading/push/push_stream/ws_callbacks.py`、`runner.py`）

- `_Expected10054Filter` は、`trading.push.push_stream` 配下の logger の行を捨てない（自分の行は 1 行の警告で traceback を付けていない）。websocket ライブラリの「goodbye」や、ほかの logger の 10054 の行は従来どおり捨てる。`PUSH_WS_10054_KEEP_LOGGER_PREFIXES=`（空）で従来どおり全部捨てる。
- 切断の理由を `state._disconnect_info` に残し（on_error の 10054 / ping timeout / その他、on_close の close code、monitor の自己切断の理由）、run loop の既存の 1 行に足した。例:

```
[push_stream] reconnect after 0.3s uptime=5.2s verdict=received_then_reset consecutive=0 total=812 base=0.3s max=15.0s min_healthy=60s vendor_ws=True cause=reset_by_peer(10054) close_code=None close_msg=None error=[WinError 10054] ... recv_in_conn=184 session=intraday
```

- 古い socket の遅れた on_error は理由を上書きしない。

## 3. 期待する効果（見込み。反映後の実測で確かめる）

- RST 1 回あたりの受信の空白が、約 16〜20 秒から約 1 秒（0.3 秒待ち + 接続）になる。今の頻度（10 分に 20〜25 回）なら、場中に受信 0 の秒は 45〜56% から数 % に下がる見込み。
- 前場の全解除が 171 回 → ほぼ 0 回（はみ出しの解除の失敗や 4002006 のときだけ）。
- 切断の理由がログで数えられるようになる。
- RST そのものの原因は直していない（4 を参照）。

## 4. テスト

- 追加: `test_script/test_push_reconnect_session_backoff.py`（22 件。待ち・場の切替・自己切断・ロールバック用の env・理由の記録・10054 フィルタ・偽の WebSocketApp で run loop を通しで）、`test_script/test_register_no_full_clear.py`（10 件。RegistList を持つ偽の kabu で、/board の自動登録の形・0 件にならない・48 を超えない・200 回のローテーション・ロールバック）。変更前のコードでは 32 件のうち 26 件が落ちる（通る 6 件は「env で従来どおり」「48 のまま」などを確かめる側）。
- 直した既存のテスト: `test_script/test_register_fastswap.py::test_vendor_list_overrides_stale_program_state`（残す銘柄 0 件の入れ替えが 2 段になったので、解除の合計で確かめる形にした）。
- push 関係の既存のテスト（p0_round15/18/20/22/23/27、test_no_undefined_names、test_push_*、test_register_*、test_rotation_and_ranking_flush）は、変更前と同じ結果（p0_round20 test_r20_cycle_break の既知の 1 件の失敗だけ）。
- 全体テスト（run_all_tests.py --isolate）の結果はコミットのメッセージに書く。

## 5. 反映の手順（引け後か寄り前。本番を止めている間に行う）

1. 本番を止める: `ops/local_run/stop_all.ps1`（`taskkill /IM python.exe` は使わない）。
2. 本番のリポジトリ（OneDrive 配下の `ソースコード`）で、作業ツリーに未コミットの変更が無いことを確かめ、このブランチのコミットを取り込む（例: `git cherry-pick <このブランチのコミット>`）。本番の HEAD は b7ec820 のはず（このブランチの基点）。
3. 構文の確認: `C:\autostock\venv\Scripts\python.exe -m py_compile trading\push\push_stream\runner.py trading\push\push_stream\ws_callbacks.py trading\push\push_stream\monitor.py trading\push\push_stream\state.py trading\push\subscription_manager\register_ops.py core\startup\runtime_env\defaults.py`
4. `ops/local_run/restart_local.ps1` で起動する。
5. 起動後 5 分以内に、push_receiver のログ（`%LOCALAPPDATA%\autostock\logs\console\data_collectors_runner_*.log`）で次を確かめる。
   - `reconnect after` の行に `cause=` と `session=` が出ている（場外なら `session=off`）。
   - `[DIFF REGISTER]` が出ていて、`Traceback` が増えていない。
   - `[PUSH MONITOR]` の `registered_count` が 0 になっていない。

## 6. 反映後に見る指標（翌日の場中、10/27・10/28 と比べる）

| 指標 | 出し方 | 目安 |
|---|---|---|
| 場中に受信 0 の秒の割合 | push DB の `stream_data_raw` の `received_at` を秒に丸め、9:00〜11:30・12:30〜15:30 の 19,500 秒のうち行が無い秒を数える（10/29 の調査の `gaps.py` と同じ） | 10/27 56%・10/28 45% → 数 % |
| `--- DISCONNECTED ---` の数 | ログで場中の行を数える | 減るとは限らない（RST は直していない）。急に増えたら 7 を見る |
| 再接続までの秒 | `reconnect after X.Xs` の X と、次の `--- CONNECTED ---` までの時間 | 場中はほぼ 0.3〜2 秒 |
| 切断の理由の内訳 | `cause=` を数える（`reset_by_peer(10054)` / `self_close:*` / `close` など） | 場中は reset_by_peer が大半のはず |
| 全解除の回数 | `unregister_all done` の行を数える | 前場 171 回 → ほぼ 0 |
| はみ出しの解除 | `[DIFF REGISTER][TRIM]` を数える | 全解除の代わりにこれが出る |
| `WS RECONNECT STORM` | ERROR の行 | 場中に連発しないこと |
| kabu Station の負荷 | `KabuS_*.log` の `WebSocket接続要求受け入れ` の回数 | 1 分に数回〜10 回程度。それより大きく増えたら 7 |

## 7. ロールバック

- 待ちだけ戻す: 起動の環境に `PUSH_STREAM_RECONNECT_SESSION_AWARE=0` を入れて再起動する（場中も 1,2,4,8,15 秒・1 本の回数に戻る）。上限だけ戻すなら `PUSH_STREAM_RECONNECT_INTRADAY_MAX_SEC=15` と `PUSH_STREAM_RECONNECT_INTRADAY_LADDER_SEC=1,2,4,8,15`。
- 差分登録だけ戻す: `PUSH_REGISTER_DIFF_TRIM_EXTRA=0`。
- ログのフィルタだけ戻す: `PUSH_WS_10054_KEEP_LOGGER_PREFIXES=`（空）。
- env の入れ方: defaults.py は「既に env にある値を上書きしない」ので、`ops/local_run/start_local.bat` の `start_all.py` の行の前に `set "PUSH_STREAM_RECONNECT_SESSION_AWARE=0"` のように足す（この bat は ASCII のみで書く）。または `core/startup/runtime_env/defaults.py` の該当の値を書き換える。どちらも本番を止めてから行い、`restart_local.ps1` で再起動する。
- 全部戻す: 本番のリポジトリで、取り込んだコミットを `git revert` して再起動する。

## 8. 残る危険

- **RST の原因は直していない。** 待ちを短くすると、kabu Station への接続要求の回数は増える（RST 1 回につき 1 回は変わらないが、15 秒待ちで間引かれていた分が無くなる）。再接続が速いことで RST が増えるかどうかは分からない。反映した日は `WebSocket接続要求受け入れ` の回数と `--- DISCONNECTED ---` の数を見る。
- **登録が壊れて受信 0 の即死が続く場合**（9/2 の形）、場中は 2 秒間隔（1 分に約 30 回）で再接続する。変更前は 15 秒間隔（1 分に約 4 回）。場外は変更前と同じ。
- **はみ出しの解除は、/board・/symbol で自動登録された銘柄を外す。** 全解除もそれらを外していたので、外れる範囲は狭くなるが、main.py が同じ銘柄の `/board` をすぐ呼ぶと、また自動登録される（次の周期でまた外す）。はみ出しの正体が /board だというのは、ログからの推定（直接は確かめていない）。
- **受信が増えると、発注の数が増える可能性がある。** 1 分足・板の鮮度が上がると、STALE で落ちていた候補が発注に届く。通常の買い・売りのシグナルはコスト後に優位性が無いと検証済み（メモリの 10/9・10/28 の記録）なので、反映した日は発注の件数と実約定の損益（fill_reconcile.py）も 10/27・10/28 と比べる。発注を増やしたくなければ、反映の前に ENTRY_PAUSE などで新規を絞るかを判断する。
- 場の時間は平日の時刻だけで決めている（祝日は見ない）。祝日は受信が無いだけで、待ちが短くなる以外の害は無い。
- monitor の場外の自己切断（夜間に約 8 秒ごと）はそのまま。回数は場外で別に数えるので場中には響かないが、ログの量は変わらない。
