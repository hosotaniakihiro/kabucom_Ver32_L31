import type { ArNote, Building, Inspection, SavedBuilding } from '@ie-miru/domain';
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

type SavedRow = { building_id: string; nickname: string; status: string; snapshot: string | null; created_at: string; updated_at: string };
const toSaved = (r: SavedRow): SavedBuilding => ({
  buildingId: r.building_id,
  nickname: r.nickname,
  status: r.status as SavedBuilding['status'],
  snapshot: j(r.snapshot, null),
  createdAt: r.created_at,
  updatedAt: r.updated_at,
});

export function createRepositories(db: D1Like, photos: R2Like | null): Repositories {
  return {
    analysis: {
      async saveReport(r: BuildingReport) {
        const b = r.building;
        const now = r.generatedAt;
        const { footprint, source, centroid, ...attrs } = b;
        const v = r.valuation.status === 'available' ? r.valuation.value : null;
        const z = r.zoning.status === 'available' ? r.zoning.value : null;
        const stmts = [
          db
            .prepare(
              `INSERT INTO buildings (id, source_id, source_mode, centroid_lat, centroid_lng, footprint, attributes, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (id) DO UPDATE SET source_id = excluded.source_id, source_mode = excluded.source_mode, centroid_lat = excluded.centroid_lat, centroid_lng = excluded.centroid_lng, footprint = excluded.footprint, attributes = excluded.attributes, updated_at = excluded.updated_at`,
            )
            .bind(b.id, source.id, source.mode, centroid.lat, centroid.lng, JSON.stringify(footprint), JSON.stringify({ ...attrs, source }), now),
          db
            .prepare(
              `INSERT INTO property_analysis (building_id, generated_at, data_mode, valuation_low, valuation_mid, valuation_high, valuation_confidence, comparables_count, use_district, coverage_ratio_pct, floor_area_ratio_pct, report_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (building_id) DO UPDATE SET generated_at = excluded.generated_at, data_mode = excluded.data_mode, valuation_low = excluded.valuation_low, valuation_mid = excluded.valuation_mid, valuation_high = excluded.valuation_high, valuation_confidence = excluded.valuation_confidence, comparables_count = excluded.comparables_count, use_district = excluded.use_district, coverage_ratio_pct = excluded.coverage_ratio_pct, floor_area_ratio_pct = excluded.floor_area_ratio_pct, report_json = excluded.report_json`,
            )
            .bind(
              b.id, now, r.dataMode, v?.estimatedLow ?? null, v?.estimatedMid ?? null, v?.estimatedHigh ?? null, v?.confidence ?? null,
              r.comparables.status === 'available' ? r.comparables.value.count : null, z?.useDistrict ?? null, z?.coverageRatioPct ?? null, z?.floorAreaRatioPct ?? null, JSON.stringify(r),
            ),
        ];
        const uniqueSources = new Map(
          [b.source, ...r.zoning.sources, ...r.landPrices.sources, ...r.transactions.sources, ...r.hazards.flatMap((h) => h.sources)].map((x) => [x.id, x]),
        );
        for (const sr of uniqueSources.values()) {
          stmts.push(
            db
              .prepare(
                `INSERT INTO building_sources (building_id, source_id, source_name, mode, url, license, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?)
                 ON CONFLICT (building_id, source_id) DO UPDATE SET source_name = excluded.source_name, mode = excluded.mode, url = excluded.url, license = excluded.license, fetched_at = excluded.fetched_at`,
              )
              .bind(b.id, sr.id, sr.name, sr.mode, sr.url, sr.license, sr.fetchedAt),
          );
        }
        for (const h of r.hazards) {
          stmts.push(
            db
              .prepare(
                `INSERT INTO hazards (building_id, hazard_type, status, level, severity, source_ids, checked_at) VALUES (?, ?, ?, ?, ?, ?, ?)
                 ON CONFLICT (building_id, hazard_type) DO UPDATE SET status = excluded.status, level = excluded.level, severity = excluded.severity, source_ids = excluded.source_ids, checked_at = excluded.checked_at`,
              )
              .bind(b.id, h.type, h.status, h.level, h.severity, JSON.stringify(h.sources.map((x) => `${x.id}:${x.mode}`)), now),
          );
        }
        await db.batch(stmts);
      },
      async getReport(buildingId, maxAgeMs, now) {
        const row = await db.prepare('SELECT generated_at, report_json FROM property_analysis WHERE building_id = ?').bind(buildingId).first<{ generated_at: string; report_json: string }>();
        if (!row) return null;
        if (now.getTime() - Date.parse(row.generated_at) > maxAgeMs) return null;
        return j<BuildingReport | null>(row.report_json, null);
      },
      async getBuilding(buildingId) {
        const row = await db.prepare('SELECT * FROM buildings WHERE id = ?').bind(buildingId).first<{ id: string; centroid_lat: number; centroid_lng: number; footprint: string; attributes: string }>();
        if (!row) return null;
        const attrs = j<Record<string, unknown>>(row.attributes, {});
        return { ...(attrs as object), id: row.id, centroid: { lat: row.centroid_lat, lng: row.centroid_lng }, footprint: j(row.footprint, []) } as unknown as Building;
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
    saved: {
      async upsert(deviceId, sv, now) {
        await db
          .prepare(
            `INSERT INTO saved_buildings (device_id, building_id, nickname, status, snapshot, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)
             ON CONFLICT (device_id, building_id) DO UPDATE SET nickname = excluded.nickname, status = excluded.status, snapshot = COALESCE(excluded.snapshot, saved_buildings.snapshot), updated_at = excluded.updated_at`,
          )
          .bind(deviceId, sv.buildingId, sv.nickname, sv.status, sv.snapshot ? JSON.stringify(sv.snapshot) : null, now, now)
          .run();
        return (await this.get(deviceId, sv.buildingId))!;
      },
      async list(deviceId) {
        return (await db.prepare('SELECT * FROM saved_buildings WHERE device_id = ? ORDER BY updated_at DESC LIMIT 500').bind(deviceId).all<SavedRow>()).results.map(toSaved);
      },
      async get(deviceId, buildingId) {
        const r = await db.prepare('SELECT * FROM saved_buildings WHERE device_id = ? AND building_id = ?').bind(deviceId, buildingId).first<SavedRow>();
        return r ? toSaved(r) : null;
      },
      async delete(deviceId, buildingId) {
        const r = await db.prepare('DELETE FROM saved_buildings WHERE device_id = ? AND building_id = ?').bind(deviceId, buildingId).run();
        return (r.meta?.changes ?? 0) > 0;
      },
    },
  };
}
