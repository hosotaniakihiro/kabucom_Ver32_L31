import { distanceToPolygonM, makeBuilding, type Building, type LatLng, type SourceRef } from '@ie-miru/domain';
import type { BuildingQueryResult, BuildingSource } from './types';

/**
 * デモ用建物ソース。PLATEAU の URL が未設定の環境（開発・E2E・審査用）で使う。
 * 緯度経度グリッドから決定的に「架空の住宅街」を生成するため、同じ地点では常に同じ ID になる。
 * UI では必ず「デモ建物（実在の建物ではありません）」と表示する。
 */
export const DEMO_SOURCE: Omit<SourceRef, 'fetchedAt'> = {
  id: 'demo.buildings',
  name: 'デモ建物（実在の建物ではありません）',
  url: null,
  license: null,
  mode: 'demo',
};

const D_LAT = 0.00018; // ≒20m
const D_LNG = 0.00022; // ≒20m（北緯35度付近）

function hash(i: number, j: number, salt = 0): number {
  let h = (i * 374761393 + j * 668265263 + salt * 2147483647) | 0;
  h = Math.imul(h ^ (h >>> 13), 1274126177);
  h ^= h >>> 16;
  return (h >>> 0) / 4294967295;
}

const USAGES = [
  { usage: '住宅', code: '411' },
  { usage: '住宅', code: '411' },
  { usage: '住宅', code: '411' },
  { usage: '共同住宅', code: '412' },
  { usage: '店舗等併用住宅', code: '413' },
];
const STRUCTURES = [
  { structure: '木造・土蔵造', code: '601' },
  { structure: '木造・土蔵造', code: '601' },
  { structure: '軽量鉄骨造', code: '605' },
  { structure: '鉄筋コンクリート造', code: '603' },
];

export function demoBuildingAt(i: number, j: number): Building | null {
  // 4 区画ごとに道路（建物なし）
  if (((i % 4) + 4) % 4 === 0 || ((j % 5) + 5) % 5 === 0) return null;
  if (hash(i, j, 1) < 0.08) return null; // 空き地
  const south = i * D_LAT;
  const west = j * D_LNG;
  const insetLat = D_LAT * (0.12 + hash(i, j, 2) * 0.12);
  const insetLng = D_LNG * (0.12 + hash(i, j, 3) * 0.12);
  const u = USAGES[Math.floor(hash(i, j, 4) * USAGES.length)]!;
  const s = u.code === '412' ? STRUCTURES[3]! : STRUCTURES[Math.floor(hash(i, j, 5) * 3)]!;
  const floors = u.code === '412' ? 3 + Math.floor(hash(i, j, 6) * 3) : 1 + Math.floor(hash(i, j, 6) * 3);
  // 属性欠損も再現する（実データでは珍しくない）
  const hasYear = hash(i, j, 7) > 0.35;
  const hasHeight = hash(i, j, 8) > 0.1;
  const year = 1965 + Math.floor(hash(i, j, 9) * 58);
  return makeBuilding({
    id: `demo:${i}_${j}`,
    source: { ...DEMO_SOURCE, fetchedAt: null },
    footprint: [
      { lat: south + insetLat, lng: west + insetLng },
      { lat: south + insetLat, lng: west + D_LNG - insetLng },
      { lat: south + D_LAT - insetLat, lng: west + D_LNG - insetLng },
      { lat: south + D_LAT - insetLat, lng: west + insetLng },
      { lat: south + insetLat, lng: west + insetLng },
    ],
    heightM: hasHeight ? Math.round((floors * 3.1 + 1.2) * 10) / 10 : null,
    floorsAbove: floors,
    floorsBelow: null,
    usage: u.usage,
    usageCode: u.code,
    structure: s.structure,
    structureCode: s.code,
    builtYear: hasYear ? year : null,
    name: null,
    lod: 1,
  });
}

export class DemoBuildingSource implements BuildingSource {
  readonly id = 'demo';

  async findNear(center: LatLng, radiusM: number): Promise<BuildingQueryResult> {
    const di = Math.ceil(radiusM / 20) + 1;
    const ci = Math.floor(center.lat / D_LAT);
    const cj = Math.floor(center.lng / D_LNG);
    const out: Building[] = [];
    for (let i = ci - di; i <= ci + di; i++) {
      for (let j = cj - di; j <= cj + di; j++) {
        const b = demoBuildingAt(i, j);
        if (b && distanceToPolygonM(center, b.footprint) <= radiusM) out.push(b);
      }
    }
    return { buildings: out, sources: [{ ...DEMO_SOURCE, fetchedAt: new Date().toISOString() }], status: 'ok' };
  }

  async getById(id: string): Promise<Building | null> {
    const m = /^demo:(-?\d+)_(-?\d+)$/.exec(id);
    return m ? demoBuildingAt(Number(m[1]), Number(m[2])) : null;
  }
}
