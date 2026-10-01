# 家を見るAI (ie-miru) — アーキテクチャ

コンセプト: **家を見る → 家を特定する → 家を理解する → 家について行動する**

## 1. 全体構成

```
 iPhone (ネイティブ: apps/ios)              iPhone Safari (Web: apps/web)
 CoreLocation / CoreMotion / ARKit          Geolocation / DeviceOrientation / getUserMedia
 RealityKit / LiDAR                         Leaflet(国土地理院タイル) / Three.js
        │  JSON over HTTPS (X-IeMiru-Device: 匿名端末ID)  │
        └──────────────────────┬──────────────────────────┘
                               ▼
                  apps/api  (Hono — Cloudflare Workers / Node 共通)
                               │
        ┌──────────────────────┼─────────────────────────────┐
        ▼                      ▼                             ▼
 packages/domain        packages/services                D1 / R2
 純粋ロジック            外部データアダプタ               利用者データ・分析キャッシュ
 (I/O なし)             PLATEAU MVT / 不動産情報ライブラリ
                        国土地理院逆ジオコーダ / OSM(任意)
                        デモ建物 / モック(キー未設定時)
```

| パッケージ | 責務 | 依存 |
|---|---|---|
| `packages/config` | 表示名・コード名（`brand.ts`）、既定値 | なし |
| `packages/domain` | 測地、方位・カメラ姿勢、AR 追跡モード判定、建物候補ランキング、出自/状態型、用途地域、ハザード状態、周辺取引の類似度、AI参考査定、買う/売る/貸す試算、修繕記録、ARメモ再配置、LiDAR 計算、建て替えボリューム、保存した家 | config |
| `packages/services` | `BuildingSource`（PLATEAU/OSM/デモ/合成）、`ReinfolibClient`（Live/Mock）、`RealEstateDataService`、`HazardService`、`Geocoder`、`AppraisalProvider`、`buildReport` | domain |
| `packages/ui` | 表示フォーマッタ、値セル（データなし/対象外/確認できず）、出典バッジ・出典行、UI 文言 | domain |
| `apps/api` | HTTP API、D1/R2 リポジトリ、マイグレーション、Workers/Node エントリ | 上記すべて |
| `apps/web` | 画面（見る/マップ/保存した家/詳細シート/各アクション） | domain, ui |
| `apps/ios` | SwiftUI ネイティブ（AR・LiDAR はこちらが本命） | API |

**原則**
- 外部データ固有の処理（PLATEAU 属性名、不動産情報ライブラリの JSON 形状）はアダプタ内に閉じ、domain は正規化済みの型だけを扱う。
- すべての値は `Sourced<T>`（`available` / `no_data` / `not_applicable` / `unavailable` ＋ 出自 `public` / `ai_estimate` / `user` / `reference` ＋ 出典）。**欠損は 0 にしない。**
- 外部 API の失敗は例外で全体を落とさず、セクション単位で「確認できず」にする。
- 計算ロジックは TypeScript の domain が単一ソース。iOS は API（`/v1/simulations/*` 等）経由で同じ計算を使い、二重実装を避ける（カメラ姿勢の換算のみ Swift にも同式を置き、TS と同じ期待値の XCTest を用意）。

## 2. 建物特定の流れ

1. 端末: 位置（`LocationFix`: latitude, longitude, horizontalAccuracy, altitude, heading, headingAccuracy, timestamp）とカメラ姿勢（`CameraPose`: heading, yaw, pitch, roll, headingAccuracy, source）を取得。
   - カメラ方位は**端末上端の方位（CLHeading）ではなく**、姿勢行列から背面カメラ光軸（端末 -Z）を求める（`cameraPoseFromMatrix`）。
   - 入力: ARKit camera.transform（gravityAndHeading）/ CoreMotion xTrueNorthZVertical / W3C DeviceOrientation + webkitCompassHeading（磁気偏角補正）/ 手動。
2. `POST /v1/candidates` → `assessLocation`（許可・精度・鮮度）→ 建物取得（半径 100m＋位置誤差）→ `rankCandidates`。
   - 半径内 → 視野（±20°、方位誤差に応じて最大 ±60° まで拡大）→ 中心レイと建物外形の交差（手前優先＝遮蔽考慮）→ 角度ずれ・距離・AR/LiDAR 計測距離でスコア。
   - **自動確定しない**。候補 2〜5 件を返し、最有力でも「この建物ですか？」と確認する。
   - 精度不良・方位不安定時は距離順の候補＋「現在地の精度が低いため、建物を手動選択してください。」→ 地図での手動選択。
3. `GET /v1/buildings/:id/report` → `buildReport`: 逆ジオコーダ（市区町村コード）・用途地域・地価・取引事例・6種ハザードを並列取得 → 対象推定（`estimateSubject`）→ 類似事例（`summarizeComparables`）→ 参考査定（`estimateValue`）。

AR 追跡モードのフォールバック（`chooseTrackingMode`）:
`geo`（ARGeoTracking）→ `world_heading`（ARWorldTracking + gravityAndHeading）→ `camera_compass` → `compass_only` → `map_only`。どこで止まってもアプリ全体は使える。

## 3. API

| メソッド | パス | 内容 | 永続化 |
|---|---|---|---|
| GET | `/v1/health` | 稼働・データモード | - |
| POST | `/v1/candidates` | 位置＋姿勢 → 候補 | - |
| GET | `/v1/buildings/nearby?lat&lng&radius` | 地図用の建物一覧 | - |
| GET | `/v1/buildings/:id` | 建物 | D1 フォールバック |
| GET | `/v1/buildings/:id/report` | この家について（全セクション） | D1 キャッシュ 24h |
| POST | `/v1/simulations/{buy,sell,rent,rebuild}` | 試算（iOS 用。Web はローカル実行） | - |
| GET/POST | `/v1/appraisal-providers`, `/v1/appraisal-requests` | 不動産会社査定（既定は未接続） | - |
| GET/POST/PATCH/DELETE | `/v1/saved[/:buildingId]` | 保存した家 | D1 |
| GET/POST/DELETE | `/v1/inspections[/:id][/photo]` | 直す（写真は multipart） | D1 + R2 |
| GET/POST/PATCH/DELETE | `/v1/ar-notes[/:id]`, `PUT/GET /v1/ar-notes/:id/world-map` | ARメモ・ARWorldMap | D1 + R2 |

利用者データ系は `X-IeMiru-Device`（匿名 UUID）必須。端末間でデータは見えない。

## 4. データベース（D1）

マイグレーションは追加のみ・冪等（`CREATE ... IF NOT EXISTS`、DROP/DELETE なしをテストで保証）。

| ファイル | テーブル |
|---|---|
| `0001_inspections.sql` | `inspections` |
| `0002_ar_notes.sql` | `ar_notes` |
| `0003_saved_buildings.sql` | `saved_buildings` |
| `0004_buildings_analysis.sql` | `buildings`, `building_sources`, `property_analysis`, `hazards` |

R2: `inspections/<device>/<id>`（修繕写真）、`ar-worldmaps/<device>/<id>`（ARWorldMap）。

## 5. 実行環境

| 環境 | エントリ | DB | 写真 |
|---|---|---|---|
| Cloudflare Workers（本番想定・未デプロイ） | `apps/api/src/worker.ts` | D1 | R2 |
| ローカル workerd（`wrangler dev --local`） | 同上 | ローカル D1 | ローカル R2 |
| Node（開発・E2E） | `apps/api/src/node-server.ts` | `node:sqlite`（D1 互換シム） | ファイル |

設定（環境変数 / Secret）: `REINFOLIB_API_KEY`（Secret）, `PLATEAU_MVT_URL`, `PLATEAU_MVT_LAYER`, `PLATEAU_MVT_ZOOM`, `ENABLE_OSM_FALLBACK`, `DATA_MODE`(`live`/`mock`), `ALLOWED_ORIGINS`。

## 6. UI 構成

- トップは **見る / マップ / 保存した家** の3つだけ。「見る」で即カメラ。
- 詳細は下からのシート。**この家について**（参考相場・土地・用途地域・災害・建物概要）は折りたたみで、最初から全部は見せない。
- **この家でできること**: 買う / 売る / 直す / 貸す / 建て替える（＋直すから ARメモ）。
- 値ごとにバッジ: 公的データ / AI推定 / ユーザー登録 / 参考情報、モック・デモは別バッジと帯で明示。各セクションに出典行。

## 7. 拡張ポイント

- `BuildingSource`: PLATEAU 3D Tiles / CityGML 直接読み込み、自治体独自データ。
- `ReinfolibClient`: 他の公的 API（ハザードマップポータルのタイル等）を `HazardService` に追加。
- `AppraisalProvider`: 不動産会社・一括査定（特定社に依存しないレジストリ）。
- `MassingModelProvider`: Madori3D / sumai3d 等の 3D 資産（現状は簡易ボリューム）。
