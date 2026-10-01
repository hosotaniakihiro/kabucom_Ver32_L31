import type { Context, Hono } from 'hono';
import { ALLOWED_PHOTO_TYPES, AR_NOTE_STATUSES, MAX_PHOTO_BYTES, validateArNote, validateInspection, type ArNoteStatus, type Building, type LatLng } from '@ie-miru/domain';
import type { AppDeps, AppEnv } from '../app';

export interface UserRouteDeps extends AppDeps {
  findBuilding(id: string, hint: LatLng | null): Promise<Building | null>;
}

const newId = () => (globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`);

/** 利用者データ（保存した家・修繕記録・ARメモ）。匿名端末IDで分離し、他人のデータは見えない。 */
export function registerUserRoutes(app: Hono<AppEnv>, deps: UserRouteDeps) {
  const guard = (c: Context<AppEnv>) => {
    if (!deps.repos) return c.json({ error: 'persistence_not_configured' }, 503);
    if (!c.get('deviceId')) return c.json({ error: 'device_id_required' }, 401);
    return null;
  };
  const now = () => (deps.now?.() ?? new Date()).toISOString();

  // ───── 直す（修繕写真） ─────
  app.post('/v1/inspections', async (c) => {
    const g = guard(c);
    if (g) return g;
    const form = await c.req.formData().catch(() => null);
    if (!form) return c.json({ error: 'multipart_required' }, 400);
    let marks: unknown = [];
    let measurements: unknown = [];
    try {
      marks = JSON.parse(String(form.get('marks') ?? '[]'));
      measurements = JSON.parse(String(form.get('measurements') ?? '[]'));
    } catch {
      return c.json({ error: 'invalid_marks' }, 400);
    }
    const v = validateInspection({ buildingId: form.get('buildingId'), category: form.get('category'), memo: form.get('memo') ?? '', marks, measurements });
    if (!v.ok) return c.json({ error: 'invalid_input', details: v.errors }, 400);
    const file = form.get('photo');
    let photo: { data: Uint8Array; contentType: string } | null = null;
    if (file && typeof file === 'object' && 'arrayBuffer' in file) {
      const f = file as File;
      if (!ALLOWED_PHOTO_TYPES.includes(f.type)) return c.json({ error: 'unsupported_photo_type' }, 415);
      if (f.size > MAX_PHOTO_BYTES) return c.json({ error: 'photo_too_large' }, 413);
      photo = { data: new Uint8Array(await f.arrayBuffer()), contentType: f.type };
    }
    const created = await deps.repos!.inspections.create(c.get('deviceId')!, { id: newId(), photoKey: null, createdAt: now(), ...v.value }, photo);
    return c.json({ inspection: created }, 201);
  });

  app.get('/v1/inspections', async (c) => {
    const g = guard(c);
    if (g) return g;
    return c.json({ inspections: await deps.repos!.inspections.list(c.get('deviceId')!, c.req.query('buildingId') ?? null) });
  });

  app.get('/v1/inspections/:id/photo', async (c) => {
    const g = guard(c);
    if (g) return g;
    const p = await deps.repos!.inspections.photo(c.get('deviceId')!, c.req.param('id'));
    if (!p) return c.json({ error: 'not_found' }, 404);
    return new Response(p.data, { headers: { 'Content-Type': p.contentType, 'Cache-Control': 'private, max-age=3600' } });
  });

  app.delete('/v1/inspections/:id', async (c) => {
    const g = guard(c);
    if (g) return g;
    return (await deps.repos!.inspections.delete(c.get('deviceId')!, c.req.param('id'))) ? c.json({ ok: true }) : c.json({ error: 'not_found' }, 404);
  });

  // ───── ARメモ ─────
  app.post('/v1/ar-notes', async (c) => {
    const g = guard(c);
    if (g) return g;
    const body = (await c.req.json().catch(() => null)) as Record<string, unknown> | null;
    if (!body) return c.json({ error: 'invalid_json' }, 400);
    const v = validateArNote(body);
    if (!v.ok) return c.json({ error: 'invalid_input', details: v.errors }, 400);
    const t = now();
    const note = await deps.repos!.arNotes.create(c.get('deviceId')!, { id: newId(), ...v.value, createdAt: t, updatedAt: t });
    return c.json({ note }, 201);
  });

  app.get('/v1/ar-notes', async (c) => {
    const g = guard(c);
    if (g) return g;
    return c.json({ notes: await deps.repos!.arNotes.list(c.get('deviceId')!, c.req.query('buildingId') ?? null) });
  });

  app.patch('/v1/ar-notes/:id', async (c) => {
    const g = guard(c);
    if (g) return g;
    const body = ((await c.req.json().catch(() => ({}))) ?? {}) as Record<string, unknown>;
    const status = typeof body.status === 'string' && body.status in AR_NOTE_STATUSES ? (body.status as ArNoteStatus) : undefined;
    if (body.status !== undefined && !status) return c.json({ error: 'invalid_status' }, 400);
    const text = typeof body.text === 'string' ? body.text.slice(0, 500) : undefined;
    const n = await deps.repos!.arNotes.update(c.get('deviceId')!, c.req.param('id'), { status, text, updatedAt: now() });
    return n ? c.json({ note: n }) : c.json({ error: 'not_found' }, 404);
  });

  app.delete('/v1/ar-notes/:id', async (c) => {
    const g = guard(c);
    if (g) return g;
    return (await deps.repos!.arNotes.delete(c.get('deviceId')!, c.req.param('id'))) ? c.json({ ok: true }) : c.json({ error: 'not_found' }, 404);
  });

  /** iOS ARWorldMap（NSKeyedArchiver でアーカイブしたバイナリ）。最大 20MB */
  app.put('/v1/ar-notes/:id/world-map', async (c) => {
    const g = guard(c);
    if (g) return g;
    const buf = new Uint8Array(await c.req.arrayBuffer());
    if (buf.byteLength === 0 || buf.byteLength > 20 * 1024 * 1024) return c.json({ error: 'invalid_size' }, 413);
    const key = await deps.repos!.arNotes.putWorldMap(c.get('deviceId')!, c.req.param('id'), buf);
    return key ? c.json({ ok: true }) : c.json({ error: 'not_found' }, 404);
  });

  app.get('/v1/ar-notes/:id/world-map', async (c) => {
    const g = guard(c);
    if (g) return g;
    const data = await deps.repos!.arNotes.getWorldMap(c.get('deviceId')!, c.req.param('id'));
    return data ? new Response(data, { headers: { 'Content-Type': 'application/octet-stream' } }) : c.json({ error: 'not_found' }, 404);
  });
}
