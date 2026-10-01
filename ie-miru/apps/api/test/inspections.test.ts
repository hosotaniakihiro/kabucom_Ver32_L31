import { describe, expect, it } from 'vitest';
import { DEVICE, json, makeApp, makePersistentApp, PNG_1PX } from './helpers';

function form(fields: Record<string, string>, photo?: { data: Uint8Array; type: string }) {
  const f = new FormData();
  for (const [k, v] of Object.entries(fields)) f.set(k, v);
  if (photo) f.set('photo', new File([photo.data as BlobPart], 'p', { type: photo.type }));
  return f;
}

describe('API: inspections (直す)', () => {
  it('creates with photo + marks, lists, serves photo, deletes', async () => {
    const { app, r2 } = await makePersistentApp();
    const res = await json(app.request('/v1/inspections', {
      method: 'POST',
      headers: DEVICE,
      body: form({ buildingId: 'demo:1_1', category: 'exterior_wall', memo: '外壁のひび', marks: JSON.stringify([{ x: 0.4, y: 0.5, r: 0.05, label: 'ひび' }, { x: 1.5, y: -1 }]) }, { data: PNG_1PX, type: 'image/png' }),
    }));
    expect(res.status).toBe(201);
    const ins = res.body.inspection;
    expect(ins).toMatchObject({ buildingId: 'demo:1_1', category: 'exterior_wall', memo: '外壁のひび' });
    expect(ins.marks[1]).toMatchObject({ x: 1, y: 0 }); // clamped
    expect(ins.createdAt).toBe('2026-10-01T03:00:00.000Z');
    expect(r2.objects.size).toBe(1);

    const list = await json(app.request('/v1/inspections?buildingId=demo%3A1_1', { headers: DEVICE }));
    expect(list.body.inspections).toHaveLength(1);

    const photo = await app.request(`/v1/inspections/${ins.id}/photo`, { headers: DEVICE });
    expect(photo.status).toBe(200);
    expect(photo.headers.get('content-type')).toBe('image/png');
    expect(new Uint8Array(await photo.arrayBuffer())).toEqual(PNG_1PX);

    // other devices cannot see it
    const other = { 'X-IeMiru-Device': 'another-device-99' };
    expect((await json(app.request('/v1/inspections', { headers: other }))).body.inspections).toHaveLength(0);
    expect((await app.request(`/v1/inspections/${ins.id}/photo`, { headers: other })).status).toBe(404);

    expect((await app.request(`/v1/inspections/${ins.id}`, { method: 'DELETE', headers: DEVICE })).status).toBe(200);
    expect(r2.objects.size).toBe(0);
  });

  it('validates input', async () => {
    const { app } = await makePersistentApp();
    const bad = await app.request('/v1/inspections', { method: 'POST', headers: DEVICE, body: form({ buildingId: 'x', category: 'kitchen' }) });
    expect(bad.status).toBe(400);
    const type = await app.request('/v1/inspections', { method: 'POST', headers: DEVICE, body: form({ buildingId: 'x', category: 'roof' }, { data: new Uint8Array([1]), type: 'application/pdf' }) });
    expect(type.status).toBe(415);
  });

  it('requires device id and persistence', async () => {
    const { app } = await makePersistentApp();
    expect((await app.request('/v1/inspections')).status).toBe(401);
    expect((await makeApp().request('/v1/inspections', { headers: DEVICE })).status).toBe(503);
  });

  it('works without a photo (memo only)', async () => {
    const { app } = await makePersistentApp();
    const r = await app.request('/v1/inspections', { method: 'POST', headers: DEVICE, body: form({ buildingId: 'demo:1_1', category: 'gutter', memo: '詰まり' }) });
    expect(r.status).toBe(201);
  });
});
