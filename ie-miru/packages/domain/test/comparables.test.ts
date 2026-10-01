import { describe, expect, it } from 'vitest';
import { estimateSubject, quantile, similarity, summarizeComparables, type PropertySubject, type Transaction } from '../src';
import { bldg } from './helpers';

const tx = (over: Partial<Transaction> = {}): Transaction => ({
  id: Math.random().toString(36), priceCategory: 'transaction', type: '宅地(土地と建物)', prefecture: '東京都', municipality: '世田谷区', municipalityCode: '13112',
  district: 'サンプル一丁目', priceYen: 50_000_000, areaM2: 110, areaIsLowerBound: false, unitPriceYenPerM2: null, totalFloorAreaM2: 95, buildingYear: 1995,
  structure: '木造', use: '住宅', floorPlan: null, cityPlanning: '第一種低層住居専用地域', coverageRatioPct: 60, floorAreaRatioPct: 150, frontRoadWidthM: 4,
  stationMinutes: null, year: 2025, quarter: 2, remarks: null, ...over,
});

const subject: PropertySubject = {
  transactionType: '宅地(土地と建物)', landAreaM2: 110, landAreaEstimated: true, floorAreaM2: 95, floorAreaEstimated: true, builtYear: 1995,
  structure: '木造', useDistrict: '第一種低層住居専用地域', stationMinutes: null, townName: 'サンプル二丁目',
};

describe('estimateSubject', () => {
  it('estimates land and floor area from footprint and coverage', () => {
    const b = bldg('X', 0, 20, 10, 6, { floorsAbove: 2, builtYear: 1990 });
    const s = estimateSubject(b, { useDistrict: '第一種低層住居専用地域', coverageRatioPct: 60, floorAreaRatioPct: 150, firePrevention: null, notes: [] });
    expect(s.landAreaM2).toBeCloseTo(60 / 0.51, -1);
    expect(s.floorAreaM2).toBe(114);
    expect(s.landAreaEstimated).toBe(true);
  });
  it('keeps unknown values null', () => {
    const s = estimateSubject(bldg('Y', 0, 20), null);
    expect(s.floorAreaM2).toBeNull();
    expect(s.builtYear).toBeNull();
  });
  it('user overrides win and are no longer flagged as estimates', () => {
    const s = estimateSubject(bldg('Y', 0, 20), null, { overrides: { landAreaM2: 132 } });
    expect(s).toMatchObject({ landAreaM2: 132, landAreaEstimated: false });
  });
});

describe('similarity', () => {
  it('identical is high', () => {
    expect(similarity(subject, tx()).score).toBeGreaterThan(0.85);
  });
  it('different type / size / age reduce score', () => {
    const base = similarity(subject, tx()).score;
    expect(similarity(subject, tx({ type: '中古マンション等' })).score).toBeLessThan(base);
    expect(similarity(subject, tx({ areaM2: 400 })).score).toBeLessThan(base);
    expect(similarity(subject, tx({ buildingYear: 1965 })).score).toBeLessThan(base);
    expect(similarity(subject, tx({ cityPlanning: '商業地域' })).score).toBeLessThan(base);
  });
  it('missing attributes are skipped, not scored as zero', () => {
    const r = similarity(subject, tx({ buildingYear: null, areaM2: null }));
    expect(r.skipped).toEqual(expect.arrayContaining(['age', 'area']));
    expect(r.score).toBeGreaterThan(0.5);
  });
  it('lower-bound area is not compared', () => {
    expect(similarity(subject, tx({ areaM2: 2000, areaIsLowerBound: true })).skipped).toContain('area');
  });
});

describe('summarizeComparables', () => {
  it('summarizes the most similar transactions with quantiles', () => {
    const txs = [30, 40, 50, 60, 70].map((m) => tx({ priceYen: m * 1_000_000 })).concat([tx({ type: '農地', priceYen: 1 })]);
    const s = summarizeComparables(subject, txs);
    expect(s.count).toBe(5);
    expect(s.totalInArea).toBe(5);
    expect(s.medianPriceYen).toBe(50_000_000);
    expect(s.p25PriceYen).toBe(40_000_000);
    expect(s.notes[0]).toContain('この建物そのものの売買価格ではありません');
  });
  it('land-only transactions provide land unit price', () => {
    const s = summarizeComparables(subject, [tx(), tx({ type: '宅地(土地)', unitPriceYenPerM2: 450_000, buildingYear: null })]);
    expect(s.medianLandUnitPriceYenPerM2).toBe(450_000);
  });
  it('no similar transactions -> count 0 and null medians (not 0)', () => {
    const s = summarizeComparables(subject, [tx({ type: '中古マンション等', areaM2: 20, buildingYear: 2020, cityPlanning: '商業地域', district: '遠い町' })]);
    expect(s.count).toBe(0);
    expect(s.medianPriceYen).toBeNull();
  });
  it('filters by year', () => {
    expect(summarizeComparables(subject, [tx({ year: 2015 })], { sinceYear: 2023 }).count).toBe(0);
  });
  it('quantile edge cases', () => {
    expect(quantile([], 0.5)).toBeNull();
    expect(quantile([5], 0.9)).toBe(5);
  });
});
