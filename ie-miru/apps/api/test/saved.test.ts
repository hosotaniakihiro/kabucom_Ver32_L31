import { describe, expect, it } from 'vitest';
import { DEMO_CENTER, DEVICE, json, makePersistentApp } from './helpers';

const req = (app: any, path: string, method: string, body?: unknown, headers: Record<string, string> = DEVICE) =>
  app.request(path, { method, headers: { 'Content-Type': 'application/json', ...headers }, body: body === undefined ? undefined : JSON.stringify(body) });

describe('API: saved buildings (保存した家)', () => {
  it('save, list, update status/nickname, delete', async () => {
    const { app } = await makePersistentApp();
    const { body: near } = await json(app.request(`/v1/buildings/nearby?lat=${DEMO_CENTER.lat}&lng=${DEMO_CENTER.lng}&radius=60`));
    const id = near.buildings[0].id;
    const s = await json(req(app, '/v1/saved', 'POST', { buildingId: id, nickname: '駅前の家', status: 'interested' }));
    expect(s.status).toBe(201);
    expect(s.body.saved).toMatchObject({ buildingId: id, nickname: '駅前の家', status: 'interested' });
    expect(s.body.saved.snapshot.lat).toBeCloseTo(near.buildings[0].centroid.lat, 6);

    // idempotent save (upsert)
    await req(app, '/v1/saved', 'POST', { buildingId: id, nickname: '駅前の家', status: 'watching' });
    const list = await json(req(app, '/v1/saved', 'GET'));
    expect(list.body.saved).toHaveLength(1);
    expect(list.body.saved[0].status).toBe('watching');

    const u = await json(req(app, `/v1/saved/${encodeURIComponent(id)}`, 'PATCH', { status: 'family_home', nickname: '実家' }));
    expect(u.body.saved).toMatchObject({ status: 'family_home', nickname: '実家' });
    expect((await req(app, `/v1/saved/${encodeURIComponent(id)}`, 'PATCH', { status: 'mansion' })).status).toBe(400);

    // other device sees nothing
    expect((await json(req(app, '/v1/saved', 'GET', undefined, { 'X-IeMiru-Device': 'other-device-77' }))).body.saved).toHaveLength(0);

    expect((await req(app, `/v1/saved/${encodeURIComponent(id)}`, 'DELETE')).status).toBe(200);
    expect((await json(req(app, '/v1/saved', 'GET'))).body.saved).toHaveLength(0);
  });

  it('all six statuses are accepted, unknown rejected', async () => {
    const { app } = await makePersistentApp();
    for (const st of ['interested', 'own', 'family_home', 'selling', 'renovating', 'watching']) {
      expect((await req(app, '/v1/saved', 'POST', { buildingId: `demo:9_${st.length}`, status: st })).status).toBe(201);
    }
    expect((await req(app, '/v1/saved', 'POST', { buildingId: 'demo:1_1', status: 'rich' })).status).toBe(400);
  });

  it('default nickname and status', async () => {
    const { app } = await makePersistentApp();
    const s = await json(req(app, '/v1/saved', 'POST', { buildingId: 'demo:1_1' }));
    expect(s.body.saved).toMatchObject({ nickname: '保存した家', status: 'interested' });
  });
});
