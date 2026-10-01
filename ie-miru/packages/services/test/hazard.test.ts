import { describe, expect, it } from 'vitest';
import { HazardService, interpretHazardFeature, MockReinfolibClient, type GeoJsonFC, type ReinfolibClient } from '../src';
import { lngLatToTile } from '@ie-miru/domain';
import { tileBbox } from '../src';

const P = { lat: 35.6466, lng: 139.6532 };

function clientWith(map: Partial<Record<string, GeoJsonFC | Error>>): ReinfolibClient {
  return {
    mode: 'live',
    transactions: async () => [],
    tile: async (api) => {
      const v = map[api];
      if (v instanceof Error) throw Object.assign(v, { kind: 'timeout' });
      return v ?? { type: 'FeatureCollection', features: [] };
    },
  };
}
const covering = (props: Record<string, unknown>): GeoJsonFC => {
  const t = lngLatToTile(P, 15);
  const b = tileBbox(t.x, t.y, t.z);
  return { type: 'FeatureCollection', features: [{ type: 'Feature', properties: props, geometry: { type: 'Polygon', coordinates: [[[b.w, b.s], [b.e, b.s], [b.e, b.n], [b.w, b.n], [b.w, b.s]]] } }] };
};
const elsewhere: GeoJsonFC = { type: 'FeatureCollection', features: [{ type: 'Feature', properties: { A31a_205: '5.0m以上' }, geometry: { type: 'Polygon', coordinates: [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]] } }] };

describe('HazardService', () => {
  it('returns all six types with distinct statuses', async () => {
    const svc = new HazardService(clientWith({
      XKT026: covering({ A31a_205: '0.5m以上3.0m未満', river_name: '多摩川' }),
      XKT028: elsewhere,
      XKT027: new Error('timeout'),
      XKT029: covering({ A33_001: '1', A33_002: '2' }),
      XKT025: covering({ liquefaction_tendency_level: '液状化しやすい' }),
    }));
    const res = await svc.check(P, { prefectureCode: '13' });
    const by = Object.fromEntries(res.map((r) => [r.type, r]));
    expect(res).toHaveLength(6);
    expect(by.flood).toMatchObject({ status: 'in_zone', level: '0.5m以上3.0m未満', severity: 2, detail: '対象河川: 多摩川' });
    expect(by.tsunami!.status).toBe('no_data'); // polygon exists but not at this point
    expect(by.storm_surge!.status).toBe('unavailable'); // API failure
    expect(by.landslide).toMatchObject({ status: 'in_zone', level: '土砂災害特別警戒区域（急傾斜地の崩壊）', severity: 4 });
    expect(by.liquefaction).toMatchObject({ status: 'graded', severity: 3 });
    expect(by.inland_flood!.status).toBe('unavailable');
    expect(by.inland_flood!.reason).toContain('内水');
  });

  it('landlocked prefecture -> tsunami/storm surge not applicable', async () => {
    const res = await new HazardService(clientWith({})).check(P, { prefectureCode: '11' });
    const by = Object.fromEntries(res.map((r) => [r.type, r]));
    expect(by.tsunami!.status).toBe('not_applicable');
    expect(by.storm_surge!.status).toBe('not_applicable');
    expect(by.flood!.status).toBe('no_data');
  });

  it('works with the mock client and labels mock', async () => {
    const res = await new HazardService(new MockReinfolibClient()).check(P);
    expect(res.every((r) => r.sources.every((s) => s.mode === 'mock'))).toBe(true);
  });

  it('flood without recognizable depth still in_zone with null severity', () => {
    expect(interpretHazardFeature('flood', { foo: 'bar' })).toMatchObject({ status: 'in_zone', level: null, severity: null });
  });
});
