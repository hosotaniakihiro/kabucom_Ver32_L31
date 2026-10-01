import { describe, expect, it } from 'vitest';
import { available, missing, type HazardResult } from '@ie-miru/domain';
import { cell, formatArea, formatHeading, formatPct, formatUnitPrice, formatYen, formatYenRange, hazardRow, sourceLine, UI_TEXT } from '../src';

describe('formatters', () => {
  it('yen', () => {
    expect(formatYen(42_000_000)).toBe('4,200万円');
    expect(formatYen(123_000_000)).toBe('1億2,300万円');
    expect(formatYen(200_000_000)).toBe('2億円');
  });
  it('yen range', () => {
    expect(formatYenRange(39_000_000, 44_000_000)).toBe('3,900〜4,400万円');
    expect(formatYenRange(95_000_000, 120_000_000)).toBe('9,500万円〜1億2,000万円');
  });
  it('unit price with tsubo', () => {
    expect(formatUnitPrice(512_000)).toBe('51.2万円/㎡（169万円/坪）');
  });
  it('misc', () => {
    expect(formatArea(123.456)).toBe('123.5㎡');
    expect(formatPct(2.34, true)).toBe('+2.3%');
    expect(formatHeading(91)).toBe('東 91°');
    expect(formatHeading(359)).toBe('北 359°');
  });
});

describe('value cells distinguish missing from zero', () => {
  const src = [{ id: 'x', name: 'X', url: null, license: null, mode: 'live' as const, fetchedAt: null }];
  it('available', () => {
    const c = cell(available(0, 'public', src), (v) => `${v}階`);
    expect(c).toMatchObject({ text: '0階', empty: false, badge: { label: '公的データ' } });
  });
  it.each([
    ['no_data', 'データなし'],
    ['not_applicable', '対象外'],
    ['unavailable', '確認できず'],
  ] as const)('%s -> %s', (status, text) => {
    const c = cell(missing<number>(status, 'public', src, 'why'), (v) => `${v}`);
    expect(c.text).toBe(text);
    expect(c.text).not.toBe('0');
    expect(c.empty).toBe(true);
    expect(c.note).toBe('why');
  });
  it('AI estimate badge', () => {
    expect(cell(available(3, 'ai_estimate', src), String).badge.label).toBe('AI推定');
  });
  it('mock is flagged', () => {
    const m = [{ ...src[0]!, mode: 'mock' as const, name: 'M' }];
    expect(cell(available(1, 'public', m), String).mock).toBe(true);
    expect(sourceLine(m)).toContain('モックデータ');
  });
});

describe('hazard rows', () => {
  const h = (status: HazardResult['status'], severity: HazardResult['severity'] = null): HazardResult => ({ type: 'flood', status, level: '0.5m以上3.0m未満', severity, detail: null, reason: null, sources: [] });
  it('tone', () => {
    expect(hazardRow(h('in_zone', 4)).tone).toBe('alert');
    expect(hazardRow(h('in_zone', 2)).tone).toBe('caution');
    expect(hazardRow(h('no_data')).tone).toBe('muted');
  });
  it('never says safe', () => {
    for (const s of ['in_zone', 'graded', 'no_data', 'not_applicable', 'unavailable'] as const) {
      expect(hazardRow(h(s)).status).not.toContain('安全');
    }
  });
});

describe('UI text', () => {
  it('home has exactly three entries', () => {
    expect(Object.values(UI_TEXT.home)).toEqual(['見る', 'マップ', '保存した家']);
  });
  it('actions', () => {
    expect(Object.values(UI_TEXT.actionLabels)).toEqual(['買う', '売る', '直す', '貸す', '建て替える']);
  });
});
