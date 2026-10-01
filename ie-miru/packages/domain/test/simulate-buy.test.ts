import { describe, expect, it } from 'vitest';
import { brokerageFee, defaultBuyInput, monthlyPayment, simulateBuy, stampDuty } from '../src';

describe('fees', () => {
  it.each([
    [1_000_000, 55_000],
    [3_000_000, 154_000],
    [40_000_000, 1_386_000],
    [0, 0],
  ])('brokerage(%s) = %s', (p, f) => expect(brokerageFee(p)).toBe(f));
  it.each([
    [80_000, 0], [3_000_000, 1_000], [40_000_000, 10_000], [80_000_000, 30_000], [6_000_000_000, 480_000],
  ])('stamp(%s) = %s', (p, f) => expect(stampDuty(p)).toBe(f));
  it('monthly payment', () => {
    expect(monthlyPayment(36_000_000, 1.0, 35)).toBeCloseTo(101_625, -1);
    expect(monthlyPayment(12_000_000, 0, 10)).toBe(100_000);
    expect(monthlyPayment(0, 1, 35)).toBe(0);
  });
});

describe('simulateBuy', () => {
  it('computes total acquisition = price + costs + renovation', () => {
    const r = simulateBuy({ ...defaultBuyInput(40_000_000), renovationBudget: 5_000_000 });
    expect(r.totalAcquisition).toBe(40_000_000 + r.totalCosts + 5_000_000);
    expect(r.costs.find((c) => c.key === 'brokerage')!.amount).toBe(1_386_000);
    expect(r.costs.every((c) => c.estimated)).toBe(true);
    expect(r.monthlyPayment).toBeGreaterThan(0);
  });
  it('every number is user-editable via overrides', () => {
    const r = simulateBuy({ ...defaultBuyInput(40_000_000), overrides: { brokerage: 0, registration: 250_000 } });
    const b = r.costs.find((c) => c.key === 'brokerage')!;
    expect(b).toMatchObject({ amount: 0, estimated: false });
    expect(r.costs.find((c) => c.key === 'registration')!.amount).toBe(250_000);
  });
  it('no reference price -> zero price but no crash', () => {
    const r = simulateBuy(defaultBuyInput(null));
    expect(r.purchasePrice).toBe(0);
    expect(r.totalAcquisition).toBe(0);
  });
  it('negative inputs are clamped', () => {
    const r = simulateBuy({ ...defaultBuyInput(10_000_000), renovationBudget: -5 });
    expect(r.renovationBudget).toBe(0);
  });
  it('down payment covers what the loan does not', () => {
    const r = simulateBuy({ ...defaultBuyInput(30_000_000), loanAmount: 20_000_000 });
    expect(r.downPaymentNeeded).toBe(r.totalAcquisition - 20_000_000);
  });
});
