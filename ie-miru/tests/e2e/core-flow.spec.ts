import { expect, mockCompass, PNG, STREET, test } from './fixtures';

/**
 * 最終E2Eシナリオ（18ステップ）。データは DATA_MODE=mock（デモ建物＋モック不動産/ハザード）。
 * 実センサー・実データではない点は docs/IE_MIRU_TEST_PLAN.md に記載。
 */
test('core flow: look → pick building → public info → save → buy/sell/fix/photo/AR note → saved list', async ({ page, context }) => {
  // 2. 位置情報許可
  await context.grantPermissions(['geolocation', 'camera']);
  await context.setGeolocation(STREET);
  await mockCompass(page);

  // 1. アプリ起動（トップは3つだけ）
  await page.goto('/');
  const homeButtons = page.locator('.home-actions button');
  await expect(homeButtons).toHaveCount(3);
  await expect(homeButtons).toHaveText([/見る/, /マップ/, /保存した家/]);

  // 3. カメラ起動（「見る」で即カメラ）
  await page.getByTestId('home-look').click();
  await expect(page).toHaveURL(/#\/look/);
  await expect.poll(() => page.getByTestId('camera').evaluate((v: HTMLVideoElement) => !!v.srcObject)).toBe(true);

  // 4. 建物に向ける（真北） → 方位・精度が HUD に出る
  await expect(page.getByTestId('hud')).toContainText('方位 北');
  await expect(page.getByTestId('hud')).toContainText('精度 ±6m');

  // 5. 建物候補表示（2〜5件、「この建物ですか？」）
  const first = page.getByTestId('candidate-0');
  await expect(first).toBeVisible({ timeout: 15_000 });
  await expect(page.getByTestId('candidates')).toContainText('この建物ですか？');
  const n = await page.locator('[data-testid^="candidate-"]').count();
  expect(n).toBeGreaterThanOrEqual(2);
  expect(n).toBeLessThanOrEqual(5);
  await expect(page.getByTestId('demo-flag')).toBeVisible();
  await expect(first).toContainText('カメラの正面');

  // 6. 建物を選択
  const buildingId = await first.getAttribute('data-building-id');
  await first.click();
  await expect(page.getByTestId('building-title')).toBeVisible({ timeout: 20_000 });
  await expect(page.getByTestId('mock-flag')).toBeVisible();

  // 7. 用途地域
  await page.getByTestId('sec-zoning').locator('summary').click();
  await expect(page.getByTestId('zoning-district')).toContainText('地域');
  await expect(page.getByTestId('zoning-coverage')).toContainText('%');
  await expect(page.getByTestId('zoning-far')).toContainText('%');

  // 8. 地価
  await page.getByTestId('sec-land').locator('summary').click();
  await expect(page.getByTestId('land-price')).toContainText('万円/㎡');

  // 9. 周辺取引（物件価格と誤認させない表記）
  await page.getByTestId('sec-market').locator('summary').click();
  await expect(page.getByTestId('comparables-count')).toContainText('周辺取引事例');
  await expect(page.getByTestId('sec-market')).toContainText('この建物そのものの売買価格ではありません');

  // 10. ハザード（安全とは表示しない）
  await page.getByTestId('sec-hazard').locator('summary').click();
  for (const t of ['flood', 'inland_flood', 'tsunami', 'storm_surge', 'landslide', 'liquefaction']) await expect(page.getByTestId(`hazard-${t}`)).toBeVisible();
  await expect(page.getByTestId('hazard-inland_flood')).toContainText('確認できず');
  const hazardText = await page.getByTestId('sec-hazard').innerText();
  expect(hazardText.replace(/安全を意味(しません|するものではありません)/g, '')).not.toContain('安全');

  // 11. 参考査定（レンジ＋非鑑定の注記）
  await expect(page.getByTestId('valuation')).toContainText(/参考価格 [\d,]+〜[\d,]+万円|参考価格 .*億/);
  await expect(page.getByTestId('valuation-disclaimer')).toContainText('正式な不動産鑑定・査定ではありません');
  await expect(page.getByTestId('valuation-reasons').locator('li')).not.toHaveCount(0);

  // 12. 保存
  await page.getByTestId('save-button').click();
  await page.getByTestId('save-nickname').fill('E2Eの家');
  await page.getByTestId('save-status').selectOption('interested');
  await page.getByTestId('save-confirm').click();
  await expect(page.getByTestId('save-button')).toHaveText('★ 保存済み');

  // 13. 「買う」試算（全項目編集可）
  await page.getByTestId('action-buy').click();
  await expect(page.getByTestId('panel-buy')).toBeVisible();
  await page.getByTestId('buy-price').fill('4000');
  await page.getByTestId('buy-renovation').fill('500');
  await page.getByTestId('buy-cost-brokerage').fill('0');
  await expect(page.getByTestId('buy-total')).toContainText('万円');
  const total = await page.getByTestId('buy-total').innerText();
  const costs = await page.getByTestId('buy-total-costs').innerText();
  const num = (s: string) => Number(s.replace(/[^\d]/g, ''));
  expect(num(total)).toBe(4000 + 500 + num(costs));
  await page.goBack();

  // 14. 「売る」試算
  await page.getByTestId('action-sell').click();
  await expect(page.getByTestId('panel-sell')).toBeVisible();
  await page.getByTestId('sell-price').fill('5000');
  await page.getByTestId('sell-payoff').fill('1000');
  await expect(page.getByTestId('sell-net')).toContainText('万円');
  await page.getByTestId('appraisal-request').click();
  await expect(page.getByTestId('appraisal-status')).toContainText('接続されていません');
  await page.goBack();

  // 15. 「直す」 / 16. 写真登録（マーキング付き）
  await page.getByTestId('action-fix').click();
  await expect(page.getByTestId('panel-fix')).toBeVisible();
  await page.getByTestId('fix-category').selectOption('exterior_wall');
  await page.getByTestId('fix-photo').setInputFiles({ name: 'wall.png', mimeType: 'image/png', buffer: PNG });
  await page.getByTestId('markup-canvas').click({ position: { x: 20, y: 20 } });
  await expect(page.getByTestId('fix-mark-count')).toHaveText('マーク 1件');
  await page.getByTestId('fix-memo').fill('外壁のひび（E2E）');
  await page.getByTestId('fix-size').fill('0.35');
  await page.getByTestId('fix-save').click();
  await expect(page.getByTestId('fix-item')).toHaveCount(1);
  await expect(page.getByTestId('fix-item')).toContainText('外壁のひび（E2E）');
  await expect(page.getByTestId('fix-item')).toContainText('長さ 約35cm（手入力）');
  await expect.poll(() => page.locator('[data-testid="fix-item"] img').evaluate((i: HTMLImageElement) => i.src.startsWith('blob:'))).toBe(true);

  // 17. ARメモ登録 → 同じ場所を向けると表示
  await page.getByTestId('open-arnote').click();
  await expect(page.getByTestId('arnote-hud')).toContainText('方位 北', { timeout: 10_000 });
  await page.getByTestId('arnote-status').selectOption('needs_repair');
  await page.getByTestId('arnote-text').fill('外壁のひび');
  await page.getByTestId('arnote-place').click();
  await expect(page.getByTestId('arnote-item')).toHaveCount(1);
  await expect(page.getByTestId('arnote-pin')).toBeVisible();
  await expect(page.getByTestId('arnote-pin')).toContainText('修理必要');
  await expect(page.getByTestId('arnote-pin')).toContainText('発見');
  // 東を向くとメモは視野外になる
  await page.evaluate(() => ((window as any).__heading = 97.5));
  await expect(page.getByTestId('arnote-pin')).toHaveCount(0);

  // 18. 保存した家から再表示
  await page.goto('/#/saved');
  await expect(page.getByTestId('saved-item')).toHaveCount(1);
  await expect(page.getByTestId('saved-item')).toContainText('E2Eの家');
  await page.getByTestId('saved-item').click();
  await expect(page).toHaveURL(new RegExp(`#/b/${encodeURIComponent(buildingId!)}`));
  await expect(page.getByTestId('building-title')).toBeVisible();
  await expect(page.getByTestId('save-button')).toHaveText('★ 保存済み');
});
