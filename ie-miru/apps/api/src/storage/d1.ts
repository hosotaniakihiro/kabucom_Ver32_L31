import type { Inspection } from '@ie-miru/domain';
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
  };
}
