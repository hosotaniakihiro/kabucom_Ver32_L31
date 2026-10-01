/**
 * LiDAR / AR 計測の計算。LiDAR は必須ではなく、あれば精度を上げる補助として使う。
 * iPhone の LiDAR の有効距離は概ね 5m。建物までの距離（10m〜）は ARKit の平面推定 raycast で補う。
 */
import type { Vec3 } from './heading';
export type { Vec3 };

export const LIDAR_EFFECTIVE_RANGE_M = 5;

export function length3(a: Vec3, b: Vec3): number {
  return Math.hypot(a[0] - b[0], a[1] - b[1], a[2] - b[2]);
}

/** 折れ線の長さ（ひびの長さ等） */
export function polylineLength(pts: Vec3[]): number {
  let s = 0;
  for (let i = 1; i < pts.length; i++) s += length3(pts[i - 1]!, pts[i]!);
  return s;
}

/** 3D 多角形の面積（Newell 法）。外壁の補修範囲など、ほぼ平面上の点列を想定 */
export function polygonArea3(pts: Vec3[]): number {
  if (pts.length < 3) return 0;
  let nx = 0, ny = 0, nz = 0;
  for (let i = 0; i < pts.length; i++) {
    const [x1, y1, z1] = pts[i]!;
    const [x2, y2, z2] = pts[(i + 1) % pts.length]!;
    nx += (y1 - y2) * (z1 + z2);
    ny += (z1 - z2) * (x1 + x2);
    nz += (x1 - x2) * (y1 + y2);
  }
  return Math.hypot(nx, ny, nz) / 2;
}

/** 点群の平面からのずれ（RMS）。大きいときは面積が不正確と警告する */
export function planarityRms(pts: Vec3[]): number {
  if (pts.length < 4) return 0;
  const c = pts.reduce<Vec3>((a, p) => [a[0] + p[0] / pts.length, a[1] + p[1] / pts.length, a[2] + p[2] / pts.length], [0, 0, 0]);
  // Newell 法の法線
  let nx = 0, ny = 0, nz = 0;
  for (let i = 0; i < pts.length; i++) {
    const [x1, y1, z1] = pts[i]!;
    const [x2, y2, z2] = pts[(i + 1) % pts.length]!;
    nx += (y1 - y2) * (z1 + z2);
    ny += (z1 - z2) * (x1 + x2);
    nz += (x1 - x2) * (y1 + y2);
  }
  const l = Math.hypot(nx, ny, nz) || 1;
  const d = pts.map((p) => ((p[0] - c[0]) * nx + (p[1] - c[1]) * ny + (p[2] - c[2]) * nz) / l);
  return Math.sqrt(d.reduce((s, x) => s + x * x, 0) / d.length);
}

export interface DistanceObservation {
  distanceM: number;
  source: 'lidar' | 'ar_raycast' | 'gps_footprint';
  /** 1σ 誤差 [m] */
  sigmaM: number;
}

/** 測距の誤差モデル（概算） */
export function observation(distanceM: number, source: DistanceObservation['source'], gpsAccuracyM: number | null = null): DistanceObservation {
  if (source === 'lidar') {
    // 有効距離内は数 cm。超えると ARKit 推定に近い精度まで落ちる
    const sigma = distanceM <= LIDAR_EFFECTIVE_RANGE_M ? 0.02 + 0.01 * distanceM : 0.05 * distanceM;
    return { distanceM, source, sigmaM: sigma };
  }
  if (source === 'ar_raycast') return { distanceM, source, sigmaM: 0.08 * distanceM + 0.1 };
  return { distanceM, source, sigmaM: Math.max(3, gpsAccuracyM ?? 15) };
}

/** 逆分散重み付けで複数の距離観測を融合 */
export function fuseDistances(obs: DistanceObservation[]): DistanceObservation | null {
  const valid = obs.filter((o) => Number.isFinite(o.distanceM) && o.distanceM > 0 && o.sigmaM > 0);
  if (valid.length === 0) return null;
  let w = 0, s = 0;
  for (const o of valid) {
    const wi = 1 / o.sigmaM ** 2;
    w += wi;
    s += wi * o.distanceM;
  }
  const best = valid.reduce((a, b) => (a.sigmaM <= b.sigmaM ? a : b));
  return { distanceM: s / w, source: best.source, sigmaM: Math.sqrt(1 / w) };
}

/** 修理箇所サイズの表示用まとめ */
export function describeRepairSize(input: { lengthM?: number | null; areaM2?: number | null; method: 'lidar' | 'ar_plane' | 'manual' }): string {
  const parts: string[] = [];
  if (input.lengthM != null) parts.push(input.lengthM < 1 ? `長さ 約${Math.round(input.lengthM * 100)}cm` : `長さ 約${input.lengthM.toFixed(2)}m`);
  if (input.areaM2 != null) parts.push(`面積 約${input.areaM2.toFixed(2)}㎡`);
  const how = { lidar: 'LiDAR計測', ar_plane: 'AR推定', manual: '手入力' }[input.method];
  return parts.length ? `${parts.join('・')}（${how}）` : 'サイズ未計測';
}
