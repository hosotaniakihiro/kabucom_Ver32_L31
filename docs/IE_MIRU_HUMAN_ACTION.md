# 家を見るAI — HUMAN ACTION（人間が行う操作）

自動では実行しなかった操作の一覧。上から優先度順。

## A. すぐ必要（開発継続・検証）

1. ~~**GitHub へのプッシュ権限**~~ → **2026-10-02 解決済み**（Windows 実機から push 成功）。: 本セッションからの `git push` が 403 で拒否された（Claude GitHub App がこのリポジトリに未インストール／未連携）。https://claude.ai/connect-github で GitHub を再連携し、`hosotaniakihiro/kabucom_Ver32_L31` に Claude GitHub App をインストールしてから、ブランチ `claude/ie-miru-phases-1-20-ukqz0z` を push する（ローカルにはコミット済み）。
2. **不動産情報ライブラリ API キーの申請**（https://www.reinfolib.mlit.go.jp/ の API 利用申請）。取得後:
   - ローカル: `ie-miru/apps/api/.dev.vars` に `REINFOLIB_API_KEY=...`（git 管理外）
   - Cloudflare: `wrangler secret put REINFOLIB_API_KEY`
3. **PLATEAU 建物データの配信元を決める**（利用規約確認込み）。候補の実測結果は `IE_MIRU_DATA_SOURCES.md` §7。MVT の URL テンプレートを `PLATEAU_MVT_URL` に設定（レイヤ名が必要なら `PLATEAU_MVT_LAYER`）。自前でタイル化する場合はその作業も。
4. ~~**開発環境のネットワーク許可**~~ → Windows 実機からは国土地理院・PLATEAU 候補・不動産情報ライブラリ（401=キー待ち）すべて到達できる。（元の記載）: Claude Code のクラウド環境設定で `www.reinfolib.mlit.go.jp`, `mreversegeocoder.gsi.go.jp`, `cyberjapandata.gsi.go.jp`, PLATEAU 配信ホストを許可すると、ライブ API での契約確認ができる。
5. **ライブ API での属性名確認**: キー取得後、XPT002 / XKT002 / XKT025〜029 の実レスポンスと `packages/services/src/reinfolib/parse.ts`・`hazard/service.ts` の候補キーを突き合わせる（fixture は公開仕様の形状で作成）。

## B. iOS（Apple Developer 関連・課金を伴う可能性）

6. Mac + Xcode で `cd ie-miru/apps/ios && xcodegen generate` → ビルド（Swift は Linux コンテナでコンパイル未検証。エラーがあれば修正）。
7. `project.yml` の `DEVELOPMENT_TEAM` に Team ID、Bundle ID（`app.iemiru.ios`）の確定、署名。
8. 実機テスト R1〜R12（`docs/IE_MIRU_TEST_PLAN.md` §5）。
9. TestFlight / App Store 提出（プライバシー表示: 位置情報・写真・カメラ）。

## C. Cloudflare（本番・課金・DNS）

10. D1 データベース・R2 バケットの作成（`wrangler d1 create ie-miru`, `wrangler r2 bucket create ie-miru-photos`）→ `apps/api/wrangler.toml` の `REPLACE_WITH_*` を置換。
11. 本番 D1 へのマイグレーション適用（`wrangler d1 migrations apply ie-miru --remote`）。すべて追加型・冪等。
12. Secret 設定（上記 2）、`ALLOWED_ORIGINS` の設定、デプロイ（`wrangler deploy`）。
13. Web（`apps/web/dist`）のホスティング（Cloudflare Pages 等）と独自ドメイン・DNS 設定。
14. レート制限ルール・WAF の設定（匿名 API のため）。

## D. 方針決定

15. Madori3D / sumai3d / 既存 Three.js エンジン / RoomPlan 資産が別リポジトリにある場合、そのリポジトリへのアクセス付与（`MassingModelProvider` に接続する）。
16. 不動産会社査定の接続先（`AppraisalProvider`）と個人情報の取り扱い（同意文言）。
17. 周辺賃料データの調達（「貸す」は現在仮置き）、内水ハザードの自治体データの扱い。
18. 認証方式（匿名端末ID → アカウント）、利用規約・プライバシーポリシー本文、退会・全データ削除フロー。
19. 名称の最終決定（`ie-miru/packages/config/src/brand.ts` と iOS `project.yml` の `IEMIRU_DISPLAY_NAME`）。

## 実行していないこと（ルールどおり）

本番環境変更・本番 DB 操作・DNS 変更・Cloudflare Secret 変更・有料契約・Apple Developer 操作・API キー発行・スクレイピング・force push・既存システム（株式自動売買コード）の変更は一切行っていない。
