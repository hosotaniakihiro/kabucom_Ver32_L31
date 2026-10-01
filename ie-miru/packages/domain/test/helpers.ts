import { destination, makeBuilding, type Building, type LatLng, type Ring, type SourceRef } from '../src';

export const ORIGIN: LatLng = { lat: 35.68, lng: 139.76 };
export const TEST_SOURCE: SourceRef = { id: 'test', name: 'テスト', url: null, license: null, mode: 'demo', fetchedAt: null };

/** 原点から 東 cx[m], 北 cy[m] を中心にした w×h の矩形 */
export function rect(cx: number, cy: number, w: number, h: number, origin: LatLng = ORIGIN): Ring {
  const c = destination(destination(origin, 0, cy), 90, cx);
  const n = destination(c, 0, h / 2).lat;
  const s = destination(c, 180, h / 2).lat;
  const e = destination(c, 90, w / 2).lng;
  const wv = destination(c, 270, w / 2).lng;
  return [{ lat: s, lng: wv }, { lat: s, lng: e }, { lat: n, lng: e }, { lat: n, lng: wv }, { lat: s, lng: wv }];
}

export function bldg(id: string, cx: number, cy: number, w = 10, h = 10, over: Partial<Building> = {}): Building {
  return makeBuilding({
    id, source: TEST_SOURCE, footprint: rect(cx, cy, w, h), heightM: null, floorsAbove: null, floorsBelow: null,
    usage: null, usageCode: null, structure: null, structureCode: null, builtYear: null, name: null, lod: null, ...over,
  });
}
