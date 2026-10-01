# 家を見るAI — テスト計画と結果

## 1. 実行方法

```bash
cd ie-miru
npm install
npm run typecheck          # TypeScript 全パッケージ
npm test                   # Vitest（domain / adapter / API / UI）
npm run e2e                # Web をビルド → Node API(DATA_MODE=mock) を起動 → Playwright（iPhone 13 エミュレーション）
```

## 2. 自動テスト結果（2026-10-01 時点）

| 区分 | 件数 | 結果 |
|---|---|---|
| Vitest（30 ファイル） | **288** | 全件成功 |
| Playwright E2E | **8** | 全件成功 |
| 失敗 | **0** | |

追加の検証（自動テスト外・本セッションで実施）:
- `wrangler deploy --dry-run`: Worker バンドル成功（約 224 KiB、D1/R2 バインディング認識）
- `wrangler d1 migrations apply --local`: 0001〜0004 適用成功
- `wrangler dev --local`（workerd）: health / report / 保存 / 写真アップロード（ローカル R2）が動作

## 3. 要求カテゴリとテストの対応

| 要求 | 主なテスト |
|---|---|
| domain unit tests | `packages/domain/test/*`（geo, location, heading, ar, selection, zoning, hazard, comparables, valuation, simulate-*, inspection, arNote, lidar, rebuild） |
| API adapter tests | `packages/services/test/plateau.test.ts`（MVT デコード・属性正規化）, `reinfolib.test.ts`（XIT001/XPT002/XKT002 パーサ、ヘッダ認証、キー未設定拒否）, `buildings.test.ts`（OSM/デモ/合成）, `hazard.test.ts` |
| API mock tests | `MockReinfolibClient` を通した `RealEstateDataService` / `HazardService` / API 全体（`apps/api/test/*` は `DATA_MODE=mock`） |
| building selection tests | `selection.test.ts`（遮蔽順、視野、2〜5件、方位不安定→手動、半径、LiDAR 距離一致、建物内の利用者） |
| heading tests | `heading.test.ts`（DeviceOrientation 行列、webkitCompassHeading＋偏角、CoreMotion 四元数、roll、円周平均・平滑化、FOV 拡大）、`ar.test.ts`（ARKit transform） |
| GPS accuracy tests | `location.test.ts`（good/fair/poor/invalid、鮮度、許可拒否、CoreLocation 負の精度、融合） |
| no-data tests | `ui.test.ts`（データなし/対象外/確認できず ≠ 0）、`plateau.test.ts`（-9999→null）、`reinfolib.test.ts`（空応答→no_data、価格0行の除外）、`comparables.test.ts`（欠損属性はスキップ）、`valuation.test.ts`（入力不足なら数値を作らない）、`hazard.test.ts`（「安全」を生成しない） |
| API failure tests | `http.test.ts`（5xx 再試行、401 非再試行、タイムアウト、不正 JSON）、`plateau.test.ts`（503→error）、`reinfolib.test.ts`（失敗→unavailable）、`hazard.test.ts`（一部失敗でも他は返す） |
| offline-ish fallback tests | E2E `offline-ish`（API 遮断時に前回レポートを「オフライン表示」、見る画面の通信エラー案内）、`storage.test.ts`（D1 に保存したレポートを別 isolate で返す） |
| UI tests | `apps/web/test/ui.test.ts`（jsdom: ルーター、XSS エスケープ、値セル・バッジ、トップ3項目）、`packages/ui/test/ui.test.ts` |
| E2E | `tests/e2e/core-flow.spec.ts`（18 ステップ）、`fallbacks.spec.ts`（位置拒否→地図、精度不良、コンパスなし、カメラ拒否、オフライン、建て替え、貸す・不正ID） |
| 永続化 | `inspections/arNotes/saved/storage.test.ts`（node:sqlite D1 シム＋メモリ R2、端末間分離、冪等・追加のみのマイグレーション） |

## 4. 最終 E2E シナリオ（core-flow.spec.ts）

1. アプリ起動（トップ3項目）→ 2. 位置情報許可 → 3. カメラ起動（fake media stream）→ 4. 建物に向ける（擬似コンパス: 真北）→ 5. 候補 2〜5 件・「この建物ですか？」→ 6. 選択 → 7. 用途地域・建ぺい率・容積率 → 8. 地価 → 9. 周辺取引（「この建物そのものの売買価格ではありません」）→ 10. ハザード6種（内水=確認できず、「安全」表示なし）→ 11. 参考査定（レンジ＋非鑑定注記＋根拠）→ 12. 保存 → 13. 買う（編集して総取得費＝価格＋リフォーム＋諸費用を検証）→ 14. 売る（手取り、査定依頼=未接続）→ 15. 直す → 16. 写真登録（マーキング・サイズ・サムネイル）→ 17. ARメモ（置く→ピン表示、向きを変えると消える）→ 18. 保存した家から再表示

**注意**: E2E は `DATA_MODE=mock`（デモ建物＋モック不動産/ハザード）・擬似センサーで実行している。実データ・実センサーでの確認ではない。

## 5. 実機テスト（HUMAN ACTION — 本セッションでは未実施）

自動テストで代替できないもの。**実機未確認のため最終判定は GO にしない。**

| # | 項目 | 端末 | 確認内容 |
|---|---|---|---|
| R1 | iOS ビルド | Mac + Xcode 16 | `xcodegen generate` → ビルド・XCTest（`CameraPoseTests`）が通る（Swift はコンパイル未検証） |
| R2 | 位置許可ダイアログ・拒否時 | iPhone | 拒否→地図選択の案内、クラッシュしない |
| R3 | カメラ方位の実測 | iPhone | 既知の方角の建物に向け、表示方位が ±15° 以内か（縦持ち・やや上向き） |
| R4 | 街での建物特定率 | iPhone | 住宅街・ビル街で各 20 棟、上位候補に正解が含まれる率 |
| R5 | Geo Tracking | 対応都市の iPhone XS 以降 | `geo` モードになり精度が上がるか |
| R6 | 非対応地域・非 AR 端末 | — | `world_heading` / `camera_compass` に落ちても使えるか |
| R7 | LiDAR 計測 | iPhone Pro | 既知寸法（A4 紙など）の長さ・面積誤差 |
| R8 | ARメモ再表示 | iPhone Pro | 当日・翌日・1か月後に同じ場所で ARWorldMap 再ローカライズ成功率、失敗時の推定表示 |
| R9 | 建て替え AR | iPhone | 地面への実寸配置、2/3階・賃貸併用の切替 |
| R10 | Web 版（Safari） | iPhone | 方位許可ダイアログ（「見る」タップ時）、webkitCompassHeading の向き、カメラ、地図タイル表示 |
| R11 | 実データ | — | APIキー設定後、用途地域・地価・取引・ハザードが実値で出るか、属性名の差異がないか |
| R12 | 電波不良 | iPhone | 機内モード切替でオフライン表示・再試行 |
