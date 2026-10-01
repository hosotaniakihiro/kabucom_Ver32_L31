# 家を見るAI — AR・センサー・LiDAR

## 1. カメラ姿勢（Phase 3）

- **端末方位 ≠ カメラ方位**。CLHeading（端末上端の方位）は、iPhone を縦に構えて建物へ向けると空を指す。建物特定には背面カメラ光軸（端末座標の -Z）を世界座標（ENU）へ回したベクトルを使う。
  - heading = atan2(東, 北)、pitch = asin(上)、roll = 光軸まわりの端末上方向の傾き、yaw = heading を ±180° 表記。
- 入力別:

| 入力 | 実装 | 精度の目安 |
|---|---|---|
| ARKit `camera.transform`（worldAlignment = gravityAndHeading） | `cameraPoseFromArkitTransform` / `ARSessionController.pose` | コンパス精度（±10〜15°）。Geo Tracking 時は数度 |
| CoreMotion `xTrueNorthZVertical` | `cameraPoseFromCoreMotionQuaternion` / `MotionService` | ±10〜20°（磁気環境依存） |
| Web: DeviceOrientation + `webkitCompassHeading` | `cameraPoseFromDeviceOrientation` | ±15〜25°。磁北なので偏角補正（日本 −7.5° 既定） |
| 手動（スライダー） | `look.ts` | 利用者次第 |

- pitch が ±75° を超える（真上/真下）・方位精度 > 45° のとき `headingUnreliable` とし、方位を候補選定に使わない。
- 円周 EMA で平滑化（359°→1° を正しく扱う）。

## 2. AR セッション（Phase 4）

フォールバック順（`chooseTrackingMode` / Swift `ARCapabilities.choose`）:

1. **geo**: `ARGeoTrackingConfiguration`（端末対応かつ `checkAvailability` が true の地域のみ。Apple の VPS 対応都市）
2. **world_heading**: `ARWorldTrackingConfiguration`、`worldAlignment = .gravityAndHeading`、LiDAR 端末は `sceneReconstruction = .mesh`・`sceneDepth`
3. **camera_compass**: AR 非対応。カメラ映像＋CoreMotion 方位
4. **compass_only**: カメラ不許可
5. **map_only**: 位置または方位が無い → 地図で手動選択

Geo Tracking が `notAvailable` に落ちたら実行中に world_heading へ切り替える。AR 失敗時も候補提示・地図選択は動く。

## 3. 建物候補抽出（Phase 5）

`rankCandidates`:
1. 半径 100m（＋位置誤差、最大 +50m）内の建物
2. 視野: ±20°（方位誤差に応じて最大 ±60°）と建物の視角が重なるもの
3. 中心レイと建物外形の交差を距離順に並べ、**最初に当たった建物＝見えている建物**（遮蔽考慮）
4. スコア = 0.4×交差 + 0.35×角度 + 0.25×距離（＋AR/LiDAR 計測距離との一致で ±0.15）
5. 2〜5 件。最有力でも「この建物ですか？」と確認。利用者が中にいる建物は減点。

## 4. AR 修繕メモ（Phase 15）

### 再配置方式（完全永続アンカーを前提にしない）

| 優先 | 方式 | 対応 | 精度の目安 |
|---|---|---|---|
| 1 | **ARWorldMap** に `ARAnchor(name: noteId)` を保存 → 次回 `initialWorldMap` で再ローカライズ | iOS（LiDAR 端末で特に安定） | cm〜数十 cm（同じ場所・似た照明でのみ成功） |
| 2 | **ARGeoAnchor 相当の緯度経度・高さ**（raycast 距離から算出） | iOS | Geo Tracking 地域で数 m |
| 3 | **建物ID＋建物重心からの相対位置（ENU）** | Web / iOS | GPS 誤差＋方位誤差（数 m・数度） |
| 4 | **撮影時の GPS＋カメラ姿勢＋距離** | Web / iOS | 同上。建物データが無い場合 |
| 5 | **写真（修繕記録）** | 共通 | 目視照合の手掛かり |

- 距離は LiDAR / AR raycast → 建物外形とのレイ交差 → 不明 の順。
- `relocalizeNotes` は現在の位置・姿勢から各メモの方位・仰角・距離・画面座標・**推定角度誤差**を返し、UI に「誤差 ±n°」を表示する。

### 技術的限界（正直な記載）

- ARWorldMap の再ローカライズは、**同じ位置・同じ向き・似た明るさ**でないと失敗しやすい。半年後は季節・植栽・照明・工事で失敗する可能性が高い。失敗時は GPS 推定表示に自動で落ちる（「推定位置」と明示）。
- ARWorldMap はサイズが大きい（数 MB〜）。R2 に保存するが通信量に注意。
- GPS は都市部で 5〜30m、コンパスは 10〜25° ずれる。10m 先の外壁で 2〜4m の横ずれは普通に起きる。Web 版の「簡易 AR」は**位置の目安**であり、外壁の特定の“ひび”にピン留めできる精度ではない。
- Web（Safari）は ARKit・LiDAR・WebXR AR を使えない。Web の AR 表示はカメラ映像に方位計算で重ねたもの。
- 屋外の大きな建物までの距離（10m〜）は LiDAR の有効距離（約 5m）外。ARKit の平面推定 raycast で補う。

## 5. LiDAR（Phase 16）

- 必須にしない。`ARWorldTrackingConfiguration.supportsSceneReconstruction(.mesh)` が true の端末のみ計測 UI（`ARMeasureView`）を出す。
- 用途: 建物までの距離補助（候補ランキングの `measuredDistanceM`）、修理箇所の長さ（折れ線）・面積（Newell 法、平面性チェック）、ARメモ位置の精度向上。
- 距離の融合: LiDAR（5m 以内 σ≈数 cm、以遠は 5%）・AR raycast（8%）・GPS 外形交差（σ = 位置精度）を逆分散重み付け（`fuseDistances`）。
- 非 LiDAR 端末・Web: サイズは手入力（`method: manual` と明示）。

## 6. 建て替える AR（Phase 17）

- 既存の Madori3D / sumai3d は本セッションから参照できなかった（`docs/IE_MIRU_BASELINE.md`）。`MassingModelProvider` を用意し、資産が見つかればモデル（glTF/USDZ）を差し替える。
- 現状は簡易ボリューム（各階の箱＋切妻/陸屋根）。2階建て / 3階建て / 賃貸併用。
- iOS: 検出した地面を raycast して**実寸**で配置（RealityKit）。Web: Three.js で描画し、カメラ映像に重ね、端末の方位変化で視点を回す（実空間に固定はされない）。
- 建ぺい率・容積率・用途地域・低層住居系の絶対高さを**参考情報として併記**（範囲内（参考）/ 超過の可能性 / 要確認）。斜線・日影・地区計画・条例・接道は未考慮で、**「建築可能」とは表示しない**（テストで文言を検査）。

## 7. 実機でしか確認できないこと

`docs/IE_MIRU_TEST_PLAN.md` の「実機テスト（HUMAN ACTION）」を参照。
