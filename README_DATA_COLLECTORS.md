# kabu_data_collectors サブプロジェクト

## 目的

このサブプロジェクトは、既存の `main.py` から以下の処理を分離するためのものです。

- DB作成・スキーマ準備
- ランキング取得・ranking DB保存
- 株ステーションPUSH受信用の銘柄登録
- PUSH受信・push DB保存

`summary作成`、`AI判定`、`entry判定`、`表示`、`Discord通知` は既存どおり `main.py` 側に残す想定です。

---

## 追加ファイル

```text
scripts/
  data_collectors_runner.py
  db_prepare_runner.py
  ranking_collector_runner.py
  push_receiver_runner.py

data_collectors/
  __init__.py
  config.py
  logging_setup.py
  import_resolver.py
  db_prepare.py
  ranking_runtime.py
  push_runtime.py
  heartbeat.py
```

---

## 起動方法

```bat
D:\Users\owner\anaconda3\python.exe F:\script\python\kabu\kabucom_Ver32_L31\scripts\data_collectors_runner.py
```

この1本で以下をまとめて実行します。

```text
1. DB作成・スキーマ補正
2. ランキング取得runner起動
3. PUSH受信runner起動
4. 子プロセス監視
```

---

## タスクスケジューラ構成

おすすめは2本です。

```text
08:10 data_collectors_runner.py
08:30 main.py
```

`data_collectors_runner.py` は常駐します。  
`main.py` を再起動しても、ランキング取得とPUSH受信は止まりません。

---

## PUSH受信と銘柄登録について

PUSHを受けるには、株ステーションへ銘柄登録が必要です。

このサブプロジェクトでは `push_receiver_runner.py` から `data_collectors.push_runtime` を呼び、以下を試みます。

1. `trading.push.subscription_manager.start_symbol_subscription_manager`
2. `trading.push.subscription_manager.force_refresh_subscriptions`
3. `trading.push.push_stream.runtime.start_push_stream`
4. `trading.push.push_stream.runtime.start`

既存プロジェクト側の関数名が多少違っていても、候補を順番に探す設計にしています。

---

## main.py 側で二重起動させないもの

この構成を使う場合、`main.py` 側では以下を二重起動しないようにしてください。

```text
DB初期作成
ランキングAPIの常駐取得
PUSH WebSocket本体
PUSH銘柄登録ローテーション本体
```

main.py 側は以下を残します。

```text
summary作成
ranking summary作成
AI判定
entry判定
表示
Discord通知
```
