import { describe, expect, it } from 'vitest';
import { CompositeBuildingSource, DemoBuildingSource, OverpassBuildingSource, parseOverpass, type BuildingSource } from '../src';

const C = { lat: 35.6466, lng: 139.6532 };

describe('DemoBuildingSource', () => {
  it('is deterministic and labelled demo', async () => {
    const s = new DemoBuildingSource();
    const a = await s.findNear(C, 100);
    const b = await s.findNear({ lat: C.lat + 0.00001, lng: C.lng }, 100);
    expect(a.buildings.length).toBeGreaterThan(5);
    expect(a.sources[0]!.mode).toBe('demo');
    const ids = new Set(b.buildings.map((x) => x.id));
    expect(a.buildings.filter((x) => ids.has(x.id)).length).toBeGreaterThan(a.buildings.length * 0.8);
    const one = a.buildings[0]!;
    expect(await s.getById(one.id)).toEqual(one);
    expect(await s.getById('plateau:x')).toBeNull();
  });
  it('includes buildings with missing attributes', async () => {
    const r = await new DemoBuildingSource().findNear(C, 150);
    expect(r.buildings.some((b) => b.builtYear === null)).toBe(true);
  });
});

describe('Overpass parser', () => {
  it('maps OSM tags', () => {
    const src = { id: 'osm', name: 'OSM', url: null, license: null, mode: 'live' as const, fetchedAt: null };
    const list = parseOverpass(
      [
        { type: 'way', id: 1, tags: { building: 'house', 'building:levels': '2', start_date: '1985' }, geometry: [{ lat: 35, lon: 139 }, { lat: 35, lon: 139.0001 }, { lat: 35.0001, lon: 139.0001 }, { lat: 35, lon: 139 }] },
        { type: 'way', id: 2, tags: { building: 'yes' }, geometry: [{ lat: 35, lon: 139 }] },
        { type: 'node', id: 3 },
      ],
      src,
    );
    expect(list).toHaveLength(1);
    expect(list[0]).toMatchObject({ id: 'osm:way/1', usage: '住宅', floorsAbove: 2, builtYear: 1985, heightM: null });
  });
  it('network failure becomes error status', async () => {
    const s = new OverpassBuildingSource({ fetch: async () => { throw new TypeError('fetch failed'); }, timeoutMs: 20 });
    const r = await s.findNear(C, 50);
    expect(r).toMatchObject({ status: 'error', error: 'network' });
  });
});

describe('CompositeBuildingSource', () => {
  const failing: BuildingSource = { id: 'plateau', findNear: async () => ({ buildings: [], sources: [], status: 'error', error: 'timeout' }), getById: async () => null };
  const empty: BuildingSource = { id: 'osm', findNear: async () => ({ buildings: [], sources: [], status: 'ok' }), getById: async () => null };
  it('falls back to the next source', async () => {
    const r = await new CompositeBuildingSource([failing, empty, new DemoBuildingSource()]).findNear(C, 80);
    expect(r.status).toBe('ok');
    expect(r.buildings[0]!.id.startsWith('demo:')).toBe(true);
  });
  it('all failing -> error', async () => {
    const r = await new CompositeBuildingSource([failing]).findNear(C, 80);
    expect(r.status).toBe('error');
  });
  it('getById routes by namespace', async () => {
    const demo = new DemoBuildingSource();
    const any = (await demo.findNear(C, 50)).buildings[0]!;
    expect((await new CompositeBuildingSource([failing, demo]).getById(any.id))!.id).toBe(any.id);
  });
});
