import { expect, mockCompass, STREET, test } from './fixtures';

test('location denied: no crash, guides to manual map selection, map picker opens detail', async ({ page, context }) => {
  await context.clearPermissions();
  await mockCompass(page);
  await page.goto('/#/look');
  await expect(page.getByTestId('look-message')).toContainText('位置情報が許可されていません', { timeout: 15_000 });
  await page.getByTestId('look-to-map').click();
  await expect(page.getByTestId('map-status')).toContainText('建物をタップ', { timeout: 15_000 });
  await expect(page.locator('path.bpoly').first()).toBeAttached();
  // 画面内に完全に収まっている建物ポリゴンの中心をタップ
  const pt = await page.evaluate(() => {
    const map = document.querySelector('[data-testid="map"]')!.getBoundingClientRect();
    for (const p of Array.from(document.querySelectorAll('path.bpoly'))) {
      const r = p.getBoundingClientRect();
      if (r.left > map.left + 60 && r.right < map.right - 10 && r.top > map.top + 10 && r.bottom < map.bottom - 40 && r.width > 10) return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
    }
    return null;
  });
  expect(pt).not.toBeNull();
  await page.mouse.click(pt!.x, pt!.y);
  await expect(page.getByTestId('building-title')).toBeVisible({ timeout: 20_000 });
});

test('poor GPS accuracy: shows the manual-selection message and distance-ordered candidates', async ({ page, context }) => {
  await context.grantPermissions(['geolocation', 'camera']);
  await context.setGeolocation({ ...STREET, accuracy: 120 });
  await mockCompass(page);
  await page.goto('/#/look');
  await expect(page.getByTestId('look-message')).toContainText('現在地の精度が低いため、建物を手動選択してください。', { timeout: 15_000 });
  await expect(page.getByTestId('candidates')).toContainText('現在地の精度が低いため');
});

test('no compass: manual heading slider appears and still yields candidates', async ({ page, context }) => {
  await context.grantPermissions(['geolocation', 'camera']);
  await context.setGeolocation(STREET);
  await page.goto('/#/look');
  await expect(page.getByTestId('manual-heading')).toBeVisible({ timeout: 10_000 });
  await expect(page.getByTestId('candidate-0')).toBeVisible({ timeout: 15_000 });
  await expect(page.getByTestId('hud')).toContainText('手動');
});

test('camera denied: app keeps working with compass + location', async ({ browser }) => {
  const context = await browser.newContext({ geolocation: STREET, permissions: ['geolocation'], viewport: { width: 390, height: 844 } });
  const page = await context.newPage();
  await page.addInitScript(() => {
    Object.defineProperty(navigator, 'mediaDevices', { value: { getUserMedia: () => Promise.reject(Object.assign(new Error('denied'), { name: 'NotAllowedError' })) } });
    setInterval(() => {
      const e = new Event('deviceorientation');
      Object.assign(e, { alpha: 0, beta: 90, gamma: 0, webkitCompassHeading: 7.5, webkitCompassAccuracy: 8 });
      window.dispatchEvent(e);
    }, 200);
  });
  await page.goto('http://localhost:8799/#/look');
  await expect(page.getByTestId('look-message')).toContainText('カメラが許可されていません');
  await expect(page.getByTestId('candidate-0')).toBeVisible({ timeout: 15_000 });
  await context.close();
});

test('offline-ish: when the API is unreachable, previously loaded report is shown with an offline label', async ({ page, context }) => {
  await context.grantPermissions(['geolocation']);
  await context.setGeolocation(STREET);
  await page.goto('/');
  const id = await page.evaluate(async () => (await (await fetch('/v1/buildings/nearby?lat=35.64585&lng=139.65303&radius=40')).json()).buildings[0].id);
  await page.goto(`/#/b/${encodeURIComponent(id)}`);
  await expect(page.getByTestId('building-title')).toBeVisible({ timeout: 20_000 });
  await page.route('**/v1/**', (r) => r.abort('internetdisconnected'));
  await page.goto('/');
  await page.goto(`/#/b/${encodeURIComponent(id)}`);
  await expect(page.getByTestId('offline-flag')).toBeVisible({ timeout: 20_000 });
  await expect(page.getByTestId('building-title')).toBeVisible();
  // 「見る」でも通信不可の案内が出る
  await page.goto('/#/look');
  await expect(page.getByTestId('candidates')).toContainText('通信できませんでした', { timeout: 15_000 });
});

test('rebuild AR panel shows 3 presets with reference zoning info and no "buildable" claim', async ({ page, context }) => {
  await context.grantPermissions(['geolocation', 'camera']);
  await context.setGeolocation(STREET);
  await mockCompass(page);
  await page.goto('/');
  const id = await page.evaluate(async () => (await (await fetch('/v1/buildings/nearby?lat=35.64585&lng=139.65303&radius=40')).json()).buildings[0].id);
  await page.goto(`/#/b/${encodeURIComponent(id)}/rebuild`);
  await expect(page.getByTestId('rebuild-info')).toBeVisible({ timeout: 20_000 });
  for (const p of ['two_story', 'three_story', 'rental_combo']) {
    await page.getByTestId(`rebuild-${p}`).click();
    await expect(page.getByTestId('rebuild-checks')).toContainText('建ぺい率');
    await expect(page.getByTestId('rebuild-checks')).toContainText('容積率');
    await expect(page.getByTestId('rebuild-checks')).toContainText('用途地域');
  }
  await expect(page.getByTestId('rebuild-disclaimer').first()).toContainText('「建築可能」であることを示すものではありません');
  // Three.js が描画されている（キャンバスが空でない）
  await expect.poll(() => page.getByTestId('rebuild-canvas').evaluate((c: HTMLCanvasElement) => c.width > 0 && c.toDataURL().length > 5000), { timeout: 15_000 }).toBe(true);
});

test('rent panel and invalid building id', async ({ page }) => {
  await page.goto('/#/b/plateau%3Anot-exist');
  await expect(page.getByTestId('report-error')).toContainText('建物が見つかりませんでした', { timeout: 20_000 });
  const id = await page.evaluate(async () => (await (await fetch('/v1/buildings/nearby?lat=35.64585&lng=139.65303&radius=40')).json()).buildings[0].id);
  await page.goto(`/#/b/${encodeURIComponent(id)}/rent`);
  await expect(page.getByTestId('rent-net')).toContainText('万円', { timeout: 20_000 });
});
