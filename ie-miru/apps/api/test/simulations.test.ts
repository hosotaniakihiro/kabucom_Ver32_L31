import { describe, expect, it } from 'vitest';
import { json, makeApp } from './helpers';

const post = (path: string, body: unknown) => makeApp().request(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });

describe('API: simulations', () => {
  it('buy uses reference price as default and accepts overrides', async () => {
    const { body } = await json(post('/v1/simulations/buy', { referencePrice: 40_000_000, renovationBudget: 3_000_000, overrides: { brokerage: 0 } }));
    expect(body.input.purchasePrice).toBe(40_000_000);
    expect(body.result.costs.find((c: any) => c.key === 'brokerage')).toMatchObject({ amount: 0, estimated: false });
    expect(body.result.totalAcquisition).toBe(40_000_000 + body.result.totalCosts + 3_000_000);
  });
  it('buy with garbage input does not fail', async () => {
    const { status } = await json(post('/v1/simulations/buy', { purchasePrice: 'x' }));
    expect(status).toBe(200);
  });
});
