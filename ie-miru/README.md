# ie-miru（家を見るAI）

**家を見る → 家を特定する → 家を理解する → 家について行動する**

iPhone を目の前の建物に向けると、建物候補を特定し、公的データ（用途地域・地価・取引事例・ハザード）と
AI参考査定・買う/売る/直す/貸す/建て替える の試算を表示するサービス。

## 構成（責務分離）

| ディレクトリ | 責務 |
|---|---|
| `packages/config` | 名称（brand.ts）・既定値 |
| `packages/domain` | 純粋ロジック。I/Oなし。測地・方位・候補ランキング・ハザード状態・査定・試算・ARメモ再配置 |
| `packages/services` | 外部データアダプタ（PLATEAU / 不動産情報ライブラリ / 国土地理院 / OSM）と統合サービス |
| `packages/ui` | 表示文言・フォーマッタ・出典バッジ（Web/iOSの文言の単一ソース） |
| `apps/api` | Cloudflare Workers (Hono) + D1 + R2 |
| `apps/web` | iPhone Safari で動くクライアント（カメラ・位置・方位・地図・試算・写真マーキング・簡易AR） |
| `apps/ios` | ネイティブ iOS（SwiftUI / CoreLocation / CoreMotion / ARKit / RealityKit / LiDAR） |
| `tests/e2e` | Playwright E2E |

ドキュメントはリポジトリ直下 `docs/IE_MIRU_*.md`。

## コマンド

> **Windows の注意**
> - npm workspaces はシンボリックリンクを使うので、NAS / SMB 共有（例: `W:`）上では `npm install` が `symlink UNKNOWN` で失敗する。ローカルディスク（例: `C:\tmp\ie-miru`）に clone して実行する。
> - Smart App Control が rollup のネイティブモジュール（未署名 `.node`）をブロックするため、`package.json` の `overrides` で rollup を公式 WASM 版 `@rollup/wasm-node` に差し替えている（Linux/macOS でも同じく動く）。
> - E2E の初回は `npx playwright install chromium` が必要。

```bash
npm install
npm run typecheck
npm test          # domain / adapter / API / UI の自動テスト
npm run e2e       # Web をビルドして Playwright E2E
npm run dev:api   # ローカルAPI（Node + SQLite + ファイルR2）http://localhost:8787
```

環境変数（`apps/api/.dev.vars` もしくは Cloudflare Secret）:

| 変数 | 意味 | 未設定時 |
|---|---|---|
| `REINFOLIB_API_KEY` | 不動産情報ライブラリ APIキー | mock（UIに「モックデータ」と表示） |
| `PLATEAU_MVT_URL` | PLATEAU 建物 MVT のURLテンプレート `{z}/{x}/{y}` | デモ建物データ |
| `ENABLE_OSM_FALLBACK` | `1` で OSM Overpass を建物輪郭のフォールバックに使う | 無効 |
| `DATA_MODE` | `live` / `mock` | キー有無から自動 |
