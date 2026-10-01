import type { Hono } from 'hono';
import {
  defaultBuyInput, defaultRentInput, defaultSellInput, simulateBuy, simulateRent, simulateSell,
  type BuyInput, type RentInput, type SellInput,
} from '@ie-miru/domain';
import type { AppEnv } from '../app';

const n = (v: unknown, d: number) => (typeof v === 'number' && Number.isFinite(v) ? v : d);
const nn = (v: unknown) => (typeof v === 'number' && Number.isFinite(v) ? v : null);
function overrides<K extends string>(v: unknown, keys: readonly K[]): Partial<Record<K, number>> {
  const out: Partial<Record<K, number>> = {};
  if (v && typeof v === 'object') for (const k of keys) { const x = (v as Record<string, unknown>)[k]; if (typeof x === 'number' && Number.isFinite(x)) out[k] = x; }
  return out;
}

/** 試算 API（iOS など domain を直接持たないクライアント向け。Web は同じ domain をローカルで実行） */
export function registerSimulationRoutes(app: Hono<AppEnv>) {
  app.post('/v1/simulations/buy', async (c) => {
    const b = ((await c.req.json().catch(() => ({}))) ?? {}) as Record<string, unknown>;
    const d = defaultBuyInput(nn(b.referencePrice));
    const input: BuyInput = {
      purchasePrice: n(b.purchasePrice, d.purchasePrice),
      renovationBudget: n(b.renovationBudget, d.renovationBudget),
      loanAmount: n(b.loanAmount, d.loanAmount),
      interestRatePct: n(b.interestRatePct, d.interestRatePct),
      loanYears: n(b.loanYears, d.loanYears),
      overrides: overrides(b.overrides, ['brokerage', 'stamp', 'registration', 'acquisitionTax', 'loanFee', 'insurance', 'other'] as const),
    };
    return c.json({ input, result: simulateBuy(input) });
  });
  app.post('/v1/simulations/sell', async (c) => {
    const b = ((await c.req.json().catch(() => ({}))) ?? {}) as Record<string, unknown>;
    const d = defaultSellInput(nn(b.referencePrice));
    const input: SellInput = {
      salePrice: n(b.salePrice, d.salePrice),
      mortgagePayoff: n(b.mortgagePayoff, 0),
      acquisitionCost: nn(b.acquisitionCost),
      holdingYears: nn(b.holdingYears),
      ownHomeDeduction: b.ownHomeDeduction !== false,
      overrides: overrides(b.overrides, ['brokerage', 'stamp', 'lienRelease', 'other'] as const),
    };
    return c.json({ input, result: simulateSell(input) });
  });
  app.post('/v1/simulations/rent', async (c) => {
    const b = ((await c.req.json().catch(() => ({}))) ?? {}) as Record<string, unknown>;
    const d = defaultRentInput(nn(b.referencePrice), nn(b.floorAreaM2));
    const input: RentInput = {
      monthlyRent: n(b.monthlyRent, d.monthlyRent),
      occupancyPct: n(b.occupancyPct, d.occupancyPct),
      managementPct: n(b.managementPct, d.managementPct),
      annualTaxesAndInsurance: n(b.annualTaxesAndInsurance, d.annualTaxesAndInsurance),
      annualRepairReserve: n(b.annualRepairReserve, d.annualRepairReserve),
      propertyValue: n(b.propertyValue, d.propertyValue),
    };
    return c.json({ input, result: simulateRent(input) });
  });
}
