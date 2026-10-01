// @ts-expect-error no types
import geojsonvt from 'geojson-vt';
// @ts-expect-error no types
import vtpbf from 'vt-pbf';
import { lngLatToTile, type LatLng } from '@ie-miru/domain';

/** PLATEAU 建物 MVT 形状のテスト用タイルを生成する（属性名は PLATEAU 配信と同形） */
export function makeBuildingTile(features: Array<{ ring: LatLng[]; props: Record<string, unknown> }>, at: LatLng, z = 16, layer = 'bldg') {
  const fc = {
    type: 'FeatureCollection',
    features: features.map((f) => ({
      type: 'Feature',
      properties: f.props,
      geometry: { type: 'Polygon', coordinates: [f.ring.map((p) => [p.lng, p.lat])] },
    })),
  };
  const idx = geojsonvt(fc as never, { maxZoom: z, indexMaxZoom: z, tolerance: 0, extent: 4096, buffer: 64 });
  const t = lngLatToTile(at, z);
  const tile = idx.getTile(z, t.x, t.y);
  if (!tile) throw new Error('no tile');
  const buf: Uint8Array = vtpbf.fromGeojsonVt({ [layer]: tile }, { version: 2 });
  return { bin: buf, ...t };
}
