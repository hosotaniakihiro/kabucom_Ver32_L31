import type { ArNote, Inspection } from '@ie-miru/domain';
import type { BuildingReport } from '@ie-miru/services';
import type { D1Like, R2Like } from './types';
import type { Repositories } from './repositories';

const j = <T>(s: unknown, d: T): T => {
  try {
    return typeof s === 'string' ? (JSON.parse(s) as T) : d;
  } catch {
    return d;
  }
};

type InspectionRow = { id: string; building_id: string; category: string; photo_key: string | null; marks: string; memo: string; measurements: string; created_at: string };
const toInspection = (r: InspectionRow): Inspection => ({
  id: r.id,
  buildingId: r.building_id,
  category: r.category as Inspection['category'],
  photoKey: r.photo_key,
  marks: j(r.marks, []),
  memo: r.memo,
  measurements: j(r.measurements, []),
  createdAt: r.created_at,
});

type ArNoteRow = { id: string; building_id: string; status: string; text: string; found_on: string; anchor: string; world_map_key: string | null; created_at: string; updated_at: string };
const toArNote = (r: ArNoteRow): ArNote => {
  const anchor = j<ArNote['anchor']>(r.anchor, null as never);
  return {
    id: r.id,
    buildingId: r.building_id,
    status: r.status as ArNote['status'],
    text: r.text,
    foundOn: r.found_on,
    anchor: r.world_map_key && anchor ? { ...anchor, worldMap: anchor.worldMap ? { ...anchor.worldMap, key: r.world_map_key } : anchor.worldMap } : anchor,
    createdAt: r.created_at,
    updatedAt: r.updated_at,
  };
};

export function createRepositories(db: D1Like, photos: R2Like | null): Repositories {
  return {
    analysis: {
      async saveReport(_r: BuildingReport) {
        /* Phase 19 */
      },
    },
    inspections: {
      async create(deviceId, i, photo) {
        let key: string | null = null;
        if (photo) {
          if (!photos) throw new Error('photo storage (R2) is not configured');
          key = `inspections/${deviceId}/${i.id}`;
          await photos.put(key, photo.data, { httpMetadata: { contentType: photo.contentType } });
        }
        await db
          .prepare('INSERT INTO inspections (id, device_id, building_id, category, photo_key, marks, memo, measurements, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)')
          .bind(i.id, deviceId, i.buildingId, i.category, key, JSON.stringify(i.marks), i.memo, JSON.stringify(i.measurements), i.createdAt)
          .run();
        return { ...i, photoKey: key };
      },
      async list(deviceId, buildingId) {
        const stmt = buildingId
          ? db.prepare('SELECT * FROM inspections WHERE device_id = ? AND building_id = ? ORDER BY created_at DESC LIMIT 200').bind(deviceId, buildingId)
          : db.prepare('SELECT * FROM inspections WHERE device_id = ? ORDER BY created_at DESC LIMIT 200').bind(deviceId);
        return (await stmt.all<InspectionRow>()).results.map(toInspection);
      },
      async get(deviceId, id) {
        const r = await db.prepare('SELECT * FROM inspections WHERE device_id = ? AND id = ?').bind(deviceId, id).first<InspectionRow>();
        return r ? toInspection(r) : null;
      },
      async photo(deviceId, id) {
        const r = await db.prepare('SELECT photo_key FROM inspections WHERE device_id = ? AND id = ?').bind(deviceId, id).first<{ photo_key: string | null }>();
        if (!r?.photo_key || !photos) return null;
        const o = await photos.get(r.photo_key);
        return o ? { data: await o.arrayBuffer(), contentType: o.httpMetadata?.contentType ?? 'application/octet-stream' } : null;
      },
      async delete(deviceId, id) {
        const r = await db.prepare('SELECT photo_key FROM inspections WHERE device_id = ? AND id = ?').bind(deviceId, id).first<{ photo_key: string | null }>();
        if (!r) return false;
        if (r.photo_key && photos) await photos.delete(r.photo_key);
        await db.prepare('DELETE FROM inspections WHERE device_id = ? AND id = ?').bind(deviceId, id).run();
        return true;
      },
    },
    arNotes: {
      async create(deviceId, n) {
        await db
          .prepare('INSERT INTO ar_notes (id, device_id, building_id, status, text, found_on, anchor, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)')
          .bind(n.id, deviceId, n.buildingId, n.status, n.text, n.foundOn, JSON.stringify(n.anchor), n.createdAt, n.updatedAt)
          .run();
        return n;
      },
      async list(deviceId, buildingId) {
        const stmt = buildingId
          ? db.prepare('SELECT * FROM ar_notes WHERE device_id = ? AND building_id = ? ORDER BY updated_at DESC LIMIT 500').bind(deviceId, buildingId)
          : db.prepare('SELECT * FROM ar_notes WHERE device_id = ? ORDER BY updated_at DESC LIMIT 500').bind(deviceId);
        return (await stmt.all<ArNoteRow>()).results.map(toArNote);
      },
      async update(deviceId, id, patch) {
        const cur = await db.prepare('SELECT * FROM ar_notes WHERE device_id = ? AND id = ?').bind(deviceId, id).first<ArNoteRow>();
        if (!cur) return null;
        await db
          .prepare('UPDATE ar_notes SET status = ?, text = ?, updated_at = ? WHERE device_id = ? AND id = ?')
          .bind(patch.status ?? cur.status, patch.text ?? cur.text, patch.updatedAt, deviceId, id)
          .run();
        return toArNote({ ...cur, status: patch.status ?? cur.status, text: patch.text ?? cur.text, updated_at: patch.updatedAt });
      },
      async delete(deviceId, id) {
        const cur = await db.prepare('SELECT world_map_key FROM ar_notes WHERE device_id = ? AND id = ?').bind(deviceId, id).first<{ world_map_key: string | null }>();
        if (!cur) return false;
        if (cur.world_map_key && photos) await photos.delete(cur.world_map_key);
        await db.prepare('DELETE FROM ar_notes WHERE device_id = ? AND id = ?').bind(deviceId, id).run();
        return true;
      },
      async putWorldMap(deviceId, id, data) {
        if (!photos) return null;
        const cur = await db.prepare('SELECT id FROM ar_notes WHERE device_id = ? AND id = ?').bind(deviceId, id).first();
        if (!cur) return null;
        const key = `ar-worldmaps/${deviceId}/${id}`;
        await photos.put(key, data, { httpMetadata: { contentType: 'application/octet-stream' } });
        await db.prepare('UPDATE ar_notes SET world_map_key = ? WHERE device_id = ? AND id = ?').bind(key, deviceId, id).run();
        return key;
      },
      async getWorldMap(deviceId, id) {
        const cur = await db.prepare('SELECT world_map_key FROM ar_notes WHERE device_id = ? AND id = ?').bind(deviceId, id).first<{ world_map_key: string | null }>();
        if (!cur?.world_map_key || !photos) return null;
        const o = await photos.get(cur.world_map_key);
        return o ? o.arrayBuffer() : null;
      },
    },
  };
}
