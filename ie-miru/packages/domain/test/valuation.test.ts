import { describe, expect, it } from 'vitest';
import { buildingResidualRatio, estimateValue, weightedLandPrice, type ComparablesSummary, type LandPricePoint, type PropertySubject } from '../src';

const NOW = new Date('2026-10-01');
const subject = (o: Partial<PropertySubject> = {}): PropertySubject => ({
  transactionType: '宅地(土地と建物)', landAreaM2: 120, landAreaEstimated: false, floorAreaM2: 100, floorAreaEstimated: false, builtYear: 2006,
  structure: '木造・土蔵造', useDistrict: '第一種低層住居専用地域', stationMinutes: null, townName: null, ...o,
});
const lp = (price: number, d: number): LandPricePoint => ({ id: `${price}-${d}`, kind: '地価公示', year: 2026, pricePerM2: price, yoyChangePct: 1, address: null, useCategory: null, zoning: null, coverageRatioPct: 60, floorAreaRatioPct: 150, nearestStation: null, stationDistanceM: null, location: { lat: 0, lng: 0 }, distanceM: d });
const comps = (o: Partial<ComparablesSummary> = {}): ComparablesSummary => ({ count: 6, totalInArea: 20, items: [], medianPriceYen: 60_000_000, p25PriceYen: 55_000_000, p75PriceYen: 65_000_000, medianUnitPriceYenPerM2: 500_000, medianLandUnitPriceYenPerM2: 450_000, notes: [], ...o });

describe('valuation', () => {
  it('returns a range with low < mid < high and reasons', () => {
    const r = estimateValue({ subject: subject(), landPrices: [lp(400_000, 300)], comparables: comps(), now: NOW });
    expect(r.ok).toBe(true);
    if (!r.ok) return;
    const v = r.valuation;
    expect(v.estimatedLow).toBeLessThan(v.estimatedMid);
    expect(v.estimatedMid).toBeLessThan(v.estimatedHigh);
    expect(v.estimatedHigh - v.estimatedLow).toBeGreaterThanOrEqual(1_000_000);
    expect(v.method).toBe('cost+comparables');
    expect(v.reasons.length).toBeGreaterThanOrEqual(4);
    expect(v.reasons.join()).toContain('地価公示');
    // land 400k*1.1*120 = 52.8M, building: wood 20y -> ratio max(.1, 1-20/22)=0.0909->0.1 -> 100*200k*0.1=2M
    expect(v.components.landValueYen).toBe(52_800_000);
    expect(v.components.buildingValueYen).toBe(2_000_000);
    expect(v.components.comparableApproachYen).toBe(60_000_000);
    expect(v.confidence).toBeGreaterThan(0.6);
    expect(['高', '中']).toContain(v.confidenceLabel);
  });

  it('missing build year widens the range and lowers confidence', () => {
    const a = estimateValue({ subject: subject(), landPrices: [lp(400_000, 300)], comparables: comps(), now: NOW });
    const b = estimateValue({ subject: subject({ builtYear: null, landAreaEstimated: true, floorAreaEstimated: true }), landPrices: [lp(400_000, 300)], comparables: comps({ count: 2 }), now: NOW });
    if (!a.ok || !b.ok) throw new Error('expected ok');
    expect(b.valuation.confidence).toBeLessThan(a.valuation.confidence);
    expect((b.valuation.estimatedHigh - b.valuation.estimatedLow) / b.valuation.estimatedMid).toBeGreaterThan((a.valuation.estimatedHigh - a.valuation.estimatedLow) / a.valuation.estimatedMid);
    expect(b.valuation.reasons.join()).toContain('築年が不明');
  });

  it('works with land prices only (cost approach)', () => {
    const r = estimateValue({ subject: subject(), landPrices: [lp(300_000, 200)], comparables: null, now: NOW });
    expect(r.ok && r.valuation.method).toBe('cost');
  });

  it('works with comparables only, using land unit from land-only transactions', () => {
    const r = estimateValue({ subject: subject(), landPrices: [], comparables: comps(), now: NOW });
    expect(r.ok && r.valuation.method).toBe('cost+comparables');
    if (r.ok) expect(r.valuation.reasons.join()).toContain('地価公示地点が近くにない');
  });

  it('no data -> not ok (no number is invented)', () => {
    const r = estimateValue({ subject: subject(), landPrices: null, comparables: null, now: NOW });
    expect(r.ok).toBe(false);
    const r2 = estimateValue({ subject: subject({ landAreaM2: null }), landPrices: [lp(1, 1)], comparables: comps(), now: NOW });
    expect(r2.ok).toBe(false);
  });

  it('unknown floor area is not treated as zero building value silently', () => {
    const r = estimateValue({ subject: subject({ floorAreaM2: null }), landPrices: [lp(400_000, 300)], comparables: null, now: NOW });
    expect(r.ok && r.valuation.components.buildingValueYen).toBeNull();
    if (r.ok) expect(r.valuation.reasons.join()).toContain('建物価格は 0 ではなく「不明」');
  });

  it('weighted land price ignores far points', () => {
    expect(weightedLandPrice([lp(1, 5000)])).toBeNull();
    expect(weightedLandPrice([lp(100, 100), lp(300, 1000)])!.price).toBeLessThan(200);
  });

  it('residual ratio floors at 10% and uses 40% when age unknown', () => {
    expect(buildingResidualRatio('wood', 50)).toBe(0.1);
    expect(buildingResidualRatio('rc', 0)).toBe(1);
    expect(buildingResidualRatio('wood', null)).toBe(0.4);
  });
});
