/**
 * 測地計算。建物候補の探索範囲（〜数百m）では局所平面近似で十分な精度が出るため、
 * 距離は haversine、図形演算は原点まわりの ENU（東・北）平面で行う。
 */

export interface LatLng {
  lat: number;
  lng: number;
}

/** 原点からの局所平面座標 [m]（x = 東, y = 北） */
export interface EnuPoint {
  x: number;
  y: number;
}

/** 外周リング（閉じていなくてよい）。[lng, lat] ではなく LatLng の配列で統一する。 */
export type Ring = LatLng[];

export const EARTH_RADIUS_M = 6_371_008.8;

const toRad = (deg: number) => (deg * Math.PI) / 180;
const toDeg = (rad: number) => (rad * 180) / Math.PI;

export function isValidLatLng(p: Partial<LatLng> | null | undefined): p is LatLng {
  return (
    !!p &&
    typeof p.lat === 'number' &&
    typeof p.lng === 'number' &&
    Number.isFinite(p.lat) &&
    Number.isFinite(p.lng) &&
    Math.abs(p.lat) <= 90 &&
    Math.abs(p.lng) <= 180
  );
}

export function distanceM(a: LatLng, b: LatLng): number {
  const dLat = toRad(b.lat - a.lat);
  const dLng = toRad(b.lng - a.lng);
  const s =
    Math.sin(dLat / 2) ** 2 + Math.cos(toRad(a.lat)) * Math.cos(toRad(b.lat)) * Math.sin(dLng / 2) ** 2;
  return 2 * EARTH_RADIUS_M * Math.asin(Math.min(1, Math.sqrt(s)));
}

/** a から b への初期方位角 [deg, 0=真北, 時計回り, 0..360) */
export function bearingDeg(a: LatLng, b: LatLng): number {
  const φ1 = toRad(a.lat);
  const φ2 = toRad(b.lat);
  const Δλ = toRad(b.lng - a.lng);
  const y = Math.sin(Δλ) * Math.cos(φ2);
  const x = Math.cos(φ1) * Math.sin(φ2) - Math.sin(φ1) * Math.cos(φ2) * Math.cos(Δλ);
  return (toDeg(Math.atan2(y, x)) + 360) % 360;
}

/** 始点から方位・距離だけ進んだ点 */
export function destination(from: LatLng, bearing: number, distance: number): LatLng {
  const δ = distance / EARTH_RADIUS_M;
  const θ = toRad(bearing);
  const φ1 = toRad(from.lat);
  const λ1 = toRad(from.lng);
  const φ2 = Math.asin(Math.sin(φ1) * Math.cos(δ) + Math.cos(φ1) * Math.sin(δ) * Math.cos(θ));
  const λ2 = λ1 + Math.atan2(Math.sin(θ) * Math.sin(δ) * Math.cos(φ1), Math.cos(δ) - Math.sin(φ1) * Math.sin(φ2));
  return { lat: toDeg(φ2), lng: ((toDeg(λ2) + 540) % 360) - 180 };
}

export function toEnu(origin: LatLng, p: LatLng): EnuPoint {
  const x = toRad(p.lng - origin.lng) * Math.cos(toRad((origin.lat + p.lat) / 2)) * EARTH_RADIUS_M;
  const y = toRad(p.lat - origin.lat) * EARTH_RADIUS_M;
  return { x, y };
}

export function fromEnu(origin: LatLng, p: EnuPoint): LatLng {
  const lat = origin.lat + toDeg(p.y / EARTH_RADIUS_M);
  const lng = origin.lng + toDeg(p.x / (EARTH_RADIUS_M * Math.cos(toRad((origin.lat + lat) / 2))));
  return { lat, lng };
}

/** 閉じたリングの末尾重複点を除く */
export function openRing(ring: Ring): Ring {
  if (ring.length > 1) {
    const first = ring[0]!;
    const last = ring[ring.length - 1]!;
    if (first.lat === last.lat && first.lng === last.lng) return ring.slice(0, -1);
  }
  return ring;
}

export function pointInPolygon(p: LatLng, ring: Ring): boolean {
  const r = openRing(ring);
  let inside = false;
  for (let i = 0, j = r.length - 1; i < r.length; j = i++) {
    const a = r[i]!;
    const b = r[j]!;
    const intersect =
      a.lat > p.lat !== b.lat > p.lat && p.lng < ((b.lng - a.lng) * (p.lat - a.lat)) / (b.lat - a.lat) + a.lng;
    if (intersect) inside = !inside;
  }
  return inside;
}

/** 面積 [m²]（局所平面・靴紐公式） */
export function polygonAreaM2(ring: Ring): number {
  const r = openRing(ring);
  if (r.length < 3) return 0;
  const o = r[0]!;
  const pts = r.map((p) => toEnu(o, p));
  let s = 0;
  for (let i = 0; i < pts.length; i++) {
    const a = pts[i]!;
    const b = pts[(i + 1) % pts.length]!;
    s += a.x * b.y - b.x * a.y;
  }
  return Math.abs(s) / 2;
}

export function polygonCentroid(ring: Ring): LatLng {
  const r = openRing(ring);
  if (r.length === 0) throw new Error('empty ring');
  const o = r[0]!;
  const pts = r.map((p) => toEnu(o, p));
  let a2 = 0;
  let cx = 0;
  let cy = 0;
  for (let i = 0; i < pts.length; i++) {
    const p = pts[i]!;
    const q = pts[(i + 1) % pts.length]!;
    const cross = p.x * q.y - q.x * p.y;
    a2 += cross;
    cx += (p.x + q.x) * cross;
    cy += (p.y + q.y) * cross;
  }
  if (Math.abs(a2) < 1e-9) {
    // 退化（線・点）: 頂点平均
    const avg = pts.reduce((acc, p) => ({ x: acc.x + p.x / pts.length, y: acc.y + p.y / pts.length }), { x: 0, y: 0 });
    return fromEnu(o, avg);
  }
  return fromEnu(o, { x: cx / (3 * a2), y: cy / (3 * a2) });
}

/** 点からポリゴン境界までの最短距離 [m]（内部なら 0） */
export function distanceToPolygonM(p: LatLng, ring: Ring): number {
  if (pointInPolygon(p, ring)) return 0;
  const r = openRing(ring);
  const pts = r.map((q) => toEnu(p, q));
  let best = Infinity;
  for (let i = 0; i < pts.length; i++) {
    const a = pts[i]!;
    const b = pts[(i + 1) % pts.length]!;
    best = Math.min(best, distancePointToSegment({ x: 0, y: 0 }, a, b));
  }
  return best;
}

function distancePointToSegment(p: EnuPoint, a: EnuPoint, b: EnuPoint): number {
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  const len2 = dx * dx + dy * dy;
  const t = len2 === 0 ? 0 : Math.max(0, Math.min(1, ((p.x - a.x) * dx + (p.y - a.y) * dy) / len2));
  return Math.hypot(a.x + t * dx - p.x, a.y + t * dy - p.y);
}

/**
 * 原点から方位 bearing へ伸ばした半直線とポリゴン辺の最初の交点までの距離 [m]。
 * 交差しなければ null。原点がポリゴン内部なら 0。
 */
export function rayPolygonHitDistanceM(origin: LatLng, bearing: number, ring: Ring): number | null {
  if (pointInPolygon(origin, ring)) return 0;
  const θ = toRad(bearing);
  const d = { x: Math.sin(θ), y: Math.cos(θ) };
  const pts = openRing(ring).map((q) => toEnu(origin, q));
  let best: number | null = null;
  for (let i = 0; i < pts.length; i++) {
    const a = pts[i]!;
    const b = pts[(i + 1) % pts.length]!;
    const e = { x: b.x - a.x, y: b.y - a.y };
    const denom = d.x * e.y - d.y * e.x;
    if (Math.abs(denom) < 1e-12) continue; // 平行
    // origin + t*d = a + u*e
    const t = (a.x * e.y - a.y * e.x) / denom;
    const u = (a.x * d.y - a.y * d.x) / denom;
    if (t >= 0 && u >= 0 && u <= 1 && (best === null || t < best)) best = t;
  }
  return best;
}

/** 原点から見たポリゴンの方位範囲（視角）。center を中心に [start, end] を時計回りで返す。 */
export function angularExtent(origin: LatLng, ring: Ring): { center: number; halfWidth: number } {
  const pts = openRing(ring);
  const c = polygonCentroid(ring);
  const center = bearingDeg(origin, c);
  let maxOff = 0;
  for (const p of pts) {
    const off = Math.abs(signedAngleDiff(bearingDeg(origin, p), center));
    if (off > maxOff) maxOff = off;
  }
  return { center, halfWidth: maxOff };
}

/** b - a を (-180, 180] に正規化 */
export function signedAngleDiff(a: number, b: number): number {
  let d = (((a - b) % 360) + 540) % 360 - 180;
  if (d === -180) d = 180;
  return d;
}

/** 緯度経度 → Web Mercator タイル座標 */
export function lngLatToTile(p: LatLng, z: number): { x: number; y: number; z: number } {
  const n = 2 ** z;
  const x = Math.floor(((p.lng + 180) / 360) * n);
  const latRad = toRad(p.lat);
  const y = Math.floor(((1 - Math.log(Math.tan(latRad) + 1 / Math.cos(latRad)) / Math.PI) / 2) * n);
  return { x: Math.min(n - 1, Math.max(0, x)), y: Math.min(n - 1, Math.max(0, y)), z };
}

/** 中心と半径を覆うタイル一覧 */
export function tilesCovering(center: LatLng, radiusM: number, z: number): Array<{ x: number; y: number; z: number }> {
  const nw = destination(destination(center, 0, radiusM), 270, radiusM);
  const se = destination(destination(center, 180, radiusM), 90, radiusM);
  const a = lngLatToTile(nw, z);
  const b = lngLatToTile(se, z);
  const out: Array<{ x: number; y: number; z: number }> = [];
  for (let x = Math.min(a.x, b.x); x <= Math.max(a.x, b.x); x++) {
    for (let y = Math.min(a.y, b.y); y <= Math.max(a.y, b.y); y++) out.push({ x, y, z });
  }
  return out;
}
