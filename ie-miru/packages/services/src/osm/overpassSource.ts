import { distanceToPolygonM, makeBuilding, type Building, type LatLng, type SourceRef } from '@ie-miru/domain';
import { requestJson, UpstreamError, type FetchLike } from '../http';
import type { BuildingQueryResult, BuildingSource } from '../buildings/types';
import { toNumberOrNull } from '../plateau/attributes';

/**
 * OpenStreetMap（Overpass API）の建物輪郭。PLATEAU 未整備地域のフォールバック（既定 OFF）。
 * Overpass の利用ポリシーに従い、半径を小さく・頻度を抑えて使う（API 側でキャッシュ）。
 */
export const OSM_SOURCE_BASE = {
  id: 'osm.overpass',
  name: 'OpenStreetMap 建物輪郭',
  url: 'https://www.openstreetmap.org/copyright',
  license: '© OpenStreetMap contributors (ODbL)',
} as const;

interface OverpassElement {
  type: string;
  id: number;
  tags?: Record<string, string>;
  geometry?: Array<{ lat: number; lon: number }>;
}

export class OverpassBuildingSource implements BuildingSource {
  readonly id = 'osm';
  constructor(private readonly opts: { endpoint?: string; fetch?: FetchLike; timeoutMs?: number } = {}) {}

  private ref(): SourceRef {
    return { ...OSM_SOURCE_BASE, mode: 'live', fetchedAt: new Date().toISOString() };
  }

  async findNear(center: LatLng, radiusM: number, signal?: AbortSignal): Promise<BuildingQueryResult> {
    const r = Math.min(200, Math.round(radiusM));
    const q = `[out:json][timeout:10];way["building"](around:${r},${center.lat},${center.lng});out geom tags;`;
    const url = `${this.opts.endpoint ?? 'https://overpass-api.de/api/interpreter'}?data=${encodeURIComponent(q)}`;
    try {
      const json = await requestJson<{ elements?: OverpassElement[] }>(url, { fetch: this.opts.fetch, timeoutMs: this.opts.timeoutMs, sourceId: 'osm.overpass', signal });
      const buildings = parseOverpass(json?.elements ?? [], this.ref()).filter((b) => distanceToPolygonM(center, b.footprint) <= radiusM);
      return { buildings, sources: [this.ref()], status: 'ok' };
    } catch (e) {
      return { buildings: [], sources: [this.ref()], status: 'error', error: e instanceof UpstreamError ? e.kind : String(e) };
    }
  }

  async getById(id: string, hint?: LatLng): Promise<Building | null> {
    if (!hint) return null;
    const r = await this.findNear(hint, 150);
    return r.buildings.find((b) => b.id === id) ?? null;
  }
}

const OSM_USAGE: Record<string, string> = {
  house: '住宅', detached: '住宅', residential: '住宅', apartments: '共同住宅', commercial: '商業施設', retail: '商業施設',
  office: '業務施設', industrial: '工場', warehouse: '運輸倉庫施設', school: '文教厚生施設', hospital: '文教厚生施設',
};

export function parseOverpass(elements: OverpassElement[], source: SourceRef): Building[] {
  const out: Building[] = [];
  for (const el of elements) {
    if (el.type !== 'way' || !el.geometry || el.geometry.length < 4) continue;
    const t = el.tags ?? {};
    const levels = toNumberOrNull(t['building:levels'], { min: 1, max: 300 });
    const year = /(\d{4})/.exec(t['start_date'] ?? '');
    out.push(
      makeBuilding({
        id: `osm:way/${el.id}`,
        source,
        footprint: el.geometry.map((g) => ({ lat: g.lat, lng: g.lon })),
        heightM: toNumberOrNull(t['height'], { min: 0.5, max: 700 }),
        floorsAbove: levels == null ? null : Math.round(levels),
        floorsBelow: toNumberOrNull(t['building:levels:underground'], { min: 0, max: 20 }),
        usage: t['building'] && t['building'] !== 'yes' ? (OSM_USAGE[t['building']] ?? null) : null,
        usageCode: null,
        structure: null,
        structureCode: null,
        builtYear: year ? Number(year[1]) : null,
        // 公開施設名のみ（個人宅の表札等は OSM 規約上も登録されない想定）
        name: t['name'] ?? null,
        lod: null,
      }),
    );
  }
  return out;
}
