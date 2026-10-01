import { describe, expect, it } from 'vitest';
import { DEMO_CENTER, DEVICE, json, makeApp, NOW } from './helpers';

const fix = (over: Record<string, unknown> = {}) => ({ latitude: DEMO_CENTER.lat, longitude: DEMO_CENTER.lng, horizontalAccuracy: 8, altitude: null, heading: 0, headingAccuracy: 10, timestamp: NOW().getTime() - 500, ...over });
const pose = (heading: number, over: Record<string, unknown> = {}) => ({ heading, pitch: 0, roll: 0, headingAccuracy: 10, source: 'device_orientation', ...over });
const post = (app: ReturnType<typeof makeApp>, path: string, body: unknown) =>
  app.request(path, { method: 'POST', headers: { 'Content-Type': 'application/json', ...DEVICE }, body: JSON.stringify(body) });

describe('API: health', () => {
  it('reports modes', async () => {
    const { body } = await json(makeApp().request('/v1/health'));
    expect(body).toMatchObject({ ok: true, name: 'ie-miru', modes: { buildings: 'demo', realEstate: 'mock' } });
  });
});

describe('API: candidates', () => {
  it('returns 2-5 candidates with the confirm question', async () => {
    const { status, body } = await json(post(makeApp(), '/v1/candidates', { fix: fix(), pose: pose(0) }));
    expect(status).toBe(200);
    expect(body.assessment.usable).toBe(true);
    expect(body.selection.candidates.length).toBeGreaterThanOrEqual(2);
    expect(body.selection.candidates.length).toBeLessThanOrEqual(5);
    expect(['suggest', 'choose']).toContain(body.selection.mode);
    expect(body.sources[0].mode).toBe('demo');
  });
  it('poor GPS -> manual message, distance order, heading ignored', async () => {
    const { body } = await json(post(makeApp(), '/v1/candidates', { fix: fix({ horizontalAccuracy: 90 }), pose: pose(0) }));
    expect(body.assessment.usable).toBe(false);
    expect(body.selection.mode).toBe('manual');
    expect(body.selection.message).toBe('現在地の精度が低いため、建物を手動選択してください。');
    expect(body.selection.usedHeading).toBe(false);
  });
  it('permission denied does not fail', async () => {
    const { status, body } = await json(post(makeApp(), '/v1/candidates', { permission: 'denied' }));
    expect(status).toBe(200);
    expect(body.assessment.reason).toBe('permission_denied');
    expect(body.selection).toBeNull();
  });
  it('invalid JSON -> 400', async () => {
    const r = await makeApp().request('/v1/candidates', { method: 'POST', body: '{', headers: { 'Content-Type': 'application/json' } });
    expect(r.status).toBe(400);
  });
  it('missing pose -> manual', async () => {
    const { body } = await json(post(makeApp(), '/v1/candidates', { fix: fix() }));
    expect(body.selection.mode).toBe('manual');
  });
});

describe('API: buildings', () => {
  it('nearby validates input', async () => {
    expect((await makeApp().request('/v1/buildings/nearby?lat=abc&lng=1')).status).toBe(400);
  });
  it('nearby + report for a building', async () => {
    const app = makeApp();
    const { body: near } = await json(app.request(`/v1/buildings/nearby?lat=${DEMO_CENTER.lat}&lng=${DEMO_CENTER.lng}&radius=80`));
    expect(near.buildings.length).toBeGreaterThan(0);
    const b = near.buildings[0];
    const { status, body: rep } = await json(app.request(`/v1/buildings/${encodeURIComponent(b.id)}/report`));
    expect(status).toBe(200);
    expect(rep.building.id).toBe(b.id);
    expect(rep.zoning.status).toBe('available');
    expect(rep.landPrices.status).toBe('available');
    expect(rep.transactions.status).toBe('available');
    expect(rep.hazards).toHaveLength(6);
    expect(rep.dataMode).toBe('mixed');
    expect(rep.disclaimers.join()).toContain('この建物そのものの売買価格ではありません');
    // building endpoint
    const { body: one } = await json(app.request(`/v1/buildings/${encodeURIComponent(b.id)}`));
    expect(one.id).toBe(b.id);
  });
  it('unknown building -> 404', async () => {
    expect((await makeApp().request('/v1/buildings/plateau%3Anope/report')).status).toBe(404);
  });
  it('report for demo building resolves without cache (fresh isolate)', async () => {
    const { body: near } = await json(makeApp().request(`/v1/buildings/nearby?lat=${DEMO_CENTER.lat}&lng=${DEMO_CENTER.lng}`));
    const r = await makeApp().request(`/v1/buildings/${encodeURIComponent(near.buildings[0].id)}/report`);
    expect(r.status).toBe(200);
  });
});

describe('API: report includes valuation range and comparables', () => {
  it('valuation is a range with disclaimer, comparables are labelled reference', async () => {
    const app = makeApp();
    const { body: near } = await json(app.request(`/v1/buildings/nearby?lat=${DEMO_CENTER.lat}&lng=${DEMO_CENTER.lng}&radius=80`));
    const { body: rep } = await json(app.request(`/v1/buildings/${encodeURIComponent(near.buildings[0].id)}/report`));
    expect(rep.valuation.status).toBe('available');
    expect(rep.valuation.kind).toBe('ai_estimate');
    expect(rep.valuation.value.estimatedLow).toBeLessThan(rep.valuation.value.estimatedHigh);
    expect(rep.valuation.note).toContain('正式な不動産鑑定ではありません');
    expect(rep.comparables.kind).toBe('reference');
    expect(rep.subject.kind).toBe('ai_estimate');
  });
});
