import { describe, expect, it, vi } from 'vitest';
import { destination, type LatLng } from '@ie-miru/domain';
import { decodePlateauTile, normalizePlateauAttributes, PlateauMvtBuildingSource } from '../src';
import { makeBuildingTile } from './mvtFixture';

const C: LatLng = { lat: 35.6895, lng: 139.6917 };
function square(cx: number, cy: number, s = 10) {
  const c = destination(destination(C, 0, cy), 90, cx);
  const n = destination(c, 0, s / 2).lat, so = destination(c, 180, s / 2).lat;
  const e = destination(c, 90, s / 2).lng, w = destination(c, 270, s / 2).lng;
  return [{ lat: so, lng: w }, { lat: so, lng: e }, { lat: n, lng: e }, { lat: n, lng: w }, { lat: so, lng: w }];
}
const src = { id: 'plateau.mvt', name: 'PLATEAU', url: null, license: null, mode: 'live' as const, fetchedAt: null };

describe('normalizePlateauAttributes', () => {
  it('maps i-UR codes and namespaced keys', () => {
    const a = normalizePlateauAttributes({
      gml_id: 'bldg_123', 'bldg:measuredHeight': 9.8, 'bldg:storeysAboveGround': 3, 'bldg:usage': '411',
      'uro:buildingStructureType': '601', 'bldg:yearOfConstruction': '1998',
    });
    expect(a).toMatchObject({ id: 'bldg_123', heightM: 9.8, floorsAbove: 3, usage: '住宅', usageCode: '411', structure: '木造・土蔵造', structureCode: '601', builtYear: 1998 });
  });
  it('accepts Japanese keys (CSV / attribute table style)', () => {
    const a = normalizePlateauAttributes({ 計測高さ: '12.5', 地上階数: '4', 用途: '共同住宅', 構造種別: '鉄筋コンクリート造', 建築年: '2005年' });
    expect(a).toMatchObject({ heightM: 12.5, floorsAbove: 4, usage: '共同住宅', structure: '鉄筋コンクリート造', builtYear: 2005 });
  });
  it('treats missing / sentinel values as null, never 0', () => {
    const a = normalizePlateauAttributes({ measuredHeight: -9999, storeysAboveGround: 9999, yearOfConstruction: 0, usage: '461', buildingStructureType: '611' });
    expect(a).toMatchObject({ heightM: null, floorsAbove: null, builtYear: null, usage: null, structure: null });
    const b = normalizePlateauAttributes({});
    expect(Object.values(b).every((v) => v === null)).toBe(true);
  });
});

describe('PLATEAU MVT decode', () => {
  it('decodes polygons with attributes and tolerates missing ones', () => {
    const { bin, x, y, z } = makeBuildingTile(
      [
        { ring: square(0, 20), props: { gml_id: 'A', measuredHeight: 7.1, storeysAboveGround: 2, usage: '411', yearOfConstruction: 1990 } },
        { ring: square(30, 0), props: { gml_id: 'B' } },
      ],
      C,
    );
    const list = decodePlateauTile(bin, x, y, z, null, src);
    expect(list).toHaveLength(2);
    const a = list.find((b) => b.id === 'plateau:A')!;
    expect(a).toMatchObject({ heightM: 7.1, floorsAbove: 2, usage: '住宅', builtYear: 1990 });
    expect(a.footprintAreaM2).toBeGreaterThan(80);
    expect(a.footprintAreaM2).toBeLessThan(120);
    const b = list.find((b) => b.id === 'plateau:B')!;
    expect(b).toMatchObject({ heightM: null, floorsAbove: null, usage: null, builtYear: null });
  });

  it('source fetches tiles via URL template and filters by radius', async () => {
    const { bin, x, y } = makeBuildingTile([{ ring: square(0, 20), props: { gml_id: 'A' } }, { ring: square(0, 140), props: { gml_id: 'FAR' } }], C);
    const fetch = vi.fn(async (url: string) => {
      expect(url).toMatch(/^https:\/\/tiles\.example\/16\/\d+\/\d+\.pbf$/);
      if (url !== `https://tiles.example/16/${x}/${y}.pbf`) return new Response(null, { status: 404 });
      return new Response(bin as unknown as BodyInit, { status: 200 });
    });
    const s = new PlateauMvtBuildingSource({ urlTemplate: 'https://tiles.example/{z}/{x}/{y}.pbf', fetch });
    const r = await s.findNear(C, 100);
    expect(r.status).toBe('ok');
    expect(r.buildings.map((b) => b.id)).toEqual(['plateau:A']);
    expect(r.sources[0]!.mode).toBe('live');
    // cache: second call does not refetch
    const n = fetch.mock.calls.length;
    await s.findNear(C, 100);
    expect(fetch.mock.calls.length).toBe(n);
    expect((await s.getById('plateau:A', C))!.id).toBe('plateau:A');
  });

  it('missing tile (404) is an empty, successful result', async () => {
    const s = new PlateauMvtBuildingSource({ urlTemplate: 'https://t/{z}/{x}/{y}', fetch: async () => new Response(null, { status: 404 }) });
    const r = await s.findNear(C, 50);
    expect(r).toMatchObject({ status: 'ok', buildings: [] });
  });

  it('API failure is reported as error (確認できず), not as zero buildings found', async () => {
    const s = new PlateauMvtBuildingSource({ urlTemplate: 'https://t/{z}/{x}/{y}', fetch: async () => new Response('x', { status: 503 }), timeoutMs: 50 });
    const r = await s.findNear(C, 50);
    expect(r.status).toBe('error');
    expect(r.error).toBe('http');
  });
});
