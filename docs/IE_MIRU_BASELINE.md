# 家を見るAI (ie-miru) — ベースライン調査

調査日: 2026-10-01 / 調査者: Claude Code（主任エンジニア役）

## 1. 調査範囲

| 対象 | 調査方法 |
|---|---|
| このリポジトリ `hosotaniakihiro/kabucom_Ver32_L31`（約2,200ファイル） | 全文 grep（`madori` `sumai3d` `three.js` `roomplan` `lidar` `arkit` `wrangler` `cloudflare` `d1` `plateau` `reinfolib` `geolocation` `latitude` 等） |
| セッションからアクセス可能な他リポジトリ | `list_repos` → **このリポジトリ1件のみ** |
| 外部データ源への到達性 | コンテナから curl |

## 2. 既存資産の有無

| 資産 | 結果 | 備考 |
|---|---|---|
| Madori3D | **なし** | アクセス可能なリポジトリに存在しない |
| sumai3d | **なし** | 同上 |
| 既存 Three.js エンジン | **なし** | |
| RoomPlan 連携 | **なし** | |
| LiDAR 関連 | **なし** | |
| scan pipeline | **なし** | |
| AR 関連コード | **なし** | |
| Cloudflare Workers / D1 / R2 | **なし** | `wrangler.toml` なし |
| 認証 | **なし**（不動産向け） | 既存は kabuステーションAPIトークン管理（`token_manager.py`）のみ。用途が異なり再利用不可 |
| 共通UI / brand.ts | **なし** | |
| 共通API | **なし** | |
| 位置情報・地図関連 | **なし** | `utils_common.py` のヒットは無関係 |
| 不動産関連 | **なし** | |

既存リポジトリの中身は **株式自動売買システム（Python, kabuステーションAPI / DuckDB / SQLite）** であり、家を見るAIと共有できる資産はない。

## 3. 再利用対象

| 対象 | 再利用方法 |
|---|---|
| リポジトリの git 運用・`docs/` ディレクトリ | ドキュメントは既存 `docs/` に `IE_MIRU_*.md` として追加 |
| `.gitignore` の Secrets 除外ルール（`.env`, `*.key` 等） | そのまま有効。`ie-miru/` 側にも追加の ignore を置く |
| （将来）Madori3D / sumai3d | 本セッションから参照不可。`packages/domain` の `RebuildPlan`・`apps/web` の `massing` 描画を **差し替え可能な interface** にしてあるので、資産が見つかり次第アダプタで接続する（HUMAN ACTION 参照） |

## 4. 新規実装が必要な部分（= 全部）

既存株式システムと完全に独立させるため、リポジトリ直下に **`ie-miru/`** という独立 npm workspace を新設する。既存 Python コードには一切触れない。

- `ie-miru/packages/config` — 表示名・コード名・定数（brand.ts）
- `ie-miru/packages/domain` — 純粋ロジック（測地・方位・建物候補ランキング・ハザード状態・用途地域・類似事例・査定・試算・修繕・ARメモ再配置・LiDAR計算）
- `ie-miru/packages/services` — 外部データアダプタ（PLATEAU / 不動産情報ライブラリ / 国土地理院逆ジオコーダ / OSM）と統合サービス
- `ie-miru/packages/ui` — 表示用フォーマッタ・出典バッジ・ラベル（Web/iOS 双方の文言の単一ソース）
- `ie-miru/apps/api` — Cloudflare Workers (Hono) + D1 + R2
- `ie-miru/apps/web` — iPhone Safari で動く Web クライアント（カメラ・位置・方位・地図・試算・写真マーキング・簡易AR）
- `ie-miru/apps/ios` — ネイティブ iOS (SwiftUI / CoreLocation / CoreMotion / ARKit / RealityKit / LiDAR)
- `ie-miru/tests/e2e` — Playwright E2E

## 5. リスク

| リスク | 影響 | 対応 |
|---|---|---|
| 外部APIにこの開発コンテナから到達できない（後述） | 実データでのライブ疎通を本セッションで確認できない | 公開仕様どおりにアダプタを実装し、仕様形状のfixtureで契約テスト。ライブ確認は HUMAN ACTION |
| 不動産情報ライブラリは APIキー必須 | キー未設定時は実データ不可 | `REINFOLIB_API_KEY` 未設定時は **mock** に自動フォールバックし、UIに「モックデータ」と明示 |
| iOS ビルド環境（Xcode）がLinuxコンテナにない | Swift コードをコンパイル検証できない | ロジックはTS domainに寄せて自動テスト。Swift は薄いシェルとし、ビルド・実機確認は HUMAN ACTION |
| PLATEAU 属性の地域差（高さ・階数・築年がNULL） | 表示欠落 | 全属性 nullable。NULLは「データなし」と表示し 0 にしない |
| GPS / コンパス誤差（都市部で10〜50m, 方位±15°以上） | 誤った建物を特定 | 自動確定せず候補2〜5件をユーザーに選ばせる。精度不良時は手動選択に誘導 |
| ハザードを「安全」と誤表示 | 利用者の判断ミス | domain 層で「安全」というラベルを生成不能にし、テストで保証 |
| 周辺取引を物件価格と誤認 | 誤解 | 「周辺取引事例」「参考相場」と固定表示、査定はレンジ＋非鑑定の注記 |

## 6. 外部依存

| 依存 | 用途 | キー | コンテナから到達 |
|---|---|---|---|
| 国土交通省 不動産情報ライブラリ API (`www.reinfolib.mlit.go.jp/ex-api/external/*`) | 取引価格・成約価格・地価公示/地価調査・用途地域・ハザード | **要** (`Ocp-Apim-Subscription-Key`) | ✕（プロキシ403） |
| PLATEAU 建物 MVT（URLテンプレート設定式） | 建物polygon・高さ・階数・用途・構造・築年 | 不要 | ✕ |
| 国土地理院 逆ジオコーダ (`mreversegeocoder.gsi.go.jp`) | 市区町村コード取得 | 不要 | ✕ |
| 国土地理院 地図タイル (`cyberjapandata.gsi.go.jp`) | Web地図背景 | 不要 | ✕ |
| OpenStreetMap Overpass API（任意・既定OFF） | PLATEAU 未整備地域の建物輪郭 | 不要 | ✕ |
| npm registry | 開発依存 | — | ○ |

## 7. 本番への影響範囲

- 既存株式自動売買システム: **影響なし**（ファイル変更ゼロ。`ie-miru/` と `docs/IE_MIRU_*.md` の追加のみ）
- 本番 Cloudflare: **デプロイしていない**。`wrangler.toml` の D1/R2 ID はプレースホルダ
- DNS / Secret / 課金 / Apple Developer: **操作していない**
