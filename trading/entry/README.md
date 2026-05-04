# ENTRY AI 設計書（FINAL）

本ディレクトリは **ENTRY（新規建て）判定に関する AI / ロジック**を
「壊れない・拡張できる」構造で管理するためのものです。

本 README は **設計の固定文書（変更時は必ず更新）** とします。

---

## 🎯 ENTRY の最終目的

- 無駄な ENTRY を減らす
- 勝率の高い初動のみを取る
- 判断ロジックを **1本のパイプライン**に統一する

---

## 🧱 ENTRY 判定の全体構造（3段階ゲート）
① 市場・物理ガード（entry_controller）
② 方式選択AI（ai_boost）
③ 最終可否AI（LightGBM）
→ ENTRY 実行

---

## ① entry_controller（最終責任者）

**役割**
- ENTRY する / しないを最終決定
- 注文方式（成行 / 指値）を決定
- 発注・ポジション管理を行う

**絶対ルール**
- ENTRY を決めてよいのは entry_controller のみ
- 他モジュールは「判断材料」しか返さない

---

## ② ai_boost（方式選択・オーケストレーター）

**ファイル**
pj/trading/entry/ignition/ai_boost.py


**役割**
- ENTRY 方式を決めるだけ
- BREAKOUT / PULLBACK / SKIP を返す
- ENTRY 可否の最終判断はしない

**返却形式**
```python
{
  "ok": bool,              # 方式選択まで通過したか
  "entry_mode": str,       # BREAKOUT / PULLBACK / SKIP
  "reason": str,
  "features": dict,
  "ai_confidence": float | None  # 参考値
}
③ tonosama_entry_lgbm（最終可否AI）

ファイル

AI/train/entry/tonosama_entry_lgbm.py


役割

「この条件で ENTRY してよいか？」を YES / NO で返す

方式選択はしない

閾値付きの最終ゲート

返却形式

True / False

🔁 ENTRY の成立条件（厳守）

ENTRY が実行されるのは 以下すべてを満たす場合のみ

ai_boost.ok == True
AND
tonosama_entry_lgbm == True

🧠 entry_mode の意味
entry_mode	意味	注文方式
BREAKOUT	初動追随	成行
PULLBACK	押し目	指値
SKIP	見送り	ENTRY しない
📊 即含み益AI（補助スコア）

ファイル

AI/train/entry/entry_immediate_profit.py


役割

ENTRY 直後に含み益になる確率（0.0〜1.0）を返す

ENTRY 可否は決めない

分析・confidence 用

📁 train/entry ディレクトリ構成
train/entry/
 ├─ build_train_csv_entry_immediate_profit.py
 ├─ train_lightgbm_entry_immediate_profit.py
 ├─ entry_immediate_profit.py
 ├─ train_tonosama_entry_lgbm.py
 ├─ tonosama_entry_lgbm.py
 └─ README.md  ← 本ファイル

🚨 絶対にやってはいけないこと

❌ ai_boost で ENTRY を決定する
❌ LightGBM で方式選択をする
❌ entry_controller 以外で発注する
❌ train と infer を混在させる