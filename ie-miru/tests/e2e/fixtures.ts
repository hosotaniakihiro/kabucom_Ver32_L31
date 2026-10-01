import { test as base, expect, type Page } from '@playwright/test';

/** デモ住宅街の道路上（北側すぐに建物がある地点） */
export const STREET = { latitude: 35.64585, longitude: 139.65303, accuracy: 6 };

/**
 * 方位センサーの擬似: iOS Safari と同じ webkitCompassHeading 付き deviceorientation を発火する。
 * window.__heading（磁北基準）を書き換えると向きが変わる。日本の偏角 -7.5° を加味して 7.5 = 真北。
 */
export async function mockCompass(page: Page, magneticHeading = 7.5) {
  await page.addInitScript((h) => {
    (window as any).__heading = h;
    setInterval(() => {
      const e = new Event('deviceorientation');
      Object.assign(e, { alpha: 0, beta: 90, gamma: 0, absolute: false, webkitCompassHeading: (window as any).__heading, webkitCompassAccuracy: 8 });
      window.dispatchEvent(e);
    }, 200);
  }, magneticHeading);
}

export const test = base;
export { expect };

/** 1x1 PNG（写真アップロード用） */
export const PNG = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==', 'base64');
