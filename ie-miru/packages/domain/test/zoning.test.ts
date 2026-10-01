import { describe, expect, it } from 'vitest';
import {
  absoluteHeightLimitM, effectiveFarPct, normalizeUseDistrict, parseJapaneseYear, parseLooseNumber, parsePeriod,
  roadLimitedFarPct, useDistrictGroup,
} from '../src';

describe('Japanese parsing helpers', () => {
  it.each([
    ['1990年', 1990], ['平成2年', 1990], ['平成元年', 1989], ['昭和45年', 1970], ['令和3年', 2021], ['戦前', 1945], ['', null], [null, null], ['不明', null],
  ])('year %s -> %s', (s, y) => expect(parseJapaneseYear(s as string)).toBe(y));
  it.each([
    ['123,000(円/㎡)', 123000], ['60%', 60], ['2000㎡以上', 2000], ['', null], ['-', null], [42, 42], [Number.NaN, null], [undefined, null],
  ])('number %s -> %s', (s, n) => expect(parseLooseNumber(s)).toBe(n));
  it('period', () => {
    expect(parsePeriod('2023年第1四半期')).toEqual({ year: 2023, quarter: 1 });
    expect(parsePeriod('令和5年第４四半期')).toEqual({ year: 2023, quarter: 4 });
    expect(parsePeriod('2023年')).toEqual({ year: 2023, quarter: null });
    expect(parsePeriod(null)).toBeNull();
  });
});

describe('zoning', () => {
  it.each([
    ['第１種低層住居専用地域', '第一種低層住居専用地域'],
    ['一低層', '第一種低層住居専用地域'],
    ['2中高', '第二種中高層住居専用地域'],
    ['準住居地域', '準住居地域'],
    ['近商', '近隣商業地域'],
    ['商業地域', '商業地域'],
    ['準工業地域', '準工業地域'],
    ['工業専用地域', '工業専用地域'],
    ['市街化調整区域', null],
  ])('%s -> %s', (raw, d) => expect(normalizeUseDistrict(raw)).toBe(d));
  it('groups and height limits', () => {
    expect(useDistrictGroup('第一種低層住居専用地域')).toBe('low_rise_residential');
    expect(absoluteHeightLimitM('第一種低層住居専用地域')).toEqual({ minM: 10, maxM: 12 });
    expect(absoluteHeightLimitM('商業地域')).toBeNull();
    expect(useDistrictGroup(null)).toBe('unknown');
  });
  it('road width limits FAR', () => {
    expect(roadLimitedFarPct('第一種低層住居専用地域', 4)).toBe(160);
    expect(roadLimitedFarPct('商業地域', 4)).toBe(240);
    expect(roadLimitedFarPct('商業地域', 12)).toBeNull();
    expect(effectiveFarPct('第一種住居地域', 200, 4)).toBe(160);
    expect(effectiveFarPct('第一種住居地域', 150, 6)).toBe(150);
    expect(effectiveFarPct('第一種住居地域', null, null)).toBeNull();
  });
});
