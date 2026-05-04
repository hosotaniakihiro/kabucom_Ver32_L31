# SELL AI 設計書（FINAL）

本ディレクトリは **SELL（決済・利確・損切り）判定に関する AI / ロジック**を
ENTRY と同一思想（3段階ゲート）で管理するためのものです。

本 README は **SELL システムの設計固定文書**です。

---

## 🎯 SELL の最終目的

- 利益を守る
- 損失を最小化する
- 「早すぎる利確」「遅すぎる損切り」を防ぐ
- ENTRY と同じ構造で **判断経路を一本化**

---

## 🧱 SELL 判定の全体構造（ENTRY と完全対称）

① 物理・市場ガード（sell_controller / exit_controller）
② 方式選択AI（sell_ai_boost）
③ 最終可否AI（LightGBM）
→ SELL 実行

yaml
コードをコピーする

---

## ① sell_controller / exit_controller（最終責任者）

**役割**
- SELL する / しないを最終決定
- SELL 方式（利確 / 損切り / トレール）を実行
- 発注・ポジション更新を行う

**絶対ルール**
- SELL を決めてよいのは controller のみ
- AI は「判断材料」を返すだけ

---

## ② sell_ai_boost（方式選択・オーケストレーター）

**ファイル（想定）**
pj/trading/exit/ignition/sell_ai_boost.py

python
コードをコピーする

**役割**
- SELL の方式を選ぶだけ
- SELL 可否の最終判断はしない

**SELL 方式（例）**

| sell_mode | 意味 |
|----------|------|
| TAKE_PROFIT | 利確 |
| STOP | 損切り |
| TRAIL | トレーリング |
| SKIP | 何もしない |

**返却形式（例）**
```python
{
  "ok": bool,                # 方式選択まで通過したか
  "sell_mode": str,          # TAKE_PROFIT / STOP / TRAIL / SKIP
  "reason": str,
  "features": dict,
  "ai_confidence": float | None
}
③ sell_lgbm（最終可否AI）
ファイル

bash
コードをコピーする
AI/train/sell/sell_lgbm.py
役割

「今 SELL してよいか？」を YES / NO で返す

SELL 方式は選ばない

最終ゲートとしてのみ機能

返却形式

python
コードをコピーする
True / False
🔁 SELL の成立条件（厳守）
SELL が実行されるのは 以下すべてを満たす場合のみ

graphql
コードをコピーする
sell_ai_boost.ok == True
AND
sell_lgbm == True
🧠 sell_mode の意味（確定）
sell_mode	意味	実行内容
TAKE_PROFIT	利確	成行 or 指値
STOP	損切り	成行
TRAIL	トレール	トレーリング更新
SKIP	見送り	何もしない

📊 補助AI（任意）
即時リスク判定（例）
bash
コードをコピーする
AI/train/sell/sell_immediate_risk.py
役割

急落・板崩れなどの危険度スコア算出

SELL 可否は決めない

ai_confidence / reason 用

📁 train/sell ディレクトリ構成（予定）
bash
コードをコピーする
train/sell/
 ├─ train_sell_lgbm.py        # SELL 可否 学習
 ├─ sell_lgbm.py              # SELL 可否 推論
 ├─ sell_immediate_risk.py    # 補助スコア（任意）
 └─ README.md                 # 本ファイル