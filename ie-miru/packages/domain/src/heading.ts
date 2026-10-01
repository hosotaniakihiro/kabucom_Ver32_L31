import { LOCATION_DEFAULTS } from '@ie-miru/config';

/**
 * カメラの向き。
 * - heading: 背面カメラの光軸を水平面へ投影した方位 [deg, 真北0, 時計回り, 0..360)
 * - yaw:     heading を (-180, 180] で表したもの（ARKit/RealityKit の慣例に合わせた別名）
 * - pitch:   光軸の仰角 [deg]。水平 0, 上向き +, 下向き -
 * - roll:    光軸まわりの傾き [deg]。端末上端が右へ倒れると +
 *
 * 端末方位（CLHeading など、端末上端の方位）とカメラ方位は一致しない。
 * iPhone を立てて構えると端末上端は空を向くため、カメラ方位は姿勢から求める必要がある。
 */
export interface CameraPose {
  heading: number;
  yaw: number;
  pitch: number;
  roll: number;
  /** 方位の誤差 [deg]。不明なら null */
  headingAccuracy: number | null;
  /** 方位が当てにならない（真上/真下を向いている・精度不良）場合 true */
  headingUnreliable: boolean;
  source: CameraPoseSource;
}

export type CameraPoseSource = 'arkit' | 'coremotion' | 'device_orientation' | 'compass_only' | 'manual';

export type Vec3 = [number, number, number];
/** 行優先 3x3 行列。端末座標 → 世界座標(ENU: x=東, y=北, z=上) */
export type Mat3 = [number, number, number, number, number, number, number, number, number];

const DEG = Math.PI / 180;

export function normalizeHeading(deg: number): number {
  return ((deg % 360) + 360) % 360;
}

export function headingToYaw(deg: number): number {
  const h = normalizeHeading(deg);
  return h > 180 ? h - 360 : h;
}

function mul(m: Mat3, v: Vec3): Vec3 {
  return [
    m[0] * v[0] + m[1] * v[1] + m[2] * v[2],
    m[3] * v[0] + m[4] * v[1] + m[5] * v[2],
    m[6] * v[0] + m[7] * v[1] + m[8] * v[2],
  ];
}
function cross(a: Vec3, b: Vec3): Vec3 {
  return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
}
function dot(a: Vec3, b: Vec3): number {
  return a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
}
function norm(a: Vec3): Vec3 {
  const l = Math.hypot(a[0], a[1], a[2]) || 1;
  return [a[0] / l, a[1] / l, a[2] / l];
}

/** 真上/真下に近いと方位が不定になる閾値 [deg] */
export const PITCH_UNRELIABLE_DEG = 75;

/**
 * 端末→ENU の回転行列から背面カメラの姿勢を求める。
 * 端末座標: x=画面右, y=画面上, z=画面から手前（利用者側）。背面カメラは -z を向く。
 */
export function cameraPoseFromMatrix(
  r: Mat3,
  opts: { headingAccuracy?: number | null; source: CameraPoseSource },
): CameraPose {
  const f = norm(mul(r, [0, 0, -1]));
  const deviceUp = norm(mul(r, [0, 1, 0]));
  const heading = normalizeHeading(Math.atan2(f[0], f[1]) / DEG);
  const pitch = Math.asin(Math.max(-1, Math.min(1, f[2]))) / DEG;
  let roll = 0;
  const horizontal = Math.hypot(f[0], f[1]);
  if (horizontal > 1e-6) {
    const right = norm(cross(f, [0, 0, 1]));
    const upRef = cross(right, f);
    roll = Math.atan2(dot(deviceUp, right), dot(deviceUp, upRef)) / DEG;
  }
  const acc = opts.headingAccuracy ?? null;
  return {
    heading,
    yaw: headingToYaw(heading),
    pitch,
    roll,
    headingAccuracy: acc,
    headingUnreliable: Math.abs(pitch) > PITCH_UNRELIABLE_DEG || (acc != null && acc > LOCATION_DEFAULTS.headingUnreliableDeg),
    source: opts.source,
  };
}

/**
 * W3C DeviceOrientation (alpha, beta, gamma) → 回転行列（R = Rz(α)·Rx(β)·Ry(γ)）。
 * alpha は「絶対方位（北基準）」である必要がある。iOS Safari では webkitCompassHeading から
 * alphaFromCompassHeading() で作る。
 */
export function matrixFromDeviceOrientation(alpha: number, beta: number, gamma: number): Mat3 {
  const a = alpha * DEG;
  const b = beta * DEG;
  const g = gamma * DEG;
  const cA = Math.cos(a), sA = Math.sin(a);
  const cB = Math.cos(b), sB = Math.sin(b);
  const cG = Math.cos(g), sG = Math.sin(g);
  // W3C DeviceOrientation spec の行列（地球座標: x=東, y=北, z=上）
  return [
    cA * cG - sA * sB * sG, -cB * sA, cG * sA * sB + cA * sG,
    cG * sA + cA * sB * sG, cA * cB, sA * sG - cA * cG * sB,
    -cB * sG, sB, cB * cG,
  ];
}

/**
 * DeviceOrientation の alpha は反時計回り（z軸まわり）なので、方位（時計回り）とは符号が逆。
 * webkitCompassHeading（磁北基準・時計回り）から絶対 alpha を作る。
 */
export function alphaFromCompassHeading(compassHeadingDeg: number, declinationDeg: number = LOCATION_DEFAULTS.defaultMagneticDeclinationDeg): number {
  const trueHeading = normalizeHeading(compassHeadingDeg + declinationDeg);
  return normalizeHeading(360 - trueHeading);
}

export function cameraPoseFromDeviceOrientation(input: {
  alpha: number | null;
  beta: number | null;
  gamma: number | null;
  absolute?: boolean;
  webkitCompassHeading?: number | null;
  webkitCompassAccuracy?: number | null;
  declinationDeg?: number;
}): CameraPose | null {
  const { beta, gamma } = input;
  if (beta == null || gamma == null) return null;
  let alpha: number | null = null;
  let accuracy: number | null = null;
  if (input.webkitCompassHeading != null && input.webkitCompassHeading >= 0) {
    alpha = alphaFromCompassHeading(input.webkitCompassHeading, input.declinationDeg);
    accuracy = input.webkitCompassAccuracy != null && input.webkitCompassAccuracy >= 0 ? input.webkitCompassAccuracy : 20;
  } else if (input.absolute && input.alpha != null) {
    alpha = input.alpha;
    accuracy = 25;
  }
  if (alpha == null) return null; // 北基準が無い向きでは建物方向を決めない
  return cameraPoseFromMatrix(matrixFromDeviceOrientation(alpha, beta, gamma), { headingAccuracy: accuracy, source: 'device_orientation' });
}

/**
 * CoreMotion の CMAttitudeReferenceFrame.xTrueNorthZVertical の四元数（端末→参照系）から姿勢を求める。
 * 参照系は X=真北, Y=西, Z=上。ENU へは E=-Y, N=X, U=Z。
 */
export function cameraPoseFromCoreMotionQuaternion(
  q: { x: number; y: number; z: number; w: number },
  headingAccuracy: number | null = null,
): CameraPose {
  const { x, y, z, w } = q;
  // 四元数 → 回転行列（端末→NWU）
  const m: Mat3 = [
    1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w),
    2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w),
    2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y),
  ];
  // NWU → ENU: 行の並べ替え（E = -W, N = N, U = U）
  const enu: Mat3 = [-m[3], -m[4], -m[5], m[0], m[1], m[2], m[6], m[7], m[8]];
  return cameraPoseFromMatrix(enu, { headingAccuracy, source: 'coremotion' });
}

/**
 * 方位のみ（コンパス値）から姿勢を作る簡易版。iPhone を縦に構えて正面へ向けている前提。
 * 端末上端の方位ではなく、カメラ方位として扱える値（CLHeading.headingOrientation=.faceUp 等で補正済み）を渡すこと。
 */
export function cameraPoseFromCompassOnly(heading: number, headingAccuracy: number | null): CameraPose {
  return {
    heading: normalizeHeading(heading),
    yaw: headingToYaw(heading),
    pitch: 0,
    roll: 0,
    headingAccuracy,
    headingUnreliable: headingAccuracy != null && headingAccuracy > LOCATION_DEFAULTS.headingUnreliableDeg,
    source: 'compass_only',
  };
}

/** 方位の円周平均（359° と 1° の平均は 0°） */
export function circularMean(headings: number[]): number | null {
  if (headings.length === 0) return null;
  let s = 0;
  let c = 0;
  for (const h of headings) {
    s += Math.sin(h * DEG);
    c += Math.cos(h * DEG);
  }
  if (Math.hypot(s, c) < 1e-9) return null;
  return normalizeHeading(Math.atan2(s, c) / DEG);
}

/** 円周上の指数移動平均（センサーのブレを抑える） */
export function smoothHeading(prev: number | null, next: number, factor = 0.25): number {
  if (prev == null) return normalizeHeading(next);
  let d = normalizeHeading(next) - normalizeHeading(prev);
  if (d > 180) d -= 360;
  if (d < -180) d += 360;
  return normalizeHeading(prev + d * factor);
}

/**
 * 候補抽出に使う実効半視野角。方位精度が悪いほど広げる（最大 60°）。
 */
export function effectiveHalfFov(pose: Pick<CameraPose, 'headingAccuracy'> | null, base = LOCATION_DEFAULTS.halfFieldOfViewDeg): number {
  const acc = pose?.headingAccuracy;
  if (acc == null) return Math.min(60, base + 15);
  return Math.min(60, base + Math.max(0, acc - 10));
}

export function parseCameraPose(input: unknown): CameraPose | null {
  if (!input || typeof input !== 'object') return null;
  const o = input as Record<string, unknown>;
  const n = (v: unknown) => (typeof v === 'number' && Number.isFinite(v) ? v : null);
  const heading = n(o.heading);
  if (heading == null) return null;
  const pitch = n(o.pitch) ?? 0;
  const acc = n(o.headingAccuracy);
  const sources: CameraPoseSource[] = ['arkit', 'coremotion', 'device_orientation', 'compass_only', 'manual'];
  const source = sources.includes(o.source as CameraPoseSource) ? (o.source as CameraPoseSource) : 'manual';
  return {
    heading: normalizeHeading(heading),
    yaw: headingToYaw(heading),
    pitch,
    roll: n(o.roll) ?? 0,
    headingAccuracy: acc,
    headingUnreliable: Math.abs(pitch) > PITCH_UNRELIABLE_DEG || (acc != null && acc > LOCATION_DEFAULTS.headingUnreliableDeg),
    source,
  };
}
