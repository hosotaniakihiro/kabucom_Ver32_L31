// @vitest-environment jsdom
import { describe, expect, it } from 'vitest';
import { available, missing } from '@ie-miru/domain';
import { cell } from '@ie-miru/ui';
import { parseRoute, buildingPath } from '../src/router';
import { h } from '../src/dom';
import { valueRow } from '../src/views/detail';
import { homeView } from '../src/views/home';

const src = [{ id: 's', name: 'S', url: null, license: null, mode: 'live' as const, fetchedAt: null }];

describe('router', () => {
  it('parses routes and building ids with namespaces', () => {
    expect(parseRoute('#/look')).toEqual({ name: 'look' });
    expect(parseRoute('')).toEqual({ name: 'home' });
    const p = buildingPath('osm:way/123', 'buy');
    expect(parseRoute(`#${p}`)).toEqual({ name: 'building', id: 'osm:way/123', action: 'buy' });
  });
});

describe('DOM helpers', () => {
  it('escapes text (no HTML injection)', () => {
    const el = h('div', {}, '<img src=x onerror=alert(1)>');
    expect(el.querySelector('img')).toBeNull();
    expect(el.textContent).toContain('<img');
  });
});

describe('value rows', () => {
  it('render provenance badge and value', () => {
    const el = valueRow('階数', cell(available(2, 'public', src), (v) => `地上${v}階`));
    expect(el.textContent).toContain('地上2階');
    expect(el.querySelector('.badge.public')!.textContent).toBe('公的データ');
    expect(el.classList.contains('empty')).toBe(false);
  });
  it('render missing data as データなし (never 0)', () => {
    const el = valueRow('築年', cell(missing<number>('no_data', 'public', src, '建築年のデータがありません'), (v) => `${v}年`));
    expect(el.textContent).toContain('データなし');
    expect(el.textContent).not.toMatch(/\b0年/);
    expect(el.classList.contains('empty')).toBe(true);
  });
  it('AI estimate and mock badges', () => {
    const el = valueRow('参考', cell(available(1, 'ai_estimate', [{ ...src[0]!, mode: 'mock' }]), String));
    expect(el.querySelector('.badge.ai_estimate')!.textContent).toBe('AI推定');
    expect(el.querySelector('.badge.mock')).not.toBeNull();
  });
});

describe('home', () => {
  it('has exactly three entries', () => {
    const el = homeView();
    expect([...el.querySelectorAll('.home-actions button')].map((b) => b.textContent?.replace(/[^\p{L}]/gu, ''))).toEqual(['見る', 'マップ', '保存した家']);
  });
});
