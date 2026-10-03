# PUSH 中継 (2 つ目の kabu 口座)

kabu の PUSH 登録は 1 口座 50 銘柄まで。別 PC で 2 つ目の口座の kabu Station を動かし、
その PUSH を本番 PC に転送して、合計 100 銘柄の株価・板を本番の判定に使う。
**2 つ目の口座では発注しない** (データの受け取りだけ)。

## しくみ
- 本番 PC: `trading/push/push_relay_server.py` が push_receiver プロセスの中で TCP 18090 を待ち受ける。
  **常連 50 銘柄は中継側**、本番の口座は 保有中・発注中 → 入れ替わる候補 (常連以外) で 50 銘柄。
  常連は直近 10 営業日の push DB で 200 ティック以上受信した日数が多い順 (起動時に計算、
  runtime/relay_regulars_YYYYMMDD.json)。保有中・発注中になった常連は本番側に移す。
  常連の計算前は 先頭 50 を自分・51〜100 番目を中継。
  届いた PUSH は自分の websocket のものと同じ `on_message` に流す (DB・1 分足・5 秒足・板すべて共通)。
- 中継 PC: `ops/push_relay/relay_agent.py` が本番 PC から銘柄リストを受け取って登録し、PUSH を転送する。
- 発注前の板: REST `/board` は自分の口座に登録した銘柄しか返さないので、取れなければ
  push DB の最新ティックの 10 本板を使う (`_rfb_board_from_push_db`、15 秒より古ければ使わない)。

## 本番 PC の準備
1. `C:\autostock\src\settings.ini` に追加する:
   ```
   [push_relay]
   enabled = 1
   port = 18090
   secret = (16 文字以上のランダムな文字列)
   ```
2. Windows のファイアウォールで、受信 TCP 18090 を LAN (プライベート) から許可する。
   初回起動時に「アクセスを許可する」画面が出たら「プライベート ネットワーク」で許可する。
3. 本番 PC の LAN の IP アドレスを確認する (`ipconfig`)。

## 中継 PC の準備
1. 2 つ目の口座で kabu Station をインストール・ログインし、API を有効にして API パスワードを設定する。
2. Python 3 を入れて `pip install websocket-client requests`。
3. このフォルダ (`relay_agent.py`, `relay_agent.ini.example`) を中継 PC にコピーし、
   `relay_agent.ini` を作って api_password / host / secret を入れる。
4. kabu Station を起動して 1〜2 分待ってから `python relay_agent.py`。

## 確認
- 中継 PC のログ (`relay_agent.log`): `main connected` → `register count=50 status=200` → `stats {...}`。
- 本番 PC の push_receiver のログ: `[PUSH RELAY] connected` / `[PUSH RELAY] register sent count=..` /
  1 分ごとの `[PUSH RELAY] stats`。
- 中継が切れても本番は従来どおり 50 銘柄で動く。

## 止め方
- 中継 PC の `relay_agent.py` を止める (本番は自動で 50 銘柄に戻る)。
- 恒久的に止めるなら本番 PC の `[push_relay] enabled = 0`。
