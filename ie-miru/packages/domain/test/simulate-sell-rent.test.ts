import { describe, expect, it } from 'vitest';
import { defaultRentInput, defaultSellInput, simulateRent, simulateSell } from '../src';

describe('simulateSell', () => {
  it('net proceeds = price - costs - payoff (tax unknown -> not computed)', () => {
    const r = simulateSell({ ...defaultSellInput(42_000_000), mortgagePayoff: 10_000_000 });
    expect(r.capitalGainsTax).toBeNull();
    expect(r.taxNote).toContain('計算していません');
    expect(r.netProceeds).toBe(42_000_000 - r.totalCosts - 10_000_000);
    expect(r.costs.find((c) => c.key === 'lienRelease')!.amount).toBe(30_000);
  });
  it('capital gains tax with own-home deduction', () => {
    const r = simulateSell({ ...defaultSellInput(80_000_000), acquisitionCost: 30_000_000, holdingYears: 10, ownHomeDeduction: true });
    const gain = 80_000_000 - 30_000_000 - r.totalCosts - 30_000_000;
    expect(r.capitalGainsTax).toBe(Math.round(gain * 0.20315));
  });
  it('short-term rate and no negative tax', () => {
    const r = simulateSell({ ...defaultSellInput(20_000_000), acquisitionCost: 30_000_000, holdingYears: 2, ownHomeDeduction: false });
    expect(r.capitalGainsTax).toBe(0);
    expect(r.taxNote).toContain('短期');
  });
  it('overrides', () => {
    const r = simulateSell({ ...defaultSellInput(42_000_000), overrides: { brokerage: 500_000 } });
    expect(r.costs[0]).toMatchObject({ amount: 500_000, estimated: false });
  });
});

describe('simulateRent', () => {
  it('yield calculation', () => {
    const i = defaultRentInput(48_000_000, 100);
    expect(i.monthlyRent).toBe(200_000);
    const r = simulateRent(i);
    expect(r.grossYieldPct).toBe(5);
    expect(r.netYieldPct).toBeLessThan(5);
    expect(r.notes[0]).toContain('仮置き');
  });
  it('no property value -> yields null, not 0', () => {
    const r = simulateRent(defaultRentInput(null, 80));
    expect(r.grossYieldPct).toBeNull();
    expect(r.annualGrossIncome).toBeGreaterThan(0);
  });
});
