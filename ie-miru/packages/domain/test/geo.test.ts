import { describe, expect, it } from 'vitest';
import {
  angularExtent, bearingDeg, destination, distanceM, distanceToPolygonM, lngLatToTile, pointInPolygon,
  polygonAreaM2, polygonCentroid, rayPolygonHitDistanceM, signedAngleDiff, tilesCovering, type LatLng, type Ring,
} from '../src';

const O: LatLng = { lat: 35.68, lng: 139.76 };
/** 原点から北へ dy, 東へ dx [m] の矩形 */
function rect(cx: number, cy: number, w: number, h: number): Ring {
  const c = destination(destination(O, 0, cy), 90, cx);
  const n = destination(c, 0, h / 2).lat;
  const s = destination(c, 180, h / 2).lat;
  const e = destination(c, 90, w / 2).lng;
  const wv = destination(c, 270, w / 2).lng;
  return [{ lat: s, lng: wv }, { lat: s, lng: e }, { lat: n, lng: e }, { lat: n, lng: wv }, { lat: s, lng: wv }];
}

describe('geo', () => {
  it('distance and destination are inverse', () => {
    const p = destination(O, 45, 100);
    expect(distanceM(O, p)).toBeCloseTo(100, 1);
    expect(bearingDeg(O, p)).toBeCloseTo(45, 1);
  });
  it('signedAngleDiff wraps', () => {
    expect(signedAngleDiff(10, 350)).toBeCloseTo(20);
    expect(signedAngleDiff(350, 10)).toBeCloseTo(-20);
    expect(signedAngleDiff(180, 0)).toBe(180);
  });
  it('polygon area of 10x20 rect ~ 200 m2', () => {
    expect(polygonAreaM2(rect(0, 50, 10, 20))).toBeCloseTo(200, 0);
  });
  it('centroid of rect', () => {
    const c = polygonCentroid(rect(0, 50, 10, 20));
    expect(distanceM(O, c)).toBeCloseTo(50, 0);
  });
  it('point in polygon', () => {
    expect(pointInPolygon(O, rect(0, 0, 10, 10))).toBe(true);
    expect(pointInPolygon(O, rect(0, 50, 10, 10))).toBe(false);
  });
  it('distance to polygon', () => {
    expect(distanceToPolygonM(O, rect(0, 50, 10, 20))).toBeCloseTo(40, 0);
    expect(distanceToPolygonM(O, rect(0, 0, 10, 10))).toBe(0);
  });
  it('ray hits near face of a building to the north', () => {
    expect(rayPolygonHitDistanceM(O, 0, rect(0, 50, 10, 20))!).toBeCloseTo(40, 0);
    expect(rayPolygonHitDistanceM(O, 90, rect(0, 50, 10, 20))).toBeNull();
    expect(rayPolygonHitDistanceM(O, 180, rect(0, 50, 10, 20))).toBeNull();
  });
  it('angular extent', () => {
    const ext = angularExtent(O, rect(0, 50, 10, 20));
    expect(ext.center).toBeCloseTo(0, 0);
    expect(ext.halfWidth).toBeGreaterThan(5);
    expect(ext.halfWidth).toBeLessThan(8);
  });
  it('tile math', () => {
    const t = lngLatToTile({ lat: 35.681236, lng: 139.767125 }, 16);
    expect(t).toEqual({ x: 58211, y: 25806, z: 16 });
    expect(tilesCovering(O, 100, 16).length).toBeGreaterThanOrEqual(1);
  });
});
