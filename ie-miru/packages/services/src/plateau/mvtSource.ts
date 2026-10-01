import { VectorTile } from '@mapbox/vector-tile';
import { PbfReader } from 'pbf';
import { distanceToPolygonM, makeBuilding, tilesCovering, type Building, type LatLng, type Ring, type SourceRef } from '@ie-miru/domain';
import { fillTemplate, requestBinary, UpstreamError, type FetchLike } from '../http';
import type { BuildingQueryResult, BuildingSource } from '../buildings/types';
import { normalizePlateauAttributes } from './attributes';

export interface PlateauMvtOptions {
  /** 例: https://example.org/plateau/bldg/{z}/{x}/{y}.pbf */
  urlTemplate: string;
  /** 建物が収録されているズーム（多くの配信は 15〜16） */
  zoom?: number;
  /** 建物レイヤ名。未指定ならポリゴンを含む全レイヤ */
  layer?: string | null;
  fetch?: FetchLike;
  timeoutMs?: number;
  attribution?: string;
}

export const PLATEAU_SOURCE_BASE = {
  id: 'plateau.mvt',
  name: '国土交通省 PLATEAU（3D都市モデル）建物',
  url: 'https://www.mlit.go.jp/plateau/',
  license: 'PLATEAU 利用規約（CC BY 4.0 互換）。出典: 国土交通省 Project PLATEAU',
} as const;

/** PLATEAU の建物 MVT（ベクトルタイル）アダプタ */
export class PlateauMvtBuildingSource implements BuildingSource {
  readonly id = 'plateau';
  private readonly zoom: number;
  private readonly cache = new Map<string, Building[]>();

  constructor(private readonly opts: PlateauMvtOptions) {
    this.zoom = opts.zoom ?? 16;
  }

  private sourceRef(): SourceRef {
    return { ...PLATEAU_SOURCE_BASE, mode: 'live', fetchedAt: new Date().toISOString() };
  }

  async loadTile(x: number, y: number, z: number, signal?: AbortSignal): Promise<Building[]> {
    const key = `${z}/${x}/${y}`;
    const hit = this.cache.get(key);
    if (hit) return hit;
    const url = fillTemplate(this.opts.urlTemplate, { z, x, y });
    const bin = await requestBinary(url, { fetch: this.opts.fetch, timeoutMs: this.opts.timeoutMs, sourceId: 'plateau.mvt', signal });
    const out = bin ? decodePlateauTile(bin, x, y, z, this.opts.layer ?? null, this.sourceRef()) : [];
    if (this.cache.size > 256) this.cache.clear();
    this.cache.set(key, out);
    return out;
  }

  async findNear(center: LatLng, radiusM: number, signal?: AbortSignal): Promise<BuildingQueryResult> {
    const tiles = tilesCovering(center, radiusM, this.zoom);
    try {
      const lists = await Promise.all(tiles.map((t) => this.loadTile(t.x, t.y, t.z, signal)));
      const merged = mergeById(lists.flat()).filter((b) => distanceToPolygonM(center, b.footprint) <= radiusM);
      return { buildings: merged, sources: [this.sourceRef()], status: 'ok' };
    } catch (e) {
      return { buildings: [], sources: [this.sourceRef()], status: 'error', error: e instanceof UpstreamError ? e.kind : String(e) };
    }
  }

  async getById(id: string, hint?: LatLng): Promise<Building | null> {
    if (!hint) return null;
    const r = await this.findNear(hint, 150);
    return r.buildings.find((b) => b.id === id) ?? null;
  }
}

/** タイル境界で分割された同一建物は面積最大の断片を採用する */
function mergeById(list: Building[]): Building[] {
  const m = new Map<string, Building>();
  for (const b of list) {
    const prev = m.get(b.id);
    if (!prev || b.footprintAreaM2 > prev.footprintAreaM2) m.set(b.id, b);
  }
  return [...m.values()];
}

export function decodePlateauTile(bin: Uint8Array, x: number, y: number, z: number, layerName: string | null, source: SourceRef): Building[] {
  const tile = new VectorTile(new PbfReader(bin));
  const out: Building[] = [];
  const layers = layerName ? [layerName] : Object.keys(tile.layers);
  for (const ln of layers) {
    const layer = tile.layers[ln];
    if (!layer) continue;
    for (let i = 0; i < layer.length; i++) {
      const f = layer.feature(i);
      if (f.type !== 3) continue; // polygon only
      const gj = f.toGeoJSON(x, y, z);
      const outer = outerRing(gj.geometry);
      if (!outer || outer.length < 4) continue;
      const a = normalizePlateauAttributes(f.properties);
      const rawId = a.id ?? (f.id != null ? String(f.id) : `${z}-${x}-${y}-${ln}-${i}`);
      out.push(
        makeBuilding({
          id: `plateau:${rawId}`,
          source,
          footprint: outer,
          heightM: a.heightM,
          floorsAbove: a.floorsAbove,
          floorsBelow: a.floorsBelow,
          usage: a.usage,
          usageCode: a.usageCode,
          structure: a.structure,
          structureCode: a.structureCode,
          builtYear: a.builtYear,
          name: a.name,
          lod: a.lod,
        }),
      );
    }
  }
  return out;
}

type Geometry = { type: string; coordinates?: unknown };

function outerRing(g: Geometry): Ring | null {
  let coords: number[][] | undefined;
  if (g.type === 'Polygon') coords = (g.coordinates as number[][][])[0];
  else if (g.type === 'MultiPolygon') {
    // 最大の外周を採用
    const polys = g.coordinates as number[][][][];
    coords = polys.map((p) => p[0]!).sort((a, b) => b.length - a.length)[0];
  }
  return coords ? coords.map(([lng, lat]) => ({ lat: lat!, lng: lng! })) : null;
}
