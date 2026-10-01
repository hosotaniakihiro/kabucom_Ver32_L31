import { describe, expect, it } from 'vitest';
import { createAnchor } from '@ie-miru/domain';
import { DEVICE, json, makePersistentApp } from './helpers';

const anchor = createAnchor({ position: { lat: 35.6466, lng: 139.6532 }, accuracyM: 6, pose: { heading: 10, pitch: 5, roll: 0, headingAccuracy: 8 }, building: null });
const req = (app: any, path: string, method: string, body?: unknown, headers: Record<string, string> = DEVICE) =>
  app.request(path, { method, headers: { 'Content-Type': 'application/json', ...headers }, body: body === undefined ? undefined : JSON.stringify(body) });

describe('API: AR notes', () => {
  it('create, list, update status, world map, delete', async () => {
    const { app } = await makePersistentApp();
    const c = await json(req(app, '/v1/ar-notes', 'POST', { buildingId: 'demo:1_1', status: 'needs_repair', text: '外壁のひび', foundOn: '2026-10-02', anchor }));
    expect(c.status).toBe(201);
    const id = c.body.note.id;
    expect(c.body.note.anchor.gps.lat).toBe(35.6466);

    const l = await json(req(app, '/v1/ar-notes?buildingId=demo%3A1_1', 'GET'));
    expect(l.body.notes).toHaveLength(1);
    expect(l.body.notes[0].foundOn).toBe('2026-10-02');

    const u = await json(req(app, `/v1/ar-notes/${id}`, 'PATCH', { status: 'done' }));
    expect(u.body.note.status).toBe('done');
    expect((await req(app, `/v1/ar-notes/${id}`, 'PATCH', { status: 'weird' })).status).toBe(400);

    const wm = await app.request(`/v1/ar-notes/${id}/world-map`, { method: 'PUT', headers: DEVICE, body: new Uint8Array([1, 2, 3]) });
    expect(wm.status).toBe(200);
    const got = await app.request(`/v1/ar-notes/${id}/world-map`, { headers: DEVICE });
    expect(new Uint8Array(await got.arrayBuffer())).toEqual(new Uint8Array([1, 2, 3]));

    // isolation
    expect((await json(req(app, '/v1/ar-notes', 'GET', undefined, { 'X-IeMiru-Device': 'someone-else-1' }))).body.notes).toHaveLength(0);
    expect((await req(app, `/v1/ar-notes/${id}`, 'DELETE')).status).toBe(200);
    expect((await req(app, `/v1/ar-notes/${id}`, 'DELETE')).status).toBe(404);
  });

  it('rejects invalid notes', async () => {
    const { app } = await makePersistentApp();
    const r = await json(req(app, '/v1/ar-notes', 'POST', { buildingId: 'b', status: 'needs_repair', foundOn: 'yesterday', anchor: {} }));
    expect(r.status).toBe(400);
    expect(r.body.details.length).toBe(2);
  });
});
